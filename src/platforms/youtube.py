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
        self._client_config = self._build_client_config()
        self._credentials: Credentials | None = None
        if credentials_json:
            self._credentials = self._json_to_credentials(credentials_json)

    def _build_client_config(self) -> dict:
        """
        Build the OAuth client config dict from environment variables.
        Priority:
          1. YOUTUBE_CLIENT_ID + YOUTUBE_CLIENT_SECRET  (simplest, recommended for Coolify)
          2. YOUTUBE_CLIENT_SECRETS_PATH file on disk
        """
        client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
        client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/youtube/callback")

        if client_id and client_secret:
            logger.info("Using YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET env vars.")
            return {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            }

        # Fallback: read from file
        path = os.getenv("YOUTUBE_CLIENT_SECRETS_PATH", "auth/client_secrets.json")
        if os.path.exists(path):
            logger.info(f"Using client_secrets file at {path}.")
            with open(path) as f:
                return json.load(f)

        raise FileNotFoundError(
            "YouTube credentials not found. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET "
            "in your environment variables, or provide a client_secrets.json file."
        )

    # ------------------------------------------------------------------
    # Web OAuth flow (used by the dashboard to connect new accounts)
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str) -> tuple[str, str | None]:
        """
        Generate Google consent screen URL.
        Returns (auth_url, code_verifier). Store code_verifier and pass it to exchange_code().
        """
        flow = Flow.from_client_config(self._client_config, scopes=SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            state=state,
        )
        # code_verifier is set automatically by the library (PKCE); must be preserved for token exchange
        return auth_url, getattr(flow, "code_verifier", None)

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> str:
        """Exchange authorization code for credentials. Returns JSON string to store in DB."""
        flow = Flow.from_client_config(self._client_config, scopes=SCOPES, redirect_uri=redirect_uri)
        if code_verifier:
            flow.code_verifier = code_verifier
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
        import traceback

        # Step 1 — check auth
        logger.info(f"[publish] is_authenticated={self.is_authenticated()}, credentials={self._credentials is not None}")
        if not self.is_authenticated():
            return PublishResult(platform=Platform.YOUTUBE, success=False, error_message="Account not authenticated.")

        # Step 2 — refresh token if needed
        try:
            self.authenticate()
            logger.info("[publish] authenticate() OK")
        except Exception as e:
            detail = f"Token refresh failed — {type(e).__name__}: {e!r}"
            logger.error(detail)
            return PublishResult(platform=Platform.YOUTUBE, success=False, error_message=detail)

        # Step 3 — check file exists
        logger.info(f"[publish] file_path={video.file_path!r}, exists={os.path.exists(video.file_path)}")
        if not os.path.exists(video.file_path):
            msg = f"Video file not found at path: {video.file_path}"
            logger.error(msg)
            return PublishResult(platform=Platform.YOUTUBE, success=False, error_message=msg)

        # Step 4 — build API client
        try:
            youtube = build("youtube", "v3", credentials=self._credentials)
            logger.info("[publish] YouTube client built OK")
        except Exception as e:
            detail = f"Failed to build YouTube client — {type(e).__name__}: {e!r}"
            logger.error(detail)
            return PublishResult(platform=Platform.YOUTUBE, success=False, error_message=detail)

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

        # Step 5 — upload
        try:
            media = MediaFileUpload(video.file_path, mimetype="video/*", resumable=True, chunksize=5 * 1024 * 1024)
            request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
            logger.info("[publish] Starting chunked upload...")
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"[publish] Upload progress: {int(status.progress() * 100)}%")
            video_id = response["id"]
            url = (
                f"https://www.youtube.com/shorts/{video_id}"
                if video.is_short
                else f"https://www.youtube.com/watch?v={video_id}"
            )
            logger.info(f"[publish] Upload successful: {url}")
            return PublishResult(platform=Platform.YOUTUBE, success=True, video_id=video_id, video_url=url)
        except Exception as e:
            detail = f"{type(e).__name__}: {e!r}\n{traceback.format_exc()}"
            logger.error(f"[publish] Upload failed — {detail}")
            return PublishResult(platform=Platform.YOUTUBE, success=False, error_message=f"{type(e).__name__}: {e!r}")

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
