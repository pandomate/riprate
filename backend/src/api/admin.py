from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import require_admin
from src.database import AuditLog, DownloadJob, JobStatus, SessionLocal, User, get_db, get_setting
from src.library.categories import all_categories, get_category, save_categories
from src.subtitles import normalize_language
from src.providers.base import configured_providers, enabled_provider_meta, resolve_one
from src.queue.manager import download_manager
from src.security import audit, client_ip

_log = logging.getLogger("admin")

router = APIRouter(tags=["admin"])

_ACTIVE = [JobStatus.queued, JobStatus.downloading, JobStatus.processing, JobStatus.paused]


def _username_map(db: Session) -> dict[int, str]:
    return {u.id: u.username for u in db.query(User).all()}


def _job_dict(job: DownloadJob, users: dict[int, str] | None = None) -> dict:
    has_subtitle = False
    if job.file_path:
        try:
            video = Path(job.file_path)
            has_subtitle = any(video.parent.glob(f"{video.stem}.*.vtt")) or video.with_suffix(".vtt").exists()
        except OSError:
            pass
    d = {
        "id": job.id,
        "label": job.label(),
        "title": job.title,
        "year": job.year,
        "imdb_id": job.imdb_id,
        "media_type": job.media_type,
        "category": job.category,
        "season": job.season,
        "episode": job.episode,
        "episode_title": job.episode_title,
        "status": job.status,
        "progress": job.progress,
        "position": job.position,
        "file_path": job.file_path,
        "has_subtitle": has_subtitle,
        "error_message": job.error_message,
        "downloaded_bytes": job.downloaded_bytes,
        "total_bytes": job.total_bytes,
        "download_bitrate": job.download_bitrate,
        "source_url": job.source_url,
        "current_source": job.current_source,
        "request_id": job.request_id,
        "added_at": job.added_at.isoformat() if job.added_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
    if users is not None:
        d["requested_by"] = users.get(job.requested_by) if job.requested_by else None
    return d


def _queue_jobs(db: Session) -> list[DownloadJob]:
    return (
        db.query(DownloadJob)
        .filter(DownloadJob.status.in_(_ACTIVE))
        .order_by(DownloadJob.position, DownloadJob.added_at)
        .all()
    )


# ── SSE live stream ────────────────────────────────────────────────────────

_subtitle_sync_tasks: dict[int, asyncio.Task] = {}
_subtitle_sync_states: dict[int, dict] = {}


async def stop_subtitle_sync_tasks():
    tasks = list(_subtitle_sync_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _subtitle_files(job_id: int, db: Session) -> tuple[Path, Path]:
    from src.api.watch import _job_file, _sidecar, _inside_media_dir
    video = _job_file(db, job_id)
    sidecar = _sidecar(video)
    if sidecar is None or not _inside_media_dir(sidecar):
        raise HTTPException(status_code=404, detail="No external subtitle file")
    return video, sidecar


async def _sync_existing_subtitle(job_id: int, video: Path, sidecar: Path):
    from src.subtitle_sync import align_subtitles, replace_sidecar
    state = _subtitle_sync_states[job_id]
    try:
        with tempfile.TemporaryDirectory(prefix="riprate-sync-existing-") as work:
            original = sidecar.read_text(encoding="utf-8")
            source, output = Path(work) / "source.vtt", Path(work) / "aligned.vtt"
            source.write_text(original, encoding="utf-8")
            ok = await align_subtitles(video, source, output,
                                      log=lambda message: state.update(message=message))
            if not ok:
                state["status"] = "failed"
                return
            if not video.exists() or not sidecar.exists() or sidecar.read_text(encoding="utf-8") != original:
                state.update(status="failed", message="The media or subtitles changed during alignment. Try again.")
                return
            replace_sidecar(sidecar, output.read_text(encoding="utf-8"))
            state.update(status="completed", message="External subtitles aligned. Reload the subtitle track in other players.")
    except asyncio.CancelledError:
        state.update(status="failed", message="Subtitle alignment was interrupted")
        raise
    except Exception as exc:
        _log.warning("Subtitle alignment for job %s failed: %s", job_id, type(exc).__name__)
        state.update(status="failed", message="Could not align subtitles; the original was kept.")
    finally:
        _subtitle_sync_tasks.pop(job_id, None)


@router.post("/subtitles/{job_id}/sync")
async def sync_subtitles(job_id: int, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    video, sidecar = _subtitle_files(job_id, db)
    if job_id not in _subtitle_sync_tasks:
        if len(_subtitle_sync_tasks) >= 4:
            raise HTTPException(status_code=429, detail="Subtitle alignment queue is full; try again shortly")
        # Keep the in-memory completion reports bounded.
        for key in list(_subtitle_sync_states)[:-50]:
            if key not in _subtitle_sync_tasks:
                _subtitle_sync_states.pop(key)
        _subtitle_sync_states[job_id] = {"status": "running", "message": "Waiting to align subtitles to audio…"}
        _subtitle_sync_tasks[job_id] = asyncio.create_task(_sync_existing_subtitle(job_id, video, sidecar))
    return _subtitle_sync_states[job_id]


@router.get("/subtitles/{job_id}/sync")
def subtitle_sync_status(job_id: int, _admin=Depends(require_admin)):
    return _subtitle_sync_states.get(job_id, {"status": "idle", "message": "No alignment is running"})


class SubtitleTiming(BaseModel):
    offset: float = Field(default=0, ge=-600, le=600, allow_inf_nan=False)
    scale: float = Field(default=1, ge=.8, le=1.25, allow_inf_nan=False)


@router.post("/subtitles/{job_id}/timing")
def save_subtitle_timing(job_id: int, body: SubtitleTiming,
                        db: Session = Depends(get_db), _admin=Depends(require_admin)):
    if job_id in _subtitle_sync_tasks:
        raise HTTPException(status_code=409, detail="Wait for subtitle alignment to finish")
    _, sidecar = _subtitle_files(job_id, db)
    from src.subtitles import retime_vtt, has_timed_cues
    from src.subtitle_sync import replace_sidecar
    text = retime_vtt(sidecar.read_text(encoding="utf-8"), body.offset, body.scale)
    if not has_timed_cues(text):
        raise HTTPException(status_code=422, detail="This adjustment removes all subtitle cues")
    replace_sidecar(sidecar, text)
    return {"ok": True}


@router.post("/subtitles/{job_id}/restore")
def restore_subtitle_timing(job_id: int, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    if job_id in _subtitle_sync_tasks:
        raise HTTPException(status_code=409, detail="Wait for subtitle alignment to finish")
    _, sidecar = _subtitle_files(job_id, db)
    backup = sidecar.with_suffix(sidecar.suffix + ".before-sync")
    from src.api.watch import _inside_media_dir
    if not backup.is_file() or not _inside_media_dir(backup):
        raise HTTPException(status_code=404, detail="No original subtitle backup is available")
    from src.library.organizer import _atomic_write_text
    _atomic_write_text(sidecar, backup.read_text(encoding="utf-8"))
    return {"ok": True}

@router.get("/stream")
async def stream_queue(_admin=Depends(require_admin)):
    """Server-Sent Events endpoint — pushes queue state every 2 s."""
    async def event_generator():
        try:
            while True:
                with SessionLocal() as db:
                    users = _username_map(db)
                    active = _queue_jobs(db)
                    recent = (
                        db.query(DownloadJob)
                        .filter(DownloadJob.status.in_([JobStatus.completed, JobStatus.failed]))
                        .filter(DownloadJob.archived.isnot(True))
                        .filter(DownloadJob.source != "schedule")
                        .order_by(DownloadJob.completed_at.desc())
                        .limit(50)
                        .all()
                    )
                    stats = _build_stats(db)
                active_dicts = []
                for j in active:
                    d = _job_dict(j, users)
                    d["log"] = download_manager.get_logs(j.id)
                    active_dicts.append(d)
                payload = json.dumps({
                    "active": active_dicts,
                    "history": [_job_dict(j, users) for j in recent],
                    "stats": stats,
                })
                yield f"data: {payload}\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer SSE
        },
    )


# ── REST endpoints ─────────────────────────────────────────────────────────

@router.get("/queue")
def get_queue(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    users = _username_map(db)
    out = []
    for j in _queue_jobs(db):
        d = _job_dict(j, users)
        d["log"] = download_manager.get_logs(j.id)
        out.append(d)
    return out


def _history_query(db: Session, status: str, q: str):
    statuses = {
        "failed": [JobStatus.failed],
        "completed": [JobStatus.completed],
    }.get(status, [JobStatus.completed, JobStatus.failed])
    query = (
        db.query(DownloadJob)
        .filter(DownloadJob.status.in_(statuses))
        .filter(DownloadJob.archived.isnot(True))
        # Schedule-triggered downloads never land in Downloads history — see
        # them in the Schedule page's run-attempt history instead.
        .filter(DownloadJob.source != "schedule")
    )
    if q:
        like = f"%{q}%"
        query = query.filter(
            DownloadJob.title.ilike(like) | DownloadJob.error_message.ilike(like)
        )
    return query


@router.get("/history")
def get_history(
    status: str = Query("all"),
    q: str = Query(""),
    page: int = Query(0, ge=0),
    per: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    users = _username_map(db)
    query = _history_query(db, status, q.strip())
    total = query.count()
    jobs = (
        query.order_by(DownloadJob.completed_at.desc())
        .offset(page * per).limit(per).all()
    )
    return {"items": [_job_dict(j, users) for j in jobs], "total": total, "page": page, "per": per}


@router.get("/library")
def get_library(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    users = _username_map(db)
    jobs = (
        db.query(DownloadJob)
        .filter_by(status=JobStatus.completed)
        .order_by(DownloadJob.media_type, DownloadJob.title, DownloadJob.season, DownloadJob.episode)
        .all()
    )
    return [_job_dict(j, users) for j in jobs]


def _build_stats(db: Session) -> dict:
    from sqlalchemy import func, distinct
    from src.vpn.openvpn_service import openvpn_service
    total = db.query(DownloadJob).count()
    completed = db.query(DownloadJob).filter_by(status=JobStatus.completed).count()
    failed = db.query(DownloadJob).filter_by(status=JobStatus.failed).count()
    queued = db.query(DownloadJob).filter_by(status=JobStatus.queued).count()
    downloading = db.query(DownloadJob).filter(
        DownloadJob.status.in_([JobStatus.downloading, JobStatus.paused])
    ).count()
    processing = db.query(DownloadJob).filter_by(status=JobStatus.processing).count()
    movies = db.query(DownloadJob).filter_by(
        status=JobStatus.completed, media_type="movie"
    ).count()
    shows_episodes = db.query(DownloadJob).filter_by(
        status=JobStatus.completed, media_type="tvSeries"
    ).count()
    shows_titles = db.query(func.count(distinct(DownloadJob.title))).filter_by(
        status=JobStatus.completed, media_type="tvSeries"
    ).scalar() or 0
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "queued": queued,
        "downloading": downloading,
        "processing": processing,
        "movies": movies,
        "shows": shows_titles,
        "episodes": shows_episodes,
        "globally_paused": download_manager.paused_all,
        "network_ok": download_manager.network_ok,
        "max_slots": download_manager.max_slots,
        "active_slots": download_manager.active_slots,
        "free_slots": max(0, download_manager.max_slots - download_manager.active_slots),
        "vpn_gated": download_manager.vpn_blocked,
        "vpn_active": openvpn_service.cached_active,
        "public_ip": openvpn_service.cached_public_ip,
    }


@router.get("/stats")
def get_stats(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    return _build_stats(db)


@router.get("/audit")
def get_audit(
    page: int = Query(0, ge=0),
    per: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    q = db.query(AuditLog)
    total = q.count()
    rows = q.order_by(AuditLog.ts.desc()).offset(page * per).limit(per).all()
    return {
        "items": [
            {
                "ts": r.ts.isoformat() if r.ts else None,
                "username": r.username,
                "action": r.action,
                "detail": r.detail,
                "ip": r.ip,
            }
            for r in rows
        ],
        "total": total, "page": page, "per": per,
    }


# ── App settings ───────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    history_retention_days: Optional[int] = Field(default=None, ge=0, le=3650)
    failed_temp_retention_hours: Optional[int] = Field(default=None, ge=0, le=87600)
    max_concurrent_downloads: Optional[int] = Field(default=None, ge=1, le=10)
    imdb_cookie: Optional[str] = Field(default=None, max_length=8000)
    watched_threshold_percent: Optional[int] = Field(default=None, ge=50, le=100)
    stream_providers: Optional[list[dict]] = None   # [{key, enabled}] in try-order
    wyzie_api_key: Optional[str] = Field(default=None, max_length=200)
    wyzie_enabled: Optional[bool] = None
    subtitle_language: Optional[str] = Field(default=None, max_length=16)
    subtitle_auto_sync: Optional[bool] = None
    tmdb_api_key: Optional[str] = Field(default=None, max_length=500)
    search_source: Optional[str] = Field(default=None, pattern="^(tmdb|imdb)$")
    movies_folder: Optional[str] = Field(default=None, max_length=120)
    shows_folder: Optional[str] = Field(default=None, max_length=120)
    media_categories: Optional[list[dict]] = None   # [{key,label,folder}]
    preferred_quality: Optional[int] = None
    segment_connections: Optional[int] = Field(default=None, ge=1, le=16)
    video_crf: Optional[int] = Field(default=None, ge=0, le=51)
    video_preset: Optional[str] = Field(default=None, max_length=20)
    max_file_gb: Optional[float] = Field(default=None, ge=0.5, le=200)


def _settings_payload() -> dict:
    from src.database import all_settings
    from src.config import settings as cfg
    from src.providers import tmdb, wyzie
    s = all_settings()
    cookie_set = bool((s.get("imdb_cookie") or "").strip() or cfg.imdb_cookie)
    return {
        "history_retention_days": int(s.get("history_retention_days", "0") or 0),
        "failed_temp_retention_hours": int(s.get("failed_temp_retention_hours", "0") or 0),
        "max_concurrent_downloads": download_manager.max_slots,
        # Secrets — never returned in plaintext, only whether one is configured
        "imdb_cookie_set": cookie_set,
        "wyzie_api_key_set": bool(wyzie.effective_key()),
        "wyzie_enabled": wyzie.preference_enabled(),
        "subtitle_language": normalize_language(s.get("subtitle_language") or "en"),
        "subtitle_auto_sync": s.get("subtitle_auto_sync", "true").lower() in ("true", "1", "yes", "on"),
        "tmdb_api_key_set": tmdb.has_key(),
        "watched_threshold_percent": int(s.get("watched_threshold_percent", "90") or 90),
        "stream_providers": configured_providers(),
        "search_source": (s.get("search_source") or "tmdb")
            if (s.get("search_source") or "tmdb") in ("tmdb", "imdb")
            else "tmdb",
        "media_root": cfg.media_dir,
        "movies_folder": s.get("movies_folder") or "Movies",
        "shows_folder": s.get("shows_folder") or "Shows",
        "media_categories": all_categories(),
        "preferred_quality": int(s.get("preferred_quality") or "1080"),
        "segment_connections": int(s.get("segment_connections") or "6"),
        "video_crf": int(s.get("video_crf") or "20"),
        "video_preset": s.get("video_preset") or "medium",
        "max_file_gb": float(s.get("max_file_gb") or "10.0"),
    }


@router.get("/settings")
def get_settings(_admin=Depends(require_admin)):
    return _settings_payload()


@router.patch("/settings")
async def update_settings(
    body: SettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    from src.database import set_setting
    if body.history_retention_days is not None:
        set_setting("history_retention_days", str(body.history_retention_days))
        audit(db, "settings.updated", username=admin.username,
              detail=f"history_retention_days={body.history_retention_days}", ip=client_ip(request))
    if body.failed_temp_retention_hours is not None:
        hours = body.failed_temp_retention_hours
        set_setting("failed_temp_retention_hours", str(hours))
        audit(db, "settings.updated", username=admin.username,
              detail=f"failed_temp_retention_hours={hours}", ip=client_ip(request))
        # Apply a stricter policy immediately instead of waiting for the next
        # periodic cleanup pass.
        await download_manager.cleanup_failed_temp_files()
    if body.max_concurrent_downloads is not None:
        n = await download_manager.set_concurrency(body.max_concurrent_downloads)
        set_setting("max_concurrent_downloads", str(n))
        audit(db, "settings.updated", username=admin.username,
              detail=f"max_concurrent_downloads={n}", ip=client_ip(request))
    if body.watched_threshold_percent is not None:
        set_setting("watched_threshold_percent", str(body.watched_threshold_percent))
        audit(db, "settings.updated", username=admin.username,
              detail=f"watched_threshold_percent={body.watched_threshold_percent}", ip=client_ip(request))
    if body.stream_providers is not None:
        import json
        from src.providers.base import all_provider_meta
        valid_keys = {p["key"] for p in all_provider_meta()}
        clean = [
            {"key": p["key"], "enabled": bool(p.get("enabled", True))}
            for p in body.stream_providers
            if isinstance(p, dict) and p.get("key") in valid_keys
        ]
        if not clean or not any(p["enabled"] for p in clean):
            raise HTTPException(
                status_code=422,
                detail="At least one stream source must remain enabled.",
            )
        from src.providers import tmdb
        tmdb_ready = tmdb.has_key() or bool((body.tmdb_api_key or "").strip())
        nova_enabled = any(p["key"] == "nova" and p["enabled"] for p in clean)
        if nova_enabled and not tmdb_ready:
            raise HTTPException(
                status_code=422,
                detail="Nova requires a TMDB API key before it can be enabled.",
            )
        set_setting("stream_providers", json.dumps(clean))
        audit(db, "settings.updated", username=admin.username,
              detail="stream_providers=" + ",".join(
                  f"{p['key']}:{'on' if p['enabled'] else 'off'}" for p in clean),
              ip=client_ip(request))
    if body.imdb_cookie is not None:
        from datetime import datetime
        cookie = body.imdb_cookie.strip()
        set_setting("imdb_cookie", cookie)
        set_setting("imdb_cookie_set_at", datetime.utcnow().isoformat() if cookie else "")
        audit(db, "settings.updated", username=admin.username,
              detail="imdb_cookie " + ("set" if cookie else "cleared"), ip=client_ip(request))
    if body.wyzie_api_key is not None:
        key = body.wyzie_api_key.strip()
        set_setting("wyzie_api_key", key)
        audit(db, "settings.updated", username=admin.username,
              detail="wyzie_api_key " + ("set" if key else "cleared"), ip=client_ip(request))
    if body.wyzie_enabled is not None:
        set_setting("wyzie_enabled", "true" if body.wyzie_enabled else "false")
        audit(db, "settings.updated", username=admin.username,
              detail=f"wyzie_enabled={body.wyzie_enabled}", ip=client_ip(request))
    if body.subtitle_language is not None:
        lang = normalize_language(body.subtitle_language)
        set_setting("subtitle_language", lang)
        audit(db, "settings.updated", username=admin.username,
              detail=f"subtitle_language={lang}", ip=client_ip(request))
    if body.subtitle_auto_sync is not None:
        set_setting("subtitle_auto_sync", "true" if body.subtitle_auto_sync else "false")
        audit(db, "settings.updated", username=admin.username,
              detail=f"subtitle_auto_sync={body.subtitle_auto_sync}", ip=client_ip(request))
    if body.tmdb_api_key is not None:
        key = body.tmdb_api_key.strip()
        if not key:
            from src.providers.base import configured_providers
            if ((get_setting("search_source") or "tmdb") == "tmdb"
                    or any(p["key"] == "nova" and p["enabled"] for p in configured_providers())):
                raise HTTPException(
                    status_code=422,
                    detail="Nova is enabled and requires a TMDB API key; disable Nova first.",
                )
        set_setting("tmdb_api_key", key)
        audit(db, "settings.updated", username=admin.username,
              detail="tmdb_api_key " + ("set" if key else "cleared"), ip=client_ip(request))
    if body.search_source is not None:
        if body.search_source == "tmdb":
            from src.providers import tmdb
            if not tmdb.has_key() and not (body.tmdb_api_key or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail="TMDB search requires a TMDB API key.",
                )
        set_setting("search_source", body.search_source)
        audit(db, "settings.updated", username=admin.username,
              detail=f"search_source={body.search_source}", ip=client_ip(request))
    if body.media_categories is not None:
        try:
            saved = save_categories(body.media_categories)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        audit(db, "settings.updated", username=admin.username,
              detail="media_categories=" + ",".join(f"{c['key']}:{c['folder']}" for c in saved),
              ip=client_ip(request))
    for cat, value in (("movies", body.movies_folder), ("shows", body.shows_folder)):
        if value is None:
            continue
        # Legacy per-folder fields — routed through the category system so
        # both stay consistent.
        cats = all_categories()
        for c in cats:
            if c["key"] == cat:
                c["folder"] = value
        try:
            save_categories(cats)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        audit(db, "settings.updated", username=admin.username,
              detail=f"{cat}_folder={value}", ip=client_ip(request))
    if body.preferred_quality is not None:
        if body.preferred_quality not in (480, 720, 1080, 1440, 2160):
            raise HTTPException(status_code=422, detail="preferred_quality must be one of 480/720/1080/1440/2160")
        set_setting("preferred_quality", str(body.preferred_quality))
        audit(db, "settings.updated", username=admin.username,
              detail=f"preferred_quality={body.preferred_quality}p", ip=client_ip(request))
    if body.segment_connections is not None:
        set_setting("segment_connections", str(body.segment_connections))
        audit(db, "settings.updated", username=admin.username,
              detail=f"segment_connections={body.segment_connections}", ip=client_ip(request))
    if body.video_crf is not None:
        set_setting("video_crf", str(body.video_crf))
        audit(db, "settings.updated", username=admin.username,
              detail=f"video_crf={body.video_crf}", ip=client_ip(request))
    if body.video_preset is not None:
        from src.queue.manager import _X264_PRESETS
        if body.video_preset not in _X264_PRESETS:
            raise HTTPException(status_code=422, detail="video_preset is not a valid libx264 preset")
        set_setting("video_preset", body.video_preset)
        audit(db, "settings.updated", username=admin.username,
              detail=f"video_preset={body.video_preset}", ip=client_ip(request))
    if body.max_file_gb is not None:
        set_setting("max_file_gb", str(body.max_file_gb))
        audit(db, "settings.updated", username=admin.username,
              detail=f"max_file_gb={body.max_file_gb}", ip=client_ip(request))
    return _settings_payload()


# ── OpenVPN profile pool ───────────────────────────────────────────────────

class OpenVPNProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    config: str = Field(min_length=1, max_length=512000)
    username: Optional[str] = Field(default=None, max_length=500)
    password: Optional[str] = Field(default=None, max_length=500)
    profile_type: str = Field(default="auto", pattern="^(auto|country|individual)$")
    group: Optional[str] = Field(default=None, max_length=80)


class OpenVPNProfileEnabledInput(BaseModel):
    enabled: bool


@router.get("/vpn/openvpn")
async def get_openvpn(_admin=Depends(require_admin)):
    from src.vpn.openvpn_service import openvpn_service
    return await openvpn_service.status()


@router.post("/vpn/openvpn/profiles")
async def add_openvpn_profile(body: OpenVPNProfileInput, request: Request,
                              db: Session = Depends(get_db), admin=Depends(require_admin)):
    from vpn_profile_manager import OpenVPNConfigError
    from src.vpn.openvpn_service import openvpn_service
    try:
        profile = openvpn_service.add_profile(
            body.name, body.config, body.username, body.password,
            body.profile_type, body.group,
        )
    except OpenVPNConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit(db, "vpn.openvpn.profile_added", username=admin.username,
          detail=f"profile={profile.name}", ip=client_ip(request))
    return {"profile": profile.as_dict(), "status": await openvpn_service.status()}


@router.patch("/vpn/openvpn/profiles/{profile_id}")
async def update_openvpn_profile(profile_id: str, body: OpenVPNProfileEnabledInput,
                                 request: Request, db: Session = Depends(get_db),
                                 admin=Depends(require_admin)):
    from vpn_profile_manager import OpenVPNConfigError
    from src.vpn.openvpn_service import openvpn_service
    try:
        profile = openvpn_service.set_profile_enabled(profile_id, body.enabled)
    except OpenVPNConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit(db, "vpn.openvpn.profile_updated", username=admin.username,
          detail=f"profile={profile.name} enabled={body.enabled}", ip=client_ip(request))
    return await openvpn_service.status()


@router.delete("/vpn/openvpn/profiles/{profile_id}")
async def delete_openvpn_profile(profile_id: str, request: Request,
                                 db: Session = Depends(get_db), admin=Depends(require_admin)):
    from vpn_profile_manager import OpenVPNConfigError
    from src.vpn.openvpn_service import openvpn_service
    try:
        await openvpn_service.remove_profile(profile_id)
    except OpenVPNConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit(db, "vpn.openvpn.profile_removed", username=admin.username,
          detail=f"profile={profile_id}", ip=client_ip(request))
    return await openvpn_service.status()


@router.post("/vpn/openvpn/enable")
async def enable_openvpn(request: Request, db: Session = Depends(get_db),
                         admin=Depends(require_admin)):
    from src.vpn.openvpn_service import openvpn_service
    result = await openvpn_service.enable_random()
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.message)
    audit(db, "vpn.openvpn.enabled", username=admin.username,
          detail=f"profile={result.profile_name}", ip=client_ip(request))
    return {"result": result.as_dict(), "status": await openvpn_service.status()}


@router.post("/vpn/openvpn/rotate")
async def rotate_openvpn(request: Request, db: Session = Depends(get_db),
                         admin=Depends(require_admin)):
    from src.vpn.openvpn_service import openvpn_service
    result = await openvpn_service.rotate("manual rotation requested")
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.message)
    audit(db, "vpn.openvpn.rotated", username=admin.username,
          detail=f"profile={result.profile_name}", ip=client_ip(request))
    return {"result": result.as_dict(), "status": await openvpn_service.status()}


@router.post("/vpn/openvpn/disable")
async def disable_openvpn(request: Request, db: Session = Depends(get_db),
                          admin=Depends(require_admin)):
    from src.vpn.openvpn_service import openvpn_service
    await openvpn_service.disable()
    audit(db, "vpn.openvpn.disabled", username=admin.username, ip=client_ip(request))
    return await openvpn_service.status()


# ── Library gaps (missing episodes per season) ─────────────────────────────

@router.get("/library/show/{imdb_id}/gaps")
async def library_gaps(imdb_id: str, _admin=Depends(require_admin)):
    from src.scheduler import compute_gaps
    seasons = await compute_gaps(imdb_id)
    return {"seasons": seasons}


# ── Media tree (Settings wizard) + move-folder-into-category tool ──────────

def _mounted_paths() -> set[str]:
    """Mount points visible inside the container (/proc/mounts octal-unescaped)."""
    out: set[str] = set()
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                out.add(parts[1].replace("\\040", " ").replace("\\011", "\t"))
    except OSError:
        pass
    return out


@router.get("/media-tree")
def media_tree(_admin=Depends(require_admin)):
    """What actually exists under the media root: each top-level folder,
    whether it's a dedicated host mount (MOUNT_* in .env) or a plain
    subfolder, its free space, and its immediate children (title folders) —
    everything the Settings wizard and the move tool need."""
    import shutil as _sh
    from src.config import settings as cfg
    root = Path(cfg.media_dir)
    mounts = _mounted_paths()
    category_folders = {c["folder"]: c["key"] for c in all_categories()}

    def usage(p: Path) -> tuple[float, float]:
        try:
            du = _sh.disk_usage(p)
            return round(du.free / 2**30, 1), round(du.total / 2**30, 1)
        except OSError:
            return 0.0, 0.0

    folders = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            try:
                if not child.is_dir() or child.name.startswith(".") \
                   or child.name == "System Volume Information":
                    continue
            except OSError:
                continue   # unreadable entry (e.g. FS corruption) — skip
            free, total = usage(child)
            try:
                children = sorted(c.name for c in child.iterdir() if c.is_dir())[:300]
            except OSError:
                children = []
            folders.append({
                "name": child.name,
                "is_mount": str(child) in mounts,
                "category": category_folders.get(child.name),
                "free_gb": free,
                "total_gb": total,
                "children": children,
            })
    root_free, root_total = usage(root)
    return {
        "root": cfg.media_dir,
        "root_is_mount": str(root) in mounts,
        "root_free_gb": root_free,
        "root_total_gb": root_total,
        "folders": folders,
    }


# ── Filesystem browser (Jellyfin-style folder picker) ──────────────────────

def _fs_resolve(rel: str) -> Path:
    """A relative path under the media root, traversal-safe."""
    from src.config import settings as cfg
    root = Path(cfg.media_dir).resolve()
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        raise HTTPException(status_code=404, detail="Not found")
    return target


@router.get("/fs")
def fs_list(path: str = "", _admin=Depends(require_admin)):
    """Browse the media tree inside the container: the sub-directories of
    `path` (relative to /media/library), with mount + free-space info and
    which category each folder is assigned to. Drives the folder-picker
    dialog in Settings."""
    import shutil as _sh
    from src.config import settings as cfg
    root = Path(cfg.media_dir).resolve()
    target = _fs_resolve(path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Not a folder")
    mounts = _mounted_paths()
    cat_by_folder = {c["folder"]: c["label"] for c in all_categories()}

    dirs = []
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        children = []
    for child in children:
        try:
            if not child.is_dir() or child.name.startswith(".") \
               or child.name == "System Volume Information":
                continue
        except OSError:
            continue   # unreadable entry (e.g. FS corruption) — skip, don't 500
        rel = child.relative_to(root).as_posix()
        entry: dict = {
            "name": child.name,
            "path": rel,
            "is_mount": str(child) in mounts,
            "category": cat_by_folder.get(rel),
        }
        if entry["is_mount"]:
            try:
                du = _sh.disk_usage(child)
                entry["free_gb"] = round(du.free / 2**30, 1)
                entry["total_gb"] = round(du.total / 2**30, 1)
            except OSError:
                pass
        dirs.append(entry)

    rel_path = "" if target == root else target.relative_to(root).as_posix()
    parent = "" if target == root else Path(rel_path).parent.as_posix()
    return {
        "root": cfg.media_dir,
        "path": rel_path,
        "parent": None if target == root else ("" if parent == "." else parent),
        "dirs": dirs,
    }


class MkdirRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)   # rel path to create


@router.post("/fs/mkdir")
def fs_mkdir(body: MkdirRequest, request: Request, db: Session = Depends(get_db),
             admin=Depends(require_admin)):
    from src.config import settings as cfg
    root = Path(cfg.media_dir).resolve()
    target = (root / body.path).resolve()
    if root not in target.parents:
        raise HTTPException(status_code=422, detail="Folder must stay inside the media root")
    if target.exists():
        raise HTTPException(status_code=409, detail="That folder already exists")
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not create folder: {exc}")
    audit(db, "library.folder_created", username=admin.username,
          detail=target.relative_to(root).as_posix(), ip=client_ip(request))
    return {"ok": True, "path": target.relative_to(root).as_posix()}


class FsDeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)          # rel path under media root
    confirm_name: str = Field(min_length=1, max_length=255)  # must equal the folder's name


@router.post("/fs/delete")
def fs_delete(body: FsDeleteRequest, request: Request, db: Session = Depends(get_db),
              admin=Depends(require_admin)):
    """Permanently delete a folder (and everything in it) inside the media
    root. Strictly contained to the media root — never escapes it and never
    the root itself. The caller must echo the exact folder name as a
    safeguard; deletion removes the folder on the host via the bind mount."""
    import shutil as _sh
    from src.config import settings as cfg
    root = Path(cfg.media_dir).resolve()
    target = _fs_resolve(body.path)   # traversal-safe: raises 404 if outside root
    if target == root:
        raise HTTPException(status_code=422, detail="Cannot delete the media root")
    if root not in target.parents:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Not a folder")
    if body.confirm_name != target.name:
        raise HTTPException(status_code=400, detail="Type the exact folder name to confirm")
    rel = target.relative_to(root).as_posix()
    try:
        _sh.rmtree(target)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete folder: {exc}")
    audit(db, "library.folder_deleted", username=admin.username,
          detail=rel, ip=client_ip(request))
    return {"ok": True, "path": rel}


_move_state: dict = {"running": False, "source": None, "target": None,
                     "moved_jobs": 0, "error": None}


class MoveFolderRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)   # rel path under media root
    category: str = Field(min_length=1, max_length=32)


@router.post("/library/move-folder")
async def move_folder(
    body: MoveFolderRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    """Move one folder (a title folder, or any folder under the media root)
    into a category's folder, updating every affected job's paths + category.
    Runs in the background — cross-mount moves of big shows take a while."""
    import shutil as _sh
    from src.config import settings as cfg

    if _move_state["running"]:
        raise HTTPException(status_code=409, detail="A move is already running")
    cat = get_category(body.category)
    if cat is None:
        raise HTTPException(status_code=422, detail="Unknown category")

    root = Path(cfg.media_dir).resolve()
    source = (root / body.source).resolve()
    if root not in source.parents or not source.is_dir():
        raise HTTPException(status_code=404, detail="Source folder not found")
    cat_root = (root / cat["folder"]).resolve()
    if source == cat_root or source == root or cat_root in [source] + list(source.parents):
        raise HTTPException(status_code=422, detail="That folder can't be moved into itself")
    target = cat_root / source.name
    if target.exists():
        raise HTTPException(status_code=409,
                            detail=f"'{cat['folder']}/{source.name}' already exists")

    audit(db, "library.folder_moved", username=admin.username,
          detail=f"{body.source} → {cat['folder']}/", ip=client_ip(request))
    _move_state.update(running=True, source=body.source,
                       target=f"{cat['folder']}/{source.name}", moved_jobs=0, error=None)

    def do_move() -> None:
        try:
            cat_root.mkdir(parents=True, exist_ok=True)
            _sh.move(str(source), str(target))
            old_prefix, new_prefix = str(source), str(target)
            with SessionLocal() as sdb:
                n = 0
                for job in sdb.query(DownloadJob).filter(
                        DownloadJob.file_path.like(old_prefix + "/%")).all():
                    job.file_path = new_prefix + job.file_path[len(old_prefix):]
                    if job.subtitle_path and job.subtitle_path.startswith(old_prefix):
                        job.subtitle_path = new_prefix + job.subtitle_path[len(old_prefix):]
                    job.category = cat["key"]
                    n += 1
                sdb.commit()
            _move_state.update(moved_jobs=n)
        except Exception as exc:
            _move_state.update(error=str(exc)[:300])
            _log.error("move-folder failed: %s", exc, exc_info=True)
        finally:
            _move_state.update(running=False)

    asyncio.get_running_loop().run_in_executor(None, do_move)
    return {"ok": True, "started": True, "target": _move_state["target"]}


@router.get("/library/move-status")
def move_status(_admin=Depends(require_admin)):
    return dict(_move_state)


# ── Queue reorder ──────────────────────────────────────────────────────────

class MoveRequest(BaseModel):
    direction: str = Field(pattern="^(up|down|top|bottom)$")


@router.post("/queue/move/{job_id}")
def move_job(
    job_id: int,
    body: MoveRequest,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    queued = (
        db.query(DownloadJob)
        .filter_by(status=JobStatus.queued)
        .order_by(DownloadJob.position, DownloadJob.added_at)
        .all()
    )
    idx = next((i for i, j in enumerate(queued) if j.id == job_id), None)
    if idx is None:
        raise HTTPException(status_code=400, detail="Job is not in the waiting queue")

    job = queued[idx]
    if body.direction == "up" and idx > 0:
        job.position, queued[idx - 1].position = queued[idx - 1].position, job.position
    elif body.direction == "down" and idx < len(queued) - 1:
        job.position, queued[idx + 1].position = queued[idx + 1].position, job.position
    elif body.direction == "top" and idx > 0:
        min_pos = min(j.position for j in queued)
        job.position = min_pos - 1
    elif body.direction == "bottom" and idx < len(queued) - 1:
        from sqlalchemy import func
        max_pos = db.query(func.max(DownloadJob.position)).scalar() or 0
        job.position = max_pos + 1
    db.commit()
    return {"ok": True}


# ── Reorder (drag & drop) ──────────────────────────────────────────────────

class ReorderRequest(BaseModel):
    job_ids: list[int]


@router.post("/queue/reorder")
def reorder_queue(
    body: ReorderRequest,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Set the waiting-queue order to exactly the given job-id sequence."""
    queued = {
        j.id: j for j in db.query(DownloadJob)
        .filter_by(status=JobStatus.queued).all()
    }
    pos = 0
    for jid in body.job_ids:
        job = queued.get(jid)
        if job is not None:
            job.position = pos
            pos += 1
    # Any queued jobs not named keep going after, preserving prior order
    leftovers = sorted(
        (j for j in queued.values() if j.id not in set(body.job_ids)),
        key=lambda j: j.position,
    )
    for job in leftovers:
        job.position = pos
        pos += 1
    db.commit()
    return {"ok": True}


# ── Global pause / resume ──────────────────────────────────────────────────

@router.post("/queue/pause-all")
async def pause_all(_admin=Depends(require_admin)):
    n = await download_manager.pause_all()
    return {"ok": True, "paused": n, "globally_paused": True}


@router.post("/queue/resume-all")
async def resume_all(_admin=Depends(require_admin)):
    n = await download_manager.resume_all()
    return {"ok": True, "resumed": n, "globally_paused": False}


# ── Bulk actions on selected jobs ──────────────────────────────────────────

class BulkRequest(BaseModel):
    action: str = Field(pattern="^(pause|resume|cancel|delete|retry)$")
    job_ids: list[int]


def _reset_failed_job(job: DownloadJob) -> bool:
    """Reset a failed job back to queued (keeps any resumable partial). Caller
    commits and enqueues. Returns False if the job can't be retried."""
    if job.status != JobStatus.failed:
        return False
    job.status = JobStatus.queued
    job.error_message = None
    job.progress = 0.0
    job.started_at = None
    job.completed_at = None
    job.download_bitrate = None
    return True


@router.post("/queue/bulk")
async def bulk_action(
    body: BulkRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    done = 0
    for jid in body.job_ids:
        try:
            if body.action == "pause":
                done += bool(await download_manager.pause(jid))
            elif body.action == "resume":
                done += bool(await download_manager.resume(jid))
            elif body.action == "cancel":
                done += bool(await download_manager.cancel(jid))
            elif body.action == "delete":
                done += _delete_one(db, jid, admin, request)
            elif body.action == "retry":
                job = db.query(DownloadJob).filter_by(id=jid).first()
                if job and _reset_failed_job(job):
                    db.commit()
                    await download_manager.enqueue(jid)
                    done += 1
        except Exception as exc:
            _log.warning("bulk %s job %s: %s", body.action, jid, exc)
    return {"ok": True, "count": done}


class RetryFailedRequest(BaseModel):
    q: str = ""


@router.post("/queue/retry-failed")
async def retry_all_failed(
    body: RetryFailedRequest,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Re-queue EVERY failed download (optionally filtered by title/error text)."""
    jobs = _history_query(db, "failed", body.q.strip()).all()
    ids: list[int] = []
    for job in jobs:
        if _reset_failed_job(job):
            ids.append(job.id)
    db.commit()
    for jid in ids:
        await download_manager.enqueue(jid)
    return {"ok": True, "count": len(ids)}


# ── Pause / Resume / Cancel ────────────────────────────────────────────────

@router.post("/pause/{job_id}")
async def pause_job(job_id: int, _admin=Depends(require_admin)):
    ok = await download_manager.pause(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Job is not currently running")
    return {"ok": True}


@router.post("/resume/{job_id}")
async def resume_job(job_id: int, _admin=Depends(require_admin)):
    ok = await download_manager.resume(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Job is not paused")
    return {"ok": True}


@router.post("/cancel/{job_id}")
async def cancel_job(job_id: int, _admin=Depends(require_admin)):
    ok = await download_manager.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Job cannot be cancelled")
    return {"ok": True}


@router.post("/skip-source/{job_id}")
async def skip_source(job_id: int, _admin=Depends(require_admin)):
    """Abandon the source this job is resolving/downloading from and fail over
    to the next enabled provider (only this job's slot is affected). If no
    sources remain, the job fails."""
    ok = download_manager.skip_source(job_id)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="This job isn't downloading from a source right now")
    return {"ok": True}


# ── Run-now: promote / swap ────────────────────────────────────────────────

class SwapRequest(BaseModel):
    queued_id: int
    active_id: int


@router.post("/promote/{job_id}")
async def promote_job(
    job_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Start a queued job now — used when there is a free worker slot."""
    job = db.query(DownloadJob).filter_by(id=job_id).first()
    if not job or job.status != JobStatus.queued:
        raise HTTPException(status_code=400, detail="Job is not waiting in the queue")
    download_manager.promote(job_id)
    return {"ok": True}


@router.post("/queue/swap")
async def swap_job(
    body: SwapRequest,
    _admin=Depends(require_admin),
):
    """Send an active job back to the queue (keeping progress) and start a
    queued job in the freed slot."""
    ok = await download_manager.swap(body.queued_id, body.active_id)
    if not ok:
        raise HTTPException(status_code=400, detail="That download is no longer running")
    return {"ok": True}


# ── Mark failed / permanent delete (Trash choices) ─────────────────────────

@router.post("/fail/{job_id}")
async def fail_job(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    ok = await download_manager.fail(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    audit(db, "job.failed", username=admin.username, ip=client_ip(request))
    return {"ok": True}


@router.post("/purge/{job_id}")
async def purge_job(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    """Permanently wipe a job — stop it if running, delete its record AND any
    media/temp files, as if it never happened."""
    job = db.query(DownloadJob).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in (JobStatus.downloading, JobStatus.processing, JobStatus.paused):
        await download_manager.cancel(job_id)
        job = db.query(DownloadJob).filter_by(id=job_id).first()
        if not job:
            return {"ok": True}
    # delete_file=True path removes the library media too
    return _remove_record(db, job, admin, request, delete_file=True)


# ── Retry / Delete ────────────────────────────────────────────────────────

@router.post("/retry/{job_id}")
async def retry_job(
    job_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    job = db.query(DownloadJob).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.failed,):
        raise HTTPException(status_code=400, detail="Only failed jobs can be retried")
    job.status = JobStatus.queued
    job.error_message = None
    job.progress = 0.0
    job.started_at = None
    job.completed_at = None
    job.downloaded_bytes = None
    job.total_bytes = None
    job.download_bitrate = None
    db.commit()
    await download_manager.enqueue(job.id)
    return {"ok": True, "job_id": job.id}


@router.post("/subtitles/{job_id}/retry")
async def retry_subtitles(
    job_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Fetch subtitles for an existing completed video without re-downloading it."""
    if job_id in _subtitle_sync_tasks:
        raise HTTPException(status_code=409, detail="Wait for subtitle alignment to finish")
    job = db.query(DownloadJob).filter_by(id=job_id, status=JobStatus.completed).first()
    if not job or not job.file_path:
        raise HTTPException(status_code=404, detail="Completed download not found")

    video = Path(job.file_path)
    if not video.exists():
        raise HTTPException(status_code=404, detail="Downloaded video is missing")

    language = normalize_language(get_setting("subtitle_language") or "en")
    from src.library.organizer import subtitle_sidecar_path
    sidecar = subtitle_sidecar_path(video, language)
    tmp_root = download_manager._tmp_root()
    tmp_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"subtitle-retry-{job.id}-",
                                      dir=str(tmp_root)))
    try:
        found = await download_manager._download_wyzie_vtt(job, temp_dir, language)
        source_label = "Wyzie" if found is not None else None
        for provider in enabled_provider_meta() if found is None else []:
            try:
                sources = await resolve_one(
                    provider["key"], job.imdb_id, job.media_type,
                    job.season, job.episode,
                )
                if sources.subtitles:
                    found = await download_manager._download_provider_vtt(
                        sources.subtitles, temp_dir, job.id,
                        sources.referer, language,
                    )
                    if found:
                        source_label = provider["label"]
                        break
            except Exception as exc:
                _log.info("Subtitle retry via %s failed for job %s: %s",
                          provider["label"], job.id, exc)

        if found is None:
            raise HTTPException(
                status_code=404,
                detail=f"No {language} subtitle track was available from the enabled sources",
            )

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        from src.subtitle_sync import align_subtitles, auto_sync_enabled
        if auto_sync_enabled():
            aligned = temp_dir / "aligned.vtt"
            if await align_subtitles(video, found, aligned,
                                     log=lambda m: download_manager.log_job(job_id, m), timeout=90):
                found = aligned
        shutil.copyfile(found, sidecar)
        job.subtitle_path = str(sidecar)
        db.commit()
        return {"ok": True, "language": language, "source": source_label,
                "filename": sidecar.name}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.delete("/job/{job_id}")
def delete_job(
    job_id: int,
    request: Request,
    delete_file: bool = Query(False),
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    job = db.query(DownloadJob).filter_by(id=job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in (JobStatus.downloading, JobStatus.processing, JobStatus.paused):
        raise HTTPException(status_code=400, detail="Cancel the job before deleting it")
    return _remove_record(db, job, admin, request, delete_file)


def _remove_record(db: Session, job: DownloadJob, admin: User,
                   request: Request, delete_file: bool) -> dict:
    """Archive-or-delete a finished/failed job. Shared by delete + bulk delete."""
    from pathlib import Path

    # A finished MP4 may live at the recorded path OR the computed library path
    # (a stale/renamed file_path must not trick us into "cleaning up" real media).
    mp4_path: Path | None = None
    if job.file_path and Path(job.file_path).exists():
        mp4_path = Path(job.file_path)
    else:
        computed = download_manager.dest_path(job)
        if computed.exists() and computed.stat().st_size > 0:
            mp4_path = computed
    mp4_exists = mp4_path is not None
    job_id = job.id

    # Explicit "delete the media too" (Library page) → remove file + record
    if delete_file:
        if mp4_exists:
            from src.library.organizer import delete_job_file
            try:
                delete_job_file(str(mp4_path))
            except Exception as exc:
                _log.warning("Could not delete file %s: %s", mp4_path, exc)
            audit(db, "library.file_deleted", username=admin.username,
                  detail=job.label(), ip=client_ip(request))
        download_manager.cleanup_deleted(job_id)
        db.delete(job)
        db.commit()
        return {"ok": True, "kept_file": False, "archived": False}

    # Removing a COMPLETED download from the Downloads page: keep the media and
    # keep it in the Library — just hide it from the download history.
    if job.status == JobStatus.completed and mp4_exists:
        if str(job.file_path) != str(mp4_path):
            job.file_path = str(mp4_path)   # heal a stale path
        job.archived = True
        download_manager.cleanup_deleted(job_id)   # temp working dir + logs only
        db.commit()
        return {"ok": True, "kept_file": True, "archived": True}

    # Not completed / no MP4 anywhere → smart cleanup of partial output, drop row.
    download_manager.purge_incomplete_output(job)
    download_manager.cleanup_deleted(job_id)
    db.delete(job)
    db.commit()
    return {"ok": True, "kept_file": False, "archived": False}


def _delete_one(db: Session, job_id: int, admin: User, request: Request) -> int:
    """Bulk helper: remove a finished/failed job's record (never a running one)."""
    job = db.query(DownloadJob).filter_by(id=job_id).first()
    if not job or job.status in (JobStatus.downloading, JobStatus.processing, JobStatus.paused):
        return 0
    _remove_record(db, job, admin, request, delete_file=False)
    return 1


# ── Library management endpoints ──────────────────────────────────────────────

class RenameJobRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    episode_title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1870, le=2100)


class RenameShowRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1870, le=2100)


# ── Single-file management (Library file lists) ────────────────────────────

class FileActionRequest(BaseModel):
    job_id: int
    rel_path: str = Field(min_length=1, max_length=500)


class FileRenameRequest(FileActionRequest):
    new_name: str = Field(min_length=1, max_length=255)


@router.post("/library/file/rename")
def rename_file(
    body: FileRenameRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    """Rename ONE file inside a job's folder (nfo/vtt/art or the video itself).
    Renaming the video updates the job record so playback keeps working."""
    from src.api.watch import _job_file, _safe_file_in_folder
    video = _job_file(db, body.job_id)
    target = _safe_file_in_folder(video.parent, body.rel_path)

    new_name = body.new_name.strip()
    if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
        raise HTTPException(status_code=422, detail="Invalid file name")
    new_path = target.parent / new_name
    if new_path.exists():
        raise HTTPException(status_code=409, detail="A file with that name already exists")

    job = db.query(DownloadJob).filter_by(id=body.job_id).first()
    target.rename(new_path)
    if job and job.file_path == str(target):
        job.file_path = str(new_path)
    if job and job.subtitle_path == str(target):
        job.subtitle_path = str(new_path)
    db.commit()
    audit(db, "library.file_renamed", username=admin.username,
          detail=f"{target.name} → {new_name}", ip=client_ip(request))
    return {"ok": True, "name": new_path.relative_to(video.parent).as_posix()}


@router.post("/library/file/delete")
def delete_file(
    body: FileActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    """Delete ONE file inside a job's folder. Deleting the video itself
    removes the whole library entry (record + sidecars), same as the card's
    Delete button."""
    from src.api.watch import _job_file, _safe_file_in_folder
    video = _job_file(db, body.job_id)
    target = _safe_file_in_folder(video.parent, body.rel_path)
    job = db.query(DownloadJob).filter_by(id=body.job_id).first()

    if target == video:
        return {**_remove_record(db, job, admin, request, delete_file=True),
                "deleted_job": True}

    target.unlink()
    if job and job.subtitle_path == str(target):
        job.subtitle_path = None
        db.commit()
    audit(db, "library.file_deleted", username=admin.username,
          detail=target.name, ip=client_ip(request))
    return {"ok": True, "deleted_job": False}


@router.delete("/library/show/{imdb_id}/season/{season}")
def delete_season(
    imdb_id: str,
    season: int,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    jobs = (
        db.query(DownloadJob)
        .filter_by(imdb_id=imdb_id, season=season, status=JobStatus.completed)
        .all()
    )
    if not jobs:
        raise HTTPException(status_code=404, detail="Season not found in library")

    from src.library.organizer import resolve_season_folder
    season_folder = resolve_season_folder(jobs)

    if season_folder:
        from src.library.organizer import delete_folder_tree, _remove_empty_dirs
        delete_folder_tree(season_folder)
        _remove_empty_dirs(season_folder.parent)
    else:
        from src.library.organizer import delete_job_file
        for job in jobs:
            if job.file_path:
                try:
                    delete_job_file(job.file_path)
                except Exception as exc:
                    _log.warning("Could not delete %s: %s", job.file_path, exc)

    deleted = len(jobs)
    for job in jobs:
        db.delete(job)
    db.commit()
    audit(db, "library.season_deleted", username=admin.username,
          detail=f"{imdb_id} S{season:02d} ({deleted} eps)", ip=client_ip(request))
    return {"ok": True, "deleted": deleted}


@router.delete("/library/show/{imdb_id}")
def delete_show(
    imdb_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    jobs = (
        db.query(DownloadJob)
        .filter_by(imdb_id=imdb_id, status=JobStatus.completed)
        .all()
    )
    if not jobs:
        raise HTTPException(status_code=404, detail="Show not found in library")

    from src.library.organizer import resolve_show_folder
    show_folder = resolve_show_folder(jobs)

    if show_folder:
        from src.library.organizer import delete_folder_tree, _remove_empty_dirs
        delete_folder_tree(show_folder)
        _remove_empty_dirs(show_folder.parent)
    else:
        from src.library.organizer import delete_job_file
        for job in jobs:
            if job.file_path:
                try:
                    delete_job_file(job.file_path)
                except Exception as exc:
                    _log.warning("Could not delete %s: %s", job.file_path, exc)

    deleted = len(jobs)
    for job in jobs:
        db.delete(job)
    db.commit()
    audit(db, "library.show_deleted", username=admin.username,
          detail=f"{imdb_id} ({deleted} eps)", ip=client_ip(request))
    return {"ok": True, "deleted": deleted}


@router.patch("/library/job/{job_id}")
def rename_job(
    job_id: int,
    body: RenameJobRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    job = db.query(DownloadJob).filter_by(id=job_id, status=JobStatus.completed).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or not completed")

    from src.library.organizer import rename_job_file
    try:
        new_path = rename_job_file(
            job,
            new_title=body.title,
            new_ep_title=body.episode_title,
            new_year=body.year,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rename failed: {exc}")

    if body.title is not None:
        job.title = body.title
    if body.episode_title is not None:
        job.episode_title = body.episode_title
    if body.year is not None:
        job.year = body.year
    job.file_path = str(new_path)
    db.commit()
    audit(db, "library.renamed", username=admin.username, detail=job.label(), ip=client_ip(request))
    return {"ok": True, "new_path": str(new_path)}


@router.patch("/library/show/{imdb_id}")
def rename_show(
    imdb_id: str,
    body: RenameShowRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    jobs = (
        db.query(DownloadJob)
        .filter_by(imdb_id=imdb_id, status=JobStatus.completed)
        .all()
    )
    if not jobs:
        raise HTTPException(status_code=404, detail="Show not found in library")

    from src.library.organizer import rename_job_file
    errors: list[str] = []
    for job in jobs:
        try:
            new_path = rename_job_file(job, new_title=body.title, new_year=body.year)
            job.file_path = str(new_path)
            job.title = body.title
            if body.year is not None:
                job.year = body.year
        except Exception as exc:
            _log.warning("Rename failed for job %s: %s", job.id, exc)
            errors.append(str(exc))
    db.commit()
    audit(db, "library.show_renamed", username=admin.username,
          detail=f"{imdb_id} → {body.title}", ip=client_ip(request))
    return {"ok": True, "errors": errors}
