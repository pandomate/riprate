"""TMDB client: search / seasons / episodes and title details.

Uses the v4 bearer token (Authorization: Bearer <token>). The key comes from
the `tmdb_api_key` DB setting (editable in Settings) or the TMDB_API_KEY env
var — DB wins so a key pasted in the UI takes effect without a restart.

TMDB ids are the primary search/title ids. IMDB ids remain supported for older
records and the optional IMDB metadata fallback.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.config import settings
from src.providers.types import EpisodeInfo, SearchResult, SeasonInfo

_log = logging.getLogger("tmdb")
_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p"

# title id → tmdb_id, kept for the process lifetime (ids never change). New
# TMDB search results already use numeric TMDB ids; IMDB ids remain supported
# for older library rows and the optional IMDB metadata fallback.
_id_cache: dict[str, Optional[int]] = {}
# tmdb "{type}:{id}" → imdb_id (for search results)
_ext_cache: dict[str, Optional[str]] = {}


def effective_key() -> str:
    """DB setting wins over the .env TMDB_API_KEY."""
    try:
        from src.database import get_setting
        db_key = (get_setting("tmdb_api_key") or "").strip()
    except Exception:
        db_key = ""
    return db_key or settings.tmdb_api_key


def has_key() -> bool:
    return bool(effective_key())


def _headers() -> dict:
    return {"Authorization": f"Bearer {effective_key()}", "accept": "application/json"}


def _is_anime_result(raw: dict) -> bool:
    """TMDB has no Anime media type; Animation is its stable classification.

    Do not infer Anime from a particular spoken language or country. Anime can
    have any original language, while TMDB's Animation genre is available for
    both movies and TV search results.
    """
    genres = {int(g) for g in (raw.get("genre_ids") or []) if str(g).isdigit()}
    return 16 in genres


async def imdb_to_tmdb(imdb_id: str, media_type: str) -> Optional[int]:
    """Return a TMDB id for a TMDB or IMDB title id."""
    if str(imdb_id).isdigit():
        return int(imdb_id)
    if not has_key():
        return None
    cache_key = f"{media_type}:{imdb_id}"
    if cache_key in _id_cache:
        return _id_cache[cache_key]

    result: Optional[int] = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/find/{imdb_id}",
                params={"external_source": "imdb_id"},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        bucket = "tv_results" if media_type == "tvSeries" else "movie_results"
        results = data.get(bucket) or []
        if results:
            result = int(results[0]["id"])
    except Exception as exc:
        _log.info("[tmdb] find %s failed: %s", imdb_id, exc)
        return None   # don't cache transient failures

    _id_cache[cache_key] = result
    return result


async def _tmdb_to_imdb(client: httpx.AsyncClient, tmdb_type: str, tmdb_id: int) -> Optional[str]:
    """tmdb_type is TMDB's 'movie' | 'tv'."""
    key = f"{tmdb_type}:{tmdb_id}"
    if key in _ext_cache:
        return _ext_cache[key]
    try:
        resp = await client.get(f"{_BASE}/{tmdb_type}/{tmdb_id}/external_ids", headers=_headers())
        resp.raise_for_status()
        imdb_id = resp.json().get("imdb_id") or None
    except Exception as exc:
        _log.info("[tmdb] external_ids %s/%s failed: %s", tmdb_type, tmdb_id, exc)
        return None   # transient — don't cache
    _ext_cache[key] = imdb_id
    if imdb_id:
        _id_cache[f"{'tvSeries' if tmdb_type == 'tv' else 'movie'}:{imdb_id}"] = tmdb_id
    return imdb_id


async def tmdb_to_imdb(tmdb_id: int, media_type: str) -> Optional[str]:
    """Return the external IMDB id for a TMDB title when one exists."""
    if not has_key():
        return None
    tmdb_type = "tv" if media_type == "tvSeries" else "movie"
    async with httpx.AsyncClient(timeout=15) as client:
        return await _tmdb_to_imdb(client, tmdb_type, int(tmdb_id))


