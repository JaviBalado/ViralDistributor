import os
from dotenv import load_dotenv

load_dotenv()


def get_web_credentials() -> tuple[str, str]:
    """Return (username, password) for the web dashboard from env vars."""
    username = os.getenv("DASHBOARD_USERNAME")
    password = os.getenv("DASHBOARD_PASSWORD")
    if not username or not password:
        raise EnvironmentError(
            "DASHBOARD_USERNAME and DASHBOARD_PASSWORD must be set in .env"
        )
    return username, password
