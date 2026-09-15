"""
IMDB provider.

Search:    IMDB suggestion API.
Seasons/Episodes: IMDB episodes page with __NEXT_DATA__ JSON.

If IMDB_COOKIE is set in .env or Settings, that value is sent with requests.
This provider never launches a browser or performs browser-based challenge
solving.

Parsed __NEXT_DATA__ structure (confirmed against live IMDB):
  props.pageProps.contentData.section
    .seasons  → [{"id": "...", "value": "1"}, {"id": "...", "value": "2"}, ...]
    .episodes → {"items": [{season, episode, titleText, ...}],
                 "total": N, "hasNextPage": bool, "endCursor": "..."}
    .currentSeason → int (which season's episodes are loaded)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import httpx

from src.config import settings
from src.providers.types import EpisodeInfo, SearchResult, SeasonInfo

_log = logging.getLogger("imdb")

_SUGGESTION_BASE = "https://v3.sg.media-imdb.com/suggestion/x/{query}.json?includeVideos=1"
_EPISODES_BASE = "https://www.imdb.com/title/{imdb_id}/episodes/"

# Headers matching the working browser fingerprint
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
        "Gecko/20100101 Firefox/152.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "DNT": "1",
    "Sec-GPC": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

_WAF_TTL = timedelta(hours=4)


def _manual_cookie() -> str:
    """Admin-provided cookie (Settings), if set and not older than the WAF TTL."""
    try:
        from src.database import get_setting
        val = (get_setting("imdb_cookie") or "").strip()
        if not val:
            return ""
        set_at = (get_setting("imdb_cookie_set_at") or "").strip()
        if set_at:
            try:
                if datetime.utcnow() - datetime.fromisoformat(set_at) > _WAF_TTL:
                    return ""   # expired — do not send the stale cookie
            except ValueError:
                pass
        return val
    except Exception:
        return ""


def _effective_cookie() -> str:
    """IMDB cookie to use, in priority order:
    1. Admin cookie from Settings (unless expired >4h)
    2. IMDB_COOKIE from env / .env (manual override)
    """
    return _manual_cookie() or settings.imdb_cookie


def _imdb_headers(referer: str = "https://www.imdb.com/") -> dict:
    h = dict(_BROWSER_HEADERS)
    h["Referer"] = referer
    cookie = _effective_cookie()
    if cookie:
        h["Cookie"] = cookie
    return h


# ── Search ─────────────────────────────────────────────────────────────────

async def search(query: str) -> list[SearchResult]:
    encoded = quote(query)
    url = _SUGGESTION_BASE.format(query=encoded)
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        resp = await client.get(url, headers=_BROWSER_HEADERS)
        resp.raise_for_status()

    data = resp.json()
    results: list[SearchResult] = []
    for item in data.get("d", []):
        qid = item.get("qid", "")
        if qid not in ("movie", "tvSeries", "tvMiniSeries"):
            continue
        media_type = "tvSeries" if qid in ("tvSeries", "tvMiniSeries") else "movie"
        results.append(
            SearchResult(
                imdb_id=item["id"],
                title=item.get("l", ""),
                media_type=media_type,
                year=item.get("y"),
                poster_url=item.get("i", {}).get("imageUrl"),
                cast=item.get("s", ""),
                rank=item.get("rank", 0),
                is_anime=False,
            )
        )
    return results


# ── Seasons ────────────────────────────────────────────────────────────────

# Short-lived caches so re-opening / switching seasons is instant (IMDB
# scraping is the slow part; results don't change within a browsing session).
import time as _time

_SECTION_TTL = 900  # 15 minutes
_seasons_cache: dict[str, tuple[float, list[int]]] = {}
_episodes_cache: dict[tuple[str, int], tuple[float, "SeasonInfo"]] = {}


async def get_seasons(imdb_id: str) -> list[int]:
    """Return sorted season numbers. One HTTP request (default page has all seasons)."""
    hit = _seasons_cache.get(imdb_id)
    if hit and _time.monotonic() - hit[0] < _SECTION_TTL:
        return hit[1]
    url = f"{_EPISODES_BASE.format(imdb_id=imdb_id)}?ref_=tt_epspo_eps"
    section = await _fetch_section(imdb_id, url)
    seasons = _parse_seasons(section)
    _seasons_cache[imdb_id] = (_time.monotonic(), seasons)
    return seasons


# ── Episodes ───────────────────────────────────────────────────────────────

async def get_episodes(imdb_id: str, season: int) -> SeasonInfo:
    """Return all episodes for the given season."""
    key = (imdb_id, season)
    hit = _episodes_cache.get(key)
    if hit and _time.monotonic() - hit[0] < _SECTION_TTL:
        return hit[1]
    url = (
        f"{_EPISODES_BASE.format(imdb_id=imdb_id)}"
        f"?season={season}&ref_=tt_epspo_eps"
    )
    referer = f"https://www.imdb.com/title/{imdb_id}/episodes/?ref_=tt_epspo_eps"
    section = await _fetch_section(imdb_id, url, referer=referer)
    episodes = _parse_episodes(section, season)
    if not episodes:
        raise RuntimeError(f"No episodes found for season {season}.")
    info = SeasonInfo(season_number=season, episodes=episodes)
    _episodes_cache[key] = (_time.monotonic(), info)
    return info


# ── Internal helpers ───────────────────────────────────────────────────────

async def _fetch_section(
    imdb_id: str, url: str, referer: str | None = None
) -> dict:
    """
    Fetch the IMDB episodes page and return contentData.section from
    __NEXT_DATA__. Raises RuntimeError if IMDB blocks or the page cannot be
    parsed. Browser-based challenge solving is intentionally unsupported.
    """
    if referer is None:
        referer = f"https://www.imdb.com/title/{imdb_id}/?ref_=fn_t_1"

    html = await _get_html(url, referer)
    section = _extract_section(html)

    if section is None:
        detail = (
            "IMDB blocked this request or returned an unexpected page. "
            "If using IMDB search, configure a current Cookie header in Settings."
        )
        raise RuntimeError(detail)

    return section


async def _get_html(url: str, referer: str) -> str:
    headers = _imdb_headers(referer=referer)
    async with httpx.AsyncClient(
        verify=False, timeout=25, follow_redirects=True
    ) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
    return resp.text


def _extract_section(html: str) -> Optional[dict]:
    """Parse __NEXT_DATA__ → contentData.section, or None if blocked."""
    if "awsWafCookieDomainList" in html or "challenge.js" in html:
        return None

    m = re.search(
        r'<script\s+id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data["props"]["pageProps"]["contentData"]["section"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _parse_seasons(section: dict) -> list[int]:
    """
    section.seasons = [{"id": "...", "value": "1"}, ...]
    Extract and return sorted int season numbers.
    """
    raw = section.get("seasons", [])
    seasons: list[int] = []
    for item in raw:
        val = item.get("value", "") if isinstance(item, dict) else str(item)
        if str(val).isdigit():
            seasons.append(int(val))
    return sorted(set(seasons))


def _parse_episodes(section: dict, season: int) -> list[EpisodeInfo]:
    """
    section.episodes.items = [
      {"season": "1", "episode": "1", "titleText": "Pilot", ...},
      ...
    ]
    """
    eps_block = section.get("episodes", {})
    items = eps_block.get("items", []) if isinstance(eps_block, dict) else []

    episodes: list[EpisodeInfo] = []
    for item in items:
        ep_num = item.get("episode")
        title = item.get("titleText") or f"Episode {ep_num}"
        if ep_num is None:
            continue
        try:
            ep_int = int(ep_num)
        except (ValueError, TypeError):
            continue
        episodes.append(EpisodeInfo(season=season, episode=ep_int, title=str(title),
                                    imdb_id=item.get("id")))

    return sorted(episodes, key=lambda e: e.episode)
