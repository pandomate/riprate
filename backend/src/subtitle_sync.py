"""Local subtitle alignment against speech in the selected audio track."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import tempfile

from src.subtitles import clean_vtt_text, has_timed_cues

_slots = asyncio.Semaphore(1)


def auto_sync_enabled() -> bool:
    from src.database import get_setting
    return get_setting("subtitle_auto_sync").lower() in ("true", "1", "yes", "on")


async def align_subtitles(reference: Path, subtitles: Path, output: Path,
                          log=lambda message: None, cancelled=lambda: False,
                          timeout: float = 300) -> bool:
    """Write aligned WebVTT to output; leave inputs intact on any failure.

    Explicitly use audio, never the video's potentially mistimed embedded subs.
    All decoding/alignment is local and the expensive process is bounded.
    """
    async with _slots:
        if cancelled():
            return False
        with tempfile.TemporaryDirectory(prefix="riprate-subtitle-sync-") as work:
            result = Path(work) / "aligned.srt"
            log("Aligning subtitles to audio (offset and timing drift)…")
            proc = await asyncio.create_subprocess_exec(
                "ffsubsync", str(reference),
                "-i", str(subtitles), "-o", str(result),
                "--reference-stream", "a:0", "--encoding", "utf-8",
                "--gss", "--skip-sync-on-low-quality",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
            )
            reader = asyncio.create_task(proc.communicate())
            deadline = asyncio.get_running_loop().time() + timeout
            try:
                while not reader.done():
                    if cancelled() or asyncio.get_running_loop().time() >= deadline:
                        log("Subtitle alignment cancelled or timed out; keeping original timing")
                        return False
                    await asyncio.wait({reader}, timeout=0.25)
                _, stderr = await reader
                if proc.returncode or not result.exists():
                    diagnostic = stderr.decode(errors="replace").strip().splitlines()
                    detail = diagnostic[-1] if diagnostic else "no diagnostic was returned"
                    log(f"Subtitle alignment unavailable ({detail}); keeping original timing")
                    return False
                # Quality rejection is a successful CLI run that copies input.
                diagnostics = stderr.decode(errors="replace").lower()
                if "low quality" in diagnostics or "low-quality" in diagnostics:
                    log("Subtitle alignment was uncertain; keeping original timing")
                    return False
                from src.providers.wyzie import srt_to_vtt
                text = clean_vtt_text(srt_to_vtt(result.read_text(encoding="utf-8")))[0]
                if not has_timed_cues(text):
                    log("Subtitle alignment produced no usable cues; keeping original timing")
                    return False
                output.write_text(text, encoding="utf-8")
                log("Subtitles aligned to audio")
                return True
            finally:
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        await asyncio.wait_for(asyncio.shield(reader), 3)
                    except (ProcessLookupError, asyncio.TimeoutError):
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                await reader


def replace_sidecar(sidecar: Path, text: str) -> None:
    """Keep the original once, then atomically replace the external subtitle."""
    from src.library.organizer import _atomic_write_text
    backup = sidecar.with_suffix(sidecar.suffix + ".before-sync")
    try:
        with backup.open("x", encoding="utf-8") as stream:
            stream.write(sidecar.read_text(encoding="utf-8"))
    except FileExistsError:
        pass
    _atomic_write_text(sidecar, text)
