"""
TikTok publisher — Content Posting API v2.

Supports two modes depending on the scopes granted:
  • Direct Post  (video.publish scope — requires TikTok app review)
  • Inbox Upload (video.upload scope — no review needed; video lands in creator's inbox)

The publisher auto-detects which mode to use from the stored token scope.

Env vars required:
  TIKTOK_CLIENT_KEY
  TIKTOK_CLIENT_SECRET
"""
import base64
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta

import requests

from src.models.video import VideoPost, PublishResult, Platform
from src.platforms.base import BasePlatformPublisher
from src.utils.logger import get_logger

logger = get_logger(__name__)

TIKTOK_AUTH_URL   = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL  = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_URL   = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_DIRECT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_INBOX_URL  = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

CHUNK_SIZE         = 10 * 1024 * 1024  # 10 MB
STATUS_POLL_SECS   = 15
STATUS_POLL_MAX    = 20                 # ~5 min total


class TikTokPublisher(BasePlatformPublisher):

    def __init__(self, credentials_json: str | None = None):
        self._client_key    = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
        self._client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
        self._creds: dict   = json.loads(credentials_json) if credentials_json else {}

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str) -> tuple[str, str]:
        """
        Build TikTok consent URL using PKCE (S256).
        Returns (auth_url, code_verifier) — store the verifier in the OAuth state dict.
        """
        code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")

        scopes = "user.info.basic,video.upload,video.publish"
        params = "&".join([
            f"client_key={self._client_key}",
            f"scope={scopes}",
            "response_type=code",
            f"redirect_uri={redirect_uri}",
            f"state={state}",
            f"code_challenge={code_challenge}",
            "code_challenge_method=S256",
        ])
        return f"{TIKTOK_AUTH_URL}?{params}", code_verifier

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> str:
        """Exchange authorization code for credentials. Returns JSON string for DB storage."""
        resp = requests.post(
            TIKTOK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key":    self._client_key,
                "client_secret": self._client_secret,
                "code":          code,
                "grant_type":    "authorization_code",
                "redirect_uri":  redirect_uri,
                "code_verifier": code_verifier,
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if "data" not in body:
            raise ValueError(f"TikTok token exchange error: {body}")
        td  = body["data"]
        now = datetime.now(timezone.utc)
        return json.dumps({
            "access_token":       td["access_token"],
            "refresh_token":      td.get("refresh_token"),
            "open_id":            td["open_id"],
            "scope":              td.get("scope", ""),
            "expires_at":         (now + timedelta(seconds=td.get("expires_in", 86400))).isoformat(),
            "refresh_expires_at": (now + timedelta(seconds=td.get("refresh_expires_in", 31536000))).isoformat(),
        })

    # ------------------------------------------------------------------
    # BasePlatformPublisher interface
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Refresh the access token if it is expired or about to expire."""
        if not self._creds:
            return
        raw = self._creds.get("expires_at", "2000-01-01T00:00:00+00:00")
        try:
            exp = datetime.fromisoformat(raw)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except ValueError:
            exp = datetime.min.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) < exp - timedelta(minutes=5):
            return  # still valid

        rt = self._creds.get("refresh_token")
        if not rt:
            raise ValueError("TikTok token expired and no refresh_token available. Please reconnect.")

        resp = requests.post(
            TIKTOK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key":    self._client_key,
                "client_secret": self._client_secret,
                "grant_type":    "refresh_token",
                "refresh_token": rt,
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        if "data" not in body:
            raise ValueError(f"TikTok token refresh error: {body}")
        td  = body["data"]
        now = datetime.now(timezone.utc)
        self._creds.update({
            "access_token":  td["access_token"],
            "refresh_token": td.get("refresh_token", rt),
            "expires_at":    (now + timedelta(seconds=td.get("expires_in", 86400))).isoformat(),
        })
        logger.info("TikTok access token refreshed.")

    def is_authenticated(self) -> bool:
        return bool(self._creds.get("access_token"))

    def get_updated_credentials_json(self) -> str:
        return json.dumps(self._creds)

    # ------------------------------------------------------------------
    # User info (called right after OAuth to populate channel thumbnail)
    # ------------------------------------------------------------------

    def get_user_info(self) -> dict | None:
        """Fetch display_name and avatar_url for the connected TikTok account."""
        try:
            self.authenticate()
            resp = requests.get(
                TIKTOK_USER_URL,
                params={"fields": "open_id,union_id,avatar_url,display_name"},
                headers={"Authorization": f"Bearer {self._creds['access_token']}"},
                timeout=15,
            )
            resp.raise_for_status()
            user = resp.json().get("data", {}).get("user", {})
            if user:
                return {
                    "channel_id":    user.get("open_id"),
                    "thumbnail_url": user.get("avatar_url"),
                    "display_name":  user.get("display_name"),
                }
        except Exception as e:
            logger.warning(f"TikTok get_user_info failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, video: VideoPost) -> PublishResult:
        """Upload and publish a video to TikTok."""
        if not self.is_authenticated():
            return PublishResult(platform=Platform.TIKTOK, success=False,
                                 error_message="Account not authenticated.")
        try:
            self.authenticate()
        except Exception as e:
            return PublishResult(platform=Platform.TIKTOK, success=False,
                                 error_message=f"Token refresh failed: {e}")

        if not os.path.exists(video.file_path):
            return PublishResult(platform=Platform.TIKTOK, success=False,
                                 error_message=f"Video file not found: {video.file_path}")

        file_size   = os.path.getsize(video.file_path)
        chunk_count = math.ceil(file_size / CHUNK_SIZE)

        has_publish = "video.publish" in self._creds.get("scope", "")
        init_url    = TIKTOK_DIRECT_URL if has_publish else TIKTOK_INBOX_URL
        mode        = "direct_post" if has_publish else "inbox_upload"
        logger.info(f"[TikTok] mode={mode}, size={file_size}B, chunks={chunk_count}")

        privacy = video.platform_options.get("tiktok_privacy", "PUBLIC_TO_EVERYONE")

        # ── Step 1: initialise upload ──────────────────────────
        try:
            resp = requests.post(
                init_url,
                json={
                    "post_info": {
                        "title":                    video.title[:150],
                        "privacy_level":            privacy,
                        "disable_duet":             False,
                        "disable_comment":          False,
                        "disable_stitch":           False,
                        "video_cover_timestamp_ms": 1000,
                    },
                    "source_info": {
                        "source":            "FILE_UPLOAD",
                        "video_size":        file_size,
                        "chunk_size":        CHUNK_SIZE,
                        "total_chunk_count": chunk_count,
                    },
                },
                headers={
                    "Authorization": f"Bearer {self._creds['access_token']}",
                    "Content-Type":  "application/json; charset=UTF-8",
                },
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            err  = body.get("error", {})
            if err.get("code", "ok") != "ok":
                return PublishResult(platform=Platform.TIKTOK, success=False,
                                     error_message=f"TikTok init error {err.get('code')}: {err.get('message')}")
            publish_id = body["data"]["publish_id"]
            upload_url = body["data"]["upload_url"]
            logger.info(f"[TikTok] Upload init OK — publish_id={publish_id}")
        except requests.HTTPError as e:
            return PublishResult(platform=Platform.TIKTOK, success=False,
                                 error_message=f"TikTok init HTTP {e.response.status_code}: {e.response.text[:300]}")
        except Exception as e:
            return PublishResult(platform=Platform.TIKTOK, success=False,
                                 error_message=f"TikTok init error: {e}")

        # ── Step 2: upload chunks ──────────────────────────────
        try:
            with open(video.file_path, "rb") as fh:
                for i in range(chunk_count):
                    chunk = fh.read(CHUNK_SIZE)
                    start = i * CHUNK_SIZE
                    end   = start + len(chunk) - 1
                    cr = requests.put(
                        upload_url,
                        data=chunk,
                        headers={
                            "Content-Range": f"bytes {start}-{end}/{file_size}",
                            "Content-Type":  "video/mp4",
                        },
                        timeout=300,
                    )
                    cr.raise_for_status()
                    logger.info(f"[TikTok] Chunk {i+1}/{chunk_count} OK ({len(chunk)}B)")
        except Exception as e:
            return PublishResult(platform=Platform.TIKTOK, success=False,
                                 error_message=f"TikTok chunk upload error: {e}")

        # ── Step 3: poll status ────────────────────────────────
        for attempt in range(STATUS_POLL_MAX):
            time.sleep(STATUS_POLL_SECS)
            try:
                sr = requests.post(
                    TIKTOK_STATUS_URL,
                    json={"publish_id": publish_id},
                    headers={
                        "Authorization": f"Bearer {self._creds['access_token']}",
                        "Content-Type":  "application/json; charset=UTF-8",
                    },
                    timeout=30,
                )
                sr.raise_for_status()
                sd     = sr.json().get("data", {})
                status = sd.get("status", "")
                logger.info(f"[TikTok] Status [{attempt+1}/{STATUS_POLL_MAX}]: {status}")

                if status == "PUBLISH_COMPLETE":
                    ids      = sd.get("publicaly_available_post_id", [])
                    vid_id   = str(ids[0]) if ids else ""
                    open_id  = self._creds.get("open_id", "")
                    vid_url  = (f"https://www.tiktok.com/@{open_id}/video/{vid_id}"
                                if vid_id else "https://www.tiktok.com")
                    logger.info(f"[TikTok] Published: {vid_url}")
                    return PublishResult(platform=Platform.TIKTOK, success=True,
                                         video_id=vid_id, video_url=vid_url)

                if status == "SEND_TO_USER_INBOX":
                    logger.info("[TikTok] Video sent to creator inbox.")
                    return PublishResult(
                        platform=Platform.TIKTOK, success=True,
                        video_url="https://www.tiktok.com/upload",
                        error_message="Vídeo enviado a la bandeja de TikTok — ábrelo en TikTok para finalizar la publicación.",
                    )

                if status in ("FAILED", "CANCELLED"):
                    return PublishResult(platform=Platform.TIKTOK, success=False,
                                         error_message=f"TikTok procesing failed: {sd.get('fail_reason', 'Unknown')}")
            except Exception as e:
                logger.warning(f"[TikTok] Status check error (attempt {attempt+1}): {e}")

        return PublishResult(platform=Platform.TIKTOK, success=False,
                             error_message="TikTok publish timed out waiting for processing.")
