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


def _check_and_publish():
    """Runs every minute. Finds pending posts due for publishing and uploads them."""
    from src.db.database import SessionLocal
    from src.db.models import ScheduledPost
    from src.models.video import VideoPost
    from src.platforms.youtube import YouTubePublisher

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        pending = (
            db.query(ScheduledPost)
            .filter(ScheduledPost.status == "pending", ScheduledPost.scheduled_at <= now)
            .all()
        )

        for post in pending:
            logger.info(f"Publishing post #{post.id} '{post.title}' for account #{post.account_id}")
            account = post.account

            try:
                if account.platform == "youtube":
                    publisher = YouTubePublisher(credentials_json=account.credentials_json)
                    video = VideoPost(
                        file_path=post.file_path,
                        title=post.title,
                        description=post.description,
                        tags=[t.strip() for t in post.tags.split(",") if t.strip()],
                        is_short=True,
                    )
                    result = publisher.publish(video)

                    if result.success:
                        account.credentials_json = publisher.get_updated_credentials_json()
                        post.status = "published"
                        post.video_url = result.video_url
                        logger.info(f"Post #{post.id} published: {result.video_url}")
                    else:
                        post.status = "failed"
                        post.error_message = result.error_message
                        logger.error(f"Post #{post.id} failed: {result.error_message}")
                else:
                    post.status = "failed"
                    post.error_message = f"Platform '{account.platform}' not yet implemented."

            except Exception as e:
                post.status = "failed"
                post.error_message = str(e)
                logger.error(f"Exception publishing post #{post.id}: {e}")

            db.commit()

    finally:
        db.close()
