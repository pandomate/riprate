"""
Metadata-source dispatcher: search / seasons / episodes.

Which database answers search/seasons/episodes is the admin's choice
(`search_source` setting): `tmdb` (the default) or `imdb`.

TMDB is preferred because every stream provider follows TMDB numbering
(IMDB-numbered episodes sometimes don't exist there — see tmdb.map_episode)
and because IMDB's WAF makes scraping slow/flaky. Results are imdb_id-keyed
regardless of source, so jobs/requests/library never care which one answered.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from src.providers import imdb, tmdb
from src.providers.types import SearchResult, SeasonInfo

_log = logging.getLogger("meta")

MODES = ("tmdb", "imdb")


def current_mode() -> str:
    from src.database import get_setting
    mode = (get_setting("search_source") or "").strip().lower()
    return mode if mode in MODES else "tmdb"


async def _dispatch(name: str,
                    tmdb_fn: Callable[..., Awaitable],
                    imdb_fn: Callable[..., Awaitable],
                    *args):
    mode = current_mode()
    if mode == "imdb":
        return await imdb_fn(*args)
    if mode == "tmdb":
        if not tmdb.has_key():
            raise RuntimeError(
                "Search source is TMDB, but no TMDB API key is configured — "
                "add one in Settings or select IMDB.")
        return await tmdb_fn(*args)
    return await imdb_fn(*args)


async def search(query: str) -> list[SearchResult]:
    return await _dispatch("search", tmdb.search, imdb.search, query)


async def get_seasons(imdb_id: str) -> list[int]:
    return await _dispatch("get_seasons", tmdb.get_seasons, imdb.get_seasons, imdb_id)


async def get_episodes(imdb_id: str, season: int) -> SeasonInfo:
    return await _dispatch("get_episodes", tmdb.get_episodes, imdb.get_episodes,
                           imdb_id, season)
