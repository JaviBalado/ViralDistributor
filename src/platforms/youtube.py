import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.models.video import VideoPost, PublishResult, Platform
from src.platforms.base import BasePlatformPublisher
from src.utils.logger import get_logger

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubePublisher(BasePlatformPublisher):
    """
    YouTube publisher supporting web OAuth flow for server-side deployment.
    Each instance is tied to one account's credentials (stored as JSON in DB).

    client_secrets can be provided as:
    - A file at YOUTUBE_CLIENT_SECRETS_PATH (default: auth/client_secrets.json)
    - OR the raw JSON string in YOUTUBE_CLIENT_SECRETS_JSON env var (easier for Coolify)
    """

    def __init__(self, credentials_json: str | None = None):
        self._client_secrets_path = self._resolve_client_secrets()
        self._credentials: Credentials | None = None
        if credentials_json:
            self._credentials = self._json_to_credentials(credentials_json)

    def _resolve_client_secrets(self) -> str:
        """
        If YOUTUBE_CLIENT_SECRETS_JSON env var is set, write it to a temp file and return that path.
        Otherwise use YOUTUBE_CLIENT_SECRETS_PATH (file on disk).
        """
        secrets_json = os.getenv("YOUTUBE_CLIENT_SECRETS_JSON")
        if secrets_json:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            tmp.write(secrets_json)
            tmp.close()
            return tmp.name
        return os.getenv("YOUTUBE_CLIENT_SECRETS_PATH", "auth/client_secrets.json")

    # ------------------------------------------------------------------
    # Web OAuth flow (used by the dashboard to connect new accounts)
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str) -> str:
        """Generate Google consent screen URL. Redirect user here to authorize."""
        flow = Flow.from_client_secrets_file(self._client_secrets_path, scopes=SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",       # forces refresh_token to be returned every time
            state=state,
        )
        return auth_url

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Exchange authorization code for credentials. Returns JSON string to store in DB."""
        flow = Flow.from_client_secrets_file(self._client_secrets_path, scopes=SCOPES, redirect_uri=redirect_uri)
        flow.fetch_token(code=code)
        return self._credentials_to_json(flow.credentials)

    # ------------------------------------------------------------------
    # BasePlatformPublisher interface
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        if self._credentials and self._credentials.expired and self._credentials.refresh_token:
            self._credentials.refresh(Request())

    def is_authenticated(self) -> bool:
        return self._credentials is not None and (
            self._credentials.valid or bool(self._credentials.refresh_token)
        )

    def publish(self, video: VideoPost) -> PublishResult:
        if not self.is_authenticated():
            return PublishResult(platform=Platform.YOUTUBE, success=False, error_message="Account not authenticated.")

        self.authenticate()  # refresh token if expired

        youtube = build("youtube", "v3", credentials=self._credentials)

        tags = list(video.tags)
        if video.is_short and "#Shorts" not in tags:
            tags.append("#Shorts")

        body = {
            "snippet": {
                "title": video.title,
                "description": video.description,
                "tags": tags,
                "categoryId": video.category_id or "22",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(video.file_path, mimetype="video/*", resumable=True, chunksize=5 * 1024 * 1024)

        try:
            request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
            response = None
            while response is None:
                _, response = request.next_chunk()
            video_id = response["id"]
            url = (
                f"https://www.youtube.com/shorts/{video_id}"
                if video.is_short
                else f"https://www.youtube.com/watch?v={video_id}"
            )
            logger.info(f"Upload successful: {url}")
            return PublishResult(platform=Platform.YOUTUBE, success=True, video_id=video_id, video_url=url)
        except Exception as e:
            logger.error(f"YouTube upload failed: {e}")
            return PublishResult(platform=Platform.YOUTUBE, success=False, error_message=str(e))

    def get_updated_credentials_json(self) -> str:
        """Call after publish() to persist a refreshed token back to the DB."""
        return self._credentials_to_json(self._credentials)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _credentials_to_json(self, creds: Credentials) -> str:
        return json.dumps({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else [],
        })

    def _json_to_credentials(self, credentials_json: str) -> Credentials:
        data = json.loads(credentials_json)
        return Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes"),
        )
