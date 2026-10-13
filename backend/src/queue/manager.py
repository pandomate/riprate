"""
Async download queue.

Two clearly separated phases (surfaced to the UI as distinct statuses):

  Phase 1 — status "downloading": the Python-native, *resumable* HLS downloader
            (src/queue/hls.py) fetches the stream segments to local files. A
            browser refresh, a closed tab, a transient CDN 503, or even a
            backend restart resumes from the already-downloaded segments rather
            than starting over.
  Phase 2 — status "processing": ffmpeg turns the local segments into the final
            library MP4 — stream-copy when MP4-compatible and within
            MAX_FILE_SIZE_GB, else a size-guarded libx264 re-encode.

The waiting queue lives in the database (ordered by `position`); workers claim
the lowest-position job under a lock, so the download is driven entirely
server-side and is independent of any browser connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time

import httpx
from sqlalchemy import func
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

_log = logging.getLogger("queue")

from src.config import settings
from src.database import DownloadJob, JobStatus, SessionLocal, get_setting, set_setting
from src.library.organizer import ensure_parent, episode_path, movie_path, subtitle_sidecar_path
from src.subtitles import clean_vtt_text, has_timed_cues, language_matches, normalize_language
from src.providers.nova import _REFERER as NOVA_REFERER, _USER_AGENT as NOVA_USER_AGENT
from src.providers.base import resolve_with_registry
from src.providers import wyzie
from src.queue.hls import (
    DownloadCancelled, DownloadRequeued, HlsError, HlsResult, Progress,
    SegmentAuthError, SourceSkipped, SourceStalledError, download_hls,
)

_FFMPEG_TIME_RE  = re.compile(r"time=(\d+):(\d+):([\d.]+)")
_FFMPEG_DUR_RE   = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
_FFMPEG_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

# Codecs MP4 can hold without re-encoding
_MP4_VIDEO = {"h264", "hevc", "av1", "mpeg4"}
_MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "flac"}

# Valid libx264 -preset values (used to validate the DB/.env override)
_X264_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow", "placebo"}

# Attempts on the *same* URL (resuming) before falling through to the next URL
_URL_ATTEMPTS = 3
# Times to retry a provider's RESOLVE before giving up on it and moving to the
# next source. Without this a single transient hiccup (mirror blip, connection
# flap) abandons Nova instantly even though it would resolve on a retry.
_RESOLVE_ATTEMPTS = 3
# Fresh-link re-resolves when a stream token expires/gets rejected mid-download
_TOKEN_REFRESHES = 2
# Connection-loss watchdog — driven ENTIRELY by our own download activity; we
# NEVER ping outside hosts. If downloads are actively fetching but no data has
# arrived across ALL active downloads for this long, the connection is treated
# as down: pause everything (so we don't burn sources) and periodically retry
# by resuming, until data flows again.
_NO_PROGRESS_TIMEOUT = 45.0     # seconds of zero download progress ⇒ connection down
_NET_RETRY_INTERVAL = 30.0      # while down, re-test by resuming this often
_NET_MONITOR_TICK = 5.0         # how often the watchdog checks

# last_error substrings that mean "the network dropped" (vs a dead source) — so
# a job that fails everything during an outage is re-queued, not marked failed.
_NET_ERR_HINTS = (
    "connecterror", "connect error", "connection refused", "connection reset",
    "connection aborted", "connection error", "server disconnected",
    "timed out", "timeout", "read timeout", "connect timeout",
    "getaddrinfo", "name or service not known", "temporary failure in name resolution",
    "network is unreachable", "no route to host", "all connection attempts failed",
)
# A DASH (ffmpeg) download whose output file stops growing for this long while
# un-paused is treated as a dead source — kill it and fail over to the next
# provider instead of freezing the slot forever (the HLS path has its own
# watchdog in hls.py; this covers the ffmpeg-driven .mpd path).
_DASH_STALL_TIMEOUT = 90.0

_STREAM_REFERER = NOVA_REFERER
_STREAM_HEADERS = {"User-Agent": NOVA_USER_AGENT, "Accept": "*/*"}


class UnplayableSource(Exception):
    """The source downloaded bytes but did not decode to usable video."""

# Interrupted and paused work is always resumable. Failed workspaces follow the
# admin's retention setting and can also be reused by a retry while retained.
_RESUMABLE = (JobStatus.queued, JobStatus.downloading, JobStatus.processing,
              JobStatus.paused)

_FAILED_TEMP_CLEANUP_INTERVAL = 15 * 60


def _fmt_speed(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log.warning("Could not delete %s: %s", path, exc)


def _rmtree_bg(path: Path) -> None:
    """Delete a tree without stalling the event loop: fire-and-forget onto a
    worker thread when called from async context, inline otherwise. Deleting
    a temp dir is idempotent, so nothing needs to wait for it."""
    try:
        asyncio.get_running_loop().run_in_executor(
            None, lambda: shutil.rmtree(path, ignore_errors=True))
    except RuntimeError:   # no running loop — plain sync call site
        shutil.rmtree(path, ignore_errors=True)


class _JobControl:
    """Runtime handles for an in-flight job (pause/cancel/subprocess)."""

    def __init__(self) -> None:
        self.pause = asyncio.Event()   # set = running, cleared = paused
        self.pause.set()
        self.cancel = asyncio.Event()
        self.requeue = asyncio.Event()  # yield the slot; stop_mode decides outcome
        self.skip = asyncio.Event()     # admin "Next source": abandon current source
        self.skippable = False          # True while a source is resolving/downloading
        self.stop_mode = "requeue"      # "requeue" | "fail"
        self.phase: str = "download"   # "download" | "process"
        self.paused = False            # True while the admin has this job paused
        self.downloading = False       # True only while actively fetching stream bytes
        self.proc: asyncio.subprocess.Process | None = None


class DownloadManager:
    def __init__(self) -> None:
        self._workers: dict[int, asyncio.Task] = {}   # worker index -> task
        self._worker_job: dict[int, int] = {}         # worker index -> job id
        self._target_workers = 0                       # desired concurrency
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._controls: dict[int, _JobControl] = {}
        self._logs: dict[int, deque[str]] = {}
        self._paused_all = False   # global pause: stop starting new jobs
        self._network_ok = True    # connection healthy; False auto-pauses downloads
        self._vpn_blocked = False  # VPN not active → gate the queue
        self._vpn_paused_jobs: set[int] = set()   # jobs we paused when the VPN went inactive
        self._maintenance = False  # transient gate (e.g. VPN test)
        self._maint_jobs: set[int] = set()
        self._maint_depth = 0      # re-entrant begin/end_maintenance
        self._net_paused_jobs: set[int] = set()   # jobs WE paused on an outage
        self._net_task: asyncio.Task | None = None
        self._failed_temp_task: asyncio.Task | None = None
        self._last_progress_at = time.monotonic()  # last time ANY download got bytes
        self._net_down_at = 0.0                     # when the connection went down

    # ── Live per-job log (admin CLI-style detail) ──────────────────────────

    def log_job(self, job_id: int, msg: str) -> None:
        buf = self._logs.setdefault(job_id, deque(maxlen=60))
        buf.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def get_logs(self, job_id: int, tail: int = 14) -> list[str]:
        buf = self._logs.get(job_id)
        return list(buf)[-tail:] if buf else []

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        # A global pause survives restarts — an admin who paused the queue
        # (e.g. because the media drive is offline) must not have downloads
        # silently resume just because the backend restarted.
        self._paused_all = (get_setting("queue_paused") or "false").lower() == "true"
        if self._paused_all:
            _log.warning("[queue] starting PAUSED (queue_paused setting) — resume from the Downloads page")
        self._recover()
        n = self._configured_concurrency()
        self._target_workers = n
        for i in range(n):
            self._workers[i] = asyncio.create_task(self._worker(i))
        self._net_task = asyncio.create_task(self._network_monitor())
        self._failed_temp_task = asyncio.create_task(self._failed_temp_cleanup_loop())

    async def stop(self) -> None:
        self._target_workers = 0
        for t in list(self._workers.values()):
            t.cancel()
        self._workers.clear()
        if self._net_task is not None:
            self._net_task.cancel()
            self._net_task = None
        if self._failed_temp_task is not None:
            self._failed_temp_task.cancel()
            self._failed_temp_task = None

    def _failed_temp_retention_hours(self) -> int:
        try:
            value = int(get_setting("failed_temp_retention_hours") or "0")
        except (TypeError, ValueError):
            value = 0
        return max(0, min(87600, value))

    async def _failed_temp_cleanup_loop(self) -> None:
        """Clean expired failed workspaces at startup and while the app runs."""
        while True:
            try:
                await self.cleanup_failed_temp_files()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("[tmp] failed-workspace cleanup error: %s", exc)
            await asyncio.sleep(_FAILED_TEMP_CLEANUP_INTERVAL)

    async def cleanup_failed_temp_files(self) -> int:
        """Delete failed-job workspaces that are due under the saved policy."""
        retention_hours = self._failed_temp_retention_hours()
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=retention_hours)
        with SessionLocal() as db:
            failed = db.query(DownloadJob).filter_by(status=JobStatus.failed).all()
            due_ids = []
            for job in failed:
                if retention_hours == 0:
                    due_ids.append(job.id)
                    continue
                failed_at = job.completed_at
                if failed_at is not None and failed_at <= cutoff:
                    due_ids.append(job.id)
                    continue
                if failed_at is None:
                    path = self._tmp_dir(job.id)
                    try:
                        if datetime.utcfromtimestamp(path.stat().st_mtime) <= cutoff:
                            due_ids.append(job.id)
                    except FileNotFoundError:
                        pass

        removed = 0
        for job_id in due_ids:
            # Recheck immediately before deletion: the user may have retried
            # the job while this cleanup pass was collecting candidates.
            with SessionLocal() as db:
                job = db.query(DownloadJob).filter_by(id=job_id).first()
                if job is None or job.status != JobStatus.failed:
                    continue
            path = self._tmp_dir(job_id)
            if not path.exists():
                continue
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            self._logs.pop(job_id, None)
            removed += 1
        if removed:
            _log.info("[tmp] removed %d expired failed download workspace(s)", removed)
        return removed

    @property
    def network_ok(self) -> bool:
        return self._network_ok

    def _download_active(self) -> bool:
        """True if at least one job is actively fetching stream bytes right now
        (past the resolve step, not paused). This is our ONLY connectivity
        signal — no outside host is ever contacted."""
        return any(c.downloading and c.pause.is_set() and not c.cancel.is_set()
                   for c in self._controls.values())

    async def _network_monitor(self) -> None:
        """Connection-loss watchdog, driven purely by download activity: if
        something is actively downloading but NO bytes have arrived across all
        downloads for _NO_PROGRESS_TIMEOUT, treat the connection as down and
        pause everything (rather than churning through every source and failing
        jobs). While down, periodically resume to re-test; the moment bytes flow
        again we stay resumed. A single dead source still fails over on its own —
        this only trips when nothing is moving at all."""
        await asyncio.sleep(10)   # let the app settle first
        while True:
            await asyncio.sleep(_NET_MONITOR_TICK)
            now = time.monotonic()
            if self._network_ok:
                # Only meaningful while a real download is in flight; otherwise
                # keep the clock fresh so an idle queue never looks "stalled".
                if not self._download_active():
                    self._last_progress_at = now
                elif now - self._last_progress_at > _NO_PROGRESS_TIMEOUT:
                    self._network_ok = False
                    self._net_down_at = now
                    _log.warning("[net] no download progress for %ss — connection looks down, pausing",
                                 int(_NO_PROGRESS_TIMEOUT))
                    for jid in list(self._controls.keys()):
                        if await self.pause(jid):
                            self._net_paused_jobs.add(jid)
                            self.log_job(jid, "Connection lost (no data) — paused; will auto-retry")
            else:
                # Down: every retry interval, resume what we paused (and let
                # gated queued jobs start) to re-test. If data flows, the next
                # tick sees progress and we stay up; if not, we pause again.
                if now - self._net_down_at >= _NET_RETRY_INTERVAL:
                    self._net_down_at = now
                    self._network_ok = True
                    self._last_progress_at = now
                    if not self._paused_all:
                        for jid in list(self._net_paused_jobs):
                            self.log_job(jid, "Retrying — checking if the connection is back…")
                            await self.resume(jid)
                        self._wake.set()
                    self._net_paused_jobs.clear()

    def _configured_concurrency(self) -> int:
        from src.database import get_setting
        try:
            v = int(get_setting("max_concurrent_downloads") or settings.max_concurrent_downloads)
        except (ValueError, TypeError):
            v = settings.max_concurrent_downloads
        return max(1, min(10, v))

    async def set_concurrency(self, n: int) -> int:
        """Resize the worker pool live.

        Increase → new workers immediately pull the next queued jobs into the
        freed slots. Decrease → the excess running downloads are sent back to
        the FRONT of the queue (keeping progress), and those workers retire once
        their job has yielded.
        """
        n = max(1, min(10, n))
        old = self._target_workers
        self._target_workers = n
        if n > old:
            for i in range(old, n):
                self._workers[i] = asyncio.create_task(self._worker(i))
            self._wake.set()
        elif n < old:
            for i in range(n, old):
                jid = self._worker_job.get(i)
                if jid is not None:
                    self._move_to_front(jid)
                    await self.requeue(jid)   # keeps progress; worker will retire
            self._wake.set()
        return n

    def _move_to_front(self, job_id: int) -> None:
        with SessionLocal() as db:
            min_pos = db.query(func.min(DownloadJob.position)).filter(
                DownloadJob.status.in_([
                    JobStatus.queued, JobStatus.downloading,
                    JobStatus.processing, JobStatus.paused,
                ])
            ).scalar()
            db.query(DownloadJob).filter_by(id=job_id).update(
                {"position": (min_pos if min_pos is not None else 0) - 1}
            )
            db.commit()

    def _recover(self) -> None:
        """Reset jobs interrupted by a restart WITHOUT discarding their partial
        downloads, so they resume rather than restart. Failed jobs and their
        temp dirs are removed on every application restart."""
        with SessionLocal() as db:
            interrupted = (
                db.query(DownloadJob)
                .filter(DownloadJob.status.in_([
                    JobStatus.downloading, JobStatus.processing, JobStatus.paused
                ]))
                .all()
            )
            for job in interrupted:
                job.status = JobStatus.queued
                job.started_at = None
                job.download_bitrate = None
            db.commit()

            failed = db.query(DownloadJob).filter_by(status=JobStatus.failed).all()
            for job in failed:
                db.delete(job)
            if failed:
                db.commit()
                _log.info("[recover] removed %d failed job(s) on restart", len(failed))

            completed = db.query(DownloadJob).filter_by(status=JobStatus.completed).all()
            with_path = [j for j in completed if j.file_path]
            missing = [j for j in with_path if not Path(j.file_path).exists()]

            # SAFETY VALVE: if (nearly) EVERY completed file is gone at once,
            # the media drive is almost certainly unmounted/offline — NOT
            # deleted. Re-queuing hundreds of downloads against a missing
            # mount re-downloads the whole library onto the wrong disk (this
            # actually happened when a USB media drive didn't re-attach after
            # a host reboot). Refuse the mass re-queue; single missing files
            # are still re-queued as before.
            if len(with_path) >= 10 and len(missing) > len(with_path) * 0.5:
                _log.error(
                    "[recover] %d of %d completed files are missing — media dir "
                    "'%s' looks unmounted. NOT re-queuing; fix the mount and "
                    "restart.", len(missing), len(with_path), settings.media_dir)
                # …and PAUSE the whole queue: the waiting jobs would otherwise
                # start downloading onto whatever is at the mountpoint (the
                # bare root disk). Resume from the Downloads page once the
                # drive is back.
                if not self._paused_all:
                    self._paused_all = True
                    try:
                        from src.database import set_setting
                        set_setting("queue_paused", "true")
                    except Exception:
                        pass
                    _log.error("[recover] queue force-paused until the media "
                               "drive is back (resume from the Downloads page)")
            # Filesystem reconciliation handles absent files. An external
            # deletion must not silently trigger a replacement download.
            # The library may contain imported MKV files. Never delete media
            # while recovering queue state; reconciliation only changes records.
            db.commit()

            # SELF-HEAL (inverse of the re-queue): a job stuck "queued" whose
            # finished file is on disk was flipped while its drive was offline
            # — the drive is back, so it's simply complete. Never re-download.
            healed = 0
            for job in db.query(DownloadJob).filter_by(status=JobStatus.queued).all():
                if not job.file_path:
                    continue
                mp4 = Path(job.file_path)
                if mp4.suffix == ".mp4" and mp4.exists() and mp4.stat().st_size > 0:
                    job.status = JobStatus.completed
                    job.progress = 100.0
                    healed += 1
            if healed:
                _log.warning("[recover] healed %d queued job(s) whose finished file exists", healed)
            db.commit()

            resumable_ids = {
                j.id for j in db.query(DownloadJob)
                .filter(DownloadJob.status.in_(_RESUMABLE)).all()
            }
        root = self._tmp_root()
        root.mkdir(parents=True, exist_ok=True)

        # One-time migration: the workspace used to live ON the media drive
        # (.riprate-tmp). Bring resumable partials over to the fast disk so
        # they keep resuming; everything else there is stale — drop it.
        old_root = Path(settings.media_dir) / ".riprate-tmp"
        try:
            same = old_root.resolve() == root.resolve()
        except OSError:
            same = False
        if old_root.is_dir() and not same:
            moved = 0
            for child in old_root.iterdir():
                dest = root / child.name
                keep = (child.is_dir() and child.name.isdigit()
                        and int(child.name) in resumable_ids and not dest.exists())
                if keep:
                    try:
                        shutil.move(str(child), str(dest))
                        moved += 1
                        continue
                    except OSError as exc:
                        _log.warning("[recover] tmp migration of %s failed: %s", child.name, exc)
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else _safe_unlink(child)
            try:
                old_root.rmdir()
            except OSError:
                pass
            if moved:
                _log.warning("[recover] moved %d resumable partial download(s) "
                             "off the media drive to %s", moved, root)

        if root.exists():
            for child in root.iterdir():
                if child.is_dir() and child.name.isdigit():
                    if int(child.name) not in resumable_ids:
                        shutil.rmtree(child, ignore_errors=True)
                else:
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else _safe_unlink(child)

    # ── Public API ─────────────────────────────────────────────────────────

    async def enqueue(self, job_id: int) -> None:
        self._wake.set()

    async def pause(self, job_id: int) -> bool:
        ctrl = self._controls.get(job_id)
        if ctrl is None:
            return False
        if ctrl.phase == "download":
            ctrl.pause.clear()
            ctrl.paused = True
            self.log_job(job_id, "Paused by admin")
            self._update(job_id, status=JobStatus.paused, download_bitrate="Paused")
            return True
        if ctrl.proc and ctrl.proc.returncode is None:
            ctrl.proc.send_signal(signal.SIGSTOP)
            ctrl.paused = True
            self.log_job(job_id, "Paused by admin")
            self._update(job_id, status=JobStatus.paused, download_bitrate="Paused")
            return True
        return False

    async def resume(self, job_id: int) -> bool:
        ctrl = self._controls.get(job_id)
        if ctrl is None:
            return False
        if ctrl.phase == "download":
            ctrl.pause.set()
            ctrl.paused = False
            self.log_job(job_id, "Resumed")
            self._update(job_id, status=JobStatus.downloading, download_bitrate=None)
            return True
        if ctrl.proc and ctrl.proc.returncode is None:
            ctrl.proc.send_signal(signal.SIGCONT)
            ctrl.paused = False
            self.log_job(job_id, "Resumed")
            self._update(job_id, status=JobStatus.processing)
            return True
        return False

    async def cancel(self, job_id: int) -> bool:
        """Cancel a running or queued job and delete its partial files/folders."""
        ctrl = self._controls.get(job_id)
        if ctrl is not None:
            ctrl.cancel.set()
            ctrl.pause.set()   # unblock a paused download so it can exit
            if ctrl.proc and ctrl.proc.returncode is None:
                ctrl.proc.send_signal(signal.SIGCONT)
                ctrl.proc.terminate()
                try:
                    await asyncio.wait_for(ctrl.proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    ctrl.proc.kill()
            self._purge_job_files(job_id)
            self.log_job(job_id, "Cancelled — partial files deleted")
            self._mark_cancelled(job_id)
            return True
        # Queued but not yet running — just mark failed and purge any partial
        with SessionLocal() as db:
            job = db.query(DownloadJob).filter_by(id=job_id).first()
            if job and job.status == JobStatus.queued:
                self._purge_job_files(job_id)
                job.status = JobStatus.failed
                job.error_message = "Cancelled by user"
                job.completed_at = datetime.utcnow()
                db.commit()
                return True
        return False

    # ── Slots / promote / swap (run-now) ────────────────────────────────────

    @property
    def max_slots(self) -> int:
        return self._target_workers

    @property
    def active_slots(self) -> int:
        """Worker slots currently occupied by a running/paused job."""
        return len(self._controls)

    async def requeue(self, job_id: int) -> bool:
        """Yield a running job's worker slot, keeping its partial download.

        The job goes back to `queued` (with its progress preserved) so it can be
        resumed later; the freed slot lets another job start.
        """
        return await self._stop_running(job_id, "requeue")

    async def fail(self, job_id: int) -> bool:
        """Mark a job failed and apply the failed-temp retention policy."""
        if await self._stop_running(job_id, "fail"):
            return True
        # Not running — flip its status directly and apply retention.
        with SessionLocal() as db:
            job = db.query(DownloadJob).filter_by(id=job_id).first()
            if not job:
                return False
            job.status = JobStatus.failed
            job.error_message = "Marked failed by admin"
            job.download_bitrate = None
            job.completed_at = datetime.utcnow()
            db.commit()
        self._apply_failed_temp_policy(job_id)
        return True

    async def _stop_running(self, job_id: int, mode: str) -> bool:
        ctrl = self._controls.get(job_id)
        if ctrl is None:
            return False
        ctrl.stop_mode = mode
        ctrl.requeue.set()
        ctrl.pause.set()   # unblock a paused download so it can exit
        if ctrl.proc and ctrl.proc.returncode is None:
            ctrl.proc.send_signal(signal.SIGCONT)
            ctrl.proc.terminate()
        return True

    def promote(self, job_id: int) -> None:
        """Put a queued job ahead of EVERY other pending job — including jobs
        currently running (which, when swapped out, return to the queue at their
        old low position). Without this a swapped-out job would just get
        re-claimed instead of the promoted one."""
        with SessionLocal() as db:
            min_pos = db.query(func.min(DownloadJob.position)).filter(
                DownloadJob.status.in_([
                    JobStatus.queued, JobStatus.downloading,
                    JobStatus.processing, JobStatus.paused,
                ])
            ).scalar()
            job = db.query(DownloadJob).filter_by(id=job_id).first()
            if job and job.status == JobStatus.queued:
                job.position = (min_pos if min_pos is not None else 0) - 1
                db.commit()
        self._wake.set()

    async def swap(self, queued_id: int, active_id: int) -> bool:
        """True swap: the queued job takes the active job's queue position and
        the active job takes the queued job's position, then the active job
        yields its worker slot (keeping progress). The freed worker then claims
        the promoted (now lower-positioned) queued job."""
        with SessionLocal() as db:
            q = db.query(DownloadJob).filter_by(id=queued_id, status=JobStatus.queued).first()
            a = db.query(DownloadJob).filter_by(id=active_id).first()
            if q is None or a is None:
                return False
            # A processing (mid-encode) job can't yield without discarding the
            # encode, so it is not swappable — only downloading/paused jobs are.
            if a.status not in (JobStatus.downloading, JobStatus.paused):
                return False
            q.position, a.position = a.position, q.position
            db.commit()
        ok = await self.requeue(active_id)
        self._wake.set()
        return ok

    # ── Global pause / resume ───────────────────────────────────────────────

    @property
    def paused_all(self) -> bool:
        return self._paused_all

    async def pause_all(self) -> int:
        """Pause every running job and stop starting new ones. Persists, so a
        backend restart comes back up still paused."""
        self._paused_all = True
        set_setting("queue_paused", "true")
        n = 0
        for jid in list(self._controls.keys()):
            if await self.pause(jid):
                n += 1
        return n

    async def resume_all(self) -> int:
        """Resume every paused job and allow the queue to start jobs again."""
        self._paused_all = False
        set_setting("queue_paused", "false")
        n = 0
        with SessionLocal() as db:
            paused_ids = [
                j.id for j in db.query(DownloadJob)
                .filter_by(status=JobStatus.paused).all()
            ]
        for jid in paused_ids:
            if await self.resume(jid):
                n += 1
        self._wake.set()
        return n

    # ── VPN coordination ────────────────────────────────────────────────────

    @property
    def vpn_blocked(self) -> bool:
        return self._vpn_blocked

    async def apply_vpn_gate(self, blocked: bool) -> None:
        """Gate the queue on VPN state: when blocked (VPN not active) pause
        running downloads and stop new ones; resume them once it's back."""
        was = self._vpn_blocked
        self._vpn_blocked = blocked
        if blocked and not was:
            for jid in list(self._controls.keys()):
                if jid in self._maint_jobs:
                    continue
                if await self.pause(jid):
                    self._vpn_paused_jobs.add(jid)
                    self.log_job(jid, "Paused — VPN is not active")
        elif not blocked and was:
            for jid in list(self._vpn_paused_jobs):
                self.log_job(jid, "VPN active — resuming")
                await self.resume(jid)
            self._vpn_paused_jobs.clear()
            self._wake.set()

    async def begin_maintenance(self) -> None:
        """Transiently pause the queue + running downloads (e.g. during a VPN
        test). Re-entrant; reversed by end_maintenance. Not persisted."""
        self._maint_depth += 1
        if self._maint_depth > 1:
            return
        self._maintenance = True
        for jid in list(self._controls.keys()):
            if await self.pause(jid):
                self._maint_jobs.add(jid)
                self.log_job(jid, "Paused while the VPN connection is tested…")

    async def end_maintenance(self) -> None:
        """Resume what begin_maintenance paused and re-open the queue."""
        if self._maint_depth > 0:
            self._maint_depth -= 1
        if self._maint_depth > 0:
            return
        self._maintenance = False
        for jid in list(self._maint_jobs):
            self.log_job(jid, "VPN test finished — resuming")
            await self.resume(jid)
        self._maint_jobs.clear()
        self._wake.set()

    def skip_source(self, job_id: int) -> bool:
        """Admin "Next source": abandon the source this job is currently
        downloading from and move on to the next provider (only valid while a
        source download is in flight — not during MP4 processing)."""
        ctrl = self._controls.get(job_id)
        if ctrl is None or not ctrl.skippable:
            return False
        ctrl.skip.set()
        # A DASH download runs inside ffmpeg — poke the process so the skip
        # takes effect immediately instead of at the next read.
        if ctrl.proc is not None:
            try:
                ctrl.proc.terminate()
            except ProcessLookupError:
                pass
        self.log_job(job_id, "Admin skipped this source — trying the next one…")
        return True

    async def _skippable_resolve(self, job_id: int, ctrl: _JobControl,
                                 key: str, job: DownloadJob):
        """Resolve via one provider, but bail out early if the admin presses
        "Next source" (or cancels) while the resolve is still running.

        Racing the resolve against the skip event means a slow provider call
        does not become a dead, un-interruptible slot."""
        from src.providers.base import resolve_one
        resolve_task = asyncio.ensure_future(
            resolve_one(key, job.imdb_id, job.media_type, job.season, job.episode,
                        log=lambda m: self.log_job(job_id, m)))

        async def watch() -> None:
            while True:
                await asyncio.sleep(0.4)
                if ctrl.cancel.is_set():
                    raise DownloadCancelled()
                if ctrl.skip.is_set():
                    raise SourceSkipped()

        watch_task = asyncio.ensure_future(watch())
        try:
            done, _ = await asyncio.wait(
                {resolve_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
            if resolve_task in done:
                return resolve_task.result()
            # The watchdog won the race — re-raise its skip/cancel.
            watch_task.result()
        finally:
            watch_task.cancel()
            if not resolve_task.done():
                resolve_task.cancel()
            await asyncio.gather(resolve_task, watch_task, return_exceptions=True)

    def cleanup_deleted(self, job_id: int) -> None:
        """Remove a job's temp working dir + logs only.

        This never touches the finished library MP4 — deleting a completed job
        from the Downloads page should keep the media and just drop the record.
        """
        _rmtree_bg(self._tmp_dir(job_id))
        self._logs.pop(job_id, None)

    def dest_path(self, job: DownloadJob) -> Path:
        """Public: the library path this job maps to."""
        return self._dest_path(job)

    def purge_incomplete_output(self, job: DownloadJob) -> None:
        """Remove leftover/partial output for a job whose MP4 isn't present
        (e.g. a stale .mkv from an old run, a 0-byte .mp4), plus empty dirs.

        Only called when the final MP4 does not exist, so it can never delete a
        completed download.
        """
        dest = self._dest_path(job)
        # Absolute safety: if a real MP4 exists at either the recorded or the
        # computed path, this item is a completed download — do nothing.
        for mp4 in (Path(job.file_path) if job.file_path else None, dest):
            if mp4 and mp4.suffix == ".mp4" and mp4.exists() and mp4.stat().st_size > 0:
                return

        candidates: set[Path] = set()
        bases = [dest] + ([Path(job.file_path)] if job.file_path else [])
        for base in bases:
            stem = base.with_suffix("")
            candidates.update({
                base, base.with_suffix(".mkv"), base.with_suffix(".nfo"),
                Path(str(stem) + ".en.vtt"), Path(str(stem) + ".vtt"),
            })
            # Subtitle language became configurable; remove any language
            # sidecar left by a failed attempt, without touching the video.
            candidates.update(base.parent.glob(f"{stem.name}.*.vtt"))
        if job.media_type == "movie":
            # A movie folder is exclusively this job's — early-written
            # metadata assets go too when no video ever landed.
            candidates.update({dest.parent / "movie.nfo",
                               dest.parent / "poster.jpg",
                               dest.parent / "fanart.jpg"})
        parents: set[Path] = set()
        for p in candidates:
            if p.exists() and p.suffix == ".mp4" and p.stat().st_size > 0:
                continue  # never delete a real MP4
            if p.exists():
                _safe_unlink(p)
            parents.add(p.parent)
        from src.library.organizer import _remove_empty_dirs
        for parent in parents:
            _remove_empty_dirs(parent)

    # ── Worker loop ────────────────────────────────────────────────────────

    async def _worker(self, index: int) -> None:
        while True:
            if index >= self._target_workers:
                self._workers.pop(index, None)   # retired by a concurrency decrease
                return
            job_id = await self._claim_next()
            if job_id is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                continue
            self._worker_job[index] = job_id
            try:
                await self._process(job_id)
            except Exception as exc:
                _log.error("Worker unhandled error on job %s: %s", job_id, exc, exc_info=True)
                self.log_job(job_id, f"Error: {exc}")
                self._mark_failed(job_id, str(exc))
            finally:
                self._worker_job.pop(index, None)
                self._controls.pop(job_id, None)

    async def _claim_next(self) -> int | None:
        """Atomically claim the next queued job (ordered by position)."""
        from src.library.sync import library_sync
        if library_sync.storage_error:
            return None
        if self._paused_all or not self._network_ok or self._vpn_blocked or self._maintenance:
            return None
        async with self._claim_lock:
            with SessionLocal() as db:
                job = (
                    db.query(DownloadJob)
                    .filter_by(status=JobStatus.queued)
                    .order_by(DownloadJob.position, DownloadJob.added_at)
                    .first()
                )
                if job is None:
                    return None
                job.status = JobStatus.downloading
                job.started_at = datetime.utcnow()
                job.error_message = None
                db.commit()
                return job.id

    # ── temp paths ─────────────────────────────────────────────────────────

    def _tmp_root(self) -> Path:
        return Path(settings.tmp_dir)

    def _tmp_dir(self, job_id: int) -> Path:
        return self._tmp_root() / str(job_id)

    def _purge_job_files(self, job_id: int) -> None:
        """Remove ONLY the temp working dir for a job.

        An in-progress download writes to the temp dir; the finished MP4 is
        moved into the library only on success. So a cancel/failure must never
        touch the library — `job.file_path`, when set, points to a *previously
        completed* file that we must keep.
        """
        _rmtree_bg(self._tmp_dir(job_id))

    async def _purge_job_files_async(self, job_id: int) -> None:
        """Await deletion before a terminal failure can be retried.

        The old fire-and-forget cleanup could race a retry for the same job id:
        a new workspace was created while the previous rmtree was still
        walking thousands of HLS segments.  Terminal paths use this awaited
        variant; cancellation/deletion paths can still use the non-blocking
        helper.
        """
        await asyncio.to_thread(shutil.rmtree, self._tmp_dir(job_id), ignore_errors=True)
        self._logs.pop(job_id, None)

    def _apply_failed_temp_policy(self, job_id: int) -> None:
        """Delete immediately or retain this failed job's complete workspace."""
        hours = self._failed_temp_retention_hours()
        if hours == 0:
            self._purge_job_files(job_id)
            self.log_job(job_id, "Failure cleanup: temporary files deleted")
        else:
            unit = "hour" if hours == 1 else "hours"
            self.log_job(job_id, f"Failure cleanup: temporary files kept for {hours} {unit}")

    # ── Job processing ─────────────────────────────────────────────────────

    async def _process(self, job_id: int) -> None:
        job = self._get_job(job_id)
        if job is None:
            return

        ctrl = _JobControl()
        self._controls[job_id] = ctrl
        self.log_job(job_id, f"Starting: {job.label()}")
        self.log_job(job_id, "Resolving stream source…")

        from src.providers.base import enabled_provider_meta, resolve_one
        providers = enabled_provider_meta()
        if not providers:
            self._mark_failed(job_id, "No stream providers are enabled — enable one in Settings.")
            return

        work_dir = self._tmp_dir(job_id)
        dest = self._dest_path(job)
        early_vtt: Path | None = None
        prefetched = False

        # ── Phase 1: download segments (resumable) ─────────────────────────
        last_write = 0.0

        def on_progress(p: Progress) -> None:
            nonlocal last_write
            now = time.monotonic()
            self._last_progress_at = now   # bytes are flowing → connection alive
            if now - last_write < 1.0:
                return
            last_write = now
            pct = min(99.0, p.done_segments / p.total_segments * 100) if p.total_segments else 0.0
            self._update(
                job_id,
                progress=round(pct, 1),
                downloaded_bytes=p.bytes_done,
                total_bytes=p.est_total_bytes or None,
                download_bitrate=_fmt_speed(p.speed_bps) if p.speed_bps else None,
            )

        # ── Provider failover: a source that RESOLVES but whose stream fails
        # to DOWNLOAD (403s, stalls, or the admin's "Next source" button) falls
        # through to the next enabled provider until all are exhausted. ──────
        result = None
        last_error = "no provider produced a stream"
        for pmeta in providers:
            if ctrl.cancel.is_set():
                self._finish_cancelled(job_id)
                return
            ctrl.skip.clear()
            # "Next source" is live from the moment we start trying a provider,
            # so a slow/hanging resolve can be abandoned too — not just an
            # in-flight download.
            ctrl.skippable = True
            self._update(job_id, current_source=pmeta["label"])

            # Resolve with a few retries so a transient hiccup (mirror blip,
            # connection flap) doesn't abandon an otherwise-working source. Only
            # a *permanent* answer (title genuinely unavailable or missing its
            # TMDB mapping) or the admin skip stops the retries early.
            sources = None
            for r_try in range(1, _RESOLVE_ATTEMPTS + 1):
                if ctrl.cancel.is_set():
                    ctrl.skippable = False
                    self._finish_cancelled(job_id)
                    return
                if ctrl.skip.is_set() or not self._network_ok:
                    break
                self.log_job(job_id, f"Trying {pmeta['label']}…"
                             + (f" (attempt {r_try}/{_RESOLVE_ATTEMPTS})" if r_try > 1 else ""))
                try:
                    sources = await self._skippable_resolve(job_id, ctrl, pmeta["key"], job)
                    break
                except DownloadCancelled:
                    ctrl.skippable = False
                    self._finish_cancelled(job_id)
                    return
                except SourceSkipped:
                    break
                except Exception as exc:
                    last_error = f"{pmeta['label']}: {exc}"
                    m = str(exc).lower()
                    permanent = any(k in m for k in
                                    ("not available", "isn't available", "no tmdb"))
                    if permanent or r_try >= _RESOLVE_ATTEMPTS:
                        self.log_job(job_id, f"{pmeta['label']} could not resolve: {exc}")
                        break
                    self.log_job(job_id, f"{pmeta['label']} resolve attempt {r_try} failed — retrying…")
                    await asyncio.sleep(2.0 * r_try)

            if ctrl.skip.is_set():
                ctrl.skippable = False
                last_error = f"{pmeta['label']}: skipped by admin"
                self.log_job(job_id, f"{pmeta['label']} skipped — trying the next source…")
                continue
            urls = (sources.urls if sources else None) or []
            if not urls:
                ctrl.skippable = False
                continue
            self.log_job(
                job_id,
                f"{pmeta['label']} resolved {len(urls)} stream URL(s)"
                + (f", {len(sources.subtitles)} subtitle track(s)" if sources.subtitles else ""))

            # Metadata + subtitles up front (once): NFO/artwork and the .vtt
            # sidecar land in the library folder as soon as ANY source
            # resolves — they don't wait for the video.
            if not prefetched:
                early_vtt = await self._prefetch_assets(job, dest, sources, work_dir)
                prefetched = True

            self._update(job_id, progress=0.0, download_bitrate=None)
            source_failed = False
            token_refreshes = 0
            try:
                while result is None and not source_failed:
                    auth_rejected = False
                    for url_idx, url in enumerate(urls):
                        if ctrl.cancel.is_set():
                            self._finish_cancelled(job_id)
                            return
                        # Entering the byte-fetch stage: mark it so the
                        # connection watchdog knows a real download is live, and
                        # start its no-progress clock fresh.
                        ctrl.downloading = True
                        self._last_progress_at = time.monotonic()
                        self._update(job_id, source_url=url)
                        self.log_job(job_id, f"Trying {pmeta['label']} stream {url_idx + 1}/{len(urls)}…")
                        for attempt in range(1, _URL_ATTEMPTS + 1):
                            if ctrl.cancel.is_set():
                                self._finish_cancelled(job_id)
                                return
                            try:
                                candidate: HlsResult
                                if ".mpd" in url.lower().split("?")[0]:
                                    candidate = await self._download_dash(
                                        url, work_dir, sources.referer or _STREAM_REFERER,
                                        job_id, ctrl)
                                else:
                                    candidate = await download_hls(
                                        url, work_dir,
                                        prefer_height=self._preferred_height(),
                                        audio_language=self._original_audio_language(job),
                                        subtitle_language=self._subtitle_language(),
                                        fetch_subtitles=early_vtt is None,
                                        connections=self._segment_connections(),
                                        referer=sources.referer or _STREAM_REFERER,
                                        pause=ctrl.pause,
                                        cancel=ctrl.cancel,
                                        on_progress=on_progress,
                                        log=lambda m: self.log_job(job_id, m),
                                        yield_ev=ctrl.requeue,
                                        skip_ev=ctrl.skip,
                                    )
                                if not await self._validate_playable_source(candidate.video_path):
                                    await self._reset_stream_attempt(work_dir)
                                    raise UnplayableSource(
                                        "download completed but no video frame could be decoded"
                                    )
                                result = candidate
                                break
                            except DownloadCancelled:
                                self._finish_cancelled(job_id)
                                return
                            except DownloadRequeued:
                                self._finish_yield(job_id, ctrl.stop_mode)
                                return
                            except SourceSkipped:
                                last_error = f"{pmeta['label']}: skipped by admin"
                                source_failed = True
                                break
                            except SourceStalledError as exc:
                                last_error = f"{pmeta['label']}: {exc}"
                                self.log_job(job_id, f"{pmeta['label']} stalled ({exc})")
                                source_failed = True
                                break
                            except SegmentAuthError as exc:
                                # Token expired / tier gated — backoff can't fix
                                # authorization; go get a fresh link instead.
                                last_error = f"{pmeta['label']}: {exc}"
                                self.log_job(job_id, f"Stream link rejected ({exc})")
                                auth_rejected = True
                                break
                            except UnplayableSource as exc:
                                last_error = f"{pmeta['label']}: {exc}"
                                self.log_job(job_id, f"Rejected unusable stream: {exc}")
                                # Do not retry a source that consistently has a
                                # duration but contains no decodable frames.
                                break
                            except Exception as exc:
                                last_error = f"{pmeta['label']}: {exc}"
                                self.log_job(job_id, f"Attempt {attempt} failed: {exc}")
                                if attempt < _URL_ATTEMPTS:
                                    self.log_job(job_id, "Retrying same source (resuming)…")
                                    await asyncio.sleep(3.0 * attempt)
                        if result is not None or auth_rejected or source_failed:
                            break
                        if url_idx < len(urls) - 1:
                            self.log_job(job_id, "Trying this source's alternate mirror…")
                    if result is not None or source_failed:
                        break
                    if auth_rejected and token_refreshes < _TOKEN_REFRESHES:
                        token_refreshes += 1
                        self.log_job(
                            job_id,
                            f"Getting a fresh {pmeta['label']} link "
                            f"({token_refreshes}/{_TOKEN_REFRESHES})…")
                        try:
                            sources = await resolve_one(
                                pmeta["key"], job.imdb_id, job.media_type,
                                job.season, job.episode,
                                log=lambda m: self.log_job(job_id, m))
                            urls = sources.urls or []
                            if urls:
                                continue
                            last_error = f"{pmeta['label']}: re-resolve returned no streams"
                        except Exception as exc:
                            last_error = f"{pmeta['label']}: re-resolve failed: {exc}"
                    break   # this provider is out of options
            finally:
                ctrl.skippable = False
                ctrl.downloading = False
            if result is not None:
                break
            self.log_job(job_id, f"{pmeta['label']} didn't work out — trying the next source…")

        if result is None:
            self._update(job_id, current_source=None)
            # Every source struck out. If the errors we actually hit look like a
            # dropped connection (not a dead title), don't burn the job as failed
            # — put it back on the queue to retry once we're reconnected. (No
            # host is pinged; we judge purely by the errors the download raised.)
            if not ctrl.cancel.is_set() and any(h in last_error.lower() for h in _NET_ERR_HINTS):
                self._network_ok = False
                self._net_down_at = time.monotonic()   # monitor retries after the interval
                self.log_job(job_id, "Connection appears down — will retry when reconnected")
                self._update(job_id, status=JobStatus.queued, started_at=None, download_bitrate=None)
                self._wake.set()
                return
            self.log_job(job_id, "All sources failed")
            self._mark_failed(job_id, f"All sources failed. Last error: {last_error}")
            return

        # Keep the preferred subtitles selected up front for both playback
        # and muxing. HLS subtitles are a fallback when no sidecar was fetched.
        if early_vtt is not None:
            result.subtitle_path = early_vtt
        elif result.subtitle_path is not None:
            # Stream carried its own subs — keep the sidecar in sync with them
            sidecar = subtitle_sidecar_path(dest, self._subtitle_language())
            try:
                shutil.copyfile(result.subtitle_path, sidecar)
                self._update(job_id, subtitle_path=str(sidecar))
            except OSError:
                pass

        # ── Phase 2: local segments → library MP4 ──────────────────────────
        ctrl.phase = "process"
        self.log_job(job_id, f"Download complete ({result.total_bytes // (1024*1024)} MB). Processing to MP4…")
        self._update(
            job_id,
            status=JobStatus.processing,
            progress=0.0,
            downloaded_bytes=result.total_bytes,
            total_bytes=result.total_bytes,
            download_bitrate="Preparing…",
        )

        tmp_out = work_dir / "out.mp4"
        from src.subtitle_sync import align_subtitles, auto_sync_enabled
        if result.subtitle_path is not None and auto_sync_enabled():
            aligned = work_dir / "aligned-subs.vtt"
            try:
                aligned_ok = await align_subtitles(
                    result.audio_path or result.video_path, result.subtitle_path, aligned,
                    log=lambda m: self.log_job(job_id, m),
                    cancelled=lambda: ctrl.cancel.is_set() or ctrl.requeue.is_set(),
                )
                if aligned_ok:
                    result.subtitle_path = aligned
                    sidecar = subtitle_sidecar_path(dest, self._subtitle_language())
                    from src.library.organizer import _atomic_write_text
                    _atomic_write_text(sidecar, aligned.read_text(encoding="utf-8"))
                    self._update(job_id, subtitle_path=str(sidecar))
            except Exception as exc:
                self.log_job(job_id, f"Subtitle alignment skipped ({type(exc).__name__})")
        if ctrl.requeue.is_set():
            self._finish_yield(job_id, ctrl.stop_mode)
            return
        if ctrl.cancel.is_set():
            self._finish_cancelled(job_id)
            return
        _safe_unlink(tmp_out)
        ok, err = await self._to_mp4(result, tmp_out, job_id, ctrl)

        if ctrl.requeue.is_set():
            self._finish_yield(job_id, ctrl.stop_mode)
            return
        if ctrl.cancel.is_set():
            self._finish_cancelled(job_id)
            return
        if not ok:
            self.log_job(job_id, f"Processing failed: {err[:120]}")
            self._mark_failed(job_id, f"MP4 processing failed: {err}")
            return
        if not tmp_out.exists() or tmp_out.stat().st_size == 0:
            self._mark_failed(job_id, "MP4 file missing or empty after processing")
            return
        if not await self._validate_playable_source(tmp_out):
            self._mark_failed(job_id, "MP4 failed playable-video validation")
            return

        ensure_parent(dest)
        # In a thread: cross-mount moves copy the whole file, and even a
        # same-device rename on a slow disk must not stall the other slots
        await asyncio.to_thread(shutil.move, str(tmp_out), str(dest))
        # Remove any stale .mkv sibling (e.g. left by an older pipeline version)
        _safe_unlink(dest.with_suffix(".mkv"))
        await self._write_metadata_assets(job, dest)
        final_size = dest.stat().st_size
        self.log_job(job_id, f"Done → {dest.name} ({final_size // (1024*1024)} MB)")
        self._update(
            job_id,
            status=JobStatus.completed,
            file_path=str(dest),
            progress=100.0,
            completed_at=datetime.utcnow(),
            download_bitrate=None,
            current_source=None,
            downloaded_bytes=final_size,
            total_bytes=final_size,
        )
        # Success — clear the working dir (threaded: hundreds of segment
        # files on a slow disk would otherwise freeze the other slots)
        await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)

    async def _validate_playable_source(self, path: Path) -> bool:
        """Require a real video stream and one successfully decoded frame.

        A manifest can advertise a duration while its segments contain no
        usable media.  ffprobe alone is not sufficient for that case, so this
        also asks ffmpeg to decode the first video frame before a source is
        accepted.
        """
        probe = await _ffprobe(path)
        video = next((s for s in probe.get("streams", [])
                      if s.get("codec_type") == "video"), None)
        if not video:
            return False
        duration = probe.get("format", {}).get("duration")
        try:
            if duration is not None and float(duration) <= 0.5:
                return False
        except (TypeError, ValueError):
            pass

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", str(path),
            "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return False
        if proc.returncode != 0:
            _log.info("[queue] source decode failed: %s", stderr.decode(errors="replace")[-300:])
            return False
        return True

    async def _reset_stream_attempt(self, work_dir: Path) -> None:
        """Remove bytes from one source before trying another Nova URL."""
        for name in ("video.ts", "video.mkv", "audio.ts", "video-init.mp4", "audio-init.mp4"):
            _safe_unlink(work_dir / name)
        for name in ("segments", "audio_segments"):
            await asyncio.to_thread(shutil.rmtree, work_dir / name, ignore_errors=True)

    # ── DASH (.mpd) download — ffmpeg fallback ─────────────────────────────

    async def _download_dash(self, url: str, work_dir: Path, referer: str,
                             job_id: int, ctrl: _JobControl) -> HlsResult:
        """Download a DASH manifest with ffmpeg when Nova returns one."""
        self.log_job(job_id, "DASH stream — downloading via ffmpeg")
        work_dir.mkdir(parents=True, exist_ok=True)
        out = work_dir / "video.mkv"
        _safe_unlink(out)
        headers = f"Referer: {referer}\r\nOrigin: {referer.rstrip('/')}\r\n"
        cmd = ["ffmpeg", "-y",
               "-user_agent", NOVA_USER_AGENT,
               "-headers", headers,
               "-i", url, "-c", "copy", "-f", "matroska", str(out)]
        prev_phase = ctrl.phase
        ctrl.phase = "process"   # pause/cancel act on the ffmpeg proc directly

        # A dead DASH source makes ffmpeg sit reading a socket that never
        # delivers — with no watchdog that freezes the slot indefinitely (the
        # A dead DASH source can otherwise leave ffmpeg reading forever. Watch
        # the output file:
        # if it stops growing for the stall window while un-paused, the source
        # is dead — kill ffmpeg so the loop fails over to the next provider,
        # exactly like the HLS path does. Output-size growth is a far more
        # reliable liveness signal than ffmpeg's \r-based progress logging.
        stalled = {"v": False}

        async def watchdog() -> None:
            last_size = -1
            last_grow = time.monotonic()
            while True:
                await asyncio.sleep(5)
                if ctrl.paused or ctrl.cancel.is_set() or ctrl.skip.is_set():
                    last_grow = time.monotonic()   # not a stall — expected quiet
                    continue
                try:
                    size = out.stat().st_size
                except OSError:
                    size = last_size
                if size > last_size:
                    last_size = size
                    last_grow = time.monotonic()
                elif time.monotonic() - last_grow > _DASH_STALL_TIMEOUT:
                    stalled["v"] = True
                    if ctrl.proc is not None:
                        try:
                            ctrl.proc.terminate()
                        except ProcessLookupError:
                            pass
                    return

        watch = asyncio.ensure_future(watchdog())
        try:
            ok, err = await self._run_ffmpeg(cmd, job_id, ctrl, "Downloading")
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
            ctrl.phase = prev_phase
        if ctrl.cancel.is_set():
            raise DownloadCancelled()
        if ctrl.skip.is_set():
            raise SourceSkipped()
        if stalled["v"]:
            raise SourceStalledError(f"no data received for {int(_DASH_STALL_TIMEOUT)}s")
        if not ok or not out.exists() or out.stat().st_size == 0:
            raise HlsError(f"DASH download failed: {err[:200]}")
        return HlsResult(
            video_path=out, audio_path=None, subtitle_path=None,
            duration=0.0, total_bytes=out.stat().st_size, bandwidth=0,
        )

    # ── Phase 2 helpers ────────────────────────────────────────────────────

    async def _to_mp4(self, result, tmp_out: Path, job_id: int, ctrl: _JobControl) -> tuple[bool, str]:
        video_info = await _ffprobe(result.video_path)
        v_codec = _stream_codec(video_info, "video")
        a_codec = _stream_codec(video_info, "audio")

        audio_input: Path | None = None
        if a_codec is None and result.audio_path is not None:
            audio_input = result.audio_path
            a_codec = _stream_codec(await _ffprobe(audio_input), "audio")

        max_gb = self._max_file_gb()
        crf = self._video_crf()
        preset = self._video_preset()
        size_limit = max_gb * 1024 ** 3
        oversize = result.total_bytes > size_limit
        # Re-encoding softens the picture, so ONLY do it to fit the size cap.
        # Everything else is stream-copied losslessly — including codecs outside
        # the usual MP4 set (e.g. VP9 from a browser-sniffed source), which
        # modern players/Jellyfin handle. If ffmpeg can't mux a given codec into
        # MP4, we fall back to a re-encode below so the download still succeeds.
        encode_video = oversize
        encode_audio = a_codec is not None and a_codec not in _MP4_AUDIO

        if encode_video:
            self.log_job(job_id, f"Re-encoding video (H.264 CRF {crf}) — "
                         f"{result.total_bytes / 1024**3:.2f} GB over {max_gb} GB limit")
        else:
            self.log_job(job_id, f"Stream-copying {v_codec} to MP4 (no re-encode, no quality loss)")

        def build_cmd(with_subs: bool, encode: bool) -> list[str]:
            cmd = ["ffmpeg", "-y", "-i", str(result.video_path)]
            next_input = 1
            audio_map = None
            if audio_input is not None:
                cmd += ["-i", str(audio_input)]
                audio_map = f"{next_input}:a:0"
                next_input += 1
            subs_map = None
            if with_subs and result.subtitle_path is not None:
                cmd += ["-i", str(result.subtitle_path)]
                subs_map = f"{next_input}:s:0"
                next_input += 1

            cmd += ["-map", "0:v:0"]
            if audio_map:
                cmd += ["-map", audio_map]
            elif a_codec is not None:
                cmd += ["-map", "0:a:0"]
            if subs_map:
                cmd += ["-map", subs_map]

            if encode:
                # Leave a core free so encoding doesn't monopolise the host.
                threads = max(1, (os.cpu_count() or 2) - 1)
                cmd += ["-c:v", "libx264", "-crf", str(crf),
                        "-preset", preset, "-threads", str(threads)]
            else:
                cmd += ["-c:v", "copy"]
            if a_codec is not None:
                if encode_audio:
                    cmd += ["-c:a", "aac", "-b:a", "160k"]
                else:
                    cmd += ["-c:a", "copy"]
                    if a_codec == "aac":
                        cmd += ["-bsf:a", "aac_adtstoasc"]
            if subs_map:
                cmd += ["-c:s", "mov_text"]
            cmd += ["-dn"] if subs_map else ["-dn", "-sn"]
            cmd += ["-movflags", "+faststart", str(tmp_out)]
            return cmd

        label = "Encoding" if encode_video else "Remuxing"
        # Keep subtitles only as Jellyfin-style external WebVTT sidecars.
        # Embedding the same track here as mov_text creates two identical
        # subtitle choices in Jellyfin and can make browsers render duplicates.
        ok, err = await self._run_ffmpeg(build_cmd(False, encode_video), job_id, ctrl, label)
        # Last resort: a lossless stream-copy failed (a codec MP4 can't hold) —
        # re-encode so the download still completes rather than failing.
        if not ok and not encode_video and not ctrl.cancel.is_set():
            self.log_job(job_id, f"Stream-copy failed for {v_codec} — re-encoding as a fallback")
            _safe_unlink(tmp_out)
            ok, err = await self._run_ffmpeg(build_cmd(False, True), job_id, ctrl, "Encoding")
        return ok, err

    async def _run_ffmpeg(self, cmd: list[str], job_id: int, ctrl: _JobControl,
                          label: str) -> tuple[bool, str]:
        # Lowest CPU priority so a re-encode can't starve the web server/host.
        proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "19", *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        ctrl.proc = proc
        duration = 0.0
        stderr_tail: deque[str] = deque(maxlen=40)
        last_write = 0.0

        try:
            assert proc.stderr is not None
            while True:
                try:
                    line_bytes = await proc.stderr.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    continue
                if not line_bytes:
                    break
                line = line_bytes.decode(errors="replace")
                stderr_tail.append(line)

                if duration == 0.0:
                    dm = _FFMPEG_DUR_RE.search(line)
                    if dm:
                        duration = (int(dm.group(1)) * 3600 + int(dm.group(2)) * 60
                                    + float(dm.group(3)))

                tm = _FFMPEG_TIME_RE.search(line)
                if tm and duration > 0:
                    now = time.monotonic()
                    self._last_progress_at = now   # ffmpeg advancing → data flowing
                    if now - last_write >= 1.0:
                        last_write = now
                        current = (int(tm.group(1)) * 3600 + int(tm.group(2)) * 60
                                   + float(tm.group(3)))
                        pct = min(99.0, current / duration * 100)
                        sp = _FFMPEG_SPEED_RE.search(line)
                        speed = f"{label} {float(sp.group(1)):.1f}x" if sp else label
                        self._update(job_id, progress=round(pct, 1), download_bitrate=speed)
                        if now - getattr(self, "_last_ff_log", 0) >= 4.0:
                            self._last_ff_log = now
                            self.log_job(job_id, f"{label} {pct:.0f}% ({speed})")
        finally:
            ctrl.proc = None

        await proc.wait()
        if proc.returncode == 0:
            return True, ""
        return False, "".join(list(stderr_tail)[-20:])

    # ── Up-front assets (metadata + subtitles before the video lands) ──────

    def _preferred_height(self) -> int:
        try:
            # Nova downloads always target 1080p or, when 1080p is absent,
            # the highest rendition the source offers.
            return max(1080, int(get_setting("preferred_quality") or "1080"))
        except ValueError:
            return 1080

    def _segment_connections(self) -> int:
        try:
            return max(1, min(16, int(get_setting("segment_connections") or "6")))
        except ValueError:
            return 6

    def _subtitle_language(self) -> str:
        return normalize_language(get_setting("subtitle_language") or "en")

    def _original_audio_language(self, job: DownloadJob) -> str | None:
        """Use TMDB's original language when a master playlist offers dubs."""
        try:
            from src.metadata import get_meta_map
            return (get_meta_map([job.imdb_id]).get(job.imdb_id) or {}).get("original_language")
        except Exception:
            return None

    # Transcode knobs: DB-backed (set by the setup wizard / Settings) so they
    # apply live, falling back to the config/.env value.
    def _video_crf(self) -> int:
        try:
            return int(get_setting("video_crf") or settings.video_crf)
        except (ValueError, TypeError):
            return settings.video_crf

    def _video_preset(self) -> str:
        p = (get_setting("video_preset") or settings.video_preset or "").strip().lower()
        return p if p in _X264_PRESETS else settings.video_preset

    def _max_file_gb(self) -> float:
        try:
            return float(get_setting("max_file_gb") or settings.max_file_gb)
        except (ValueError, TypeError):
            return settings.max_file_gb

    async def _download_wyzie_vtt(self, job: DownloadJob, work_dir: Path,
                                  language: str) -> Path | None:
        """Try the preferred subtitle source; failures allow stream fallback."""
        if not wyzie.is_enabled():
            return None
        self.log_job(job.id, "Trying Wyzie subtitles first…")
        try:
            vtt = await wyzie.download_subtitle(
                job.imdb_id, work_dir / "subs.vtt",
                season=job.season, episode=job.episode,
                language=language,
                log=lambda m: self.log_job(job.id, m),
            )
            if vtt is not None:
                return vtt
        except Exception as exc:
            self.log_job(job.id, f"Wyzie subtitles failed: {exc}")
        self.log_job(job.id, "Wyzie subtitles unavailable — trying stream subtitles")
        return None

    async def _prefetch_assets(self, job: DownloadJob, dest: Path,
                               sources, work_dir: Path) -> Path | None:
        """Write NFO/artwork and the subtitle sidecar into the library folder
        as soon as the stream resolves, recording the sidecar in the DB.
        Returns the work-dir VTT for later muxing (None = no subs found).
        Nothing here can fail the download."""
        try:
            ensure_parent(dest)
        except OSError as exc:
            self.log_job(job.id, f"Could not create library folder: {exc}")
            return None
        await self._write_metadata_assets(job, dest)

        work_dir.mkdir(parents=True, exist_ok=True)
        subtitle_lang = self._subtitle_language()
        sidecar = subtitle_sidecar_path(dest, subtitle_lang)
        # Re-evaluate the current source preference on retries; an old sidecar
        # may have come from a source the user has since disabled.
        vtt = await self._download_wyzie_vtt(job, work_dir, subtitle_lang)
        if vtt is None and sources.subtitles:
            vtt = await self._download_provider_vtt(
                sources.subtitles, work_dir, job.id,
                sources.referer or _STREAM_REFERER,
                subtitle_lang)
        if vtt is not None:
            try:
                shutil.copyfile(vtt, sidecar)
            except OSError as exc:
                self.log_job(job.id, f"Could not write subtitle sidecar: {exc}")
        if sidecar.exists():
            self._update(job.id, subtitle_path=str(sidecar))
            self.log_job(job.id, f"Subtitles ready → {sidecar.name}")
        return vtt

    # ── Subtitles returned by the stream provider ───────────────────────────

    async def _download_provider_vtt(self, subtitles: list[tuple[str, str]],
                                   work_dir: Path, job_id: int,
                                   referer: str = _STREAM_REFERER,
                                   preferred_language: str = "en") -> Path | None:
        """Download the best returned subtitle track to work_dir/subs.vtt."""
        def score(item: tuple[str, str]) -> int:
            lang = item[0]
            if language_matches(lang, preferred_language):
                return 0
            return 1
        ordered = sorted(subtitles, key=score)
        headers = {**_STREAM_HEADERS, "Referer": referer}
        dest = work_dir / "subs.vtt"
        for lang, url in ordered:
            try:
                async with httpx.AsyncClient(verify=False, timeout=20,
                                             follow_redirects=True) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    text = resp.text
                if not has_timed_cues(text):
                    self.log_job(job_id, f"Subtitle {lang} has no timed cues; trying next")
                    continue
                dest.write_text(clean_vtt_text(wyzie.srt_to_vtt(text))[0], encoding="utf-8")
                self.log_job(job_id, f"Fetched subtitles ({lang})")
                return dest
            except Exception as exc:
                self.log_job(job_id, f"Subtitle {lang} failed ({exc}); trying next")
        return None

    # ── NFO + Jellyfin metadata assets ─────────────────────────────────────

    async def _write_metadata_assets(self, job: DownloadJob, dest: Path) -> None:
        """NFO sidecar (always) + TMDB-enriched fields and folder art when a
        TMDB key is configured. Metadata trouble never fails the download."""
        from src.metadata import ensure_season_metadata, ensure_title, write_jellyfin_assets
        meta = None
        try:
            meta = await ensure_title(job.imdb_id, job.media_type,
                                      log=lambda m: self.log_job(job.id, m))
        except Exception as exc:
            self.log_job(job.id, f"Metadata fetch failed (skipping): {exc}")
        if job.media_type == "tvSeries" and job.season is not None:
            try:
                await ensure_season_metadata(job.imdb_id, job.season,
                                             log=lambda m: self.log_job(job.id, m))
            except Exception as exc:
                self.log_job(job.id, f"Episode metadata fetch failed (skipping): {exc}")
        try:
            write_jellyfin_assets(job, dest, meta)
        except Exception:
            pass

    # ── Helpers ────────────────────────────────────────────────────────────

    def _dest_path(self, job: DownloadJob) -> Path:
        if job.media_type == "movie":
            return movie_path(job.title, job.year, category=job.category or "movies")
        return episode_path(
            series_title=job.title,
            year=job.year,
            season=job.season or 1,
            episode=job.episode or 1,
            episode_title=job.episode_title or f"Episode {job.episode}",
            category=job.category or "shows",
        )

    def _get_job(self, job_id: int) -> DownloadJob | None:
        with SessionLocal() as db:
            return db.query(DownloadJob).filter_by(id=job_id).first()

    def _update(self, job_id: int | None, **kwargs) -> None:
        if job_id is None:
            return
        with SessionLocal() as db:
            db.query(DownloadJob).filter_by(id=job_id).update(kwargs)
            db.commit()
        status = kwargs.get("status")
        if status in (JobStatus.completed, JobStatus.failed):
            self._sync_schedule_run_item(job_id, status, kwargs.get("error_message"))

    def _sync_schedule_run_item(self, job_id: int, status: str, error_message: str | None) -> None:
        """Keep any ScheduleRunItem linked to this job in sync with its
        terminal status — this is what keeps a schedule's run-attempt history
        ("completed" vs "failed") accurate even after the DownloadJob row is
        later archived/purged by history retention. Non-terminal transitions
        don't need this: the run-detail API reads the live job status
        directly while the job row still exists."""
        from src.database import ScheduleRunItem
        from src.scheduler import RUN_ITEM_STATUS
        mapped = RUN_ITEM_STATUS.get(status)
        if mapped is None:
            return
        with SessionLocal() as db:
            items = db.query(ScheduleRunItem).filter_by(job_id=job_id).all()
            if not items:
                return
            for it in items:
                it.status = mapped
                if error_message is not None:
                    it.error_message = error_message
            db.commit()

    def _finish_cancelled(self, job_id: int) -> None:
        self._purge_job_files(job_id)
        self.log_job(job_id, "Cancelled — partial files deleted")
        self._mark_cancelled(job_id)

    def _finish_yield(self, job_id: int, mode: str) -> None:
        """Handle a job that yielded its slot: back to queue, or mark failed —
        keeping the partial download either way."""
        if mode == "fail":
            self.log_job(job_id, "Marked failed by admin")
            self._mark_failed(job_id, "Marked failed by admin")
            return
        self.log_job(job_id, "Sent back to queue (progress kept)")
        self._update(
            job_id,
            status=JobStatus.queued,
            started_at=None,
            download_bitrate=None,
        )
        self._wake.set()

    def _mark_cancelled(self, job_id: int) -> None:
        self._update(
            job_id,
            status=JobStatus.failed,
            error_message="Cancelled by user",
            progress=0.0,
            file_path=None,
            download_bitrate=None,
            completed_at=datetime.utcnow(),
        )

    def _mark_failed(self, job_id: int, message: str) -> None:
        self._update(
            job_id,
            status=JobStatus.failed,
            error_message=message,
            download_bitrate=None,
            current_source=None,
            completed_at=datetime.utcnow(),
        )
        self._apply_failed_temp_policy(job_id)


async def _ffprobe(path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    try:
        return json.loads(stdout.decode() or "{}")
    except json.JSONDecodeError:
        return {}


def _stream_codec(probe: dict, kind: str) -> str | None:
    for s in probe.get("streams", []):
        if s.get("codec_type") == kind:
            return (s.get("codec_name") or "").lower() or None
    return None


download_manager = DownloadManager()
