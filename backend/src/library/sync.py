"""Reconcile the media filesystem with library records without moving files."""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from src.config import settings
from src.database import DownloadJob, JobStatus, SessionLocal
from src.library.categories import all_categories

log = logging.getLogger("library.sync")
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".webm"}
_EPISODE = re.compile(r"\bS(\d+)E(\d+)\b", re.I)
_YEAR = re.compile(r"^(.*?)\s*\((\d{4})\)$")


def media_filesystem(root: Path) -> str:
    """Read the deepest mount covering root, including Docker bind mounts."""
    target = str(root.resolve())
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), before.split()[4])
        if target == mount or target.startswith(mount.rstrip("/") + "/"):
            matches.append((len(mount), after.split()[0]))
    return max(matches)[1] if matches else "unknown"


def check_media_storage() -> None:
    root = Path(settings.media_dir)
    if not root.is_dir():
        raise OSError(f"Media directory is unavailable: {root}")
    expected = settings.media_expected_fstype
    if expected and media_filesystem(root) != expected:
        raise OSError(f"Media storage is not on the expected {expected} filesystem. "
                      "Mount MEDIA_DIR on the host, then recreate the backend container.")


def _nfo(path: Path) -> ET.Element:
    try:
        if path.stat().st_size <= 1024 * 1024:
            return ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError):
        pass
    return ET.Element("empty")


