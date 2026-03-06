from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PrivacyStatus(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNLISTED = "unlisted"


class Platform(str, Enum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"  # Phase 2
    TIKTOK = "tiktok"        # Phase 3


@dataclass
class VideoPost:
    """
    Represents a video to be published on one or more platforms.
    This is the central data model passed to every publisher.
    """
    file_path: str
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    privacy: PrivacyStatus = PrivacyStatus.PRIVATE
    category_id: str = "22"
    thumbnail_path: Optional[str] = None
    # YouTube Shorts are detected automatically (vertical + <=60s or #Shorts tag)
    is_short: bool = True
    # Platform-specific extra options (flexible for future platforms)
    platform_options: dict = field(default_factory=dict)


@dataclass
class PublishResult:
    """Result returned by every publisher after attempting to upload."""
    platform: Platform
    success: bool
    video_id: Optional[str] = None
    video_url: Optional[str] = None
    error_message: Optional[str] = None
