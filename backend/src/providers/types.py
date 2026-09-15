from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StreamSources:
    """One or more stream URLs and optional subtitle tracks.

    Providers may return any number of URLs, but an empty video-source result
    is never valid. There is deliberately no upper bound here.
    """

    urls: list[str]
    subtitles: list[tuple[str, str]] = field(default_factory=list)
    referer: str = ""

    def __post_init__(self) -> None:
        self.urls = [url for url in self.urls if isinstance(url, str) and url.strip()]
        if not self.urls:
            raise ValueError("A provider must return at least one stream source")


class TitleUnavailable(Exception):
    """The provider has no source for this title or episode."""


@dataclass
class SearchResult:
    imdb_id: str
    title: str
    media_type: str          # "movie" | "tvSeries"
    year: Optional[int]
    poster_url: Optional[str]
    cast: str = ""
    rank: int = 0
    is_anime: bool = False
    original_language: Optional[str] = None


@dataclass
class EpisodeInfo:
    season: int
    episode: int
    title: str
    imdb_id: Optional[str] = None
    # Optional richer fields — populated by TMDB (one call per season already
    # returns these for every episode); left None for IMDB-sourced episodes.
    overview: Optional[str] = None
    air_date: Optional[str] = None
    rating: Optional[float] = None
    runtime: Optional[int] = None
    still_path: Optional[str] = None


@dataclass
class SeasonInfo:
    season_number: int
    episodes: list[EpisodeInfo] = field(default_factory=list)
