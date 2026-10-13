"""
WebVTT ad-stripping.

Subtitle files from public sources routinely carry promo cues at the very
start and very end ("Watch online free …", "www.osdb.link/…", uploader
plugs). Those are junk in a personal library, so every VTT we write gets run
through `clean_vtt_text` — and the admin can sweep the existing library with
the clean-subtitles scan.

Promo cues are BLANKED, not removed: the cue (and its timing line) stays in
the file with an empty payload, so the cue list and its sync against the
audio/video are untouched — the ad text simply never renders.

Only the HEAD and TAIL of the file are considered: a URL quoted in actual
dialogue mid-film must never be touched, so the walk stops at the first
non-promotional cue from either end.
"""
from __future__ import annotations

import re
import math
from pathlib import Path

# How many cues at each end are even eligible for removal.
_EDGE_WINDOW = 6

# A cue is considered an ad when any of its text lines matches one of these.
_AD_PATTERNS = [
    # Links / domains — the single strongest signal
    re.compile(r"https?://|www\.", re.I),
    re.compile(r"\b[a-z0-9-]+\.(?:com|net|org|link|io|to|cc|me|tv|info|biz|site|online|vip|club|pro|fun)\b", re.I),
    # Marketing copy typical of subtitle-site promos
    re.compile(r"watch\s+(?:online|.*free)", re.I),
    re.compile(r"(?:movies?|series|shows?)\s+(?:and|&)\s+(?:series|shows?|tv)\s+free", re.I),
    re.compile(r"free\s+(?:movies?|series|streaming)", re.I),
    re.compile(r"\bopen\s*subtitles?\b|\bosdb\b|\bpodnapisi\b|\bsubscene\b|\byify\b", re.I),
    re.compile(r"advertise\s+your\s+product|remove\s+all\s+ads", re.I),
    re.compile(r"support\s+us\s+and\s+become|rate\s+this\s+subtitle", re.I),
    re.compile(r"api\.osdb|osdb\.link", re.I),
]

_TIMING_RE = re.compile(r"-->")
_CUE_TIMING_RE = re.compile(
    r"^\s*(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+"
    r"(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}(?:[^\S\n].*)?$", re.M,
)


def has_timed_cues(text: str) -> bool:
    """Reject empty/error responses so the next subtitle source can be tried."""
    return _CUE_TIMING_RE.search(text) is not None


def retime_vtt(text: str, offset: float = 0, scale: float = 1) -> str:
    """Apply t' = t * scale + offset, preserving cue text and VTT settings."""
    if not math.isfinite(offset) or not math.isfinite(scale) or abs(offset) > 600 or not 0.8 <= scale <= 1.25:
        raise ValueError("Timing must use an offset within 600 seconds and a scale from 0.8 to 1.25")
    stamp = r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}"
    timing = re.compile(rf"^({stamp})[ \t]+-->[ \t]+({stamp})([^\r\n]*)$", re.M)

    def seconds(value: str) -> float:
        parts = value.split(":")
        return sum(float(part) * 60 ** i for i, part in enumerate(reversed(parts)))

    def formatted(value: float) -> str:
        ms = max(0, round(value * 1000))
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        sec, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{sec:02}.{ms:03}"

    blocks = []
    for block in re.split(r"\n{2,}", text.replace("\r\n", "\n").strip()):
        if block.startswith(("NOTE", "STYLE", "REGION")):
            blocks.append(block)
            continue
        match = timing.search(block)
        if match:
            start = seconds(match[1]) * scale + offset
            end = seconds(match[2]) * scale + offset
            if end <= 0:
                continue
            start = max(0, start)
            line = f"{formatted(start)} --> {formatted(max(start + .001, end))}{match[3]}"
            block = block[:match.start()] + line + block[match.end():]
        blocks.append(block)
    return "\n\n".join(blocks) + "\n"

SUBTITLE_LANGUAGES = (
    ("en", "English"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("pt", "Portuguese"), ("it", "Italian"),
    ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese"),
    ("ar", "Arabic"), ("ru", "Russian"), ("hi", "Hindi"),
)


def normalize_language(value: str | None, fallback: str = "en") -> str:
    """Normalize a subtitle/audio language label to its base ISO code."""
    raw = (value or "").strip().lower().replace("_", "-")
    code = raw.split("-", 1)[0]
    return code if re.fullmatch(r"[a-z]{2,3}", code) else fallback


def language_matches(label: str | None, preferred: str) -> bool:
    """Match en, en-US, or labels such as English consistently."""
    text = (label or "").strip().lower()
    wanted = normalize_language(preferred)
    if text.startswith(wanted) or wanted in text:
        return True
    names = {code: name.lower() for code, name in SUBTITLE_LANGUAGES}
    return names.get(wanted, "") in text


def _cue_text_lines(block: str) -> list[str]:
    """The payload lines of a cue block — everything after the timing line."""
    lines = block.splitlines()
    for n, line in enumerate(lines):
        if _TIMING_RE.search(line):
            return [ln for ln in lines[n + 1:] if ln.strip()]
    return []


def _is_ad(text_lines: list[str]) -> bool:
    return any(pat.search(line) for line in text_lines for pat in _AD_PATTERNS)


def _blank_cue(block: str) -> str:
    """Keep the cue's identifier + timing line, drop its text — the cue stays
    in the file (timing/sync untouched) but renders nothing."""
    lines = block.splitlines()
    for n, line in enumerate(lines):
        if _TIMING_RE.search(line):
            return "\n".join(lines[:n + 1])
    return block


def clean_vtt_text(text: str) -> tuple[str, int]:
    """Blank promo cues at the head and tail of a WebVTT document.

    Returns (cleaned_text, blanked_cue_count). The cues themselves (and their
    timings) are kept — only their text is emptied — and blocks that aren't
    cues (WEBVTT header, NOTE, STYLE, REGION) are preserved untouched.
    """
    # Normalise newlines; VTT blocks are separated by blank lines.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized.strip())
    if not blocks:
        return text, 0

    # Indices of blocks that are actual cues (contain a --> timing line)
    cue_idx = [i for i, b in enumerate(blocks) if _TIMING_RE.search(b)]
    if not cue_idx:
        return text, 0

    blank: set[int] = set()
    # Head: walk forward through the first cues, stop at the first clean one
    # (already-empty cues from an earlier pass are skipped, not stoppers)
    for i in cue_idx[:_EDGE_WINDOW]:
        lines = _cue_text_lines(blocks[i])
        if not lines:
            continue
        if _is_ad(lines):
            blank.add(i)
        else:
            break
    # Tail: walk backward through the last cues, stop at the first clean one
    for i in reversed(cue_idx[-_EDGE_WINDOW:]):
        if i in blank:
            break  # tiny file — head pass already consumed it
        lines = _cue_text_lines(blocks[i])
        if not lines:
            continue
        if _is_ad(lines):
            blank.add(i)
        else:
            break

    if not blank:
        return text, 0
    # Never blank every cue — a fully-matching "subtitle" is left as-is
    # (that pattern smells like a false positive, not a real ad file)
    if len(blank) >= len(cue_idx):
        return text, 0

    out = [_blank_cue(b) if i in blank else b for i, b in enumerate(blocks)]
    return "\n\n".join(out).strip() + "\n", len(blank)


def clean_vtt_file(path: Path) -> int:
    """Clean a .vtt file in place. Returns the number of cues blanked
    (0 = nothing to do or not a readable VTT)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    cleaned, removed = clean_vtt_text(text)
    if removed:
        path.write_text(cleaned, encoding="utf-8")
    return removed
