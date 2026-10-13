"""
Local playback of finished library files.

Every authenticated user can browse and watch what's in the library (same as
the Library page). The video is streamed from disk with HTTP Range support so
the browser <video> element can seek instantly; MP4s are written with
+faststart so playback starts immediately. A sidecar .en.vtt (if present) is
served as a subtitle track. Cached TMDB artwork/plots (src/metadata.py) ride
along in the list response so the browse UI can render poster cards without
ever calling TMDB.
"""
from __future__ import annotations

import queue
import re
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from src.api.deps import require_auth
from src.config import settings
from src.database import (
    DownloadJob, Favorite, JobStatus, User, WatchProgress, get_db, get_setting,
)
from src.library.organizer import (
    resolve_season_folder, resolve_show_folder, subtitle_sidecar_path,
)
from src.metadata import get_episode_meta_map, get_meta_map, meta_dir

router = APIRouter(tags=["watch"])

# Metadata is keyed by the title ID returned by the active search source.
# TMDB-backed results use numeric IDs (for example 260463), while older IMDB
# records use tt-prefixed IDs. Accept both safe forms so cached TMDB artwork
# can be served to the Watch page.
_META_IMAGE_RE = re.compile(r"^(?:tt\d+|\d+)_(poster|backdrop|s\d+e\d+)\.jpg$")


def _inside_media_dir(p: Path) -> bool:
    """A job's file must live under MEDIA_DIR — refuses to serve anything else
    even if a DB row were ever tampered with."""
    try:
        media_root = Path(settings.media_dir).resolve()
        return media_root in p.resolve().parents
    except Exception:
        return False


def _watched_threshold() -> float:
    """Fraction (0-1) of duration after which a title counts as watched."""
    try:
        pct = int(get_setting("watched_threshold_percent") or "90")
    except ValueError:
        pct = 90
    return min(max(pct, 50), 100) / 100.0


def _sidecar(mp4: Path) -> Path | None:
    from src.subtitles import normalize_language
    candidates = [subtitle_sidecar_path(mp4, normalize_language(get_setting("subtitle_language") or "en")),
                  subtitle_sidecar_path(mp4, "en"), mp4.with_suffix(".vtt")]
    # Keep previously downloaded languages playable after the admin changes
    # the preference; only inspect sidecars next to this exact video.
    candidates += sorted(mp4.parent.glob(f"{mp4.stem}.*.vtt"))
    for cand in candidates:
        if cand.exists():
            return cand
    return None


@router.get("/watch/list")
def watch_list(db: Session = Depends(get_db), user: User = Depends(require_auth)):
    """Everything watchable — completed jobs whose file is on disk."""
    jobs = (
        db.query(DownloadJob)
        .filter_by(status=JobStatus.completed)
        .order_by(DownloadJob.media_type, DownloadJob.title,
                  DownloadJob.season, DownloadJob.episode)
        .all()
    )
    progress = {
        p.job_id: p for p in
        db.query(WatchProgress).filter_by(user_id=user.id).all()
    }
    favorites = {
        f.imdb_id for f in db.query(Favorite).filter_by(user_id=user.id).all()
    }
    threshold = _watched_threshold()
    out = []
    for j in jobs:
        if not j.file_path:
            continue
        mp4 = Path(j.file_path)
        if not mp4.exists():
            continue
        p = progress.get(j.id)
        watched = bool(p and p.duration > 0 and p.position >= p.duration * threshold)
        out.append({
            "id": j.id,
            "imdb_id": j.imdb_id,
            "title": j.title,
            "year": j.year,
            "media_type": j.media_type,
            "season": j.season,
            "episode": j.episode,
            "episode_title": j.episode_title,
            "size": j.total_bytes,
            "has_subtitle": _sidecar(mp4) is not None,
            "mine": j.requested_by == user.id,
            "position": p.position if p else 0.0,
            "duration": p.duration if p else 0.0,
            "watched": watched,
            "progress_updated_at": p.updated_at.isoformat() if p else None,
            "favorite": j.imdb_id in favorites,
        })
    # Locally-cached TMDB metadata per title (poster/backdrop filenames,
    # plot, genres, rating…) — no TMDB call happens here.
    titles = get_meta_map(list({i["imdb_id"] for i in out}))
    return {"items": out, "titles": titles}


