"""Direct download endpoints — admin only. Normal users go through /api/requests."""
import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.api.deps import require_admin
from src.database import DownloadJob, JobStatus, get_db
from src.queue.manager import download_manager

_log = logging.getLogger("download")

router = APIRouter(tags=["download"])

_TITLE_ID_RE = re.compile(r"^(?:\d{1,12}|tt\d{6,12})$")


class DownloadRequest(BaseModel):
    imdb_id: str = Field(max_length=16)
    title: str = Field(min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1870, le=2100)
    media_type: str                  # "movie" | "tvSeries"
    category: Optional[str] = Field(default=None, max_length=32)   # None = auto
    is_anime: Optional[bool] = None

    # TV only — omit for movies
    season: Optional[int] = Field(default=None, ge=0, le=200)
    episode: Optional[int] = Field(default=None, ge=0, le=2000)
    episode_title: Optional[str] = Field(default=None, max_length=300)


class BulkDownloadRequest(BaseModel):
    imdb_id: str = Field(max_length=16)
    title: str = Field(min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1870, le=2100)
    category: Optional[str] = Field(default=None, max_length=32)
    is_anime: Optional[bool] = None
    episodes: list[dict]             # [{"season": 1, "episode": 1, "title": "..."}]


class SeasonsDownloadRequest(BaseModel):
    imdb_id: str = Field(max_length=16)
    title: str = Field(min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1870, le=2100)
    category: Optional[str] = Field(default=None, max_length=32)
    is_anime: Optional[bool] = None
    seasons: list[int]               # e.g. [1, 2, 3]


_ACTIVE_OR_DONE = [
    JobStatus.queued, JobStatus.downloading, JobStatus.processing,
    JobStatus.paused, JobStatus.completed,
]


def _validate(imdb_id: str, media_type: str = "movie") -> None:
    if not _TITLE_ID_RE.match(imdb_id):
        raise HTTPException(status_code=400, detail="Invalid TMDB title id")
    if media_type not in ("movie", "tvSeries"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tvSeries'")


def _existing_job(db: Session, imdb_id: str, media_type: str,
                  season: int | None = None, episode: int | None = None) -> DownloadJob | None:
    """Return a completed/active job for this exact item, if one exists."""
    q = db.query(DownloadJob).filter(
        DownloadJob.imdb_id == imdb_id,
        DownloadJob.media_type == media_type,
        DownloadJob.status.in_(_ACTIVE_OR_DONE),
    )
    if media_type == "tvSeries":
        q = q.filter(DownloadJob.season == season, DownloadJob.episode == episode)
    return q.first()


@router.post("/download")
async def enqueue_download(
    body: DownloadRequest,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    _validate(body.imdb_id, body.media_type)
    existing = _existing_job(db, body.imdb_id, body.media_type, body.season, body.episode)
    if existing:
        reason = ("already in your library" if existing.status == JobStatus.completed
                  else "already in the download queue")
        return {"job_id": existing.id, "status": existing.status,
                "skipped": True, "reason": reason}
    job = create_job(db, body.dict(), requested_by=admin.id)
    await download_manager.enqueue(job.id)
    return {"job_id": job.id, "status": job.status, "skipped": False}


@router.post("/download/bulk")
async def enqueue_bulk(
    body: BulkDownloadRequest,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    _validate(body.imdb_id, "tvSeries")
    job_ids: list[int] = []
    skipped = 0
    for ep in body.episodes[:500]:
        season, episode = ep.get("season"), ep.get("episode")
        if _existing_job(db, body.imdb_id, "tvSeries", season, episode):
            skipped += 1
            continue
        job = create_job(db, {
            "imdb_id": body.imdb_id,
            "title": body.title,
            "year": body.year,
            "media_type": "tvSeries",
            "category": body.category,
            "is_anime": body.is_anime,
            "season": season,
            "episode": episode,
            "episode_title": ep.get("title"),
        }, requested_by=admin.id)
        await download_manager.enqueue(job.id)
        job_ids.append(job.id)
    return {"queued": len(job_ids), "skipped": skipped, "job_ids": job_ids}


@router.post("/download/seasons")
async def enqueue_seasons(
    body: SeasonsDownloadRequest,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    """Fetch episodes for each requested season then bulk-queue them."""
    from src.providers import meta

    _validate(body.imdb_id, "tvSeries")
    if not body.seasons:
        raise HTTPException(status_code=400, detail="No seasons specified.")

    tasks = [meta.get_episodes(body.imdb_id, s) for s in body.seasons]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    job_ids: list[int] = []
    errors: list[str] = []
    skipped = 0
    for season, result in zip(body.seasons, results):
        if isinstance(result, BaseException):
            _log.warning("enqueue_seasons %s s%s: %s", body.imdb_id, season, result)
            errors.append(f"Season {season}: {result}")
            continue
        for ep in result.episodes:
            if _existing_job(db, body.imdb_id, "tvSeries", season, ep.episode):
                skipped += 1
                continue
            job = create_job(db, {
                "imdb_id": body.imdb_id,
                "title": body.title,
                "year": body.year,
                "media_type": "tvSeries",
                "category": body.category,
                "is_anime": body.is_anime,
                "season": season,
                "episode": ep.episode,
                "episode_title": ep.title,
            }, requested_by=admin.id)
            await download_manager.enqueue(job.id)
            job_ids.append(job.id)

    if not job_ids and errors:
        raise HTTPException(status_code=502, detail="; ".join(errors))

    return {"queued": len(job_ids), "skipped": skipped, "errors": errors}


def _job_category(db: Session, data: dict) -> str:
    """Category for a new job: explicit choice > the title's last-used
    category (a show categorized as Anime once stays Anime — schedules and
    request approvals inherit it automatically) > the media-type default."""
    from src.library.categories import resolve_key
    explicit = data.get("category")
    if explicit:
        return resolve_key(data["media_type"], explicit, bool(data.get("is_anime")))
    prev = (
        db.query(DownloadJob)
        .filter(DownloadJob.imdb_id == data["imdb_id"],
                DownloadJob.category.isnot(None))
        .order_by(DownloadJob.id.desc())
        .first()
    )
    return resolve_key(data["media_type"], prev.category if prev else None,
                       bool(data.get("is_anime")))


def create_job(
    db: Session,
    data: dict,
    request_id: int | None = None,
    requested_by: int | None = None,
    source: str = "manual",
) -> DownloadJob:
    next_pos = (db.query(func.max(DownloadJob.position)).scalar() or 0) + 1
    job = DownloadJob(
        imdb_id=data["imdb_id"],
        title=data["title"],
        year=data.get("year"),
        media_type=data["media_type"],
        category=_job_category(db, data),
        season=data.get("season"),
        episode=data.get("episode"),
        episode_title=data.get("episode_title"),
        status=JobStatus.queued,
        position=next_pos,
        request_id=request_id,
        requested_by=requested_by,
        source=source,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
