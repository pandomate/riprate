"""
Wyzie Subs subtitle provider (https://sub.wyzie.io).

Searches subtitles by IMDB id (plus season/episode for TV) and downloads the
best English match. Requires a free API key (store.wyzie.io/redeem) — without
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
    return (get_setting("wyzie_api_key") or "").strip() or settings.wyzie_api_key


def is_enabled() -> bool:
    """Toggle in Settings (default on) — but only usable with a key."""
    from src.database import get_setting
    if (get_setting("wyzie_enabled") or "true").lower() == "false":
        return False
    return bool(effective_key())


async def search(
    imdb_id: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    language: str = "en",
) -> list[dict]:
    key = effective_key()
    if not key:
        return []
    subtitle_id = imdb_id
    if str(imdb_id).isdigit():
        # TMDB is the primary search id now, while Wyzie's API accepts IMDB.
        from src.providers import tmdb
        mapped = await tmdb.tmdb_to_imdb(
            int(imdb_id), "tvSeries" if season is not None else "movie"
        )
        if mapped:
            subtitle_id = mapped
    params: dict = {"id": subtitle_id, "language": language, "key": key}
    if season is not None and episode is not None:
        params["season"] = season
        params["episode"] = episode
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(f"{_BASE}/search", params=params, headers=_HEADERS)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Wyzie error: {str(data)[:160]}")
    return data


def _score(sub: dict) -> tuple:
    """Prefer regular (non-HI, non-AI) subs with the most downloads."""
    return (
        0 if not sub.get("isHearingImpaired") else 1,
        0 if not sub.get("ai") else 1,
        -(sub.get("downloadCount") or 0),
    )


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

    try:
        results = await search(imdb_id, season, episode, language)
    except Exception as exc:
        _log_msg(f"Wyzie search failed: {exc}")
        return None
    if not results:
        return None

    for sub in sorted(results, key=_score)[:4]:
        url = sub.get("url")
        if not url:
            continue
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": _HEADERS["User-Agent"]})
                resp.raise_for_status()
                text = resp.text
            if len(text.strip()) < 50:
                continue
            from src.subtitles import clean_vtt_text
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(clean_vtt_text(srt_to_vtt(text))[0], encoding="utf-8")
            _log_msg(f"Wyzie subtitles: {sub.get('display', language)}"
                     f" ({sub.get('release', '')[:40]})")
            return dest
        except Exception as exc:
            _log.info("[wyzie] candidate failed (%s); trying next", exc)
    return None