# ── Metadata source: search / seasons / episodes ────────────────────────────

async def search(query: str) -> list[SearchResult]:
    """TMDB multi-search returning the TMDB id directly in each result."""
    if not has_key():
        raise RuntimeError("TMDB search needs an API key (Settings).")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{_BASE}/search/multi",
            params={"query": query, "include_adult": "false", "page": 1},
            headers=_headers(),
        )
        resp.raise_for_status()
        raw = [
            r for r in (resp.json().get("results") or [])
            if r.get("media_type") in ("movie", "tv")
        ][:10]
    results: list[SearchResult] = []
    for rank, r in enumerate(raw):
        is_tv = r["media_type"] == "tv"
        date = (r.get("first_air_date") if is_tv else r.get("release_date")) or ""
        poster = r.get("poster_path")
        results.append(SearchResult(
            # The app's existing identifier field is kept for compatibility;
            # TMDB-backed searches now put the TMDB id here directly so Nova
            # does not need a second IMDB lookup.
            imdb_id=str(r["id"]),
            title=(r.get("name") if is_tv else r.get("title")) or "?",
            media_type="tvSeries" if is_tv else "movie",
            year=int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
            poster_url=f"{_IMG_BASE}/w185{poster}" if poster else None,
            cast=(r.get("overview") or "")[:140],
            rank=rank,
            is_anime=_is_anime_result(r),
            original_language=r.get("original_language"),
        ))
    return results


async def _show_tmdb_id(imdb_id: str) -> int:
    tmdb_id = await imdb_to_tmdb(imdb_id, "tvSeries")
    if tmdb_id is None:
        raise RuntimeError(f"TMDB doesn't know show {imdb_id}")
    return tmdb_id


async def get_seasons(imdb_id: str) -> list[int]:
    """Season numbers per TMDB (includes 0 = Specials when it has episodes)."""
    tmdb_id = await _show_tmdb_id(imdb_id)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{_BASE}/tv/{tmdb_id}", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    seasons = sorted(
        s["season_number"] for s in (data.get("seasons") or [])
        if s.get("season_number") is not None and (s.get("episode_count") or 0) > 0
    )
    if not seasons:
        raise RuntimeError(f"TMDB lists no seasons for {imdb_id}")
    return seasons


async def get_episodes(imdb_id: str, season: int) -> SeasonInfo:
    """Episodes for a season, TMDB-numbered (matches every stream provider)."""
    tmdb_id = await _show_tmdb_id(imdb_id)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{_BASE}/tv/{tmdb_id}/season/{season}", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    episodes = [
        EpisodeInfo(
            season=season,
            episode=int(e["episode_number"]),
            title=e.get("name") or f"Episode {e.get('episode_number')}",
            overview=e.get("overview") or None,
            air_date=e.get("air_date") or None,
            rating=round(float(e["vote_average"]), 1) if e.get("vote_average") else None,
            runtime=e.get("runtime"),
            still_path=e.get("still_path"),
        )
        for e in (data.get("episodes") or [])
        if e.get("episode_number") is not None
    ]
    if not episodes:
        raise RuntimeError(f"No episodes found for season {season}.")
    return SeasonInfo(season_number=season,
                      episodes=sorted(episodes, key=lambda e: e.episode))


# (show_imdb, season, episode) → (tmdb_season, tmdb_episode) or None (unmapped)
_ep_map_cache: dict[tuple[str, int, int], Optional[tuple[int, int]]] = {}


