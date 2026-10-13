import os
from pathlib import Path

_INSECURE_SECRETS = {
    "",
    "change-me-in-production-please-use-env",
    "test-secret-key-for-verify",
    "secret",
    "changeme",
}


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


# Path to the project .env. In Docker the backend is mounted at /app (so the
# repo-root heuristic below would resolve to "/.env", which isn't the real
# file and isn't even mounted in), so the container sets ENV_FILE=/app/.env and
# docker-compose bind-mounts the host .env there — that's what lets the Settings
# page and first-run setup wizard actually persist back to .env.
_ENV_PATH = Path(
    os.environ.get("ENV_FILE")
    or (Path(__file__).resolve().parent.parent.parent / ".env")
)
_load_dotenv(_ENV_PATH)


def update_env_file(key: str, value: str) -> bool:
    """Persist KEY=value into the project .env (best effort). Updates an
    existing uncommented line or appends a new one."""
    try:
        lines = _ENV_PATH.read_text().splitlines() if _ENV_PATH.exists() else []
        found = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(f"{key}=") and not s.startswith("#"):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        _ENV_PATH.write_text("\n".join(lines) + "\n")
        return True
    except Exception:
        return False


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.secret_key = os.environ.get("SECRET_KEY", "")
        self.database_url = os.environ.get("DATABASE_URL", "sqlite:///./riprate.db")
        self.media_dir = os.environ.get("MEDIA_DIR", "/root/media")
        self.media_expected_fstype = os.environ.get("MEDIA_EXPECTED_FSTYPE", "").strip()
        self.admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        self.admin_password = os.environ.get("ADMIN_PASSWORD", "")
        self.max_concurrent_downloads = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))
        self.imdb_cookie = os.environ.get("IMDB_COOKIE", "")
        # TMDB v4 bearer token (Authorization: Bearer <token>), used to map
        # IMDB ids are translated to TMDB ids by the Nova stream provider.
        # and to fetch title metadata (posters, plots) for Watch/Jellyfin.
        self.tmdb_api_key = os.environ.get("TMDB_API_KEY", "")
        # Wyzie Subs API key (free — store.wyzie.io/redeem); enables subtitle
        # priority downloads from sub.wyzie.io when enabled.
        self.wyzie_api_key = os.environ.get("WYZIE_API_KEY", "")
        # Cached TMDB metadata images (posters/backdrops) live here.
        self.meta_dir = os.environ.get("META_DIR", "./data/metadata")
        # Fast local workspace for in-flight downloads (segments, assembly,
        # remux). Deliberately NOT on the media drive: media storage is often
        # a slow USB/network disk, and staging there writes every video to it
        # three times — the queue visibly stalls at "99%" while it grinds.
        # Only the finished file is moved to MEDIA_DIR, in one pass.
        self.tmp_dir = os.environ.get("TMP_DIR", "./data/tmp")

        # ── Session / cookie security ──────────────────────────────────
        # Set SESSION_SECURE=true when serving over HTTPS.
        self.session_secure = _bool("SESSION_SECURE", "false")
        # Absolute session lifetimes (seconds)
        self.session_max_age_user = int(os.environ.get("SESSION_MAX_AGE_USER", str(7 * 86400)))
        self.session_max_age_admin = int(os.environ.get("SESSION_MAX_AGE_ADMIN", str(12 * 3600)))
        # Idle timeouts (seconds)
        self.session_idle_user = int(os.environ.get("SESSION_IDLE_USER", str(24 * 3600)))
        self.session_idle_admin = int(os.environ.get("SESSION_IDLE_ADMIN", str(2 * 3600)))
        # Extra origins allowed to call the API (dev servers etc.)
        self.allowed_origins = [
            o.strip() for o in os.environ.get(
                "ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000,http://localhost:8000"
            ).split(",") if o.strip()
        ]

        # ── Download quality / size control ────────────────────────────
        # Variant selection is resolution-driven (Settings → preferred
        # quality, default 1080p, falling back to the next lower).
        # If the downloaded stream would produce a file larger than this,
        # re-encode with libx264 at VIDEO_CRF to shrink it.
        self.max_file_gb = float(os.environ.get("MAX_FILE_SIZE_GB", "4.0"))
        # CRF/preset used only when a re-encode is required. 20/medium keeps
        # visibly more detail than the old 23/fast (fixes re-encode blur)
        # at a modest size/time cost; stream-copies are untouched either way.
        self.video_crf = int(os.environ.get("VIDEO_CRF", "20"))
        self.video_preset = os.environ.get("VIDEO_PRESET", "medium")

        self._validate()

    def _validate(self) -> None:
        if self.secret_key in _INSECURE_SECRETS or len(self.secret_key) < 32:
            raise RuntimeError(
                "SECRET_KEY must be a random string of at least 32 characters. "
                "Generate one with:  openssl rand -hex 32"
            )
        # ADMIN_PASSWORD is mandatory in practice, but a BLANK value is tolerated
        # solely so a brand-new install can boot into the first-run setup wizard
        # (which then requires the operator to choose one and writes it back to
        # .env). After setup, .env always has ADMIN_PASSWORD, so an empty value
        # only ever exists before the very first setup. A non-empty value must
        # still meet the strength rule.
        if self.admin_password and len(self.admin_password) < 12:
            raise RuntimeError(
                "ADMIN_PASSWORD must be at least 12 characters — the admin account "
                "has full control and needs a strong password."
            )


settings = Settings()
