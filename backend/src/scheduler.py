"""
Background scheduler.

Two jobs on a slow loop:
  1. Media schedules — for each active Schedule, on its cadence, look up the
     show's episodes, compare against what's downloaded/queued, and auto-queue
     the missing ones.
  2. History retention — drop old download history per the admin setting
     (failed rows are deleted; completed rows are archived so the media stays
     in the Library).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from src.database import (
    DownloadJob, JobStatus, Schedule, ScheduleRun, ScheduleRunItem, SessionLocal, get_setting,
)

_log = logging.getLogger("scheduler")

_TICK_SECONDS = 1800   # 30 minutes
_ACTIVE_OR_DONE = [
    JobStatus.queued, JobStatus.downloading, JobStatus.processing,
    JobStatus.paused, JobStatus.completed,
]

# DownloadJob.status -> the collapsed set of states a ScheduleRunItem shows.
RUN_ITEM_STATUS = {
    JobStatus.queued: "queued",
    JobStatus.downloading: "downloading",
    JobStatus.processing: "downloading",
    JobStatus.paused: "downloading",
    JobStatus.completed: "completed",
    JobStatus.failed: "failed",
}


def _seasons_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def is_due(sched: Schedule, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    last = sched.last_checked
    # If a preferred hour is set, don't run before it (for daily/weekly)
    hour_ok = sched.hour is None or now.hour >= sched.hour
    if last is None:
        return hour_ok if sched.frequency in ("daily", "weekly") else True
    delta = now - last
    if sched.frequency == "daily":
        return delta >= timedelta(hours=23) and hour_ok and last.date() < now.date()
    if sched.frequency == "weekly":
        if sched.day_of_week is not None:
            return now.weekday() == sched.day_of_week and last.date() < now.date() and hour_ok
        return delta >= timedelta(days=7)
    if sched.frequency == "monthly":
        return delta >= timedelta(days=30)
    return delta >= timedelta(days=7)


async def check_schedule(sched_id: int) -> dict:
    """Run one schedule now: queue any missing episodes. Returns a summary.

    Records a ScheduleRun + one ScheduleRunItem per episode considered (plus
    a failed item with no job_id for season-level fetch errors) — this is
    the "run attempts" history shown on the Schedule page, kept entirely
    separate from Downloads history. Both the automatic scheduler loop and
    the admin's manual "Check now" call this same function, so both count as
    run attempts."""
    from src.providers import meta
    from src.api.download import create_job
    from src.queue.manager import download_manager

    with SessionLocal() as db:
        sched = db.query(Schedule).filter_by(id=sched_id).first()
        if sched is None:
            return {"queued": 0, "error": "not found"}
        imdb_id, title, year, is_anime = sched.imdb_id, sched.title, sched.year, bool(sched.is_anime)
        wanted = _seasons_list(sched.seasons)
        created_by = sched.created_by
        run = ScheduleRun(schedule_id=sched_id, status="running")
        db.add(run)
        db.commit()
        run_id = run.id

    # Determine which seasons to scan
    seasons = wanted
    if not seasons:
        try:
            seasons = await meta.get_seasons(imdb_id)
        except Exception as exc:
            _finish(sched_id, f"Could not fetch seasons: {exc}", 0)
            _finish_run(run_id, 0, 0, f"Could not fetch seasons: {exc}")
            return {"queued": 0, "error": str(exc)}

    queued = 0
    checked = 0
    errors: list[str] = []
    for season in seasons:
        try:
            info = await meta.get_episodes(imdb_id, season)
        except Exception as exc:
            errors.append(f"S{season}: {exc}")
            with SessionLocal() as db:
                db.add(ScheduleRunItem(
                    run_id=run_id, imdb_id=imdb_id, season=season, episode=None,
                    episode_title=None, job_id=None, status="failed",
                    error_message=str(exc),
                ))
                db.commit()
            continue
        for ep in info.episodes:
            checked += 1
            with SessionLocal() as db:
                exists = (
                    db.query(DownloadJob)
                    .filter(DownloadJob.imdb_id == imdb_id,
                            DownloadJob.media_type == "tvSeries",
                            DownloadJob.season == season,
                            DownloadJob.episode == ep.episode,
                            DownloadJob.status.in_(_ACTIVE_OR_DONE))
                    .first()
                )
                if exists:
                    db.add(ScheduleRunItem(
                        run_id=run_id, imdb_id=imdb_id, season=season,
                        episode=ep.episode, episode_title=ep.title, job_id=exists.id,
                        status=RUN_ITEM_STATUS.get(exists.status, "downloading"),
                    ))
                    db.commit()
                    continue
                job = create_job(db, {
                    "imdb_id": imdb_id, "title": title, "year": year,
                    "media_type": "tvSeries", "season": season,
                    "episode": ep.episode, "episode_title": ep.title,
                    "is_anime": is_anime,
                }, requested_by=created_by, source="schedule")
                job_id = job.id
                db.add(ScheduleRunItem(
                    run_id=run_id, imdb_id=imdb_id, season=season,
                    episode=ep.episode, episode_title=ep.title, job_id=job_id,
                    status="queued",
                ))
                db.commit()
            await download_manager.enqueue(job_id)
            queued += 1

    summary = (f"Queued {queued} new episode(s)" if queued
               else f"Up to date ({checked} episodes checked)")
    if errors:
        summary += " · " + "; ".join(errors[:2])
    _finish(sched_id, summary, queued)
    _finish_run(run_id, checked, queued, summary)
    return {"queued": queued, "checked": checked, "errors": errors}


def _finish(sched_id: int, summary: str, queued: int) -> None:
    with SessionLocal() as db:
        sched = db.query(Schedule).filter_by(id=sched_id).first()
        if sched:
            sched.last_checked = datetime.utcnow()
            sched.last_result = summary
            sched.last_queued = queued
            db.commit()


def _finish_run(run_id: int, checked: int, queued: int, summary: str) -> None:
    with SessionLocal() as db:
        run = db.query(ScheduleRun).filter_by(id=run_id).first()
        if run:
            run.finished_at = datetime.utcnow()
            run.status = "done"
            run.checked = checked
            run.queued = queued
            run.summary = summary
            db.commit()


def run_retention() -> None:
    """Apply the history-retention setting."""
    try:
        days = int(get_setting("history_retention_days") or "0")
    except ValueError:
        days = 0
    if days <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(days=days)
    with SessionLocal() as db:
        old = (
            db.query(DownloadJob)
            .filter(DownloadJob.status.in_([JobStatus.completed, JobStatus.failed]))
            .filter(DownloadJob.archived.isnot(True))
            .filter(DownloadJob.completed_at.isnot(None))
            .filter(DownloadJob.completed_at < cutoff)
            .all()
        )
        for job in old:
            if job.status == JobStatus.completed:
                job.archived = True        # keep media in Library, drop from history
            else:
                db.delete(job)             # failed rows are just noise
        if old:
            db.commit()
            _log.info("Retention: cleaned %d old history rows (>%d days)", len(old), days)


async def compute_gaps(imdb_id: str, seasons: list[int] | None = None) -> list[dict]:
    """Per-season download coverage: {season, total, downloaded, missing:[ep]}.

    Uses the cached IMDB episode lists. Only counts seasons that have a folder
    of downloaded episodes unless explicit seasons are given.
    """
    from src.providers import meta

    if not seasons:
        try:
            seasons = await meta.get_seasons(imdb_id)
        except Exception:
            seasons = []

    # Which episodes are already completed, per season
    have: dict[int, set[int]] = {}
    with SessionLocal() as db:
        rows = (
            db.query(DownloadJob)
            .filter(DownloadJob.imdb_id == imdb_id,
                    DownloadJob.media_type == "tvSeries",
                    DownloadJob.status == JobStatus.completed)
            .all()
        )
        for r in rows:
            if r.season and r.episode:
                have.setdefault(r.season, set()).add(r.episode)

    out: list[dict] = []
    for season in seasons:
        try:
            info = await meta.get_episodes(imdb_id, season)
            all_eps = [e.episode for e in info.episodes]
        except Exception:
            all_eps = []
        downloaded = sorted(have.get(season, set()))
        missing = [e for e in all_eps if e not in have.get(season, set())]
        out.append({
            "season": season,
            "total": len(all_eps),
            "downloaded": len(downloaded),
            "missing": missing,
        })
    return out


class SchedulerService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # Small initial delay so startup finishes first
        await asyncio.sleep(30)
        while True:
            try:
                run_retention()
                with SessionLocal() as db:
                    ids = [s.id for s in db.query(Schedule).filter_by(is_active=True).all()]
                    sched_map = {s.id: s for s in db.query(Schedule).filter(Schedule.id.in_(ids)).all()} if ids else {}
                due = [sid for sid, s in sched_map.items() if is_due(s)]
                for sid in due:
                    try:
                        await check_schedule(sid)
                    except Exception as exc:
                        _log.warning("schedule %s failed: %s", sid, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("scheduler loop error: %s", exc, exc_info=True)
            await asyncio.sleep(_TICK_SECONDS)


scheduler_service = SchedulerService()