def _number(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def describe_video(path: Path, root: Path, categories: list[dict]) -> dict:
    """Use local NFO metadata, falling back to Jellyfin-style filenames."""
    episode_nfo = _nfo(path.with_suffix(".nfo"))
    episode_match = _EPISODE.search(path.stem)
    is_episode = episode_match is not None or episode_nfo.tag == "episodedetails"
    season = _number(episode_nfo.findtext("season"))
    episode = _number(episode_nfo.findtext("episode"))
    title_root = path.parent
    if is_episode:
        if episode_match:
            season = season if season is not None else int(episode_match[1])
            episode = episode if episode is not None else int(episode_match[2])
        if season is None or episode is None:
            raise ValueError("Episode has no season/episode number")
        if re.match(r"Season\s*\d+", path.parent.name, re.I):
            title_root = path.parent.parent
        metadata = _nfo(title_root / "tvshow.nfo")
        fallback_title = title_root.name
    else:
        metadata = episode_nfo if episode_nfo.tag == "movie" else _nfo(path.parent / "movie.nfo")
        fallback_title = path.stem
    match = _YEAR.match(fallback_title)
    title = metadata.findtext("title") or (match[1] if match else fallback_title)
    year = _number(metadata.findtext("year")) or (int(match[2]) if match else None)
    media_type = "tvSeries" if is_episode else "movie"
    ids = list(metadata.findall("uniqueid"))
    # RipRate episode NFOs carry the series ID when tvshow.nfo is absent.
    if not ids:
        ids = list(episode_nfo.findall("uniqueid"))
    ids.sort(key=lambda node: node.get("default") != "true")
    identifier = next((node.text.strip() for node in ids
                       if node.text and re.fullmatch(r"(?:tt)?\d+", node.text.strip())), None)
    identifier = identifier or metadata.findtext("imdbid") or metadata.findtext("tmdbid")
    if not identifier:
        identity = f"{media_type}:{title.casefold()}:{year}"
        identifier = "local-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    category = None
    for cat in sorted(categories, key=lambda c: len(c["folder"]), reverse=True):
        if path.is_relative_to(root / cat["folder"]):
            category = cat["key"]
            break
    return dict(title=title, year=year, imdb_id=identifier, media_type=media_type,
                season=season, episode=episode, category=category,
                episode_title=(episode_nfo.findtext("title") or
                               path.stem[episode_match.end():].strip(" -"))
                if is_episode and episode_match else episode_nfo.findtext("title") if is_episode else None)


class LibrarySync:
    def __init__(self):
        self._previous: dict[str, tuple[int, int]] = {}
        self._absent: set[int] = set()
        self._task: asyncio.Task | None = None
        self.storage_error: str | None = None

    def scan(self) -> dict[str, int]:
        root = Path(settings.media_dir).resolve()
        counts = {"added": 0, "updated": 0, "missing": 0}
        try:
            check_media_storage()
            snapshot = {}
            def fail(error):
                raise error
            # Abort the entire scan on an unreadable subtree; a share outage
            # must never be mistaken for an intentional deletion.
            for folder, dirs, names in os.walk(root, onerror=fail, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith(".") and
                           not (Path(folder) / d).is_symlink()]
                for name in names:
                    path = Path(folder) / name
                    if name.startswith(".") or path.suffix.lower() not in VIDEO_EXTENSIONS or path.is_symlink():
                        continue
                    stat = path.stat()
                    snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
            check_media_storage()
        except OSError as exc:
            self.storage_error = str(exc)
            self._absent.clear()
            self._previous.clear()
            log.warning("Library scan skipped: %s", exc)
            return counts
        self.storage_error = None
        categories = all_categories()
        with SessionLocal() as db:
            jobs = db.query(DownloadJob).all()
            by_path = {j.file_path: j for j in jobs if j.file_path}
            seen = set()
            for filename, fingerprint in snapshot.items():
                job = by_path.get(filename)
                if job:
                    seen.add(job.id)
                # Two consecutive unchanged scans avoid importing an unfinished
                # copy. Active queue jobs are owned exclusively by the worker.
                if fingerprint[0] == 0 or self._previous.get(filename) != fingerprint:
                    continue
                if job and job.status not in (JobStatus.completed, JobStatus.missing):
                    continue
                path = Path(filename)
                if job is None:
                    try:
                        info = describe_video(path, root, categories)
                    except ValueError:
                        continue
                    identity = (info["imdb_id"], info["media_type"], info["season"], info["episode"])
                    matches = [j for j in jobs
                               if (j.imdb_id, j.media_type, j.season, j.episode) == identity]
                    if any(j.status in (JobStatus.queued, JobStatus.downloading,
                                        JobStatus.processing, JobStatus.paused) for j in matches):
                        continue
                    moved = [j for j in matches if j.status in (JobStatus.completed, JobStatus.missing)
                             and j.file_path not in snapshot and j.id not in seen]
                    # Without an NFO, a moved file can still match its old name.
                    if not moved:
                        moved = [j for j in jobs if j.status in (JobStatus.completed, JobStatus.missing)
                                 and j.file_path and j.file_path not in snapshot and j.id not in seen
                                 and Path(j.file_path).name == path.name
                                 and j.total_bytes == fingerprint[0]]
                    if len(moved) == 1:
                        job = moved[0]
                        job.category = info["category"]
                    else:
                        job = DownloadJob(**info, source="library", archived=True,
                                          completed_at=datetime.utcnow())
                        db.add(job)
                        db.flush()
                        jobs.append(job)
                        counts["added"] += 1
                seen.add(job.id)
                subtitles = sorted(path.parent.glob(path.stem + ".*.vtt"))
                if path.with_suffix(".vtt").is_file():
                    subtitles.insert(0, path.with_suffix(".vtt"))
                subtitle = str(subtitles[0]) if subtitles else None
                if (job.file_path, job.total_bytes, job.subtitle_path, job.status) != (
                        filename, fingerprint[0], subtitle, JobStatus.completed):
                    job.file_path = filename
                    job.total_bytes = fingerprint[0]
                    job.subtitle_path = subtitle
                    job.status = JobStatus.completed
                    job.progress = 100
                    job.error_message = None
                    counts["updated"] += 1
            absent = set()
            for job in jobs:
                if job.status == JobStatus.completed and job.file_path and job.id not in seen:
                    absent.add(job.id)
                    if job.id in self._absent:
                        job.status = JobStatus.missing
                        job.error_message = "File is absent from the media folder"
                        counts["missing"] += 1
            # Check again before committing if the mount vanished during scanning.
            try:
                check_media_storage()
            except OSError as exc:
                self.storage_error = str(exc)
                self._previous.clear()
                self._absent.clear()
                raise
            db.commit()
        self._previous = snapshot
        self._absent = absent
        if any(counts.values()):
            log.info("Library sync: %s", counts)
        return counts

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                await asyncio.to_thread(self.scan)
            except Exception:
                log.exception("Library reconciliation failed; will retry")
            await asyncio.sleep(15)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


library_sync = LibrarySync()
