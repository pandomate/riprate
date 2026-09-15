"""Admin-only: schedule shows for automatic new-episode checks."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import require_admin
from src.database import DownloadJob, Schedule, ScheduleRun, ScheduleRunItem, User, get_db
from src.scheduler import RUN_ITEM_STATUS, check_schedule, compute_gaps, is_due
from src.security import audit, client_ip

router = APIRouter(tags=["schedules"])

_TITLE_ID_RE = re.compile(r"^(?:\d{1,12}|tt\d{6,12})$")
_FREQS = ("daily", "weekly", "monthly")


class CreateSchedule(BaseModel):
    imdb_id: str = Field(max_length=16)
    title: str = Field(min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1870, le=2100)
    is_anime: bool = False
    seasons: Optional[list[int]] = None      # empty/None = all seasons
    frequency: str = "weekly"
    day_of_week: Optional[int] = Field(default=None, ge=0, le=6)
    hour: Optional[int] = Field(default=None, ge=0, le=23)


class UpdateSchedule(BaseModel):
    seasons: Optional[list[int]] = None
    frequency: Optional[str] = None
    day_of_week: Optional[int] = Field(default=None, ge=0, le=6)
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    is_active: Optional[bool] = None


def _seasons(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _dict(s: Schedule) -> dict:
    return {
        "id": s.id,
        "imdb_id": s.imdb_id,
        "title": s.title,
        "year": s.year,
        "is_anime": bool(getattr(s, "is_anime", False)),
        "seasons": _seasons(s.seasons),
        "frequency": s.frequency,
        "day_of_week": s.day_of_week,
        "hour": s.hour,
        "is_active": bool(s.is_active),
        "last_checked": s.last_checked.isoformat() if s.last_checked else None,
        "last_result": s.last_result,
        "last_queued": s.last_queued or 0,
        "due": is_due(s) if s.is_active else False,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _conflict(db: Session, imdb_id: str, seasons: list[int] | None,
             exclude_id: int | None = None) -> Schedule | None:
    """A "whole show" schedule (empty/null seasons) may only ever coexist
    with nothing else for that imdb_id; per-season schedules may coexist
    with each other as long as their season sets don't overlap, but never
    alongside a whole-show schedule. Returns the first conflicting Schedule,
    if any."""
    q = db.query(Schedule).filter(Schedule.imdb_id == imdb_id)
    if exclude_id is not None:
        q = q.filter(Schedule.id != exclude_id)
    is_whole = not seasons
    want = set(seasons or [])
    for s in q.all():
        s_seasons = set(_seasons(s.seasons))
        if is_whole or not s_seasons or (want & s_seasons):
            return s
    return None


def _conflict_message(conflict: Schedule, wanted_seasons: list[int] | None) -> str:
    existing_seasons = _seasons(conflict.seasons)
    existing_desc = "all seasons" if not existing_seasons else f"season(s) {', '.join(map(str, existing_seasons))}"
    if not wanted_seasons:
        return (f"This show already has a schedule covering {existing_desc}. "
                f"A whole-show schedule can't coexist with any other schedule for it.")
    return f"This show already has a schedule covering {existing_desc}, which overlaps what you're trying to schedule."


@router.get("/schedules")
def list_schedules(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    rows = db.query(Schedule).order_by(Schedule.created_at.desc()).all()
    return [_dict(s) for s in rows]


@router.get("/schedules/library-shows")
def library_shows(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Distinct TV shows already in the library — quick-pick for scheduling."""
    from src.database import DownloadJob, JobStatus
    rows = (
        db.query(DownloadJob)
        .filter_by(media_type="tvSeries", status=JobStatus.completed)
        .all()
    )
    scheduled = {s.imdb_id for s in db.query(Schedule).all()}
    seen: dict[str, dict] = {}
    for j in rows:
        if j.imdb_id not in seen:
            seen[j.imdb_id] = {
                "imdb_id": j.imdb_id, "title": j.title, "year": j.year,
                "scheduled": j.imdb_id in scheduled,
            }
    return sorted(seen.values(), key=lambda x: x["title"].lower())


