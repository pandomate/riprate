"""
Library endpoints available to every authenticated user.

Users can browse what exists (titles, seasons, episodes) and see which items
came from their own requests — but never file paths, source URLs, error
details, or who else requested what. Editing lives in the admin API.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from src.api.deps import require_auth
from src.database import (
    DownloadJob, JobStatus, MediaRequest, RequestStatus, SessionLocal,
    User, UserRole, get_db,
)

router = APIRouter(tags=["library"])

_ACTIVE_JOB = [JobStatus.queued, JobStatus.downloading, JobStatus.processing, JobStatus.paused]


def _nav_counts_for(db: Session, uid: int, is_admin: bool) -> dict:
    """Sidebar badge counts, scoped to the caller's role. Admins see everyone's
    totals; users see only their own."""
    running = [JobStatus.downloading, JobStatus.processing]
    if is_admin:
        return {
            "requests": db.query(MediaRequest).filter_by(status=RequestStatus.pending).count(),
            "downloads": db.query(DownloadJob).filter(DownloadJob.status.in_(_ACTIVE_JOB)).count(),
            "downloading": db.query(DownloadJob).filter(DownloadJob.status.in_(running)).count(),
        }
    pending = (
        db.query(MediaRequest)
        .filter_by(user_id=uid, status=RequestStatus.pending)
        .count()
    )
    active = (
        db.query(DownloadJob)
        .filter(DownloadJob.requested_by == uid, DownloadJob.status.in_(_ACTIVE_JOB))
        .count()
    )
    downloading = (
        db.query(DownloadJob)
        .filter(DownloadJob.requested_by == uid, DownloadJob.status.in_(running))
        .count()
    )
    return {"requests": pending + active, "downloads": 0, "downloading": downloading}


@router.get("/categories")
def list_categories(_user: User = Depends(require_auth)):
    """Media categories (Movies/Shows/Anime Movies/Anime Shows built-ins + custom categories) — used
    by Search's category picker. Available to every authenticated user."""
    from src.library.categories import all_categories, default_key
    return {
        "categories": all_categories(),
        "defaults": {"movie": default_key("movie"), "tvSeries": default_key("tvSeries")},
    }


@router.get("/nav/counts")
def nav_counts(db: Session = Depends(get_db), user: User = Depends(require_auth)):
    """One-shot badge counts (kept for the first paint / SSE-less fallback)."""
    return _nav_counts_for(db, user.id, user.role == UserRole.admin)


@router.get("/nav/stream")
async def nav_stream(user: User = Depends(require_auth)):
    """Server-Sent Events: the server PUSHES the sidebar badge counts every few
    seconds so the client never has to poll /nav/counts."""
    uid, is_admin = user.id, (user.role == UserRole.admin)

    async def gen():
        try:
            while True:
                with SessionLocal() as db:
                    counts = _nav_counts_for(db, uid, is_admin)
                yield f"data: {json.dumps(counts)}\n\n"
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/library/browse")
def browse_library(db: Session = Depends(get_db), user: User = Depends(require_auth)):
    jobs = (
        db.query(DownloadJob)
        .filter_by(status=JobStatus.completed)
        .order_by(DownloadJob.media_type, DownloadJob.title,
                  DownloadJob.season, DownloadJob.episode)
        .all()
    )
    return [
        {
            "id": j.id,   # job id — used for /api/watch/{id}/download
            "imdb_id": j.imdb_id,
            "title": j.title,
            "year": j.year,
            "media_type": j.media_type,
            "season": j.season,
            "episode": j.episode,
            "episode_title": j.episode_title,
            "added_at": j.completed_at.isoformat() if j.completed_at else None,
            "requested_by_me": j.requested_by == user.id,
        }
        for j in jobs
    ]


@router.get("/downloaded")
def downloaded_map(db: Session = Depends(get_db), _user: User = Depends(require_auth)):
    """Map of imdb_id → {type, episodes:[{season, episode}]} for completed items.

    Used by the search page ("already in collection" badges) for both roles.
    """
    jobs = db.query(DownloadJob).filter_by(status=JobStatus.completed).all()
    result: dict = {}
    for j in jobs:
        if j.imdb_id not in result:
            result[j.imdb_id] = {"type": j.media_type, "episodes": []}
        if j.media_type == "tvSeries" and j.season and j.episode:
            result[j.imdb_id]["episodes"].append({"season": j.season, "episode": j.episode})
    return result
