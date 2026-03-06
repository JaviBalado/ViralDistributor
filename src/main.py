"""
ViralDistributor — CLI entry point.
For the web dashboard, run: uvicorn src.web.app:app --host 0.0.0.0 --port 8000
"""
import argparse
import sys
from dotenv import load_dotenv

from src.models.video import PrivacyStatus, VideoPost
from src.platforms.youtube import YouTubePublisher
from src.utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="ViralDistributor CLI — publish videos to social platforms")
    subparsers = parser.add_subparsers(dest="command")

    # youtube sub-command
    yt = subparsers.add_parser("youtube", help="Upload a video to YouTube")
    yt.add_argument("--file", required=True, help="Path to the video file")
    yt.add_argument("--title", required=True, help="Video title")
    yt.add_argument("--description", default="", help="Video description")
    yt.add_argument("--tags", default="", help="Comma-separated tags")
    yt.add_argument("--privacy", default="private", choices=["public", "private", "unlisted"])
    yt.add_argument("--long", action="store_true", help="Treat as long video (not a Short)")

    args = parser.parse_args()

    if args.command == "youtube":
        tag_list = [t.strip() for t in args.tags.split(",") if t.strip()]
        video = VideoPost(
            file_path=args.file,
            title=args.title,
            description=args.description,
            tags=tag_list,
            privacy=PrivacyStatus(args.privacy),
            is_short=not args.long,
        )
        publisher = YouTubePublisher()
        result = publisher.publish(video)
        if result.success:
            logger.info(f"Published: {result.video_url}")
        else:
            logger.error(f"Failed: {result.error_message}")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
