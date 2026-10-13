#!/usr/bin/env python3
"""Refresh completed media with Wyzie's official best-match subtitle."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import DownloadJob, JobStatus, SessionLocal, get_setting
from src.library.organizer import _atomic_write_text, subtitle_sidecar_path
from src.providers import wyzie
from src.subtitles import normalize_language


async def run(job_id: int | None) -> int:
    if not wyzie.is_enabled():
        print("ERROR Wyzie is not enabled or has no API key")
        return 1
    with SessionLocal() as db:
        query = db.query(DownloadJob).filter_by(status=JobStatus.completed)
        if job_id is not None:
            query = query.filter_by(id=job_id)
        jobs = query.order_by(DownloadJob.id).all()
        targets = [(job.id, job.label(), job.imdb_id, job.season, job.episode,
                    Path(job.file_path)) for job in jobs if job.file_path]

    language = normalize_language(get_setting("subtitle_language") or "en")
    refreshed = failed = 0
    for identifier, label, title_id, season, episode, video in targets:
        if not video.is_file():
            print(f"SKIP job {identifier}: video is missing")
            continue
        print(f"REFRESH job {identifier}: {label}")
        with tempfile.TemporaryDirectory(prefix="riprate-wyzie-refresh-") as directory:
            root = Path(directory)
            downloaded = root / "best.vtt"
            found = await wyzie.download_subtitle(
                title_id, downloaded, season=season, episode=episode,
                language=language, log=print,
            )
            if found is None:
                print(f"ERROR job {identifier}: Wyzie returned no best match")
                failed += 1
                continue
            sidecar = subtitle_sidecar_path(video, language)
            if sidecar.exists():
                backup = sidecar.with_suffix(sidecar.suffix + ".before-wyzie-best")
                if not backup.exists():
                    shutil.copyfile(sidecar, backup)
            _atomic_write_text(sidecar, found.read_text(encoding="utf-8"))
            with SessionLocal() as db:
                job = db.get(DownloadJob, identifier)
                if job:
                    job.subtitle_path = str(sidecar)
                    db.commit()
            print(f"DONE job {identifier}: {sidecar.name}")
            refreshed += 1
    print(f"RESULT refreshed={refreshed} failed={failed}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=int, help="only refresh this completed job")
    args = parser.parse_args()
    return asyncio.run(run(args.job_id))


if __name__ == "__main__":
    raise SystemExit(main())
