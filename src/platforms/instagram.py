"""
Instagram Reels publisher — Phase 2 (not yet implemented).

When ready, implement using the Instagram Graph API:
  https://developers.facebook.com/docs/instagram-api/guides/reels-publishing

Required env vars (add to .env.example when implementing):
  INSTAGRAM_ACCESS_TOKEN
  INSTAGRAM_BUSINESS_ACCOUNT_ID
"""
from src.models.video import VideoPost, PublishResult, Platform
from src.platforms.base import BasePlatformPublisher


class InstagramPublisher(BasePlatformPublisher):
    def authenticate(self) -> None:
        raise NotImplementedError("Instagram publisher is not yet implemented (Phase 2).")

    def is_authenticated(self) -> bool:
        return False

    def publish(self, video: VideoPost) -> PublishResult:
        raise NotImplementedError("Instagram publisher is not yet implemented (Phase 2).")
