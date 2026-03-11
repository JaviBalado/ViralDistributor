"""
Instagram Reels publisher — Meta Graph API v19.

Requirements:
  • Instagram Business or Creator account
  • Linked to a Facebook Page
  • Facebook Developer App with Instagram Graph API product
  • Scopes: instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement

Publishing flow (resumable upload — no public video URL needed):
  1. Create a Reels media container (upload_type=resumable)
  2. POST the raw video bytes to the upload URI
  3. Poll container status until FINISHED
  4. Call media_publish with the container id

Env vars required:
  FACEBOOK_APP_ID
  FACEBOOK_APP_SECRET
"""
import json
import os
import time

import requests

from src.models.video import VideoPost, PublishResult, Platform
from src.platforms.base import BasePlatformPublisher
from src.utils.logger import get_logger

logger = get_logger(__name__)

GRAPH_URL    = "https://graph.facebook.com/v19.0"
FB_AUTH_URL  = "https://www.facebook.com/v19.0/dialog/oauth"
FB_TOKEN_URL = f"{GRAPH_URL}/oauth/access_token"

STATUS_POLL_SECS = 15
STATUS_POLL_MAX  = 20   # ~5 min total


class InstagramPublisher(BasePlatformPublisher):

    def __init__(self, credentials_json: str | None = None):
        self._app_id     = os.getenv("FACEBOOK_APP_ID", "").strip()
        self._app_secret = os.getenv("FACEBOOK_APP_SECRET", "").strip()
        self._creds: dict = json.loads(credentials_json) if credentials_json else {}

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str) -> tuple[str, None]:
        """Generate Facebook/Instagram OAuth consent URL. No PKCE needed."""
        scopes = ",".join([
            "instagram_basic",
            "instagram_content_publish",
            "pages_show_list",
            "pages_read_engagement",
        ])
        params = "&".join([
            f"client_id={self._app_id}",
            f"redirect_uri={redirect_uri}",
            f"scope={scopes}",
            "response_type=code",
            f"state={state}",
        ])
        return f"{FB_AUTH_URL}?{params}", None

    def exchange_code(self, code: str, redirect_uri: str, **kwargs) -> str:
        """
        Exchange auth code → short-lived user token → long-lived token.
        Then discovers the linked Instagram Business Account.
        Returns JSON string for DB storage.
        """
        # Step 1 — short-lived user token
        r = requests.get(FB_TOKEN_URL, params={
            "client_id":     self._app_id,
            "client_secret": self._app_secret,
            "code":          code,
            "redirect_uri":  redirect_uri,
        }, timeout=30)
        r.raise_for_status()
        short_token = r.json()["access_token"]

        # Step 2 — exchange for long-lived token (valid ~60 days)
        lr = requests.get(FB_TOKEN_URL, params={
            "grant_type":       "fb_exchange_token",
            "client_id":        self._app_id,
            "client_secret":    self._app_secret,
            "fb_exchange_token": short_token,
        }, timeout=30)
        lr.raise_for_status()
        long_token = lr.json()["access_token"]

        # Step 3 — find Instagram Business Account linked to a Facebook Page
        pages_r = requests.get(f"{GRAPH_URL}/me/accounts",
                               params={"access_token": long_token}, timeout=30)
        pages_r.raise_for_status()
        pages = pages_r.json().get("data", [])

        ig_user_id = ig_username = page_id = page_access_token = None

        for page in pages:
            p_id    = page["id"]
            p_token = page["access_token"]
            ig_r    = requests.get(
                f"{GRAPH_URL}/{p_id}",
                params={"fields": "instagram_business_account", "access_token": p_token},
                timeout=30,
            )
            ig_data = ig_r.json().get("instagram_business_account")
            if ig_data:
                ig_user_id        = ig_data["id"]
                page_id           = p_id
                page_access_token = p_token
                u = requests.get(f"{GRAPH_URL}/{ig_user_id}",
                                 params={"fields": "username,name", "access_token": p_token},
                                 timeout=15).json()
                ig_username = u.get("username") or u.get("name", "")
                break

        if not ig_user_id:
            raise ValueError(
                "No Instagram Business/Creator account found linked to any Facebook Page. "
                "Make sure your Instagram account is Business or Creator type and connected "
                "to a Facebook Page in Instagram Settings."
            )

        return json.dumps({
            "user_access_token":  long_token,
            "ig_user_id":         ig_user_id,
            "ig_username":        ig_username,
            "page_id":            page_id,
            "page_access_token":  page_access_token,
        })

    # ------------------------------------------------------------------
    # BasePlatformPublisher interface
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Facebook long-lived tokens last ~60 days and cannot be refreshed automatically
        (requires user interaction). We just validate the token is present.
        """
        if not self._creds.get("page_access_token"):
            raise ValueError("Instagram not authenticated. Please reconnect the account.")

    def is_authenticated(self) -> bool:
        return bool(self._creds.get("ig_user_id") and self._creds.get("page_access_token"))

    def get_updated_credentials_json(self) -> str:
        return json.dumps(self._creds)

    # ------------------------------------------------------------------
    # User info (called right after OAuth to populate channel thumbnail)
    # ------------------------------------------------------------------

    def get_user_info(self) -> dict | None:
        """Fetch Instagram username and profile picture URL."""
        try:
            ig_id = self._creds.get("ig_user_id")
            token = self._creds.get("page_access_token")
            if not ig_id or not token:
                return None
            resp = requests.get(
                f"{GRAPH_URL}/{ig_id}",
                params={"fields": "username,name,profile_picture_url", "access_token": token},
                timeout=15,
            )
            resp.raise_for_status()
            d = resp.json()
            return {
                "channel_id":    ig_id,
                "thumbnail_url": d.get("profile_picture_url"),
                "display_name":  d.get("username") or d.get("name"),
            }
        except Exception as e:
            logger.warning(f"Instagram get_user_info failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Publish (Reels via resumable upload)
    # ------------------------------------------------------------------

    def publish(self, video: VideoPost) -> PublishResult:
        """Upload and publish video as an Instagram Reel."""
        if not self.is_authenticated():
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message="Account not authenticated.")
        try:
            self.authenticate()
        except Exception as e:
            return PublishResult(platform=Platform.INSTAGRAM, success=False, error_message=str(e))

        if not os.path.exists(video.file_path):
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message=f"Video file not found: {video.file_path}")

        ig_id  = self._creds["ig_user_id"]
        token  = self._creds["page_access_token"]
        fsize  = os.path.getsize(video.file_path)

        # Build caption — title + description + hashtags (max 2200 chars)
        parts = [video.title]
        if video.description:
            parts.append(video.description)
        if video.tags:
            parts.append(" ".join(
                f"#{t.strip('#').strip()}" for t in video.tags if t.strip()
            ))
        caption = "\n\n".join(parts)[:2200]

        # ── Step 1: create resumable upload session ────────────
        try:
            logger.info(f"[Instagram] Creating Reels container for '{video.title}'")
            cr = requests.post(
                f"{GRAPH_URL}/{ig_id}/media",
                headers={"Authorization": f"OAuth {token}"},
                json={
                    "media_type":    "REELS",
                    "upload_type":   "resumable",
                    "caption":       caption,
                    "share_to_feed": True,
                },
                timeout=30,
            )
            cr.raise_for_status()
            cd           = cr.json()
            container_id = cd["id"]
            upload_uri   = cd.get("uri") or cd.get("upload_url", "")
            logger.info(f"[Instagram] Container created: {container_id}")
        except requests.HTTPError as e:
            body = e.response.text if e.response else str(e)
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message=f"Instagram container error: {body[:400]}")
        except Exception as e:
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message=f"Instagram container error: {e}")

        # ── Step 2: upload video bytes ─────────────────────────
        try:
            logger.info(f"[Instagram] Uploading {fsize} bytes…")
            with open(video.file_path, "rb") as fh:
                ur = requests.post(
                    upload_uri,
                    headers={
                        "Authorization": f"OAuth {token}",
                        "offset":        "0",
                        "file_size":     str(fsize),
                        "Content-Type":  "application/octet-stream",
                    },
                    data=fh,
                    timeout=600,
                )
            ur.raise_for_status()
            logger.info("[Instagram] Video bytes uploaded OK")
        except Exception as e:
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message=f"Instagram upload error: {e}")

        # ── Step 3: poll processing status ────────────────────
        for attempt in range(STATUS_POLL_MAX):
            time.sleep(STATUS_POLL_SECS)
            try:
                sr = requests.get(
                    f"{GRAPH_URL}/{container_id}",
                    params={"fields": "status_code,status", "access_token": token},
                    timeout=30,
                )
                sr.raise_for_status()
                sd     = sr.json()
                code   = sd.get("status_code", "")
                logger.info(f"[Instagram] Status [{attempt+1}/{STATUS_POLL_MAX}]: {code}")
                if code == "FINISHED":
                    break
                if code in ("ERROR", "EXPIRED"):
                    return PublishResult(
                        platform=Platform.INSTAGRAM, success=False,
                        error_message=f"Instagram processing {code}: {sd.get('status', '')}",
                    )
            except Exception as e:
                logger.warning(f"[Instagram] Status check error (attempt {attempt+1}): {e}")
        else:
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message="Instagram processing timed out.")

        # ── Step 4: publish the container ─────────────────────
        try:
            logger.info(f"[Instagram] Publishing container {container_id}")
            pr = requests.post(
                f"{GRAPH_URL}/{ig_id}/media_publish",
                headers={"Authorization": f"OAuth {token}"},
                json={"creation_id": container_id},
                timeout=30,
            )
            pr.raise_for_status()
            media_id  = pr.json().get("id", "")
            video_url = f"https://www.instagram.com/reel/{media_id}/" if media_id else "https://www.instagram.com"
            logger.info(f"[Instagram] Published: {video_url}")
            return PublishResult(platform=Platform.INSTAGRAM, success=True,
                                 video_id=media_id, video_url=video_url)
        except requests.HTTPError as e:
            body = e.response.text if e.response else str(e)
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message=f"Instagram publish error: {body[:400]}")
        except Exception as e:
            return PublishResult(platform=Platform.INSTAGRAM, success=False,
                                 error_message=f"Instagram publish error: {e}")
