from __future__ import annotations

import enum
import json
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Boolean, Float, Integer, String, Text, UniqueConstraint,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings

_is_sqlite = "sqlite" in settings.database_url
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # WAL lets readers (SSE polls) and the writer (download progress) run
        # concurrently instead of blocking each other; busy_timeout makes a
        # write wait for a lock rather than raising "database is locked".
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    queued = "queued"
    downloading = "downloading"   # phase 1: fetching HLS segments
    processing = "processing"     # phase 2: ffmpeg remux / re-encode to MP4
    paused = "paused"
    failed = "failed"
    completed = "completed"
    missing = "missing"         # record retained; file is absent from the library


class MediaType(str, enum.Enum):
    movie = "movie"
    tvSeries = "tvSeries"


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class RequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=True)
    imdb_id = Column(String, nullable=False, index=True)
    media_type = Column(String, nullable=False)
    # Routing category (Settings wizard): "movies"/"shows" built-ins or a
    # custom categories decide which /media/library folder a job lands in
    category = Column(String, nullable=True)
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    episode_title = Column(String, nullable=True)
    status = Column(String, default=JobStatus.queued, nullable=False)
    file_path = Column(String, nullable=True)
    # External .vtt sidecar next to the video (written as soon as subtitles
    # are fetched — often before the video itself finishes downloading).
    subtitle_path = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    progress = Column(Float, default=0.0)
    # Hidden from the Downloads history but kept as a Library record (set when
    # an admin "removes" a completed download — the media stays in the library).
    archived = Column(Boolean, default=False, nullable=False)
    # Queue ordering — lower runs first
    position = Column(Integer, default=0, nullable=False)
    # Provenance: which request / user caused this job (null = added by admin)
    request_id = Column(Integer, nullable=True, index=True)
    requested_by = Column(Integer, nullable=True, index=True)
    # Download telemetry (populated while the job is running)
    downloaded_bytes = Column(Integer, nullable=True)   # bytes written so far
    total_bytes = Column(Integer, nullable=True)        # estimated final size
    download_bitrate = Column(String, nullable=True)    # e.g. "2.5 MB/s" or "Encoding 3.1x"
    source_url = Column(String, nullable=True)          # resolved m3u8 URL
    current_source = Column(String, nullable=True)      # provider being tried, e.g. "Nova"
    # Provenance of the job itself: manual (admin) | request (user request) |
    # schedule (auto-queued by a Schedule run). Schedule-sourced jobs are
    # excluded from the Downloads page history — see ScheduleRun/Item below.
    source = Column(String, default="manual", nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    def label(self) -> str:
        if self.media_type == "tvSeries":
            return f"{self.title} S{self.season:02d}E{self.episode:02d}"
        return f"{self.title} ({self.year})"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default=UserRole.user, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    # Brute-force protection
    failed_logins = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    # Bumped to invalidate every existing session for this user
    session_version = Column(Integer, default=1, nullable=False)
    password_changed_at = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MediaRequest(Base):
    __tablename__ = "media_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    imdb_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=True)
    media_type = Column(String, nullable=False)
    is_anime = Column(Boolean, default=False, nullable=False)
    seasons = Column(String, nullable=True)   # JSON list of ints, TV only
    status = Column(String, default=RequestStatus.pending, nullable=False)
    deny_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(Integer, nullable=True)   # admin user id


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False)
    username = Column(String, nullable=True)
    action = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    ip = Column(String, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=True)
    is_anime = Column(Boolean, default=False, nullable=False)
    seasons = Column(String, nullable=True)     # JSON list of ints; null/[] = all seasons
    frequency = Column(String, default="weekly", nullable=False)  # daily|weekly|monthly
    day_of_week = Column(Integer, nullable=True)  # 0=Mon..6=Sun (weekly)
    hour = Column(Integer, nullable=True)         # 0-23, preferred hour to run
    is_active = Column(Boolean, default=True, nullable=False)
    last_checked = Column(DateTime, nullable=True)
    last_result = Column(Text, nullable=True)     # human summary of last run
    last_queued = Column(Integer, default=0)      # episodes queued on last run
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ScheduleRun(Base):
    """One execution of a schedule's check (automatic tick or manual "Check
    now") — a run-attempt record, distinct from Downloads history."""
    __tablename__ = "schedule_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schedule_id = Column(Integer, nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running", nullable=False)   # running|done
    checked = Column(Integer, default=0)
    queued = Column(Integer, default=0)
    summary = Column(Text, nullable=True)


