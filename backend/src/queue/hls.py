"""
Python-native, resumable HLS downloader.

ffmpeg's HLS demuxer chokes on very long playlist lines ("Separator is not
found, and chunk exceed the limit") and gives poor telemetry. This module
fetches the playlists and segments itself, one file per segment, so an
interrupted download (browser closed, backend restart, transient CDN error)
resumes from where it left off instead of starting over:

  master.m3u8 → pick variant under the bitrate cap → media playlist
  → download each .ts segment to segments/NNNNN.ts (skipped if already present,
    AES-128 decrypted if needed) → concatenate to video.ts

The result is handed to ffmpeg purely as *local file* input for a fast
copy-remux (or size-guarded re-encode) to MP4.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx

_log = logging.getLogger("hls")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')

_DEFAULT_CONNECTIONS = 6    # parallel segment fetches per download (Settings-tunable)
_SEGMENT_RETRIES = 6        # attempts per segment before giving up
_RETRY_BASE = 1.5           # backoff base seconds


def _clear_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class SegmentAuthError(Exception):
    """Segments answer 401/403 — the stream token is expired/rejected (these
    CDNs time-limit their links, and some gate whole quality tiers). Retrying
    the same URL is pointless; the caller must re-resolve for a fresh link or
    step down to another variant."""


class SourceStalledError(Exception):
    """The source stopped feeding data (no segment completed for the stall
    window while un-paused). Some hosts just silently stop mid-stream — the
    caller should move to the next source instead of waiting forever."""


class SourceSkipped(Exception):
    """The admin pressed "Next source" — abandon this source immediately."""


_STALL_TIMEOUT = 120.0   # seconds without a completed segment → source stalled


class HlsError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


class DownloadRequeued(Exception):
    """Raised to yield the worker slot while KEEPING the partial download."""
    pass


LogFn = Callable[[str], None]
ProgressFn = Callable[["Progress"], None]


@dataclass
class _Variant:
    bandwidth: int
    uri: str
    resolution: str = ""
    audio_group: Optional[str] = None
    subs_group: Optional[str] = None


@dataclass
class _Media:
    type: str
    group_id: str
    uri: Optional[str]
    language: str = ""
    name: str = ""
    default: bool = False


@dataclass
class _Segment:
    uri: str
    duration: float
    seq: int
    key_uri: Optional[str] = None
    key_iv: Optional[bytes] = None


@dataclass
class HlsResult:
    video_path: Path
    audio_path: Optional[Path]
    subtitle_path: Optional[Path]
    duration: float
    total_bytes: int
    bandwidth: int
    resolution: str = ""


@dataclass
class Progress:
    done_segments: int = 0
    total_segments: int = 0
    bytes_done: int = 0
    est_total_bytes: int = 0
    speed_bps: float = 0.0


def _headers(referer: str, dest: str = "empty") -> dict:
    origin = ""
    if referer:
        p = urlparse(referer)
        origin = f"{p.scheme}://{p.netloc}"
    h = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": dest,
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if referer:
        h["Referer"] = referer
        h["Origin"] = origin
    return h


def _parse_attrs(line: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(line)}


def _parse_master(text: str, base_url: str) -> tuple[list[_Variant], list[_Media]]:
    variants: list[_Variant] = []
    medias: list[_Media] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            a = _parse_attrs(line)
            uri = a.get("URI")
            medias.append(_Media(
                type=a.get("TYPE", ""),
                group_id=a.get("GROUP-ID", ""),
                uri=urljoin(base_url, uri) if uri else None,
                language=a.get("LANGUAGE", ""),
                name=a.get("NAME", ""),
                default=a.get("DEFAULT", "NO").upper() == "YES",
            ))
        elif line.startswith("#EXT-X-STREAM-INF:"):
            a = _parse_attrs(line)
            uri = ""
            for j in range(i + 1, min(i + 5, len(lines))):
                cand = lines[j].strip()
                if cand and not cand.startswith("#"):
                    uri = urljoin(base_url, cand)
                    break
            if uri:
                variants.append(_Variant(
                    bandwidth=int(a.get("BANDWIDTH", "0") or 0),
                    uri=uri,
                    resolution=a.get("RESOLUTION", ""),
                    audio_group=a.get("AUDIO"),
                    subs_group=a.get("SUBTITLES"),
                ))
    return variants, medias


# When a stream advertises no RESOLUTION we approximate the user's quality
# preference with a bandwidth ceiling instead. Kept GENEROUS on purpose: real
# 1080p HLS is often 8–12 Mbps, and an over-tight cap here silently picked a
# lower-bitrate (blurrier) variant of an unlabeled stream.
_QUALITY_KBPS = {480: 3500, 720: 7000, 1080: 14000, 1440: 24000, 2160: 45000}


def _variant_height(v: _Variant) -> int:
    """Vertical resolution parsed from "1920x1080" (0 = unknown)."""
    m = re.search(r"x(\d+)", v.resolution or "")
    return int(m.group(1)) if m else 0


def _select_variant(variants: list[_Variant], prefer_height: int) -> _Variant:
    """Choose 1080p when available, otherwise the highest quality available.

    Among variants at the chosen height, highest bandwidth wins. Streams
    without resolution info fall back to a bandwidth cap mapped from the
    preferred height.
    """
    with_height = [v for v in variants if _variant_height(v)]
    if with_height:
        under = [v for v in with_height if _variant_height(v) <= prefer_height]
        pool = under if under else with_height
        best_h = max(_variant_height(v) for v in pool)
        return max((v for v in pool if _variant_height(v) == best_h), key=lambda v: v.bandwidth)
    cap = _QUALITY_KBPS.get(prefer_height, 8500) * 1000
    under = [v for v in variants if v.bandwidth <= cap]
    return max(under, key=lambda v: v.bandwidth) if under else max(variants, key=lambda v: v.bandwidth)


def _parse_media_playlist(text: str, base_url: str) -> list[_Segment]:
    segments: list[_Segment] = []
    key_uri: Optional[str] = None
    key_iv: Optional[bytes] = None
    seq = 0
    duration = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-KEY:"):
            a = _parse_attrs(line)
            method = a.get("METHOD", "NONE").upper()
            if method == "NONE":
                key_uri, key_iv = None, None
            elif method == "AES-128":
                key_uri = urljoin(base_url, a["URI"]) if a.get("URI") else None
                iv = a.get("IV", "")
                key_iv = bytes.fromhex(iv[2:]) if iv.lower().startswith("0x") else None
            else:
                raise HlsError(f"Unsupported encryption method: {method}")
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                duration = 0.0
        elif line and not line.startswith("#"):
            segments.append(_Segment(
                uri=urljoin(base_url, line),
                duration=duration,
                seq=seq,
                key_uri=key_uri,
                key_iv=key_iv,
            ))
            seq += 1
            duration = 0.0
    return segments


def _parse_init_uri(text: str, base_url: str) -> Optional[str]:
    """Return the fMP4 initialization segment from EXT-X-MAP, if present."""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attrs(line)
            uri = attrs.get("URI")
            if uri:
                return urljoin(base_url, uri)
    return None


def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = decryptor.update(data) + decryptor.finalize()
    if out:  # strip PKCS7 padding
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
    return out


class _ProgressTracker:
    def __init__(self, total_segments: int, est_total: int) -> None:
        self.p = Progress(total_segments=total_segments, est_total_bytes=est_total)
        self._window: list[tuple[float, int]] = []

    def add(self, seg_bytes: int) -> None:
        self.p.done_segments += 1
        self.p.bytes_done += seg_bytes
        now = time.monotonic()
        self._window.append((now, self.p.bytes_done))
        while self._window and now - self._window[0][0] > 6.0:
            self._window.pop(0)
        if len(self._window) >= 2:
            (t0, b0), (t1, b1) = self._window[0], self._window[-1]
            self.p.speed_bps = (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0


class _Downloader:
    def __init__(self, client: httpx.AsyncClient, referer: str,
                 pause: asyncio.Event, cancel: asyncio.Event,
                 yield_ev: asyncio.Event | None = None,
                 skip_ev: asyncio.Event | None = None,
                 connections: int = _DEFAULT_CONNECTIONS) -> None:
        self.client = client
        self.referer = referer
        self.pause = pause
        self.cancel = cancel
        self.yield_ev = yield_ev
        self.skip_ev = skip_ev
        self.connections = max(1, min(16, connections))
        self._keys: dict[str, bytes] = {}

    async def _get_key(self, uri: str) -> bytes:
        if uri not in self._keys:
            resp = await self.client.get(uri, headers=_headers(self.referer))
            resp.raise_for_status()
            self._keys[uri] = resp.content
        return self._keys[uri]

    async def _fetch_bytes(self, seg: _Segment) -> bytes:
        last_exc: Exception | None = None
        auth_fails = 0
        for attempt in range(1, _SEGMENT_RETRIES + 1):
            if self.cancel.is_set():
                raise DownloadCancelled()
            if self.skip_ev is not None and self.skip_ev.is_set():
                raise SourceSkipped()
            try:
                resp = await self.client.get(seg.uri, headers=_headers(self.referer))
                if resp.status_code in (401, 403):
                    # Token expired/rejected — backoff won't fix authorization.
                    # One quick re-check (CDN blips exist), then bail out so
                    # the manager can fetch a FRESH link instead of freezing
                    # the download in minutes of pointless retries.
                    auth_fails += 1
                    if auth_fails >= 2:
                        raise SegmentAuthError(f"HTTP {resp.status_code} on segment")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                data = resp.content
                if seg.key_uri:
                    key = await self._get_key(seg.key_uri)
                    iv = seg.key_iv or seg.seq.to_bytes(16, "big")
                    # CPU-bound — run off the event loop so other downloads
                    # (and the API) never wait on this slot's decryption
                    data = await asyncio.to_thread(_decrypt, data, key, iv)
                return data
            except (DownloadCancelled, SegmentAuthError, SourceSkipped):
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < _SEGMENT_RETRIES:
                    wait = min(20.0, _RETRY_BASE * (2 ** (attempt - 1))) + random.uniform(0, 1)
                    # Honour Retry-After when the server sends one
                    if isinstance(exc, httpx.HTTPStatusError):
                        ra = exc.response.headers.get("retry-after")
                        if ra and ra.isdigit():
                            wait = max(wait, min(30.0, float(ra)))
                    await asyncio.sleep(wait)
        raise HlsError(f"Segment failed after {_SEGMENT_RETRIES} attempts: {last_exc}")

    async def fetch_playlist(
        self, segments: list[_Segment], seg_dir: Path, out_file: Path,
        tracker: _ProgressTracker, on_progress: ProgressFn, log: LogFn,
        init_path: Path | None = None,
    ) -> int:
        """Download all segments (resumable), concat into out_file, return bytes.

        All disk I/O runs in threads — each download slot must never block the
        shared event loop (that would stall every OTHER running download too).
        """
        seg_dir.mkdir(parents=True, exist_ok=True)

        # Scan already-downloaded segments (resume) in a thread — hundreds of
        # stat() calls on a slow disk otherwise freeze the loop.
        def scan() -> tuple[list[tuple[int, _Segment]], int]:
            out, done_bytes = [], 0
            for i, s in enumerate(segments):
                f = seg_dir / f"{i:05d}.ts"
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                if size > 0:
                    done_bytes += size
                else:
                    out.append((i, s))
            return out, done_bytes

        pending, existing_bytes = await asyncio.to_thread(scan)
        existing = len(segments) - len(pending)
        if existing:
            tracker.p.done_segments = existing
            tracker.p.bytes_done = existing_bytes   # count resumed bytes, not just this session's
            log(f"Resuming — {existing}/{len(segments)} segments already downloaded")
            on_progress(tracker.p)

        # Worker pool: N connections each pull the next segment as soon as
        # they finish their current one. (The old fixed-batch loop waited for
        # the SLOWEST segment of every batch — one laggard or retry backoff
        # stalled the whole pipeline.)
        work: asyncio.Queue = asyncio.Queue()
        for item in pending:
            work.put_nowait(item)
        state = {"last_log": 0.0}

        state["last_done"] = time.monotonic()

        async def worker() -> None:
            while True:
                try:
                    i, seg = work.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if self.cancel.is_set():
                    raise DownloadCancelled()
                if self.yield_ev is not None and self.yield_ev.is_set():
                    raise DownloadRequeued()
                if self.skip_ev is not None and self.skip_ev.is_set():
                    raise SourceSkipped()
                await self.pause.wait()
                data = await self._fetch_bytes(seg)
                await asyncio.to_thread((seg_dir / f"{i:05d}.ts").write_bytes, data)
                tracker.add(len(data))
                state["last_done"] = time.monotonic()
                on_progress(tracker.p)
                now = time.monotonic()
                if now - state["last_log"] >= 3.0:
                    state["last_log"] = now
                    pct = tracker.p.done_segments / max(1, tracker.p.total_segments) * 100
                    spd = tracker.p.speed_bps / (1024 * 1024)
                    log(f"Downloading {tracker.p.done_segments}/{tracker.p.total_segments} "
                        f"segments ({pct:.0f}%) at {spd:.1f} MB/s")

        async def watchdog() -> None:
            """A source that silently stops feeding data must not hang the
            slot forever — no completed segment for _STALL_TIMEOUT while
            un-paused means the source is dead; move on."""
            while True:
                await asyncio.sleep(5)
                if not self.pause.is_set():           # user-paused — not a stall
                    state["last_done"] = time.monotonic()
                    continue
                if self.skip_ev is not None and self.skip_ev.is_set():
                    raise SourceSkipped()
                if time.monotonic() - state["last_done"] > _STALL_TIMEOUT:
                    raise SourceStalledError(
                        f"no data received for {int(_STALL_TIMEOUT)}s")

        n_workers = max(1, min(self.connections, len(pending) or 1))
        workers = asyncio.gather(*[asyncio.create_task(worker()) for _ in range(n_workers)])
        watch = asyncio.create_task(watchdog())
        try:
            # Race the workers against the watchdog: workers finishing wins;
            # the watchdog only ever "finishes" by raising (stall/skip).
            done, _ = await asyncio.wait({workers, watch},
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                t.result()   # propagate whichever exception ended the race
        finally:
            watch.cancel()
            workers.cancel()
            await asyncio.gather(workers, watch, return_exceptions=True)

        # Concatenate segments in order into the output file. This reads and
        # writes the ENTIRE video — done in a thread so a slot finishing its
        # download never stalls the other active slots.
        def concat() -> int:
            total = 0
            with open(out_file, "wb") as out:
                if init_path is not None and init_path.exists():
                    init = init_path.read_bytes()
                    out.write(init)
                    total += len(init)
                for i in range(len(segments)):
                    f = seg_dir / f"{i:05d}.ts"
                    data = f.read_bytes()
                    out.write(data)
                    total += len(data)
            return total

        return await asyncio.to_thread(concat)


def _pick_subtitle_media(medias: list[_Media], group: Optional[str],
                         preferred_language: str = "en") -> Optional[_Media]:
    subs = [m for m in medias if m.type.upper() == "SUBTITLES" and m.uri
            and (group is None or m.group_id == group)]
    if not subs:
        return None
    from src.subtitles import language_matches
    preferred = [m for m in subs if language_matches(m.language or m.name, preferred_language)]
    pool = preferred or subs
    default = [m for m in pool if m.default]
    return (default or pool)[0]


def _pick_audio_media(medias: list[_Media], group: Optional[str],
                      preferred_language: Optional[str] = None) -> Optional[_Media]:
    if not group:
        return None
    audios = [m for m in medias if m.type.upper() == "AUDIO" and m.group_id == group and m.uri]
    if not audios:
        return None
    if preferred_language:
        from src.subtitles import language_matches
        preferred = [m for m in audios
                     if language_matches(m.language or m.name, preferred_language)]
        if preferred:
            return (next((m for m in preferred if m.default), None) or preferred[0])
    default = [m for m in audios if m.default]
    return (default or audios)[0]


async def download_hls(
    url: str,
    work_dir: Path,
    *,
    prefer_height: int = 1080,
    audio_language: Optional[str] = None,
    subtitle_language: str = "en",
    connections: int = _DEFAULT_CONNECTIONS,
    referer: str,
    pause: asyncio.Event,
    cancel: asyncio.Event,
    on_progress: ProgressFn,
    log: LogFn,
    yield_ev: asyncio.Event | None = None,
    skip_ev: asyncio.Event | None = None,
) -> HlsResult:
    """Download an HLS stream (master or media playlist URL) into work_dir.

    Resumable: segments already present in work_dir/segments are skipped.
    `connections` = parallel segment fetches (Settings-tunable).
    `skip_ev` set = the admin pressed "Next source" (raises SourceSkipped).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    video_path = work_dir / "video.ts"
    audio_path: Optional[Path] = None
    subtitle_path: Optional[Path] = None
    connections = max(1, min(16, connections))

    async with httpx.AsyncClient(
        verify=False, timeout=httpx.Timeout(30, read=90), follow_redirects=True,
        limits=httpx.Limits(max_connections=connections + 2),
    ) as client:
        resp = await client.get(url, headers=_headers(referer, dest="document"))
        resp.raise_for_status()
        text = resp.text

        bandwidth = 0
        resolution = ""
        audio_media: Optional[_Media] = None
        sub_media: Optional[_Media] = None
        init_uri: Optional[str] = None
        audio_init_uri: Optional[str] = None
        media_url = url

        if "#EXT-X-STREAM-INF" in text:
            variants, medias = _parse_master(text, url)
            if not variants:
                raise HlsError("Master playlist has no variants")
            # Pick a variant, then PREFLIGHT its first segment: some hosts
            # advertise a 1080p tier whose segments just 403 — step down to
            # the next quality instead of failing the whole download.
            candidates = list(variants)
            while True:
                chosen = _select_variant(candidates, prefer_height)
                bandwidth = chosen.bandwidth
                resolution = chosen.resolution
                media_url = chosen.uri
                audio_media = _pick_audio_media(medias, chosen.audio_group, audio_language)
                sub_media = _pick_subtitle_media(medias, chosen.subs_group, subtitle_language)
                if audio_media:
                    log(f"Audio track: {audio_media.name or audio_media.language or 'default'}")
                if sub_media:
                    log(f"Subtitle track: {sub_media.name or sub_media.language or subtitle_language}")
                r2 = await client.get(media_url, headers=_headers(referer))
                r2.raise_for_status()
                text = r2.text
                segments = _parse_media_playlist(text, media_url)
                init_uri = _parse_init_uri(text, media_url)
                if not segments:
                    raise HlsError("Media playlist has no segments")
                probe = await client.get(segments[0].uri, headers=_headers(referer))
                if probe.status_code in (401, 403):
                    log(f"{resolution or 'top'} tier rejected by the host "
                        f"(HTTP {probe.status_code}) — trying the next quality down")
                    bad_h = _variant_height(chosen)
                    candidates = [v for v in candidates
                                  if v is not chosen and _variant_height(v) != bad_h]
                    if not candidates:
                        raise SegmentAuthError("every quality tier was rejected (403)")
                    continue
                log(f"Selected {resolution or 'best'} variant (~{bandwidth // 1000} kbps) "
                    f"of {len(variants)} (preferred {prefer_height}p)")
                break
        else:
            segments = _parse_media_playlist(text, media_url)
            init_uri = _parse_init_uri(text, media_url)
            if not segments:
                raise HlsError("Media playlist has no segments")

        # Resume safety: partial segments from a DIFFERENT stream must never
        # be mixed into this one (indexes would align but content wouldn't).
        # Fingerprint = resolution + segment count + total duration: identical
        # for the same stream behind fresh tokens/domains, different for any
        # other variant or encode.
        marker = work_dir / "segments" / ".variant"
        variant_id = (f"{resolution}|{len(segments)}|"
                      f"{int(sum(s.duration for s in segments))}")
        if marker.exists() and marker.read_text() != variant_id:
            log("Partial download was a different quality — starting this one fresh")
            await asyncio.to_thread(_clear_dir, work_dir / "segments")
            await asyncio.to_thread(_clear_dir, work_dir / "audio_segments")
        (work_dir / "segments").mkdir(parents=True, exist_ok=True)
        marker.write_text(variant_id)
        duration = sum(s.duration for s in segments)
        est_total = int(bandwidth * duration / 8) if bandwidth and duration else 0

        log(f"{len(segments)} segments, ~{int(duration // 60)}m runtime")

        tracker = _ProgressTracker(len(segments), est_total)
        dl = _Downloader(client, referer, pause, cancel, yield_ev, skip_ev,
                         connections=connections)

        init_path: Optional[Path] = None
        if init_uri:
            init_path = work_dir / "video-init.mp4"
            try:
                init_resp = await client.get(init_uri, headers=_headers(referer))
                init_resp.raise_for_status()
                if not init_resp.content:
                    raise HlsError("empty fMP4 initialization segment")
                await asyncio.to_thread(init_path.write_bytes, init_resp.content)
            except Exception as exc:
                raise HlsError(f"fMP4 initialization segment failed: {exc}") from exc

        video_bytes = await dl.fetch_playlist(
            segments, work_dir / "segments", video_path, tracker, on_progress, log,
            init_path=init_path,
        )
        if not est_total:
            tracker.p.est_total_bytes = video_bytes

        # Separate audio rendition (video variant may be muxed-audio-less)
        if audio_media and audio_media.uri:
            try:
                log("Downloading separate audio track…")
                ar = await client.get(audio_media.uri, headers=_headers(referer))
                ar.raise_for_status()
                asegs = _parse_media_playlist(ar.text, audio_media.uri)
                audio_init_uri = _parse_init_uri(ar.text, audio_media.uri)
                if asegs:
                    audio_path = work_dir / "audio.ts"
                    audio_init_path: Optional[Path] = None
                    if audio_init_uri:
                        audio_init_path = work_dir / "audio-init.mp4"
                        ai = await client.get(audio_init_uri, headers=_headers(referer))
                        ai.raise_for_status()
                        if not ai.content:
                            raise HlsError("empty audio fMP4 initialization segment")
                        await asyncio.to_thread(audio_init_path.write_bytes, ai.content)
                    atracker = _ProgressTracker(len(asegs), 0)
                    await dl.fetch_playlist(
                        asegs, work_dir / "audio_segments", audio_path, atracker,
                        lambda _p: None, log, init_path=audio_init_path,
                    )
            except DownloadCancelled:
                raise
            except Exception as exc:
                log(f"Audio track failed ({exc}); continuing without")
                audio_path = None

        # Subtitles (best effort)
        if sub_media and sub_media.uri:
            try:
                subtitle_path = await _download_subtitles(client, sub_media.uri, referer, work_dir)
                if subtitle_path:
                    log("Downloaded subtitles")
            except Exception as exc:
                log(f"Subtitles failed ({exc}); continuing without")
                subtitle_path = None

    total = video_path.stat().st_size + (audio_path.stat().st_size if audio_path else 0)
    return HlsResult(
        video_path=video_path,
        audio_path=audio_path,
        subtitle_path=subtitle_path,
        duration=duration,
        total_bytes=total,
        bandwidth=bandwidth,
        resolution=resolution,
    )


async def _download_subtitles(client: httpx.AsyncClient, url: str, referer: str,
                              work_dir: Path) -> Optional[Path]:
    r = await client.get(url, headers=_headers(referer))
    r.raise_for_status()
    text = r.text
    dest = work_dir / "subs.vtt"

    from src.subtitles import clean_vtt_text

    if text.lstrip().startswith("WEBVTT"):
        dest.write_text(clean_vtt_text(text)[0], encoding="utf-8")
        return dest

    parts: list[str] = []
    for seg in _parse_media_playlist(text, url):
        resp = await client.get(seg.uri, headers=_headers(referer))
        resp.raise_for_status()
        body = resp.text
        if parts:
            lines = body.splitlines()
            start = 0
            for i, ln in enumerate(lines):
                if ln.strip() == "" and i > 0:
                    start = i + 1
                    break
            body = "\n".join(lines[start:])
        parts.append(body)
    if not parts:
        return None
    dest.write_text(clean_vtt_text("\n".join(parts))[0], encoding="utf-8")
    return dest