@router.get("/watch/meta-image/{name}")
def meta_image(name: str, _user=Depends(require_auth)):
    """Serve a locally-cached poster/backdrop. Filenames are strictly
    ID-prefixed poster/backdrop/still names so nothing else is reachable."""
    if not _META_IMAGE_RE.match(name):
        raise HTTPException(status_code=404, detail="Not found")
    p = meta_dir() / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/watch/episode-meta/{imdb_id}")
def episode_meta(imdb_id: str, _user=Depends(require_auth)):
    """Cached per-episode metadata (overview/air date/rating/still) for a
    show, keyed 'season:episode'. Read-only — never triggers a TMDB call;
    populated by the download pipeline."""
    return get_episode_meta_map(imdb_id)


# ── Playback progress (resume / Continue Watching) ─────────────────────────

class ProgressBody(BaseModel):
    position: float = Field(ge=0)
    duration: float = Field(ge=0)


@router.post("/watch/{job_id}/progress")
def save_progress(
    job_id: int,
    body: ProgressBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    job = db.query(DownloadJob).filter_by(id=job_id, status=JobStatus.completed).first()
    if not job:
        raise HTTPException(status_code=404, detail="Not found")
    row = db.query(WatchProgress).filter_by(user_id=user.id, job_id=job_id).first()
    if row is None:
        row = WatchProgress(user_id=user.id, job_id=job_id)
        db.add(row)
    row.position = body.position
    row.duration = body.duration
    row.updated_at = datetime.utcnow()
    db.commit()
    threshold = _watched_threshold()
    watched = body.duration > 0 and body.position >= body.duration * threshold
    return {"ok": True, "watched": watched}


@router.delete("/watch/{job_id}/progress")
def clear_progress(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    db.query(WatchProgress).filter_by(user_id=user.id, job_id=job_id).delete()
    db.commit()
    return {"ok": True}


# ── Favorites (per-title: movie or whole show) ──────────────────────────────

class FavoriteBody(BaseModel):
    imdb_id: str = Field(min_length=2, max_length=20)


@router.post("/watch/favorite")
def toggle_favorite(
    body: FavoriteBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    row = db.query(Favorite).filter_by(user_id=user.id, imdb_id=body.imdb_id).first()
    if row:
        db.delete(row)
        db.commit()
        return {"favorited": False}
    db.add(Favorite(user_id=user.id, imdb_id=body.imdb_id))
    db.commit()
    return {"favorited": True}


def _job_file(db: Session, job_id: int) -> Path:
    job = db.query(DownloadJob).filter_by(id=job_id, status=JobStatus.completed).first()
    if not job or not job.file_path:
        raise HTTPException(status_code=404, detail="Not found")
    p = Path(job.file_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File is missing")
    if not _inside_media_dir(p):
        raise HTTPException(status_code=404, detail="Not found")
    return p


@router.get("/watch/{job_id}")
def watch_stream(job_id: int, db: Session = Depends(get_db), _user=Depends(require_auth)):
    """Stream media (Starlette FileResponse handles Range/seeking)."""
    p = _job_file(db, job_id)
    return FileResponse(
        str(p),
        media_type={".mkv": "video/x-matroska", ".webm": "video/webm"}.get(
            p.suffix.lower(), "video/mp4"),
        headers={"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"},
    )


@router.get("/watch/{job_id}/download")
def watch_download(job_id: int, db: Session = Depends(get_db), _user=Depends(require_auth)):
    """Download the file to the user's computer (attachment, not playback).
    Same auth + completed-job + media-dir checks as streaming; Range support
    means interrupted downloads can resume."""
    p = _job_file(db, job_id)
    return FileResponse(
        str(p),
        media_type="video/mp4",
        filename=p.name,   # Starlette emits a safe Content-Disposition: attachment
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store", "Accept-Ranges": "bytes"},
    )


@router.get("/watch/{job_id}/subtitle")
def watch_subtitle(job_id: int, db: Session = Depends(get_db), _user=Depends(require_auth)):
    p = _job_file(db, job_id)
    vtt = _sidecar(p)
    if vtt is None:
        raise HTTPException(status_code=404, detail="No subtitles")
    return FileResponse(str(vtt), media_type="text/vtt",
                        headers={"Cache-Control": "private, no-store"})


# ── All files per item/season/show + zip-or-individual download ────────────
#
# Three scopes, matching the Library/Watch pages:
#   item   — one movie (its whole dedicated folder) or one episode (only the
#            files sharing its stem inside the shared season folder)
#   season — an episode's season folder (via imdb_id + season number)
#   show   — the whole show root, every season
# Every zip can be filtered to just the .mp4s (?mp4=1 — what the Watch page
# offers) or include everything (nfo/vtt/art — the Library default).

_EP_CODE_RE = re.compile(r"S(\d{1,3})E(\d{1,3})", re.I)


def _file_entry(p: Path, folder: Path) -> dict:
    m = _EP_CODE_RE.search(p.name)
    return {
        "name": p.relative_to(folder).as_posix(),
        "size": p.stat().st_size,
        "season": int(m.group(1)) if m else None,
        "episode": int(m.group(2)) if m else None,
    }


def _collect_files(folder: Path, mp4_only: bool = False,
                   stem: str | None = None) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
    if mp4_only:
        files = [p for p in files if p.suffix.lower() == ".mp4"]
    if stem is not None:
        files = [p for p in files if p.name.startswith(stem)]
    return files


def _safe_file_in_folder(folder: Path, rel_path: str) -> Path:
    """Resolve rel_path against folder, refusing anything that escapes it
    (blocks `../` traversal) or falls outside MEDIA_DIR."""
    folder_r = folder.resolve()
    target = (folder / rel_path).resolve()
    if folder_r != target and folder_r not in target.parents:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file() or not _inside_media_dir(target):
        raise HTTPException(status_code=404, detail="Not found")
    return target


class _ZipQueueWriter:
    """File-like object zipfile can write to; chunks land on a queue that the
    response generator drains. No seek() → zipfile uses streaming data
    descriptors, so nothing is ever spooled to disk."""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q
        self.pos = 0

    def write(self, b) -> int:
        if b:
            self.q.put(bytes(b))
            self.pos += len(b)
        return len(b)

    def flush(self) -> None:
        pass

    def tell(self) -> int:
        return self.pos


def _zip_stream(folder: Path, files: list[Path]):
    """Yield a zip of `files` (arcnames relative to `folder`) chunk by chunk.
    ZIP_STORED — video doesn't compress, and store keeps it CPU-free."""
    q: queue.Queue = queue.Queue(maxsize=32)

    def produce() -> None:
        try:
            writer = _ZipQueueWriter(q)
            with zipfile.ZipFile(writer, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for p in files:
                    zf.write(p, arcname=p.relative_to(folder).as_posix())
        except Exception:
            pass  # client disconnects surface here; nothing to clean up
        finally:
            q.put(None)

    threading.Thread(target=produce, daemon=True).start()
    while True:
        chunk = q.get()
        if chunk is None:
            break
        yield chunk


def _zip_response(folder: Path, files: list[Path], name: str) -> StreamingResponse:
    if not files:
        raise HTTPException(status_code=404, detail="No files to download")
    safe = name.replace('"', "")
    return StreamingResponse(
        _zip_stream(folder, files),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.zip"',
            "Cache-Control": "private, no-store",
        },
    )


def _show_folder(db: Session, imdb_id: str) -> Path:
    jobs = db.query(DownloadJob).filter_by(imdb_id=imdb_id, status=JobStatus.completed).all()
    folder = resolve_show_folder(jobs) if jobs else None
    if folder is None or not _inside_media_dir(folder):
        raise HTTPException(status_code=404, detail="Not found")
    return folder


def _season_folder(db: Session, imdb_id: str, season: int) -> Path:
    jobs = (db.query(DownloadJob)
            .filter_by(imdb_id=imdb_id, season=season, status=JobStatus.completed)
            .all())
    folder = resolve_season_folder(jobs) if jobs else None
    if folder is None or not _inside_media_dir(folder):
        raise HTTPException(status_code=404, detail="Not found")
    return folder


def _item_scope(db: Session, job_id: int) -> tuple[Path, str | None, str]:
    """(folder, stem-filter, download-name) for one movie/episode. A movie
    owns its whole folder; an episode owns only the files sharing its stem
    inside the shared season folder."""
    job = db.query(DownloadJob).filter_by(id=job_id, status=JobStatus.completed).first()
    if not job or not job.file_path:
        raise HTTPException(status_code=404, detail="Not found")
    p = _job_file(db, job_id)
    if job.media_type == "movie":
        return p.parent, None, p.parent.name
    return p.parent, p.stem, p.stem


@router.get("/watch/{job_id}/files")
def job_files(job_id: int, db: Session = Depends(get_db), _user=Depends(require_auth)):
    """Every file in the folder containing this job's media — a movie's own
    folder, or (for an episode) the shared season folder."""
    p = _job_file(db, job_id)
    return {"folder": p.parent.name,
            "files": [_file_entry(f, p.parent) for f in _collect_files(p.parent)]}


@router.get("/watch/{job_id}/item-files")
def job_item_files(job_id: int, db: Session = Depends(get_db), _user=Depends(require_auth)):
    """Only the files belonging to THIS movie/episode (not season siblings)."""
    folder, stem, _name = _item_scope(db, job_id)
    return {"folder": folder.name,
            "files": [_file_entry(f, folder) for f in _collect_files(folder, stem=stem)]}


@router.get("/watch/{job_id}/files/{rel_path:path}")
def job_file_download(job_id: int, rel_path: str, db: Session = Depends(get_db),
                      _user=Depends(require_auth)):
    p = _job_file(db, job_id)
    target = _safe_file_in_folder(p.parent, rel_path)
    return FileResponse(str(target), filename=target.name,
                        content_disposition_type="attachment",
                        headers={"Cache-Control": "private, no-store"})


@router.get("/watch/{job_id}/zip")
def job_zip(job_id: int, mp4: bool = False, db: Session = Depends(get_db),
            _user=Depends(require_auth)):
    """Zip the job's folder (movie folder / episode's season folder)."""
    p = _job_file(db, job_id)
    return _zip_response(p.parent, _collect_files(p.parent, mp4_only=mp4),
                         p.parent.name + ("-mp4" if mp4 else ""))


@router.get("/watch/{job_id}/item-zip")
def job_item_zip(job_id: int, db: Session = Depends(get_db), _user=Depends(require_auth)):
    """Zip of just this movie's/episode's own files (video + nfo + subs…)."""
    folder, stem, name = _item_scope(db, job_id)
    return _zip_response(folder, _collect_files(folder, stem=stem), name)


@router.get("/watch/season-files/{imdb_id}/{season}")
def season_files(imdb_id: str, season: int, db: Session = Depends(get_db),
                 _user=Depends(require_auth)):
    """Every file in one season's folder, tagged with the SxxEyy parsed from
    each filename so the UI can group per episode."""
    folder = _season_folder(db, imdb_id, season)
    return {"folder": folder.name,
            "files": [_file_entry(f, folder) for f in _collect_files(folder)]}


@router.get("/watch/season-zip/{imdb_id}/{season}")
def season_zip(imdb_id: str, season: int, mp4: bool = False,
               db: Session = Depends(get_db), _user=Depends(require_auth)):
    folder = _season_folder(db, imdb_id, season)
    return _zip_response(folder, _collect_files(folder, mp4_only=mp4),
                         folder.name + ("-mp4" if mp4 else ""))


@router.get("/watch/show-files/{imdb_id}")
def show_files(imdb_id: str, db: Session = Depends(get_db), _user=Depends(require_auth)):
    """Every file anywhere under a show's root folder (all seasons + root
    tvshow.nfo/poster/fanart)."""
    folder = _show_folder(db, imdb_id)
    return {"folder": folder.name,
            "files": [_file_entry(f, folder) for f in _collect_files(folder)]}


@router.get("/watch/show-files/{imdb_id}/{rel_path:path}")
def show_file_download(imdb_id: str, rel_path: str, db: Session = Depends(get_db),
                       _user=Depends(require_auth)):
    folder = _show_folder(db, imdb_id)
    target = _safe_file_in_folder(folder, rel_path)
    return FileResponse(str(target), filename=target.name,
                        content_disposition_type="attachment",
                        headers={"Cache-Control": "private, no-store"})


@router.get("/watch/show-zip/{imdb_id}")
def show_zip(imdb_id: str, mp4: bool = False, db: Session = Depends(get_db),
             _user=Depends(require_auth)):
    folder = _show_folder(db, imdb_id)
    return _zip_response(folder, _collect_files(folder, mp4_only=mp4),
                         folder.name + ("-mp4" if mp4 else ""))
