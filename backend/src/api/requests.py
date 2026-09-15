"""
Media request workflow.

Normal users request titles; an admin approves (which queues the download)
or denies with a reason. Users only ever see their own requests, and the
responses they get are sanitized (no file paths, no source URLs).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import require_admin, require_auth
from src.database import (
    DownloadJob, JobStatus, MediaRequest, RequestStatus, User, get_db,
)
from src.security import audit, client_ip

_log = logging.getLogger("requests")

router = APIRouter(tags=["requests"])

_TITLE_ID_RE = re.compile(r"^(?:\d{1,12}|tt\d{6,12})$")
_ACTIVE_STATUSES = (JobStatus.queued, JobStatus.downloading, JobStatus.processing, JobStatus.paused)


class CreateRequest(BaseModel):
    imdb_id: str = Field(max_length=16)
    title: str = Field(min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1870, le=2100)
    media_type: str
    is_anime: bool = False
    seasons: Optional[list[int]] = None   # required for tvSeries


class DenyRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


def _validate_create(body: CreateRequest) -> None:
    if not _TITLE_ID_RE.match(body.imdb_id):
        raise HTTPException(status_code=400, detail="Invalid TMDB title id")
    if body.media_type not in ("movie", "tvSeries"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tvSeries'")
    if body.media_type == "tvSeries":
        if not body.seasons:
            raise HTTPException(status_code=400, detail="Select at least one season to request")
        if len(body.seasons) > 100 or any(s < 0 or s > 200 for s in body.seasons):
            raise HTTPException(status_code=400, detail="Invalid season list")


def _seasons_list(req: MediaRequest) -> list[int]:
    if not req.seasons:
        return []
    try:
        return json.loads(req.seasons)
    except Exception:
        return []


def _queue_summary(db: Session, req: MediaRequest) -> dict:
    """Aggregate job state for an approved request — sanitized for users."""
    jobs = db.query(DownloadJob).filter_by(request_id=req.id).all()
    total = len(jobs)
    counts = {"completed": 0, "failed": 0, "queued": 0, "downloading": 0, "processing": 0, "paused": 0}
    progress_sum = 0.0
    for j in jobs:
        if j.status in counts:
            counts[j.status] += 1
        progress_sum += j.progress if j.status != JobStatus.completed else 100.0
    in_queue = any(j.status in _ACTIVE_STATUSES for j in jobs)
    return {
        "total_jobs": total,
        "completed": counts["completed"],
        "failed": counts["failed"],
        "in_queue": in_queue,
        "downloading": counts["downloading"] + counts["paused"],
        "processing": counts["processing"],
        "queued": counts["queued"],
        "progress": round(progress_sum / total, 1) if total else 0.0,
    }


def _request_dict(db: Session, req: MediaRequest, *, for_admin: bool = False) -> dict:
    data = {
        "id": req.id,
        "imdb_id": req.imdb_id,
        "title": req.title,
        "year": req.year,
        "media_type": req.media_type,
        "is_anime": bool(getattr(req, "is_anime", False)),
        "seasons": _seasons_list(req),
        "status": req.status,
        "deny_reason": req.deny_reason,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "decided_by": None,
        "queue": _queue_summary(db, req) if req.status == RequestStatus.approved else None,
    }
    if req.decided_by:
        decider = db.query(User).filter_by(id=req.decided_by).first()
        data["decided_by"] = decider.username if decider else "(removed)"
    if for_admin:
        requester = db.query(User).filter_by(id=req.user_id).first()
        data["requested_by"] = requester.username if requester else "(deleted user)"
    return data


# ── User endpoints ──────────────────────────────────────────────────────────

@router.post("/requests")
def create_request(
    body: CreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    _validate_create(body)

    if body.media_type == "movie":
        already = (
            db.query(DownloadJob)
            .filter_by(imdb_id=body.imdb_id, media_type="movie")
            .filter(DownloadJob.status.in_((*_ACTIVE_STATUSES, JobStatus.completed)))
            .first()
        )
        if already:
            detail = ("This movie is already in the library."
                      if already.status == JobStatus.completed
                      else "This movie is already queued for download.")
            raise HTTPException(status_code=409, detail=detail)

    dup = (
        db.query(MediaRequest)
        .filter_by(imdb_id=body.imdb_id)
        .filter(MediaRequest.status.in_((RequestStatus.pending, RequestStatus.approved)))
        .all()
    )
    if body.media_type == "movie" and dup:
        mine = any(d.user_id == user.id for d in dup)
        raise HTTPException(
            status_code=409,
            detail="You already requested this." if mine else "This title has already been requested.",
        )
    if body.media_type == "tvSeries":
        wanted = set(body.seasons or [])
        for d in dup:
            if d.user_id == user.id and wanted & set(_seasons_list(d)):
                raise HTTPException(status_code=409, detail="You already requested one of these seasons.")

    req = MediaRequest(
        user_id=user.id,
        imdb_id=body.imdb_id,
        title=body.title.strip(),
        year=body.year,
        media_type=body.media_type,
        is_anime=body.is_anime,
        seasons=json.dumps(sorted(set(body.seasons))) if body.seasons else None,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    audit(db, "request.created", username=user.username,
          detail=f"{req.title} ({req.imdb_id})", ip=client_ip(request))
    return _request_dict(db, req)


@router.get("/requests/mine")
def my_requests(db: Session = Depends(get_db), user: User = Depends(require_auth)):
    reqs = (
        db.query(MediaRequest)
        .filter_by(user_id=user.id)
        .order_by(MediaRequest.created_at.desc())
        .limit(200)
        .all()
    )
    return [_request_dict(db, r) for r in reqs]


@router.delete("/requests/{request_id}")
def cancel_request(
    request_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    req = db.query(MediaRequest).filter_by(id=request_id, user_id=user.id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != RequestStatus.pending:
        raise HTTPException(status_code=400, detail="Only pending requests can be cancelled")
    db.delete(req)
    db.commit()
    audit(db, "request.cancelled", username=user.username, detail=req.title, ip=client_ip(request))
    return {"ok": True}


# ── Admin endpoints ─────────────────────────────────────────────────────────

@router.get("/admin/requests")
def all_requests(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    reqs = (
        db.query(MediaRequest)
        .order_by(MediaRequest.status.desc(), MediaRequest.created_at.desc())
        .limit(500)
        .all()
    )
    pending = [r for r in reqs if r.status == RequestStatus.pending]
    decided = [r for r in reqs if r.status != RequestStatus.pending]
    return {
        "pending": [_request_dict(db, r, for_admin=True) for r in pending],
        "decided": [_request_dict(db, r, for_admin=True) for r in decided],
    }


@router.post("/admin/requests/{request_id}/approve")
async def approve_request(
    request_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    from src.api.download import create_job
    from src.queue.manager import download_manager

    req = db.query(MediaRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != RequestStatus.pending:
        raise HTTPException(status_code=400, detail="Request already decided")

    queued = 0
    errors: list[str] = []

    if req.media_type == "movie":
        if not _job_exists(db, req.imdb_id):
            job = create_job(db, {
                "imdb_id": req.imdb_id,
                "title": req.title,
                "year": req.year,
                "media_type": "movie",
                "is_anime": req.is_anime,
            }, request_id=req.id, requested_by=req.user_id)
            await download_manager.enqueue(job.id)
            queued = 1
    else:
        from src.providers import meta
        seasons = _seasons_list(req)
        tasks = [meta.get_episodes(req.imdb_id, s) for s in seasons]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for season, result in zip(seasons, results):
            if isinstance(result, BaseException):
                _log.warning("approve %s s%s: %s", req.imdb_id, season, result)
                errors.append(f"Season {season}: could not fetch episodes")
                continue
            for ep in result.episodes:
                if _job_exists(db, req.imdb_id, season, ep.episode):
                    continue
                job = create_job(db, {
                    "imdb_id": req.imdb_id,
                    "title": req.title,
                    "year": req.year,
                    "media_type": "tvSeries",
                    "is_anime": req.is_anime,
                    "season": season,
                    "episode": ep.episode,
                    "episode_title": ep.title,
                }, request_id=req.id, requested_by=req.user_id)
                await download_manager.enqueue(job.id)
                queued += 1
        if queued == 0 and errors:
            raise HTTPException(status_code=502, detail="; ".join(errors))

    req.status = RequestStatus.approved
    req.decided_at = datetime.utcnow()
    req.decided_by = admin.id
    db.commit()
    audit(db, "request.approved", username=admin.username,
          detail=f"{req.title} ({req.imdb_id}) — {queued} job(s)", ip=client_ip(request))
    return {"ok": True, "queued": queued, "errors": errors}


@router.post("/admin/requests/{request_id}/deny")
def deny_request(
    request_id: int,
    body: DenyRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    req = db.query(MediaRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != RequestStatus.pending:
        raise HTTPException(status_code=400, detail="Request already decided")
    req.status = RequestStatus.denied
    req.deny_reason = body.reason.strip() or None
    req.decided_at = datetime.utcnow()
    req.decided_by = admin.id
    db.commit()
    audit(db, "request.denied", username=admin.username,
          detail=f"{req.title}: {body.reason[:100]}", ip=client_ip(request))
    return {"ok": True}


def _job_exists(db: Session, imdb_id: str, season: int | None = None, episode: int | None = None) -> bool:
    q = db.query(DownloadJob).filter_by(imdb_id=imdb_id).filter(
        DownloadJob.status.in_((*_ACTIVE_STATUSES, JobStatus.completed))
    )
    if season is not None:
        q = q.filter_by(season=season, episode=episode)
    return q.first() is not None
