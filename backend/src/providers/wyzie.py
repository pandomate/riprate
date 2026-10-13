"""
Wyzie Subs subtitle provider (https://sub.wyzie.io).

Searches subtitles by IMDB or TMDB id (plus season/episode for TV) and downloads the
best match in the requested language. Requires a free API key (store.wyzie.io/redeem) — without
one, or with the provider toggled off in Settings, every call is a no-op.

Results are mostly SRT; we convert to WebVTT so the same file works both as a
browser <track> sidecar and as an ffmpeg subtitle input.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from src.config import settings

_log = logging.getLogger("wyzie")
_BASE = "https://sub.wyzie.io"
_HEADERS = {"User-Agent": "RipRate/1.0", "Accept": "application/json"}


def effective_key() -> str:
    """DB setting wins over the .env WYZIE_API_KEY."""
    from src.database import get_setting
    # An explicitly cleared DB key must not revive the startup environment key.
    return get_setting("wyzie_api_key", default=settings.wyzie_api_key).strip()


def preference_enabled() -> bool:
    """The saved switch, independent of whether a key is configured."""
    from src.database import get_setting
    return get_setting("wyzie_enabled").strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Wyzie is active only when the user enables it and configures a key."""
    return preference_enabled() and bool(effective_key())


async def search(
    imdb_id: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    language: str = "en",
) -> list[dict]:
    if not is_enabled():
        return []
    key = effective_key()
    # Wyzie accepts both ID types directly; no TMDB key or lookup is needed.
    params: dict = {"id": imdb_id, "language": language, "key": key}
    if season is not None and episode is not None:
        params["season"] = season
        params["episode"] = episode
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(f"{_BASE}/search", params=params, headers=_HEADERS)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # HTTPX includes the full request URL in its exception string;
            # that URL contains the private API key.
            raise RuntimeError(f"Wyzie search returned HTTP {exc.response.status_code}") from None
        data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("Unexpected Wyzie search response")
    return [sub for sub in data if isinstance(sub, dict)]


def _failure_reason(exc: Exception) -> str:
    # HTTP exception strings include request URLs, which can contain the API
    # key or a signed download token. Job logs are visible in the browser.
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


_SRT_TS = re.compile(
    r"(\d{2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}),(\d{3})")


def srt_to_vtt(text: str) -> str:
    if "WEBVTT" in text[:32].upper():
        return text
    body = _SRT_TS.sub(r"\1.\2 --> \3.\4", text)
    # Drop bare cue-number lines (valid VTT doesn't need them and some players choke)
    lines = [ln for ln in body.splitlines() if not ln.strip().isdigit()]
    return "WEBVTT\n\n" + "\n".join(lines).strip() + "\n"


async def download_subtitle(
    imdb_id: str,
    dest: Path,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    language: str = "en",
    log=None,
) -> Optional[Path]:
    """Search Wyzie and write the best subtitle as WebVTT to `dest`.
    Returns dest on success, None when nothing usable was found."""
    def _log_msg(m: str) -> None:
        _log.info("[wyzie] %s", m)
        if log:
            log(m)

    # Wyzie is authoritative whenever enabled. Its /download endpoint searches
    # and serves Wyzie's own best match in one operation. The stream provider
    # must never influence which Wyzie result is saved.
    params: dict = {
        "id": imdb_id, "language": language, "key": effective_key(), "to": "vtt",
    }
    if season is not None and episode is not None:
        params.update(season=season, episode=episode)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(f"{_BASE}/download", params=params, headers=_HEADERS)
            resp.raise_for_status()
            text = resp.text
        from src.subtitles import clean_vtt_text, has_timed_cues
        if not has_timed_cues(text):
            _log_msg("Wyzie best match had no timed cues")
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(clean_vtt_text(srt_to_vtt(text))[0], encoding="utf-8")
        release = resp.headers.get("X-Subtitle-Release", "")
        source = resp.headers.get("X-Subtitle-Source", "")
        detail = " · ".join(value for value in (release[:60], source) if value)
        _log_msg(f"Wyzie best match: {language}" + (f" ({detail})" if detail else ""))
        return dest
    except Exception as exc:
        _log_msg(f"Wyzie download failed: {_failure_reason(exc)}")
        return None
