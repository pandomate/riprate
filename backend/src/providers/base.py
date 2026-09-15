"""
Stream-provider registry.

Each provider lives in its own module and exposes an async ``resolve`` function.
Provider-specific URL parsing and headers stay in that module; the queue owns
download retries and playable-media validation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from src.providers.types import StreamSources, TitleUnavailable
from src.providers import nova

_log = logging.getLogger("providers")

ResolveFn = Callable[[str, str, Optional[int], Optional[int]], Awaitable[StreamSources]]


@dataclass
class Provider:
    key: str
    label: str
    resolve: ResolveFn
    # Name of the search/ID source that must be configured before this
    # provider can resolve a stream.  This is developer-owned provider
    # metadata, not a hard-coded setting in the queue.
    requires_source: str | None = None


# Every stream provider lives in a separate module. The ordered setting remains
# useful when another provider is added later.
_ALL: list[Provider] = [
    Provider("nova", "Nova", nova.resolve, requires_source="tmdb"),
]
_BY_KEY = {p.key: p for p in _ALL}

# Default config = every provider enabled, in _ALL order.
DEFAULT_CONFIG = [{"key": p.key, "enabled": True} for p in _ALL]


def all_provider_meta() -> list[dict]:
    return [{"key": p.key, "label": p.label, "requires_source": p.requires_source}
            for p in _ALL]


def _load_config() -> list[dict]:
    """Read the saved provider config; tolerate missing/garbled settings and
    always include every known provider (new ones appended, enabled)."""
    from src.database import get_setting
    raw = get_setting("stream_providers")
    saved: list[dict] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    key = item.get("key")
                    if key in _BY_KEY and not any(s["key"] == key for s in saved):
                        saved.append({"key": key, "enabled": bool(item.get("enabled", True))})
        except Exception:
            saved = []
    seen = {s["key"] for s in saved}
    for p in _ALL:
        if p.key not in seen:
            saved.append({"key": p.key, "enabled": True})
    normalized = saved or list(DEFAULT_CONFIG)
    # A download must always have at least one provider to try. This only
    # repairs an already-saved all-off configuration from before the API
    # enforced the invariant; normal settings updates reject all-off lists.
    if not any(c.get("enabled", True) for c in normalized):
        normalized[0]["enabled"] = True
        _log.warning("All stream providers were disabled; re-enabled %s", normalized[0]["key"])

    # Reconcile the persisted setting with the code registry. This removes
    # deleted providers (including VidSrc/VidKing/Videasy) from the DB instead
    # of merely hiding them in the UI, while automatically adding new provider
    # modules in their declared order.
    if raw != json.dumps(normalized, separators=(",", ":")):
        try:
            from src.database import set_setting
            set_setting("stream_providers", json.dumps(normalized, separators=(",", ":")))
        except Exception as exc:
            _log.debug("Could not persist provider registry reconciliation: %s", exc)
    return normalized


def configured_providers() -> list[dict]:
    """Provider config for the API/UI: [{key,label,enabled}, …] in order."""
    from src.providers import tmdb
    return [
        {
            **c,
            "label": _BY_KEY[c["key"]].label,
            "requires_source": _BY_KEY[c["key"]].requires_source,
            "available": (
                _BY_KEY[c["key"]].requires_source != "tmdb" or tmdb.has_key()
            ),
        }
        for c in _load_config()
    ]


def _enabled_providers() -> list[Provider]:
    return [_BY_KEY[c["key"]] for c in _load_config() if c.get("enabled", True)]


def enabled_provider_meta() -> list[dict]:
    """[{key,label}] of enabled providers in the configured try-order — the
    manager iterates these itself so a provider whose stream fails to
    DOWNLOAD (not just to resolve) still falls through to the next one."""
    return [{"key": p.key, "label": p.label} for p in _enabled_providers()]


async def resolve_one(
    key: str,
    imdb_id: str,
    media_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> StreamSources:
    """Resolve via ONE provider (with the same per-episode TMDB-numbering
    retry the registry loop does). Raises RuntimeError on failure."""
    p = _BY_KEY.get(key)
    if p is None:
        raise RuntimeError(f"Unknown provider {key!r}")
    errors: list[str] = []
    src = await _try_providers([p], imdb_id, media_type, season, episode,
                               errors, None, log)
    if src is not None:
        return src
    if media_type == "tvSeries" and season is not None and episode is not None:
        from src.providers import tmdb
        try:
            mapped = await tmdb.map_episode(imdb_id, season, episode)
        except Exception:
            mapped = None
        if mapped and mapped != (season, episode):
            m_s, m_e = mapped
            if log:
                log(f"Retrying {p.label} with TMDB numbering S{m_s:02d}E{m_e:02d}")
            src = await _try_providers([p], imdb_id, media_type, m_s, m_e,
                                       errors, None, log)
            if src is not None:
                return src
    raise RuntimeError(errors[0] if errors else f"{p.label}: no stream")


async def _try_providers(
    providers: list[Provider],
    imdb_id: str,
    media_type: str,
    season: Optional[int],
    episode: Optional[int],
    errors: list[str],
    on_attempt: Optional[Callable[[str], None]],
    log: Optional[Callable[[str], None]],
) -> Optional[StreamSources]:
    for p in providers:
        if on_attempt:
            on_attempt(p.label)
        if log:
            log(f"Trying {p.label}…")
        try:
            if p.requires_source == "tmdb":
                from src.providers import tmdb
                if not tmdb.has_key():
                    raise RuntimeError(f"{p.label} requires a TMDB API key")
            src = await p.resolve(imdb_id, media_type, season, episode)
        except TitleUnavailable as exc:
            errors.append(f"{p.label}: not available")
            if log:
                log(f"{p.label}: title not available")
            continue
        except Exception as exc:
            errors.append(f"{p.label}: {exc}")
            if log:
                log(f"{p.label} failed: {exc}")
            continue
        if src and src.urls:
            if log:
                log(f"{p.label} resolved {len(src.urls)} stream(s)"
                    + (f", {len(src.subtitles)} subtitle track(s)" if src.subtitles else ""))
            return src
        errors.append(f"{p.label}: no stream returned")
    return None


async def resolve_with_registry(
    imdb_id: str,
    media_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    *,
    on_attempt: Optional[Callable[[str], None]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> StreamSources:
    """Try each enabled provider in order; return the first resolved result."""
    providers = _enabled_providers()
    if not providers:
        raise RuntimeError("No stream providers are enabled — enable one in Settings.")

    errors: list[str] = []
    src = await _try_providers(providers, imdb_id, media_type, season, episode,
                               errors, on_attempt, log)
    if src is not None:
        return src

    # Older IMDB-keyed records can number an episode differently from TMDB.
    # Translate that one episode and retry once; numeric TMDB ids skip this
    # path because they are already in Nova's numbering system.
    if media_type == "tvSeries" and season is not None and episode is not None:
        from src.providers import tmdb
        try:
            mapped = await tmdb.map_episode(imdb_id, season, episode)
        except Exception:
            mapped = None
        if mapped and mapped != (season, episode):
            m_season, m_episode = mapped
            if log:
                log(f"IMDB S{season:02d}E{episode:02d} is TMDB "
                    f"S{m_season:02d}E{m_episode:02d} — retrying providers with TMDB numbering")
            src = await _try_providers(providers, imdb_id, media_type,
                                       m_season, m_episode, errors, on_attempt, log)
            if src is not None:
                return src

    raise RuntimeError("No provider could resolve a stream. " + " | ".join(errors[:6]))


async def resolve_stream_urls(
    imdb_id: str,
    media_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> StreamSources:
    """Compatibility helper for background subtitle/metadata jobs."""
    return await resolve_with_registry(imdb_id, media_type, season, episode)
