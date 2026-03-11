import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/viraldistributor.db")

# Ensure data directory exists for SQLite
if DATABASE_URL.startswith("sqlite"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from src.db import models  # noqa — registers models with Base
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations():
    """Add new columns to existing tables (SQLite doesn't support ADD COLUMN IF NOT EXISTS)."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    try:
        cols = {c["name"] for c in insp.get_columns("accounts")}
        with engine.connect() as conn:
            if "channel_id" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN channel_id VARCHAR(50)"))
                conn.commit()
            if "channel_thumbnail_url" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN channel_thumbnail_url TEXT"))
                conn.commit()
    except Exception:
        pass  # Table might not exist yet on first run