class ScheduleRunItem(Base):
    """One episode (or season-level error) considered during a ScheduleRun.
    status is a denormalized snapshot kept in sync with the linked job while
    it exists (see manager.py `_update`), so the run stays readable even
    after the DownloadJob row is later archived/purged by retention."""
    __tablename__ = "schedule_run_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, nullable=False, index=True)
    imdb_id = Column(String, nullable=False)
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    episode_title = Column(String, nullable=True)
    job_id = Column(Integer, nullable=True, index=True)
    # queued | downloading | completed | failed (mirrors DownloadJob.status,
    # collapsed; "failed" with no job_id means a season-level fetch error)
    status = Column(String, default="queued", nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class EpisodeMetadata(Base):
    """TMDB metadata cached per-episode (overview/air date/rating/still), one
    row per (show imdb_id, season, episode). Mirrors TitleMetadata but at
    episode granularity — powers the episode-card hover info."""
    __tablename__ = "episode_metadata"
    __table_args__ = (UniqueConstraint("imdb_id", "season", "episode", name="uq_episode_meta"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String, nullable=False, index=True)   # show's imdb_id
    season = Column(Integer, nullable=False)
    episode = Column(Integer, nullable=False)
    title = Column(String, nullable=True)
    overview = Column(Text, nullable=True)
    air_date = Column(String, nullable=True)
    rating = Column(Float, nullable=True)
    runtime = Column(Integer, nullable=True)
    still_file = Column(String, nullable=True)   # filename inside META_DIR
    fetched_at = Column(DateTime, default=datetime.utcnow)


class WatchProgress(Base):
    """Per-user playback position for a completed download (drives resume +
    the Continue Watching row). One row per (user, job)."""
    __tablename__ = "watch_progress"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_watch_user_job"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    job_id = Column(Integer, nullable=False, index=True)
    position = Column(Float, default=0.0, nullable=False)   # seconds
    duration = Column(Float, default=0.0, nullable=False)   # seconds (0 = unknown)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Favorite(Base):
    """Per-user favorited title (movie or whole show — imdb_id level)."""
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "imdb_id", name="uq_fav_user_imdb"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    imdb_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SearchHistory(Base):
    """Per-user recently-clicked search results (the cards shown on the Search
    idle screen). Server-side so it follows the user across devices and can be
    pruned individually. One row per (user, imdb_id); newest N per media_type
    are kept."""
    __tablename__ = "search_history"
    __table_args__ = (UniqueConstraint("user_id", "imdb_id", name="uq_search_user_imdb"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    imdb_id = Column(String, nullable=False)
    media_type = Column(String, nullable=False)   # movie | tvSeries
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=True)
    poster_url = Column(String, nullable=True)
    cast = Column(String, default="")
    is_anime = Column(Boolean, default=False, nullable=False)
    clicked_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class TitleMetadata(Base):
    """TMDB metadata cached locally per title (imdb_id level), so the Watch
    page and Jellyfin NFOs never re-hit TMDB for the same title. Poster and
    backdrop images are downloaded once into META_DIR and referenced by
    filename here."""
    __tablename__ = "title_metadata"

    id = Column(Integer, primary_key=True, autoincrement=True)
    imdb_id = Column(String, unique=True, nullable=False, index=True)
    tmdb_id = Column(Integer, nullable=True)
    media_type = Column(String, nullable=False)
    title = Column(String, nullable=True)
    year = Column(Integer, nullable=True)
    overview = Column(Text, nullable=True)
    tagline = Column(String, nullable=True)
    genres = Column(String, nullable=True)        # JSON list of names
    rating = Column(Float, nullable=True)         # TMDB vote_average (0-10)
    runtime = Column(Integer, nullable=True)      # minutes
    original_language = Column(String, nullable=True)
    original_title = Column(String, nullable=True)
    poster_file = Column(String, nullable=True)   # filename inside META_DIR
    backdrop_file = Column(String, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_columns()
    _seed_admin()
    _seed_settings_from_env()


def _seed_settings_from_env() -> None:
    """First run: adopt any HISTORY_RETENTION_DAYS from the environment/.env."""
    import os
    with SessionLocal() as db:
        # Remove the retired metadata-source setting from upgraded databases;
        # search source is now a separate, two-option setting.
        db.query(Setting).filter(Setting.key == "metadata_source").delete(
            synchronize_session=False
        )
        # The earlier Anime category was a single custom bucket. Remove only
        # that category definition now that Anime Movies and Anime Shows are
        # built-ins; existing media files and download records are untouched.
        categories = db.query(Setting).filter_by(key="media_categories").first()
        if categories and categories.value:
            try:
                parsed = json.loads(categories.value)
                if isinstance(parsed, list):
                    filtered = [c for c in parsed
                                if not (isinstance(c, dict)
                                        and str(c.get("key", "")).strip().lower() == "anime")]
                    if len(filtered) != len(parsed):
                        categories.value = json.dumps(filtered)
            except (TypeError, ValueError):
                pass
        db.query(Setting).filter(Setting.key == "anime_folder").delete(
            synchronize_session=False
        )
        for key, env_key in _SETTING_ENV_KEYS.items():
            if db.query(Setting).filter_by(key=key).first() is None:
                env_val = os.environ.get(env_key)
                if env_val is not None:
                    db.add(Setting(key=key, value=env_val))
        db.commit()


def _migrate_columns() -> None:
    """Add columns that were introduced after initial schema creation."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)

    migrations: dict[str, dict[str, str]] = {
        "download_jobs": {
            "downloaded_bytes": "INTEGER",
            "total_bytes": "INTEGER",
            "download_bitrate": "TEXT",
            "source_url": "TEXT",
            "position": "INTEGER DEFAULT 0",
            "request_id": "INTEGER",
            "requested_by": "INTEGER",
            "archived": "BOOLEAN DEFAULT 0",
            "current_source": "TEXT",
            "source": "TEXT DEFAULT 'manual'",
            "subtitle_path": "TEXT",
            "category": "TEXT",
        },
        "users": {
            "role": "TEXT DEFAULT 'user'",
            "is_active": "BOOLEAN DEFAULT 1",
            "failed_logins": "INTEGER DEFAULT 0",
            "locked_until": "DATETIME",
            "session_version": "INTEGER DEFAULT 1",
            "password_changed_at": "DATETIME",
            "last_login_at": "DATETIME",
        },
        "schedules": {
            "hour": "INTEGER",
            "is_anime": "BOOLEAN DEFAULT 0",
        },
        "media_requests": {
            "is_anime": "BOOLEAN DEFAULT 0",
        },
        "search_history": {
            "is_anime": "BOOLEAN DEFAULT 0",
        },
        "title_metadata": {
            "original_language": "TEXT",
            "original_title": "TEXT",
        },
    }
    with engine.connect() as conn:
        for table, cols in migrations.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col, col_type in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
        # Backfill queue positions for pre-existing rows
        conn.execute(text(
            "UPDATE download_jobs SET position = id WHERE position IS NULL OR position = 0"
        ))
        conn.commit()


def _seed_admin() -> None:
    import bcrypt

    # No ADMIN_PASSWORD yet ⇒ a brand-new install: don't seed an admin. The
    # first-run setup wizard (src/api/setup.py) creates it. `needs_setup` keys
    # off "no admin exists / setup not completed", so leaving the table empty
    # is what triggers the wizard.
    if not settings.admin_password:
        return

    with SessionLocal() as db:
        existing = db.query(User).filter_by(username=settings.admin_username).first()
        if existing:
            # Ensure the seeded account always has the admin role
            changed = False
            if existing.role != UserRole.admin:
                existing.role = UserRole.admin
                changed = True
            # .env stays the source of truth for the admin password until the
            # admin changes it in the UI (password_changed_at set).
            if existing.password_changed_at is None and not bcrypt.checkpw(
                settings.admin_password.encode(), existing.password_hash.encode()
            ):
                existing.password_hash = bcrypt.hashpw(
                    settings.admin_password.encode(), bcrypt.gensalt()
                ).decode()
                existing.session_version += 1
                changed = True
            if changed:
                db.commit()
            return
        pw_hash = bcrypt.hashpw(settings.admin_password.encode(), bcrypt.gensalt()).decode()
        user = User(
            username=settings.admin_username,
            password_hash=pw_hash,
            role=UserRole.admin,
        )
        db.add(user)
        db.commit()


# ── App settings (DB-backed key/value) ──────────────────────────────────────

_SETTING_DEFAULTS = {
    "history_retention_days": "0",   # 0 = keep forever
    # Failed download workspace cleanup: 0 = delete immediately, positive
    # values = retain for this many hours. Restarts always purge failed temp.
    "failed_temp_retention_hours": "0",
    "max_concurrent_downloads": "3",
    "imdb_cookie": "",               # secret; never returned in plaintext
    "imdb_cookie_set_at": "",        # ISO timestamp; drives the 4h expiry
    "watched_threshold_percent": "90",      # % watched → drops out of Continue Watching
    "stream_providers": "",                 # JSON [{key,enabled}]; "" = registry default
    "wyzie_api_key": "",                    # secret; overrides .env WYZIE_API_KEY
    "wyzie_enabled": "false",               # opt-in subtitle downloads via Wyzie Subs
    "subtitle_language": "en",              # ISO-639-1 code searched for/downloaded
    "subtitle_auto_sync": "true",           # align selected subtitles to local audio
    "tmdb_api_key": "",                     # secret; overrides .env TMDB_API_KEY
    "search_source": "tmdb",                # tmdb | imdb (search/seasons/episodes)
    "movies_folder": "Movies",              # legacy — kept in sync with media_categories
    "shows_folder": "Shows",                # legacy — kept in sync with media_categories
    "anime_movies_folder": "Anime Movies",
    "anime_shows_folder": "Anime Shows",
    "media_categories": "",                 # JSON [{key,label,folder,media}] — see library/categories.py
    "preferred_quality": "1080",            # target vertical resolution; falls back to next lower
    "segment_connections": "6",             # parallel segment fetches per download (1-16)
    "video_crf": "20",                      # libx264 CRF used when a re-encode is needed
    "video_preset": "medium",               # libx264 preset (ultrafast…veryslow)
    "max_file_gb": "10.0",                  # over this size → re-encode to shrink
    "queue_paused": "false",                # global queue pause — survives restarts
    # ── OpenVPN profile pool ──
    "vpn_enabled": "false",
}

# App-setting key → .env variable name (settings are mirrored to .env).
# NOTE: imdb_cookie is intentionally DB-only and ephemeral (expires after the
# WAF TTL), so it is NOT written to .env. A permanent override still works via
# the IMDB_COOKIE env var in .env.
# DB settings that are ALSO mirrored to .env (via set_setting) and re-seeded
# from .env on a fresh DB (_seed_settings_from_env). Keeps the operator's
# configuration in .env — survives a `./start.sh --fresh` DB wipe and is
# what the first-run setup wizard writes. NOTE: imdb_cookie is deliberately
# excluded — it's a short-lived (4h) DB-only value, not a durable .env secret.
_SETTING_ENV_KEYS = {
    "history_retention_days": "HISTORY_RETENTION_DAYS",
    "failed_temp_retention_hours": "FAILED_TEMP_RETENTION_HOURS",
    "max_concurrent_downloads": "MAX_CONCURRENT_DOWNLOADS",
    "segment_connections": "SEGMENT_CONNECTIONS",
    "preferred_quality": "PREFERRED_QUALITY",
    "video_crf": "VIDEO_CRF",
    "video_preset": "VIDEO_PRESET",
    "max_file_gb": "MAX_FILE_SIZE_GB",
    "search_source": "SEARCH_SOURCE",
    "tmdb_api_key": "TMDB_API_KEY",
    "wyzie_api_key": "WYZIE_API_KEY",
    "wyzie_enabled": "WYZIE_ENABLED",
    "subtitle_language": "SUBTITLE_LANGUAGE",
    "subtitle_auto_sync": "SUBTITLE_AUTO_SYNC",
}


def get_setting(key: str, default: str | None = None) -> str:
    with SessionLocal() as db:
        row = db.query(Setting).filter_by(key=key).first()
        if row is not None and row.value is not None:
            return row.value
    return _SETTING_DEFAULTS.get(key, "") if default is None else default


def set_setting(key: str, value: str) -> None:
    with SessionLocal() as db:
        row = db.query(Setting).filter_by(key=key).first()
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value
        db.commit()
    env_key = _SETTING_ENV_KEYS.get(key)
    if env_key:
        import os
        from src.config import update_env_file
        os.environ[env_key] = value        # reflect immediately in-process
        update_env_file(env_key, value)    # persist to .env


def all_settings() -> dict[str, str]:
    out = dict(_SETTING_DEFAULTS)
    with SessionLocal() as db:
        for row in db.query(Setting).all():
            out[row.key] = row.value
    return out
