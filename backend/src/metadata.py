"""
Local TMDB metadata cache.

Title metadata (plot, genres, rating, poster/backdrop images) is fetched from
TMDB once per title and stored locally — the row in `title_metadata`, the
images in META_DIR — so the Watch page never re-hits TMDB for a title we
already know. The same cached data is written out for Jellyfin next to the
media files (enriched NFOs + poster.jpg/fanart.jpg).

Everything here is a no-op when no TMDB key is configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config import settings
from src.database import EpisodeMetadata, SessionLocal, TitleMetadata
from src.providers import meta, tmdb

_log = logging.getLogger("metadata")

_fetch_lock = asyncio.Lock()   # serialize fetches so parallel jobs don't double-fetch


def meta_dir() -> Path:
    d = Path(settings.meta_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _row_to_dict(m: TitleMetadata) -> dict:
    return {
        "imdb_id": m.imdb_id,
        "tmdb_id": m.tmdb_id,
        "media_type": m.media_type,
        "title": m.title,
        "year": m.year,
        "overview": m.overview or "",
        "tagline": m.tagline or "",
        "genres": json.loads(m.genres) if m.genres else [],
        "rating": m.rating,
        "runtime": m.runtime,
        "original_language": m.original_language,
        "original_title": m.original_title,
        "poster": m.poster_file,
        "backdrop": m.backdrop_file,
    }


def get_meta_map(imdb_ids: list[str]) -> dict[str, dict]:
    """Cached metadata for a set of titles — read-only, no network."""
    if not imdb_ids:
        return {}
    with SessionLocal() as db:
        rows = db.query(TitleMetadata).filter(TitleMetadata.imdb_id.in_(set(imdb_ids))).all()
        return {m.imdb_id: _row_to_dict(m) for m in rows}


def _episode_row_to_dict(m: EpisodeMetadata) -> dict:
    return {
        "season": m.season,
        "episode": m.episode,
        "title": m.title,
        "overview": m.overview or "",
        "air_date": m.air_date,
        "rating": m.rating,
        "runtime": m.runtime,
        "still": m.still_file,
    }


def get_episode_meta_map(imdb_id: str) -> dict[str, dict]:
    """Cached per-episode metadata for a show, keyed 'season:episode' —
    read-only, no network."""
    with SessionLocal() as db:
        rows = db.query(EpisodeMetadata).filter_by(imdb_id=imdb_id).all()
        return {f"{m.season}:{m.episode}": _episode_row_to_dict(m) for m in rows}


async def ensure_title(imdb_id: str, media_type: str, log=None) -> Optional[dict]:
    """Return cached metadata for a title, fetching + caching from TMDB on
    first sight. None when TMDB is unconfigured or the title is unmapped."""
    with SessionLocal() as db:
        row = db.query(TitleMetadata).filter_by(imdb_id=imdb_id).first()
        if row is not None:
            return _row_to_dict(row)

    if not tmdb.has_key():
        return None

    async with _fetch_lock:
        # another task may have fetched while we waited
        with SessionLocal() as db:
            row = db.query(TitleMetadata).filter_by(imdb_id=imdb_id).first()
            if row is not None:
                return _row_to_dict(row)

        tmdb_id = await tmdb.imdb_to_tmdb(imdb_id, media_type)
        if tmdb_id is None:
            return None
        details = await tmdb.title_details(tmdb_id, media_type)
        if details is None:
            return None

        poster_file = backdrop_file = None
        if details.get("poster_path"):
            name = f"{imdb_id}_poster.jpg"
            if await tmdb.download_image(details["poster_path"], meta_dir() / name, "w500"):
                poster_file = name
        if details.get("backdrop_path"):
            name = f"{imdb_id}_backdrop.jpg"
            if await tmdb.download_image(details["backdrop_path"], meta_dir() / name, "w1280"):
                backdrop_file = name

        with SessionLocal() as db:
            row = TitleMetadata(
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=details["title"],
                year=details["year"],
                overview=details["overview"],
                tagline=details["tagline"],
                genres=json.dumps(details["genres"]),
                rating=details["rating"],
                runtime=details["runtime"],
                original_language=details.get("original_language"),
                original_title=details.get("original_title"),
                poster_file=poster_file,
                backdrop_file=backdrop_file,
                fetched_at=datetime.utcnow(),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            out = _row_to_dict(row)

    if log:
        log(f"TMDB metadata cached: {out['title']} ({out['year']})")
    _log.info("[metadata] cached %s (%s)", imdb_id, out["title"])
    return out


_season_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _season_lock(imdb_id: str, season: int) -> asyncio.Lock:
    key = (imdb_id, season)
    lock = _season_locks.get(key)
    if lock is None:
        lock = _season_locks[key] = asyncio.Lock()
    return lock


async def ensure_season_metadata(imdb_id: str, season: int, log=None) -> dict[int, dict]:
    """Fetch + cache per-episode metadata for one season (one network call
    covers every episode in the season). Returns {episode_number: dict},
    already-cached episodes included. No-op (returns cached-only) when the
    metadata source has nothing richer than a bare title (e.g. IMDB mode).
    Locked per (imdb_id, season) so a download-completion hook and a bulk
    backfill racing on the same season can't double-insert."""
    async with _season_lock(imdb_id, season):
        with SessionLocal() as db:
            existing = {
                m.episode: m for m in
                db.query(EpisodeMetadata).filter_by(imdb_id=imdb_id, season=season).all()
            }

        try:
            info = await meta.get_episodes(imdb_id, season)
        except Exception as exc:
            _log.info("[metadata] episode fetch failed for %s S%s: %s", imdb_id, season, exc)
            return {ep: _episode_row_to_dict(m) for ep, m in existing.items()}

        # already cached, or nothing richer than a bare title to add
        to_add = [
            ep for ep in info.episodes
            if existing.get(ep.episode) is None and (ep.overview or ep.still_path or ep.air_date)
        ]

        # Fetch still images concurrently (bounded) — a season with 20+
        # episodes doing these one at a time was the actual hot loop behind
        # a big show's backfill taking minutes.
        img_sem = asyncio.Semaphore(6)

        async def fetch_still(ep) -> str | None:
            if not ep.still_path:
                return None
            async with img_sem:
                name = f"{imdb_id}_s{season}e{ep.episode}.jpg"
                ok = await tmdb.download_image(ep.still_path, meta_dir() / name, "w300")
                return name if ok else None

        stills = await asyncio.gather(*(fetch_still(ep) for ep in to_add))

        added = 0
        for ep, still_file in zip(to_add, stills):
            with SessionLocal() as db:
                new_row = EpisodeMetadata(
                    imdb_id=imdb_id, season=season, episode=ep.episode,
                    title=ep.title, overview=ep.overview, air_date=ep.air_date,
                    rating=ep.rating, runtime=ep.runtime, still_file=still_file,
                    fetched_at=datetime.utcnow(),
                )
                db.add(new_row)
                db.commit()
                db.refresh(new_row)
                existing[ep.episode] = new_row
            added += 1

        if added and log:
            log(f"Episode metadata cached: {added} episode(s) in season {season}")
        return {ep: _episode_row_to_dict(m) for ep, m in existing.items()}


