#!/usr/bin/env python3
"""Remove redundant embedded subtitle streams when an external sidecar exists."""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.watch import _sidecar
from src.database import DownloadJob, JobStatus, SessionLocal


def _has_embedded_subtitles(video: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _repair(video: Path) -> bool:
    temp = video.with_name(f".{video.name}.without-embedded-subs.tmp.mp4")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video),
             "-map", "0:v?", "-map", "0:a?", "-map_metadata", "0",
             "-map_chapters", "0", "-c", "copy", "-sn", "-dn",
             "-movflags", "+faststart", str(temp)],
            capture_output=True, text=True,
        )
        if result.returncode or not temp.is_file() or temp.stat().st_size == 0:
            detail = result.stderr.strip().splitlines()
            print(f"ERROR {video}: {detail[-1] if detail else 'ffmpeg failed'}")
            return False
        if _has_embedded_subtitles(temp):
            print(f"ERROR {video}: repaired file still has a subtitle stream")
            return False
        shutil.copystat(video, temp)
        os.replace(temp, video)
        return True
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=int, help="only repair this completed job")
    args = parser.parse_args()
    with SessionLocal() as db:
        query = db.query(DownloadJob).filter_by(status=JobStatus.completed)
        if args.job_id is not None:
            query = query.filter_by(id=args.job_id)
        jobs = query.order_by(DownloadJob.id).all()
        targets = [(job.id, job.label(), Path(job.file_path)) for job in jobs if job.file_path]

    repaired = failed = skipped = 0
    for job_id, label, video in targets:
        if not video.is_file() or _sidecar(video) is None or not _has_embedded_subtitles(video):
            print(f"SKIP job {job_id}: {label}")
            skipped += 1
            continue
        print(f"REPAIR job {job_id}: {label}")
        if _repair(video):
            print(f"DONE job {job_id}: external subtitle retained")
            repaired += 1
        else:
            failed += 1
    print(f"RESULT repaired={repaired} failed={failed} skipped={skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