@router.post("/schedules")
def create_schedule(
    body: CreateSchedule,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if not _TITLE_ID_RE.match(body.imdb_id):
        raise HTTPException(status_code=400, detail="Invalid TMDB title id")
    if body.frequency not in _FREQS:
        raise HTTPException(status_code=400, detail="frequency must be daily, weekly or monthly")
    conflict = _conflict(db, body.imdb_id, body.seasons)
    if conflict:
        raise HTTPException(status_code=409, detail={
            "message": _conflict_message(conflict, body.seasons),
            "existing": _dict(conflict),
        })

    sched = Schedule(
        imdb_id=body.imdb_id,
        title=body.title.strip(),
        year=body.year,
        is_anime=body.is_anime,
        seasons=json.dumps(sorted(set(body.seasons))) if body.seasons else None,
        frequency=body.frequency,
        day_of_week=body.day_of_week if body.frequency == "weekly" else None,
        hour=body.hour,
        created_by=admin.id,
    )
    db.add(sched)
    db.commit()
    db.refresh(sched)
    audit(db, "schedule.created", username=admin.username,
          detail=f"{body.title} ({body.frequency})", ip=client_ip(request))
    return _dict(sched)


@router.patch("/schedules/{sched_id}")
def update_schedule(
    sched_id: int,
    body: UpdateSchedule,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    sched = db.query(Schedule).filter_by(id=sched_id).first()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if body.frequency is not None:
        if body.frequency not in _FREQS:
            raise HTTPException(status_code=400, detail="Invalid frequency")
        sched.frequency = body.frequency
    if body.seasons is not None:
        conflict = _conflict(db, sched.imdb_id, body.seasons, exclude_id=sched.id)
        if conflict:
            raise HTTPException(status_code=409, detail={
                "message": _conflict_message(conflict, body.seasons),
                "existing": _dict(conflict),
            })
        sched.seasons = json.dumps(sorted(set(body.seasons))) if body.seasons else None
    if body.day_of_week is not None:
        sched.day_of_week = body.day_of_week
    if body.hour is not None:
        sched.hour = body.hour
    if body.is_active is not None:
        sched.is_active = body.is_active
    db.commit()
    return _dict(sched)


@router.delete("/schedules/{sched_id}")
def delete_schedule(
    sched_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    sched = db.query(Schedule).filter_by(id=sched_id).first()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    run_ids = [r.id for r in db.query(ScheduleRun).filter_by(schedule_id=sched_id).all()]
    if run_ids:
        db.query(ScheduleRunItem).filter(ScheduleRunItem.run_id.in_(run_ids)).delete(synchronize_session=False)
        db.query(ScheduleRun).filter(ScheduleRun.id.in_(run_ids)).delete(synchronize_session=False)
    db.delete(sched)
    db.commit()
    return {"ok": True}


@router.post("/schedules/{sched_id}/check")
async def check_now(
    sched_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    if not db.query(Schedule).filter_by(id=sched_id).first():
        raise HTTPException(status_code=404, detail="Schedule not found")
    result = await check_schedule(sched_id)
    return {"ok": True, **result}


@router.get("/schedules/{sched_id}/status")
async def schedule_status(
    sched_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Per-season downloaded vs total for a scheduled show."""
    sched = db.query(Schedule).filter_by(id=sched_id).first()
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    gaps = await compute_gaps(sched.imdb_id, _seasons(sched.seasons) or None)
    return {"seasons": gaps}


# ── Run-attempt history (replaces Downloads history for schedule jobs) ──────

def _live_statuses(db: Session, items: list[ScheduleRunItem]) -> dict[int, str]:
    """item.id -> current status: prefers the live DownloadJob.status when
    the job still exists, else falls back to the stored snapshot (the job
    may have since been archived/purged by history retention)."""
    job_ids = [it.job_id for it in items if it.job_id is not None]
    jobs = {}
    if job_ids:
        jobs = {j.id: j.status for j in db.query(DownloadJob).filter(DownloadJob.id.in_(job_ids)).all()}
    out: dict[int, str] = {}
    for it in items:
        job_status = jobs.get(it.job_id) if it.job_id is not None else None
        out[it.id] = RUN_ITEM_STATUS.get(job_status, it.status) if job_status else it.status
    return out


@router.get("/schedules/{sched_id}/runs")
def list_runs(
    sched_id: int,
    page: int = Query(0, ge=0),
    per: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    if not db.query(Schedule).filter_by(id=sched_id).first():
        raise HTTPException(status_code=404, detail="Schedule not found")
    q = db.query(ScheduleRun).filter_by(schedule_id=sched_id).order_by(ScheduleRun.started_at.desc())
    total = q.count()
    runs = q.offset(page * per).limit(per).all()
    run_ids = [r.id for r in runs]
    items = (
        db.query(ScheduleRunItem).filter(ScheduleRunItem.run_id.in_(run_ids)).all()
        if run_ids else []
    )
    live = _live_statuses(db, items)
    counts: dict[int, dict[str, int]] = {rid: {} for rid in run_ids}
    for it in items:
        status = live[it.id]
        counts[it.run_id][status] = counts[it.run_id].get(status, 0) + 1
    return {
        "items": [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "status": r.status,
                "checked": r.checked,
                "queued": r.queued,
                "summary": r.summary,
                "counts": counts.get(r.id, {}),
            }
            for r in runs
        ],
        "total": total, "page": page, "per": per,
    }


@router.get("/schedules/{sched_id}/runs/{run_id}")
def run_detail(
    sched_id: int,
    run_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    run = db.query(ScheduleRun).filter_by(id=run_id, schedule_id=sched_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    items = (
        db.query(ScheduleRunItem).filter_by(run_id=run_id)
        .order_by(ScheduleRunItem.season, ScheduleRunItem.episode)
        .all()
    )
    live = _live_statuses(db, items)
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "status": run.status,
        "checked": run.checked,
        "queued": run.queued,
        "summary": run.summary,
        "items": [
            {
                "id": it.id,
                "season": it.season,
                "episode": it.episode,
                "episode_title": it.episode_title,
                "job_id": it.job_id,
                "status": live[it.id],
                "error_message": it.error_message,
            }
            for it in items
        ],
    }
