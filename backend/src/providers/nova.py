"""Nova stream provider.

Nova exposes one JSON source endpoint.  The app searches TMDB first; the
existing title records are still IMDB-keyed for compatibility, so this module
translates that identifier to the TMDB id immediately before calling Nova.

The endpoint returns several short-lived HLS URLs (often a mix of Orion and
Falcon edges).  They are returned in quality order.  The download queue then
tries every URL and validates decoded media before accepting the source.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional
from urllib.parse import urljoin

import httpx

from src.providers import tmdb
from src.providers.types import StreamSources, TitleUnavailable

_log = logging.getLogger("nova")

_BASE = "https://novahd.cc"
_SOURCES = f"{_BASE}/api/sources"
_REFERER = f"{_BASE}/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) "
    "Gecko/20100101 Firefox/155.0"
)


def _headers(tmdb_id: int, media_type: str, season: Optional[int], episode: Optional[int]) -> dict[str, str]:
    if media_type == "tvSeries":
        page = f"/watch/s/{tmdb_id}/{season or 0}/{episode or 0}"
    else:
        page = f"/watch/m/{tmdb_id}"
    return {
        "User-Agent": _USER_AGENT,
        # Match the request made by Nova's own player page. Some deployments
        # reject the API when it looks like a generic server-side JSON call.
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{_BASE}{page}",
        "Origin": _BASE,
        "x-nova-visitor": str(uuid.uuid4()),
        "DNT": "1",
        "Sec-GPC": "1",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Priority": "u=4",
    }


def _quality_score(source: dict) -> int:
    quality = str(source.get("quality") or "").lower()
    if quality == "auto":
        # Auto URLs in the Nova response are often master playlists. Keep
        # them beside explicit 1080p sources so a broken 1080 edge does not
        # cause the queue to settle for 720p before trying the master.
        return 1080
    digits = "".join(ch for ch in quality if ch.isdigit())
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _subtitle_tracks(raw: object) -> list[tuple[str, str]]:
    tracks: list[tuple[str, str]] = []
    if not isinstance(raw, list):
        return tracks
    for item in raw:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        url = urljoin(_BASE, str(item["url"]))
        label = str(item.get("label") or item.get("lang") or
                    item.get("language") or item.get("name") or "und")
        tracks.append((label, url))
    return tracks


async def resolve(
    imdb_id: str,
    media_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> StreamSources:
    """Resolve all Nova source URLs for a movie or TV episode."""
    # TMDB-backed search results already carry the numeric TMDB id. Older
    # library rows may still contain an IMDB id, which the client can map.
    tmdb_id = int(imdb_id) if str(imdb_id).isdigit() else await tmdb.imdb_to_tmdb(imdb_id, media_type)
    if tmdb_id is None:
        raise RuntimeError(
            f"Nova needs a TMDB id for {imdb_id}; configure a TMDB API token"
        )

    if media_type == "tvSeries" and (season is None or episode is None):
        raise ValueError("Nova TV resolution requires season and episode")

    params: dict[str, int | str] = {
        "type": "show" if media_type == "tvSeries" else "movie",
        "tmdbId": tmdb_id,
    }
    if media_type == "tvSeries":
        params.update(season=season or 0, episode=episode or 0)

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, verify=False) as client:
            payload = None
            for attempt in range(2):
                response = await client.get(
                    _SOURCES,
                    params=params,
                    headers=_headers(tmdb_id, media_type, season, episode),
                )
                if response.status_code == 404:
                    raise TitleUnavailable("Nova has no source for this title")
                if response.status_code == 403:
                    body = response.text[:500].lower()
                    challenged = any(token in body for token in ("just a moment", "cloudflare", "challenge"))
                    if challenged and attempt == 0:
                        # A profile rotation is useful only when OpenVPN is
                        # already active and another profile exists. It is
                        # deliberately one retry: repeated rotations per
                        # request would thrash the whole download queue.
                        try:
                            from src.vpn.openvpn_service import openvpn_service
                            vpn_status = await openvpn_service.status()
                            if vpn_status["enabled"] and vpn_status["running"] and len(vpn_status["profiles"]) > 1:
                                rotated = await openvpn_service.rotate("Nova returned a Cloudflare 403")
                                if rotated.ok:
                                    _log.warning("[nova] rotated OpenVPN profile after Cloudflare 403")
                                    continue
                        except Exception as rotate_exc:
                            _log.warning("[nova] OpenVPN failover unavailable: %s", rotate_exc)
                    if challenged:
                        raise RuntimeError(
                            "Nova is blocked by Cloudflare for this server IP; "
                            "the Nova API requires a browser challenge and cannot "
                            "be used through the HTTP-only provider"
                        )
                response.raise_for_status()
                payload = response.json()
                break
    except TitleUnavailable:
        raise
    except Exception as exc:
        raise RuntimeError(f"Nova source lookup failed: {exc}") from exc

    raw_sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(raw_sources, list):
        raise TitleUnavailable("Nova returned no sources")

    sources = [
        source for source in raw_sources
        if isinstance(source, dict)
        and isinstance(source.get("url"), str)
        and source["url"].startswith(("http://", "https://"))
    ]
    # Stable sort keeps Nova's edge order for equal/unknown quality values.
    sources.sort(key=_quality_score, reverse=True)
    urls = [str(source["url"]) for source in sources]
    if not urls:
        raise TitleUnavailable("Nova returned no usable stream URLs")

    subtitles = _subtitle_tracks(payload.get("subtitles") if isinstance(payload, dict) else None)
    _log.info("[nova] TMDB %s resolved %d source(s)", tmdb_id, len(urls))
    return StreamSources(urls=urls, subtitles=subtitles, referer=_REFERER)
