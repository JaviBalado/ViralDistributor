from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.utils.logger import get_logger

logger = get_logger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def start_scheduler():
    scheduler.add_job(
        _check_and_publish,
        trigger=IntervalTrigger(minutes=1),
        id="publish_checker",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — checking for pending posts every minute.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()


def _get_publisher(account):
    """Factory: return the right publisher for an account's platform."""
    from src.platforms.youtube   import YouTubePublisher
    from src.platforms.tiktok    import TikTokPublisher
    from src.platforms.instagram import InstagramPublisher

    if account.platform == "youtube":
        return YouTubePublisher(credentials_json=account.credentials_json)
    if account.platform == "tiktok":
        return TikTokPublisher(credentials_json=account.credentials_json)
    if account.platform == "instagram":
        return InstagramPublisher(credentials_json=account.credentials_json)
    return None


def _check_and_publish():
    """Runs every minute. Finds pending posts due for publishing and uploads them."""
    from src.db.database import SessionLocal
    from src.db.models   import ScheduledPost
    from src.models.video import VideoPost

    db = SessionLocal()
    try:
        now     = datetime.now(timezone.utc).replace(tzinfo=None)
        pending = (
            db.query(ScheduledPost)
            .filter(ScheduledPost.status == "pending", ScheduledPost.scheduled_at <= now)
            .all()
        )

        for post in pending:
            logger.info(f"Publishing post #{post.id} '{post.title}' → {post.account.platform} "
                        f"account #{post.account_id}")
            account = post.account

            try:
                publisher = _get_publisher(account)

                if publisher is None:
                    post.status        = "failed"
                    post.error_message = f"Platform '{account.platform}' not supported."
                    db.commit()
                    continue

                video = VideoPost(
                    file_path=post.file_path,
                    title=post.title,
                    description=post.description or "",
                    tags=[t.strip() for t in post.tags.split(",") if t.strip()] if post.tags else [],
                    is_short=True,
                )
                result = publisher.publish(video)

                if result.success:
                    post.status    = "published"
                    post.video_url = result.video_url
                    logger.info(f"Post #{post.id} published: {result.video_url}")
                    # Persist refreshed credentials (YouTube refreshes its token; TikTok may too)
                    try:
                        account.credentials_json = publisher.get_updated_credentials_json()
                    except Exception:
                        pass
                else:
                    post.status        = "failed"
                    post.error_message = result.error_message
                    logger.error(f"Post #{post.id} failed: {result.error_message}")

            except Exception as e:
                import traceback
                detail             = str(e) or repr(e) or traceback.format_exc()
                post.status        = "failed"
                post.error_message = detail
                logger.error(f"Exception publishing post #{post.id}: {detail}")

            db.commit()

    finally:
        db.close()
