"""
TikTok publisher — Phase 3 (not yet implemented).

When ready, implement using the TikTok Content Posting API:
  https://developers.tiktok.com/doc/content-posting-api-get-started

Required env vars (add to .env.example when implementing):
  TIKTOK_CLIENT_KEY
  TIKTOK_CLIENT_SECRET
  TIKTOK_ACCESS_TOKEN
"""
from src.models.video import VideoPost, PublishResult, Platform
from src.platforms.base import BasePlatformPublisher


class TikTokPublisher(BasePlatformPublisher):
    def authenticate(self) -> None:
        raise NotImplementedError("TikTok publisher is not yet implemented (Phase 3).")

    def is_authenticated(self) -> bool:
        return False

    def publish(self, video: VideoPost) -> PublishResult:
        raise NotImplementedError("TikTok publisher is not yet implemented (Phase 3).")
