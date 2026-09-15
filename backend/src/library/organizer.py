"""
Maps a finished download to a Jellyfin-compatible path and moves the file there.

Movies:  <media_dir>/Movies/<Title> (<Year>)/<Title> (<Year>).mp4
TV:      <media_dir>/Shows/<Series Title> (<Year>)/Season <NN>/<Series Title> - S<NN>E<NN> - <Episode Title>.mp4

Jellyfin NFO sidecar files are written alongside each file so the library
scan picks up IMDB metadata immediately without an internet lookup.
"""
from __future__ import annotations

import html as html_module
import os
import re
import shutil
import tempfile
from pathlib import Path

from src.config import settings

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r" {2,}")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text so the file is only ever complete or absent — never a 0-byte
    stub. We write to a temp file in the SAME directory, fsync it to force the
    bytes to disk (important on slow/flaky exFAT USB drives, where an un-synced
    write can show up as 0 KB), then atomically rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe(name: str) -> str:
    name = _ILLEGAL.sub("", name)
    name = _MULTI_SPACE.sub(" ", name)
    return name.strip(" .")


def category_folder(category: str) -> Path:
    """Where a category (e.g. "movies", "shows", "anime_movies") lives — a folder
    under MEDIA_DIR configured in the Settings wizard (src/library/
    categories.py). Unknown keys fall back to the built-in defaults so a job
    can always be routed somewhere."""
    from src.library.categories import category_root
    return category_root(category, "movie" if category == "movies" else "tvSeries")


def _esc(text: str) -> str:
    return html_module.escape(str(text))


def movie_path(title: str, year: int | None, ext: str = ".mp4",
               category: str = "movies") -> Path:
    folder_name = _safe(f"{title} ({year})" if year else title)
    file_name = folder_name + ext
    return category_folder(category or "movies") / folder_name / file_name


