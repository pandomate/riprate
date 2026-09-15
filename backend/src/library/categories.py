"""
Media categories — a category is simply a download destination: it tells the
program which folder under /media/library a title's files belong in.

Four categories are built in and undeletable: Movies, Shows, Anime Movies, and
Anime Shows. They're the automatic destinations when nothing is picked. Admins
can add more in the Settings wizard (Music, Books, …); every category
behaves identically and every category is offered for every download. Each
maps to a folder under the media root, which may itself be a dedicated host
mount (see start.sh's MOUNT_* handling).

Stored in the DB setting `media_categories` as JSON:
    [{"key": "movies", "label": "Movies", "folder": "Movies"}, …]
An empty/absent setting falls back to the built-ins, seeded from the older
movies_folder/shows_folder settings so existing installs keep their folders.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.config import settings

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

BUILTIN_KEYS = ("movies", "shows", "anime_movies", "anime_shows")


def _builtin_defaults() -> list[dict]:
    from src.database import get_setting
    return [
        {"key": "movies", "label": "Movies",
         "folder": (get_setting("movies_folder") or "Movies").strip() or "Movies"},
        {"key": "shows", "label": "Shows",
         "folder": (get_setting("shows_folder") or "Shows").strip() or "Shows"},
        {"key": "anime_movies", "label": "Anime Movies",
         "folder": (get_setting("anime_movies_folder") or "Anime Movies").strip() or "Anime Movies"},
        {"key": "anime_shows", "label": "Anime Shows",
         "folder": (get_setting("anime_shows_folder") or "Anime Shows").strip() or "Anime Shows"},
    ]


def _clean_folder(raw: str, fallback: str) -> str:
    """A folder is a relative path under the media root — traversal dropped."""
    parts = [p for p in (raw or "").replace("\\", "/").split("/")
             if p and p not in (".", "..")]
    from src.library.organizer import _safe
    cleaned = "/".join(_safe(p) for p in parts if _safe(p))
    return cleaned or fallback


def all_categories() -> list[dict]:
    """Validated category list — built-ins always present and first."""
    from src.database import get_setting
    builtins = {c["key"]: c for c in _builtin_defaults()}
    out: dict[str, dict] = {}
    try:
        stored = json.loads(get_setting("media_categories") or "[]")
    except (ValueError, TypeError):
        stored = []
    for c in stored if isinstance(stored, list) else []:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key", "")).strip().lower()
        # "anime" was the old single bucket. Anime is now split by TMDB
        # media type into the built-in anime_movies/anime_shows categories.
        # Filter it here as well as during startup migration so an old DB can
        # never leak the retired category back into the API/UI.
        if key == "anime":
            continue
        if not _KEY_RE.match(key):
            continue
        base = builtins.get(key, {})
        out[key] = {
            "key": key,
            "label": (str(c.get("label", "")).strip() or base.get("label") or key.title())[:40],
            "folder": _clean_folder(str(c.get("folder", "")), base.get("folder", key.title())),
        }
    for key, b in builtins.items():
        out.setdefault(key, b)
    # built-ins first, then the rest in stored order.
    ordered = [out[k] for k in BUILTIN_KEYS]
    ordered += [c for k, c in out.items() if k not in BUILTIN_KEYS]
    return ordered


def get_category(key: str | None) -> dict | None:
    for c in all_categories():
        if c["key"] == key:
            return c
    return None


def default_key(media_type: str, is_anime: bool = False) -> str:
    """Return the automatic destination for a title.

    Anime is a classification, not a third TMDB media type: TMDB still calls
    it movie or TV, so callers pass the classification discovered from TMDB.
    """
    if is_anime:
        return "anime_movies" if media_type == "movie" else "anime_shows"
    return "movies" if media_type == "movie" else "shows"


def categories_for(media_type: str) -> list[dict]:
    """Every category is a valid destination for any download — the category
    IS the routing choice. media_type only orders the default first."""
    default = default_key(media_type)
    cats = all_categories()
    return sorted(cats, key=lambda c: 0 if c["key"] == default else 1)


def resolve_key(media_type: str, key: str | None, is_anime: bool = False) -> str:
    """A requested category key, validated — falls back to the built-in
    default rather than erroring (jobs must always route)."""
    if key and get_category(key) is not None:
        return key
    return default_key(media_type, is_anime)


def category_root(key: str | None, media_type: str = "movie") -> Path:
    cat = get_category(key) or get_category(default_key(media_type))
    return Path(settings.media_dir) / cat["folder"]


def save_categories(cats: list[dict]) -> list[dict]:
    """Validate + persist a full category list (built-ins enforced)."""
    from src.database import set_setting
    cleaned: list[dict] = []
    seen_keys: set[str] = set()
    seen_folders: set[str] = set()
    for c in cats:
        key = str(c.get("key", "")).strip().lower()
        # Be tolerant of a stale client posting the retired category; it is
        # removed rather than recreated as a custom category.
        if key == "anime":
            continue
        if not _KEY_RE.match(key) or key in seen_keys:
            raise ValueError(f"Invalid or duplicate category key: {key!r}")
        folder = _clean_folder(str(c.get("folder", "")), "")
        if not folder:
            raise ValueError(f"Category {key!r}: folder is required")
        if folder.lower() in seen_folders:
            raise ValueError(f"Two categories share the folder {folder!r}")
        label = (str(c.get("label", "")).strip() or key.title())[:40]
        seen_keys.add(key)
        seen_folders.add(folder.lower())
        cleaned.append({"key": key, "label": label, "folder": folder})
    for key in BUILTIN_KEYS:
        if key not in seen_keys:
            raise ValueError(f"The built-in {key!r} category cannot be removed")
    set_setting("media_categories", json.dumps(cleaned))
    # keep the legacy per-folder settings in sync (older call sites read them)
    by_key = {c["key"]: c for c in cleaned}
    set_setting("movies_folder", by_key["movies"]["folder"])
    set_setting("shows_folder", by_key["shows"]["folder"])
    set_setting("anime_movies_folder", by_key["anime_movies"]["folder"])
    set_setting("anime_shows_folder", by_key["anime_shows"]["folder"])
    ensure_folders()
    return all_categories()


def ensure_folders() -> None:
    """Create every category's folder under the media root (backend startup +
    after wizard edits) — the container-side folder must exist even when the
    host has nothing mounted there yet."""
    for c in all_categories():
        try:
            (Path(settings.media_dir) / c["folder"]).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
