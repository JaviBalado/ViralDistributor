from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from src.db.database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)           # User-given label, e.g. "Gaming Channel"
    platform = Column(String(20), nullable=False)        # youtube | instagram | tiktok
    credentials_json = Column(Text, nullable=True)       # Serialized OAuth token (JSON)
    channel_id = Column(String(50), nullable=True)       # YouTube channel ID
    channel_thumbnail_url = Column(Text, nullable=True)  # YouTube channel profile picture URL
    created_at = Column(DateTime, default=datetime.utcnow)

    posts = relationship("ScheduledPost", back_populates="account", cascade="all, delete")


class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    title = Column(String(100), nullable=False)
    description = Column(Text, default="")
    tags = Column(String(500), default="")               # Comma-separated
    file_path = Column(String(500), nullable=False)      # Absolute path inside container
    scheduled_at = Column(DateTime, nullable=False)      # UTC datetime
    status = Column(String(20), default="pending")       # pending | published | failed
    video_url = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="posts")