def episode_path(
    series_title: str,
    year: int | None,
    season: int,
    episode: int,
    episode_title: str,
    ext: str = ".mp4",
    category: str = "shows",
) -> Path:
    series_folder = _safe(f"{series_title} ({year})" if year else series_title)
    season_folder = f"Season {season:02d}"
    file_name = _safe(
        f"{series_title} - S{season:02d}E{episode:02d} - {episode_title}"
    ) + ext
    return (
        category_folder(category or "shows")
        / series_folder
        / season_folder
        / file_name
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def has_subtitles(mp4_path: Path) -> bool:
    """True if the MP4 has an embedded subtitle track or an external .vtt/.srt
    sidecar next to it."""
    p = Path(mp4_path)
    stem = p.with_suffix("")
    for ext in (".vtt", ".en.vtt", ".srt", ".en.srt"):
        if Path(str(stem) + ext).exists():
            return True
    if any(p.is_file() for p in p.parent.glob(f"{p.stem}.*.vtt")):
        return True
    try:
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        if out.stdout.strip():
            return True
    except Exception:
        pass
    return False


def subtitle_sidecar_path(mp4_path: Path, lang: str = "en") -> Path:
    """Jellyfin-style external subtitle path: <video>.<lang>.vtt"""
    p = Path(mp4_path)
    return p.with_suffix(f".{lang}.vtt")


def move_to_library(src: Path, dest: Path) -> None:
    ensure_parent(dest)
    shutil.move(str(src), str(dest))


def series_dir(title: str, year: int | None) -> Path:
    folder_name = _safe(f"{title} ({year})" if year else title)
    return category_folder("shows") / folder_name


def season_dir(title: str, year: int | None, season: int) -> Path:
    return series_dir(title, year) / f"Season {season:02d}"


def delete_job_file(file_path: str) -> None:
    """Delete a job's media file, its NFO/subtitle sidecars, and empty parent dirs."""
    p = Path(file_path)
    if p.exists():
        p.unlink()
    stem = p.with_suffix("")
    for ext in (".nfo", ".vtt", ".en.vtt", ".srt", ".en.srt"):
        side = Path(str(stem) + ext)
        if side.exists():
            side.unlink()
    for side in p.parent.glob(f"{p.stem}.*.vtt"):
        if side.is_file():
            side.unlink()
    _remove_empty_dirs(p.parent)


def resolve_season_folder(jobs) -> Path | None:
    """Find a season's folder on disk from any of its (completed) jobs'
    file_path — more robust than reconstructing it from possibly-renamed
    title/year fields. `jobs` is an iterable of DownloadJob-like objects with
    a `file_path` attribute."""
    for job in jobs:
        if job.file_path:
            p = Path(job.file_path)
            if p.parent.is_dir():
                return p.parent
    return None


def resolve_show_folder(jobs) -> Path | None:
    """Find a show's root folder on disk from any of its episodes' file_path
    (episode path shape: .../Shows/<Series>/Season NN/<file>)."""
    for job in jobs:
        if job.file_path:
            p = Path(job.file_path)
            candidate = p.parent.parent
            if candidate.is_dir():
                return candidate
    return None


def delete_folder_tree(folder: Path) -> None:
    if folder.exists() and folder.is_dir():
        shutil.rmtree(str(folder), ignore_errors=True)


def rename_job_file(
    job,  # DownloadJob — avoid circular import by typing loosely
    new_title: str | None = None,
    new_ep_title: str | None = None,
    new_year: int | None = None,
) -> Path:
    """Rename a job's file to match new title / episode_title / year.

    Updates the filesystem only; the caller must update the DB record.
    Returns the new Path.
    """
    if not job.file_path:
        raise FileNotFoundError("Job has no file_path")
    old_path = Path(job.file_path)

    title = new_title or job.title
    year = new_year if new_year is not None else job.year

    category = getattr(job, "category", None)
    if job.media_type == "movie":
        new_path = movie_path(title, year, category=category or "movies")
    else:
        ep_title = new_ep_title or job.episode_title or f"Episode {job.episode}"
        new_path = episode_path(
            series_title=title,
            year=year,
            season=job.season or 1,
            episode=job.episode or 1,
            episode_title=ep_title,
            category=category or "shows",
        )

    if old_path != new_path:
        ensure_parent(new_path)
        if old_path.exists():
            old_path.rename(new_path)
        # Move sidecars (NFO + subtitles) along with the video
        old_stem, new_stem = old_path.with_suffix(""), new_path.with_suffix("")
        for ext in (".nfo", ".vtt", ".en.vtt", ".srt", ".en.srt"):
            side = Path(str(old_stem) + ext)
            if side.exists():
                side.rename(Path(str(new_stem) + ext))
        for side in old_path.parent.glob(f"{old_stem.name}.*.vtt"):
            if side.is_file():
                side.rename(new_path.parent / f"{new_stem.name}{side.name[len(old_stem.name):]}")
        _remove_empty_dirs(old_path.parent)

    return new_path


def _remove_empty_dirs(path: Path) -> None:
    """Walk up, removing empty directories until we hit MEDIA_DIR."""
    media_root = Path(settings.media_dir)
    current = path
    while current != media_root and current != current.parent:
        try:
            if current.is_dir() and not any(current.iterdir()):
                current.rmdir()
                current = current.parent
            else:
                break
        except Exception:
            break


def _meta_xml(meta: dict | None, indent: str = "  ") -> str:
    """Optional enrichment lines from cached TMDB metadata (src/metadata.py)."""
    if not meta:
        return ""
    lines: list[str] = []
    if meta.get("overview"):
        lines.append(f"<plot>{_esc(meta['overview'])}</plot>")
    if meta.get("tagline"):
        lines.append(f"<tagline>{_esc(meta['tagline'])}</tagline>")
    for g in meta.get("genres") or []:
        lines.append(f"<genre>{_esc(g)}</genre>")
    if meta.get("rating"):
        lines.append(f"<rating>{_esc(meta['rating'])}</rating>")
    if meta.get("runtime"):
        lines.append(f"<runtime>{_esc(meta['runtime'])}</runtime>")
    if meta.get("original_title"):
        lines.append(f"<originaltitle>{_esc(meta['original_title'])}</originaltitle>")
    if meta.get("original_language"):
        lines.append(f"<language>{_esc(meta['original_language'])}</language>")
    if meta.get("tmdb_id"):
        lines.append(f'<uniqueid type="tmdb">{_esc(meta["tmdb_id"])}</uniqueid>')
    return "".join(f"{indent}{ln}\n" for ln in lines)


def write_movie_nfo(dest: Path, title: str, year: int | None, imdb_id: str,
                    meta: dict | None = None) -> None:
    nfo = dest.parent / "movie.nfo"
    _atomic_write_text(nfo,
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
        f"<movie>\n"
        f"  <title>{_esc(title)}</title>\n"
        f"  <year>{_esc(year or '')}</year>\n"
        f'  <uniqueid type="imdb" default="true">{_esc(imdb_id)}</uniqueid>\n'
        f"{_meta_xml(meta)}"
        f"</movie>\n")


def write_episode_nfo(
    dest: Path,
    series_title: str,
    year: int | None,
    imdb_id: str,
    season: int,
    episode: int,
    episode_title: str,
    meta: dict | None = None,
) -> None:
    nfo = dest.with_suffix(".nfo")
    _atomic_write_text(nfo,
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
        f"<episodedetails>\n"
        f"  <title>{_esc(episode_title)}</title>\n"
        f"  <season>{season}</season>\n"
        f"  <episode>{episode}</episode>\n"
        f'  <uniqueid type="imdb" default="true">{_esc(imdb_id)}</uniqueid>\n'
        f"</episodedetails>\n")
    # tvshow.nfo in the series root: written once, but upgraded in place
    # whenever we have metadata to enrich it with.
    show_dir = dest.parent.parent
    tvshow_nfo = show_dir / "tvshow.nfo"
    if meta or not tvshow_nfo.exists():
        _atomic_write_text(tvshow_nfo,
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
            f"<tvshow>\n"
            f"  <title>{_esc(series_title)}</title>\n"
            f"  <year>{_esc(year or '')}</year>\n"
            f'  <uniqueid type="imdb" default="true">{_esc(imdb_id)}</uniqueid>\n'
            f"{_meta_xml(meta)}"
            f"</tvshow>\n")
