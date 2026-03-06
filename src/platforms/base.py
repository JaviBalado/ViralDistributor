from abc import ABC, abstractmethod
from src.models.video import VideoPost, PublishResult


class BasePlatformPublisher(ABC):
    """
    Abstract base class for all social media platform publishers.

    To add a new platform (Instagram, TikTok, etc.):
    1. Create a new file in src/platforms/ (e.g., instagram.py)
    2. Subclass BasePlatformPublisher
    3. Implement the three abstract methods below
    4. Register the publisher in src/main.py
    """

    @abstractmethod
    def authenticate(self) -> None:
        """
        Handle authentication/OAuth flow for the platform.
        Should store credentials for reuse across publish() calls.
        """
        ...

    @abstractmethod
    def publish(self, video: VideoPost) -> PublishResult:
        """
        Upload and publish the given VideoPost to the platform.
        Returns a PublishResult with the outcome.
        """
        ...

    @abstractmethod
    def is_authenticated(self) -> bool:
        """Return True if valid, non-expired credentials are available."""
        ...