# ── Jellyfin assets (NFO + folder art) ──────────────────────────────────────

def write_jellyfin_assets(job, dest: Path, meta: Optional[dict]) -> None:
    """Write the enriched NFO + poster.jpg/fanart.jpg for a finished file.
    Works with meta=None too (plain NFO, no art) so downloads never depend
    on TMDB being reachable."""
    from src.library.organizer import write_movie_nfo, write_episode_nfo

    if job.media_type == "movie":
        write_movie_nfo(dest, job.title, job.year, job.imdb_id, meta=meta)
        _copy_art(meta, dest.parent)
    else:
        write_episode_nfo(
            dest,
            series_title=job.title,
            year=job.year,
            imdb_id=job.imdb_id,
            season=job.season or 1,
            episode=job.episode or 1,
            episode_title=job.episode_title or f"Episode {job.episode}",
            meta=meta,
        )
        _copy_art(meta, dest.parent.parent)   # series root


def _copy_art(meta: Optional[dict], folder: Path) -> None:
    if not meta:
        return
    for key, name in (("poster", "poster.jpg"), ("backdrop", "fanart.jpg")):
        src_name = meta.get(key)
        if not src_name:
            continue
        src = meta_dir() / src_name
        target = folder / name
        if src.exists() and not target.exists():
            try:
                shutil.copyfile(src, target)
            except Exception as exc:
                _log.info("[metadata] art copy failed: %s", exc)