async def map_episode(show_imdb_id: str, season: int, episode: int) -> Optional[tuple[int, int]]:
    """Translate IMDB episode numbering to TMDB's.

    Some shows are split differently by the two databases (e.g. Family Guy —
    a few IMDB S22 episodes are TMDB *Season 0 specials*), and every stream
    provider we scrape follows TMDB numbering. We look up the episode's own
    IMDB id (from the IMDB episodes page) and ask TMDB /find where it lives.
    Returns None when the episode can't be mapped (or no key)."""
    if not has_key():
        return None
    if str(show_imdb_id).isdigit():
        # TMDB numbering is already what the Nova endpoint expects.
        return None
    cache_key = (show_imdb_id, season, episode)
    if cache_key in _ep_map_cache:
        return _ep_map_cache[cache_key]

    try:
        from src.providers import imdb
        info = await imdb.get_episodes(show_imdb_id, season)
        ep_imdb = next((e.imdb_id for e in info.episodes
                        if e.episode == episode and e.imdb_id), None)
    except Exception as exc:
        _log.info("[tmdb] episode-id lookup failed for %s S%sE%s: %s",
                  show_imdb_id, season, episode, exc)
        return None   # transient — don't cache
    if not ep_imdb:
        _ep_map_cache[cache_key] = None
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/find/{ep_imdb}",
                params={"external_source": "imdb_id"},
                headers=_headers(),
            )
            resp.raise_for_status()
            results = resp.json().get("tv_episode_results") or []
    except Exception as exc:
        _log.info("[tmdb] find episode %s failed: %s", ep_imdb, exc)
        return None   # transient — don't cache

    mapped: Optional[tuple[int, int]] = None
    if results:
        r = results[0]
        try:
            mapped = (int(r["season_number"]), int(r["episode_number"]))
        except (KeyError, TypeError, ValueError):
            mapped = None
    _ep_map_cache[cache_key] = mapped
    if mapped and mapped != (season, episode):
        _log.info("[tmdb] %s S%02dE%02d maps to TMDB S%02dE%02d",
                  show_imdb_id, season, episode, mapped[0], mapped[1])
    return mapped


async def episode_exists(tmdb_id: int, season: int, episode: int) -> Optional[bool]:
    """True/False if TMDB knows the episode; None when we can't tell
    (no key / network trouble) so callers don't fail hard on doubt."""
    if not has_key():
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_BASE}/tv/{tmdb_id}/season/{season}/episode/{episode}",
                headers=_headers(),
            )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True
    except Exception as exc:
        _log.info("[tmdb] episode check failed: %s", exc)
        return None


async def title_details(tmdb_id: int, media_type: str) -> Optional[dict]:
    """Normalized title details for the metadata cache. None on failure."""
    if not has_key():
        return None
    path = f"/tv/{tmdb_id}" if media_type == "tvSeries" else f"/movie/{tmdb_id}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_BASE}{path}", headers=_headers())
            resp.raise_for_status()
            d = resp.json()
    except Exception as exc:
        _log.info("[tmdb] details %s failed: %s", tmdb_id, exc)
        return None

    if media_type == "tvSeries":
        title = d.get("name")
        date = d.get("first_air_date") or ""
        runtimes = d.get("episode_run_time") or []
        runtime = runtimes[0] if runtimes else None
    else:
        title = d.get("title")
        date = d.get("release_date") or ""
        runtime = d.get("runtime")

    return {
        "tmdb_id": tmdb_id,
        "title": title,
        "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
        "overview": d.get("overview") or "",
        "tagline": d.get("tagline") or "",
        "genres": [g["name"] for g in (d.get("genres") or []) if g.get("name")],
        "rating": round(float(d.get("vote_average") or 0), 1) or None,
        "runtime": runtime,
        "poster_path": d.get("poster_path"),
        "backdrop_path": d.get("backdrop_path"),
        "original_language": d.get("original_language") or None,
        "original_title": (d.get("original_name") if media_type == "tvSeries"
                            else d.get("original_title")) or None,
        "is_anime": (
            16 in {int(g.get("id")) for g in (d.get("genres") or [])
                   if str(g.get("id", "")).isdigit()}
        ),
    }


async def download_image(tmdb_path: str, dest, size: str = "w500") -> bool:
    """Fetch an image from TMDB's CDN to `dest` (Path). True on success."""
    url = f"{_IMG_BASE}/{size}{tmdb_path}"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    except Exception as exc:
        _log.info("[tmdb] image %s failed: %s", tmdb_path, exc)
        return False
