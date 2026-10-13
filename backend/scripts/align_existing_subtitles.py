#!/usr/bin/env python3
"""Align existing external subtitle sidecars against their video's audio."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.watch import _sidecar
from src.database import DownloadJob, JobStatus, SessionLocal
from src.subtitle_sync import align_subtitles, replace_sidecar


def _subtitle_for(job: DownloadJob, video: Path) -> Path | None:
    if job.subtitle_path:
        recorded = Path(job.subtitle_path)
        if recorded.is_file():
            return recorded
    return _sidecar(video)


async def run(job_id: int | None) -> int:
    with SessionLocal() as db:
        query = db.query(DownloadJob).filter_by(status=JobStatus.completed)
        if job_id is not None:
            query = query.filter_by(id=job_id)
        jobs = query.order_by(DownloadJob.id).all()
        targets = [
            (job.id, job.label(), Path(job.file_path), _subtitle_for(job, Path(job.file_path)))
            for job in jobs if job.file_path and Path(job.file_path).is_file()
        ]

    attempted = aligned = skipped = 0
    for identifier, label, video, sidecar in targets:
        if sidecar is None or not sidecar.is_file():
            print(f"SKIP job {identifier}: {label} has no external subtitle")
            skipped += 1
            continue
        attempted += 1
        print(f"SYNC job {identifier}: {label}")
        with tempfile.TemporaryDirectory(prefix="riprate-align-existing-") as work:
            source = Path(work) / sidecar.name
            output = Path(work) / "aligned.vtt"
            shutil.copyfile(sidecar, source)
            ok = await align_subtitles(video, source, output, log=print)
            if ok:
                replace_sidecar(sidecar, output.read_text(encoding="utf-8"))
                print(f"DONE job {identifier}: {sidecar}")
                aligned += 1
            else:
                print(f"KEEP job {identifier}: alignment was not reliable")

    print(f"RESULT aligned={aligned} attempted={attempted} skipped={skipped}")
    return 0 if aligned == attempted else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=int, help="only align this completed job")
    args = parser.parse_args()
    return asyncio.run(run(args.job_id))


if __name__ == "__main__":
    raise SystemExit(main())
