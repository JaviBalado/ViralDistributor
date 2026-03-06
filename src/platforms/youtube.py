import os
import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.models.video import VideoPost, PublishResult, Platform
from src.platforms.base import BasePlatformPublisher
from src.utils.logger import get_logger

logger = get_logger(__name__)

# YouTube Data API v3 — scope required for video uploads
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

YOUTUBE_API_SERVICE = "youtube"
YOUTUBE_API_VERSION = "v3"


class YouTubePublisher(BasePlatformPublisher):
    """
    Publishes videos (Shorts and long-form) to YouTube via the Data API v3.

    Authentication uses OAuth 2.0. On first run, a browser window opens to
    authorize the app. The resulting token is saved to YOUTUBE_TOKEN_PATH
    and reused (with automatic refresh) on subsequent runs.
    """

    def __init__(self):
        self._client_secrets_path = os.getenv("YOUTUBE_CLIENT_SECRETS_PATH", "auth/client_secrets.json")
        self._token_path = os.getenv("YOUTUBE_TOKEN_PATH", "auth/tokens/youtube_token.json")
        self._default_privacy = os.getenv("YOUTUBE_DEFAULT_PRIVACY", "private")
        self._default_category = os.getenv("YOUTUBE_DEFAULT_CATEGORY_ID", "22")
        self._credentials: Credentials | None = None
        self._youtube = None

    # ------------------------------------------------------------------
    # BasePlatformPublisher interface
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Load saved token or run the OAuth consent screen flow."""
        creds = self._load_saved_credentials()

        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired YouTube token...")
            creds.refresh(Request())
        elif not creds or not creds.valid:
            logger.info("No valid token found. Starting OAuth flow...")
            creds = self._run_oauth_flow()

        self._save_credentials(creds)
        self._credentials = creds
        self._youtube = build(YOUTUBE_API_SERVICE, YOUTUBE_API_VERSION, credentials=creds)
        logger.info("YouTube authentication successful.")

    def is_authenticated(self) -> bool:
        return self._credentials is not None and self._credentials.valid

    def publish(self, video: VideoPost) -> PublishResult:
        if not self.is_authenticated():
            self.authenticate()

        logger.info(f"Uploading '{video.title}' to YouTube...")

        tags = list(video.tags)
        if video.is_short and "#Shorts" not in tags:
            tags.append("#Shorts")

        body = {
            "snippet": {
                "title": video.title,
                "description": video.description,
                "tags": tags,
                "categoryId": video.category_id or self._default_category,
            },
            "status": {
                "privacyStatus": video.privacy.value or self._default_privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            video.file_path,
            mimetype="video/*",
            resumable=True,
            chunksize=1024 * 1024 * 5,  # 5 MB chunks
        )

        try:
            request = self._youtube.videos().insert(
                part=",".join(body.keys()),
                body=body,
                media_body=media,
            )
            response = self._execute_resumable_upload(request)
            video_id = response["id"]
            video_url = f"https://www.youtube.com/shorts/{video_id}" if video.is_short else f"https://www.youtube.com/watch?v={video_id}"
            logger.info(f"Upload successful: {video_url}")
            return PublishResult(
                platform=Platform.YOUTUBE,
                success=True,
                video_id=video_id,
                video_url=video_url,
            )
        except Exception as e:
            logger.error(f"YouTube upload failed: {e}")
            return PublishResult(
                platform=Platform.YOUTUBE,
                success=False,
                error_message=str(e),
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_saved_credentials(self) -> Credentials | None:
        token_path = Path(self._token_path)
        if token_path.exists():
            with open(token_path, "rb") as f:
                return pickle.load(f)
        return None

    def _save_credentials(self, creds: Credentials) -> None:
        token_path = Path(self._token_path)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    def _run_oauth_flow(self) -> Credentials:
        secrets_path = Path(self._client_secrets_path)
        if not secrets_path.exists():
            raise FileNotFoundError(
                f"client_secrets.json not found at '{secrets_path}'. "
                "Download it from Google Cloud Console and place it at that path."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
        return flow.run_local_server(port=0)

    def _execute_resumable_upload(self, request) -> dict:
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.debug(f"Upload progress: {int(status.progress() * 100)}%")
        return response
