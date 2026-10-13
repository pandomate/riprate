import { useEffect, useMemo, useState } from "react";
import { useAuth } from "../App";
import {
  admin, FolderFile, Job, library, LibraryItem, SeasonGap, userLibrary, watch,
} from "../api/client";
import { fmtBytes, Modal, useToast } from "../components/ui";
import {
  IChevron, IDownload, IFilm, IPencil, ITrash, ITv,
} from "../components/icons";

/* Library = file management. Every movie/episode card lists its actual
   on-disk files (video/nfo/vtt/art) with per-file download / rename / delete,
   and a card-level "Download" that zips everything belonging to that item.
   Season and show headers zip their whole folder. No artwork, no hover
   tricks — the Watch page is the pretty one. */

interface Row {
  jobId?: number;              // download job id (all roles)
  imdb_id: string;
  title: string;
  year: number | null;
  media_type: string;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  size?: number | null;        // admin only
  mine: boolean;               // requested by current user
}

type ToastFn = (m: string, k?: "success" | "error" | "info") => void;

const MOVIES_PER = 10;
const SHOWS_PER = 8;

export default function Library() {
  const user = useAuth()!;
  const isAdmin = user.role === "admin";
  const toast = useToast();
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [movieQuery, setMovieQuery] = useState("");
  const [showQuery, setShowQuery] = useState("");
  const [moviePage, setMoviePage] = useState(0);
  const [showPage, setShowPage] = useState(0);

  const load = async () => {
    if (isAdmin) {
      const jobs = await admin.library();
      setRows(jobs.map((j: Job) => ({
        jobId: j.id, imdb_id: j.imdb_id, title: j.title, year: j.year,
        media_type: j.media_type, season: j.season, episode: j.episode,
        episode_title: j.episode_title, size: j.total_bytes,
        mine: j.requested_by === user.username,
      })));
    } else {
      const items = await userLibrary.browse();
      setRows(items.map((it: LibraryItem) => ({
        jobId: it.id, imdb_id: it.imdb_id, title: it.title, year: it.year,
        media_type: it.media_type, season: it.season, episode: it.episode,
        episode_title: it.episode_title, mine: it.requested_by_me,
      })));
    }
    setLoading(false);
  };
  useEffect(() => {
    load().catch(() => {}).finally(() => setLoading(false));
    const timer = window.setInterval(() => { load().catch(() => {}); }, 15000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => { setMoviePage(0); }, [movieQuery]);
  useEffect(() => { setShowPage(0); }, [showQuery]);

  const sortFn = (a: { title: string }, b: { title: string }) => a.title.localeCompare(b.title);

  const movies = useMemo(() => {
    const q = movieQuery.trim().toLowerCase();
    return rows.filter((r) => r.media_type === "movie" && r.title.toLowerCase().includes(q)).sort(sortFn);
  }, [rows, movieQuery]);

  const shows = useMemo(() => {
    const q = showQuery.trim().toLowerCase();
    const map = new Map<string, { imdb_id: string; title: string; year: number | null; rows: Row[] }>();
    for (const r of rows.filter((x) => x.media_type === "tvSeries")) {
      const g = map.get(r.imdb_id) ?? { imdb_id: r.imdb_id, title: r.title, year: r.year, rows: [] };
      g.rows.push(r);
      map.set(r.imdb_id, g);
    }
    let list = Array.from(map.values());
    if (q) {
      list = list.filter((s) =>
        s.title.toLowerCase().includes(q) ||
        s.rows.some((r) => (r.episode_title ?? "").toLowerCase().includes(q))
      );
    }
    return list.sort(sortFn);
  }, [rows, showQuery]);

  const stats = useMemo(() => {
    const movieCount = new Set(rows.filter((r) => r.media_type === "movie").map((r) => r.imdb_id)).size;
    const showSet = new Set(rows.filter((r) => r.media_type === "tvSeries").map((r) => r.imdb_id));
    const episodes = rows.filter((r) => r.media_type === "tvSeries").length;
    return { movies: movieCount, shows: showSet.size, episodes };
  }, [rows]);

  const moviePages = Math.ceil(movies.length / MOVIES_PER);
  const showPages = Math.ceil(shows.length / SHOWS_PER);
  const pagedMovies = movies.slice(moviePage * MOVIES_PER, moviePage * MOVIES_PER + MOVIES_PER);
  const pagedShows = shows.slice(showPage * SHOWS_PER, showPage * SHOWS_PER + SHOWS_PER);
  const episodeFilter = showQuery.trim().toLowerCase();

  if (loading) return <p className="text-gray-500">Loading…</p>;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Library</h1>
        <p className="text-gray-500 text-sm mt-1">
          {isAdmin
            ? "Every file on disk — download, rename or delete them here."
            : "Browse and download the collection. Titles you requested are marked."}
        </p>
      </header>

      <div className="grid grid-cols-3 gap-3">
        <LibStat label="Movies" value={stats.movies} />
        <LibStat label="Shows" value={stats.shows} />
        <LibStat label="Episodes" value={stats.episodes} />
      </div>

      {rows.length === 0 ? (
        <div className="card text-gray-500 text-center py-10">The library is empty.</div>
      ) : (
        <>
          <section>
            <SectionHeader icon={<IFilm size={15} />} title={`Movies (${movies.length})`}
              page={moviePage} pages={moviePages} onPage={setMoviePage}
              search={movieQuery} onSearch={setMovieQuery} placeholder="Search movies…" />
            {movies.length === 0 ? (
              <p className="text-gray-600 text-sm">No movies match.</p>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {pagedMovies.map((m) => (
                  <MovieCard key={m.imdb_id + (m.jobId ?? "")} row={m} isAdmin={isAdmin} onChanged={load} toast={toast} />
                ))}
              </div>
            )}
          </section>

          <section>
            <SectionHeader icon={<ITv size={15} />} title={`TV Shows (${shows.length})`}
              page={showPage} pages={showPages} onPage={setShowPage}
              search={showQuery} onSearch={setShowQuery} placeholder="Search shows or episode names…" />
            {shows.length === 0 ? (
              <p className="text-gray-600 text-sm">No shows match.</p>
            ) : (
              <div className="space-y-2">
                {pagedShows.map((s) => (
                  <ShowNode key={s.imdb_id} show={s} episodeFilter={episodeFilter} isAdmin={isAdmin} onChanged={load} toast={toast} />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function LibStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="card py-3">
      <div className="text-2xl font-bold text-gray-100">{value}</div>
      <div className="text-xs text-gray-500 uppercase tracking-wide mt-0.5">{label}</div>
    </div>
  );
}

function SectionHeader({ icon, title, page, pages, onPage, search, onSearch, placeholder }: {
  icon: React.ReactNode; title: string; page: number; pages: number; onPage: (p: number) => void;
  search: string; onSearch: (v: string) => void; placeholder: string;
}) {
  return (
    <div className="flex items-center gap-3 mb-3 flex-wrap">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500 flex items-center gap-1.5">
        {icon} {title}
      </h2>
      <input className="input py-1 max-w-xs h-8 text-sm" placeholder={placeholder}
        value={search} onChange={(e) => onSearch(e.target.value)} />
      {pages > 1 && (
        <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
          <button className="btn-secondary btn-xs" disabled={page === 0} onClick={() => onPage(page - 1)}>‹ Prev</button>
          <span>Page {page + 1} / {pages}</span>
          <button className="btn-secondary btn-xs" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>Next ›</button>
        </div>
      )}
    </div>
  );
}

/* ── Shared per-file row (download / rename / delete) ───────────────────── */

function FileRow({ file, jobId, isAdmin, onChanged, toast }: {
  file: FolderFile; jobId: number; isAdmin: boolean; onChanged: () => void; toast: ToastFn;
}) {
  const [renaming, setRenaming] = useState(false);

  const del = async () => {
    if (!confirm(`Delete "${file.name}"?${file.name.endsWith(".mp4") ? " This removes the video from the library." : ""}`)) return;
    try {
      const res = await library.deleteOneFile(jobId, file.name);
      toast(res.deleted_job ? "Video and its library entry deleted" : "File deleted", "success");
      onChanged();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Delete failed", "error");
    }
  };

  return (
    <div className="flex items-center gap-2 text-xs py-1 px-2 rounded-md bg-gray-900/50 border border-gray-800/70">
      <span className="truncate flex-1 text-gray-300" title={file.name}>{file.name}</span>
      <span className="text-gray-600 flex-shrink-0 tabular-nums">{fmtBytes(file.size)}</span>
      <a className="p-1 rounded text-gray-400 hover:text-gray-100 hover:bg-gray-700/60 flex-shrink-0"
        href={watch.fileDownloadUrl(jobId, file.name)} download title="Download this file">
        <IDownload size={14} />
      </a>
      {isAdmin && (
        <>
          <button className="p-1 rounded text-gray-400 hover:text-gray-100 hover:bg-gray-700/60 flex-shrink-0"
            onClick={() => setRenaming(true)} title="Rename this file"><IPencil size={14} /></button>
          <button className="p-1 rounded text-red-400/80 hover:text-red-300 hover:bg-gray-700/60 flex-shrink-0"
            onClick={del} title="Delete this file"><ITrash size={14} /></button>
        </>
      )}
      {renaming && (
        <FileRenameModal
          name={file.name}
          onClose={() => setRenaming(false)}
          onSubmit={async (newName) => {
            await library.renameOneFile(jobId, file.name, newName);
            toast("File renamed", "success");
            setRenaming(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

function FileList({ files, jobId, isAdmin, onChanged, toast, emptyText = "No files found." }: {
  files: FolderFile[] | null; jobId: number; isAdmin: boolean;
  onChanged: () => void; toast: ToastFn; emptyText?: string;
}) {
  if (files === null) return <p className="text-xs text-gray-600 px-1">Loading files…</p>;
  if (files.length === 0) return <p className="text-xs text-gray-600 px-1">{emptyText}</p>;
  return (
    <div className="space-y-1">
      {files.map((f) => (
        <FileRow key={f.name} file={f} jobId={jobId} isAdmin={isAdmin} onChanged={onChanged} toast={toast} />
      ))}
    </div>
  );
}

/* ── Movie card: title row + its files ──────────────────────────────────── */

function MovieCard({ row, isAdmin, onChanged, toast }: {
  row: Row; isAdmin: boolean; onChanged: () => void; toast: ToastFn;
}) {
  const [editing, setEditing] = useState(false);
  const [files, setFiles] = useState<FolderFile[] | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    if (row.jobId == null) return;
    let cancelled = false;
    watch.itemFiles(row.jobId).then((d) => { if (!cancelled) setFiles(d.files); })
      .catch(() => { if (!cancelled) setFiles([]); });
    return () => { cancelled = true; };
  }, [row.jobId, version]);

  const refresh = () => { setVersion((v) => v + 1); };
  const changed = () => { refresh(); onChanged(); };

  const del = async () => {
    if (!row.jobId) return;
    if (!confirm(`Delete "${row.title}" and all its files?`)) return;
    try {
      await library.deleteFile(row.jobId);
      toast("Deleted", "success");
      onChanged();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Delete failed", "error");
    }
  };

  return (
    <div className="card py-3 space-y-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium truncate">{row.title}</span>
            {row.year && <span className="text-xs text-gray-500">({row.year})</span>}
            {row.mine && <span className="badge-neutral">requested by you</span>}
          </div>
        </div>
        <div className="flex gap-1.5 flex-shrink-0">
          {row.jobId != null && (
            <a className="btn-secondary btn-xs inline-flex items-center gap-1"
              href={watch.itemZipUrl(row.jobId)} download
              title="Download every file of this movie as one .zip">
              <IDownload size={13} /> Download
            </a>
          )}
          {isAdmin && (
            <>
              <button className="btn-ghost btn-xs inline-flex items-center gap-1" onClick={() => setEditing(true)}>
                <IPencil size={13} /> Rename
              </button>
              <button className="btn-ghost btn-xs text-red-400 inline-flex items-center gap-1" onClick={del}>
                <ITrash size={13} /> Delete
              </button>
            </>
          )}
        </div>
      </div>
      {row.jobId != null && (
        <FileList files={files} jobId={row.jobId} isAdmin={isAdmin} onChanged={changed} toast={toast} />
      )}
      {editing && row.jobId != null && (
        <RenameModal
          title="Rename movie"
          initialTitle={row.title}
          initialYear={row.year}
          onClose={() => setEditing(false)}
          onSubmit={async (title, year) => {
            await library.renameJob(row.jobId!, { title, year: year ?? undefined });
            toast("Renamed", "success");
            setEditing(false);
            changed();
          }}
        />
      )}
    </div>
  );
}

/* ── Show tree: show → seasons → episode cards with their files ─────────── */

function ShowNode({ show, episodeFilter = "", isAdmin, onChanged, toast }: {
  show: { imdb_id: string; title: string; year: number | null; rows: Row[] };
  episodeFilter?: string;
  isAdmin: boolean;
  onChanged: () => void;
  toast: ToastFn;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [gaps, setGaps] = useState<Record<number, SeasonGap> | null>(null);
  const [checking, setChecking] = useState(false);

  // When searching episode names, only keep matching episodes and auto-expand
  const epMatch = episodeFilter && show.rows.some((r) => (r.episode_title ?? "").toLowerCase().includes(episodeFilter));
  const visibleRows = epMatch
    ? show.rows.filter((r) => (r.episode_title ?? "").toLowerCase().includes(episodeFilter))
    : show.rows;

  const seasons = useMemo(() => {
    const map = new Map<number, Row[]>();
    for (const r of visibleRows) {
      const s = r.season ?? 1;
      map.set(s, [...(map.get(s) ?? []), r]);
    }
    return Array.from(map.entries()).sort((a, b) => a[0] - b[0]);
  }, [visibleRows]);

  const checkGaps = async () => {
    setChecking(true);
    try {
      const res = await admin.gaps(show.imdb_id);
      const map: Record<number, SeasonGap> = {};
      res.seasons.forEach((g) => { map[g.season] = g; });
      setGaps(map);
      setOpen(true);
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Could not check episodes", "error");
    } finally { setChecking(false); }
  };

  const totalMissing = gaps ? Object.values(gaps).reduce((a, g) => a + g.missing.length, 0) : 0;

  const delShow = async () => {
    if (!confirm(`Delete the entire show "${show.title}" and all its files?`)) return;
    try {
      await library.deleteShow(show.imdb_id);
      toast("Show deleted", "success");
      onChanged();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Delete failed", "error");
    }
  };

  return (
    <div className="card p-0 overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2.5 flex-wrap">
        <button className="flex items-center gap-2 min-w-0 flex-1 text-left" onClick={() => setOpen(!open)}>
          <span className="text-gray-500"><IChevron dir={open ? "down" : "right"} size={14} /></span>
          <span className="text-sm font-medium truncate">{show.title}</span>
          {show.year && <span className="text-xs text-gray-500">({show.year})</span>}
          <span className="text-xs text-gray-600">
            · {seasons.length} season{seasons.length !== 1 ? "s" : ""}
          </span>
          {gaps && (totalMissing > 0
            ? <span className="badge-pending">{totalMissing} missing</span>
            : <span className="badge-completed">complete</span>)}
        </button>
        <div className="flex gap-1.5 flex-shrink-0">
          <a className="btn-secondary btn-xs inline-flex items-center gap-1"
            href={watch.showZipUrl(show.imdb_id)} download
            title="Download every file of the whole show as one .zip">
            <IDownload size={13} /> Download
          </a>
          {isAdmin && (
            <>
              <button className="btn-ghost btn-xs" onClick={checkGaps} disabled={checking}>
                {checking ? "…" : "Check missing"}
              </button>
              <button className="btn-ghost btn-xs inline-flex items-center gap-1" onClick={() => setEditing(true)}>
                <IPencil size={13} /> Rename
              </button>
              <button className="btn-ghost btn-xs text-red-400 inline-flex items-center gap-1" onClick={delShow}>
                <ITrash size={13} /> Delete
              </button>
            </>
          )}
        </div>
      </div>

      {(open || epMatch) && (
        <div className="border-t border-gray-800 divide-y divide-gray-800/60">
          {seasons.map(([season, eps]) => (
            <SeasonNode
              key={season}
              imdbId={show.imdb_id}
              season={season}
              eps={eps}
              gap={gaps?.[season]}
              defaultOpen={!!epMatch}
              isAdmin={isAdmin}
              onChanged={onChanged}
              toast={toast}
            />
          ))}
        </div>
      )}

      {editing && (
        <RenameModal
          title="Rename show"
          initialTitle={show.title}
          initialYear={show.year}
          onClose={() => setEditing(false)}
          onSubmit={async (title, year) => {
            await library.renameShow(show.imdb_id, { title, year: year ?? undefined });
            toast("Show renamed", "success");
            setEditing(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

function SeasonNode({ imdbId, season, eps, gap, defaultOpen, isAdmin, onChanged, toast }: {
  imdbId: string; season: number; eps: Row[]; gap?: SeasonGap; defaultOpen?: boolean; isAdmin: boolean;
  onChanged: () => void; toast: ToastFn;
}) {
  const [open, setOpen] = useState(!!defaultOpen);
  const [files, setFiles] = useState<FolderFile[] | null>(null);
  const [version, setVersion] = useState(0);

  const sorted = [...eps].sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0));
  const anyJobId = sorted.find((e) => e.jobId != null)?.jobId;

  // One request lists the whole season folder; files are grouped per episode
  // by the SxxEyy the backend parsed out of each filename.
  useEffect(() => {
    if (!open || anyJobId == null) return;
    let cancelled = false;
    watch.seasonFiles(imdbId, season)
      .then((d) => { if (!cancelled) setFiles(d.files); })
      .catch(() => { if (!cancelled) setFiles([]); });
    return () => { cancelled = true; };
  }, [open, imdbId, season, anyJobId, version]);

  const changed = () => { setVersion((v) => v + 1); onChanged(); };

  const byEpisode = useMemo(() => {
    const map = new Map<number, FolderFile[]>();
    for (const f of files ?? []) {
      if (f.episode == null) continue;
      map.set(f.episode, [...(map.get(f.episode) ?? []), f]);
    }
    return map;
  }, [files]);
  const sharedFiles = useMemo(() => (files ?? []).filter((f) => f.episode == null), [files]);

  const delSeason = async () => {
    if (!confirm(`Delete Season ${season} (${eps.length} episodes)?`)) return;
    try {
      await library.deleteSeason(imdbId, season);
      toast("Season deleted", "success");
      onChanged();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Delete failed", "error");
    }
  };

  return (
    <div>
      <div className="flex items-center gap-2 px-3 py-2 pl-7 flex-wrap">
        <button className="flex items-center gap-2 min-w-0 flex-1 text-left" onClick={() => setOpen(!open)}>
          <span className="text-gray-500"><IChevron dir={open ? "down" : "right"} size={14} /></span>
          <span className="text-sm">Season {season}</span>
          {eps.some((e) => e.mine) && <span className="badge-neutral">requested by you</span>}
          {gap ? (
            <span className={`text-xs ${gap.missing.length ? "text-yellow-400" : "text-green-400"}`}>
              · {gap.downloaded}/{gap.total}{gap.missing.length ? ` · missing ${gap.missing.map((e) => `E${e}`).join(", ")}` : " · complete"}
            </span>
          ) : (
            <span className="text-xs text-gray-600">· {eps.length} ep{eps.length !== 1 ? "s" : ""}</span>
          )}
        </button>
        <div className="flex gap-1.5 flex-shrink-0">
          <a className="btn-secondary btn-xs inline-flex items-center gap-1"
            href={watch.seasonZipUrl(imdbId, season)} download
            title="Download every file of this season as one .zip">
            <IDownload size={13} /> Download
          </a>
          {isAdmin && (
            <button className="btn-ghost btn-xs text-red-400 inline-flex items-center gap-1" onClick={delSeason}>
              <ITrash size={13} /> Delete
            </button>
          )}
        </div>
      </div>
      {open && (
        <div className="pl-10 pr-3 pb-3 space-y-2">
          {sharedFiles.length > 0 && anyJobId != null && (
            <div className="rounded-lg border border-gray-800 bg-gray-800/30 p-2 space-y-1.5">
              <div className="text-[11px] uppercase tracking-wide text-gray-500">Season files</div>
              <FileList files={sharedFiles} jobId={anyJobId} isAdmin={isAdmin} onChanged={changed} toast={toast} />
            </div>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
            {sorted.map((ep) => (
              <EpisodeCard key={ep.episode} ep={ep} season={season}
                files={files === null ? null : (byEpisode.get(ep.episode ?? -1) ?? [])}
                isAdmin={isAdmin} onChanged={changed} toast={toast} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function EpisodeCard({ ep, season, files, isAdmin, onChanged, toast }: {
  ep: Row; season: number; files: FolderFile[] | null; isAdmin: boolean;
  onChanged: () => void; toast: ToastFn;
}) {
  const [editing, setEditing] = useState(false);
  const code = `S${String(season).padStart(2, "0")}E${String(ep.episode ?? 0).padStart(2, "0")}`;

  const del = async () => {
    if (!ep.jobId) return;
    if (!confirm(`Delete ${code} and its files?`)) return;
    try {
      await library.deleteFile(ep.jobId);
      toast("Episode deleted", "success");
      onChanged();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Delete failed", "error");
    }
  };

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-800/40 p-2 space-y-1.5">
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-[11px] font-mono text-gray-500 flex-shrink-0">{code}</span>
        <span className="text-xs truncate flex-1" title={ep.episode_title || undefined}>
          {ep.episode_title || `Episode ${ep.episode}`}
        </span>
        <div className="flex gap-0.5 flex-shrink-0">
          {ep.jobId != null && (
            <a className="p-1 rounded text-gray-400 hover:text-gray-100 hover:bg-gray-700/60"
              href={watch.itemZipUrl(ep.jobId)} download
              title="Download every file of this episode as one .zip"><IDownload size={14} /></a>
          )}
          {isAdmin && (
            <>
              <button className="p-1 rounded text-gray-400 hover:text-gray-100 hover:bg-gray-700/60"
                onClick={() => setEditing(true)} title="Rename episode"><IPencil size={14} /></button>
              <button className="p-1 rounded text-red-400/80 hover:text-red-300 hover:bg-gray-700/60"
                onClick={del} title="Delete episode"><ITrash size={14} /></button>
            </>
          )}
        </div>
      </div>
      {ep.jobId != null && (
        <FileList files={files} jobId={ep.jobId} isAdmin={isAdmin} onChanged={onChanged} toast={toast}
          emptyText="No files on disk for this episode." />
      )}
      {editing && ep.jobId != null && (
        <RenameModal
          title={`Rename ${code}`}
          initialTitle={ep.episode_title || ""}
          episodeMode
          onClose={() => setEditing(false)}
          onSubmit={async (title) => {
            await library.renameJob(ep.jobId!, { episode_title: title });
            toast("Episode renamed", "success");
            setEditing(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

/* ── Rename modals ──────────────────────────────────────────────────────── */

function RenameModal({
  title, initialTitle, initialYear, episodeMode, onClose, onSubmit,
}: {
  title: string;
  initialTitle: string;
  initialYear?: number | null;
  episodeMode?: boolean;
  onClose: () => void;
  onSubmit: (title: string, year: number | null) => Promise<void>;
}) {
  const toast = useToast();
  const [name, setName] = useState(initialTitle);
  const [year, setYear] = useState(initialYear ? String(initialYear) : "");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim()) { toast("Name is required", "error"); return; }
    setBusy(true);
    try {
      await onSubmit(name.trim(), year ? Number(year) : null);
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Rename failed", "error");
      setBusy(false);
    }
  };

  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy} onClick={submit}>Save</button>
        </>
      }
    >
      <div>
        <label className="label">{episodeMode ? "Episode title" : "Title"}</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </div>
      {!episodeMode && (
        <div>
          <label className="label">Year</label>
          <input className="input" value={year} onChange={(e) => setYear(e.target.value)} inputMode="numeric" />
        </div>
      )}
    </Modal>
  );
}

function FileRenameModal({ name, onClose, onSubmit }: {
  name: string;
  onClose: () => void;
  onSubmit: (newName: string) => Promise<void>;
}) {
  const toast = useToast();
  // rel paths can contain a subfolder — only the basename is editable
  const slash = name.lastIndexOf("/");
  const base = slash >= 0 ? name.slice(slash + 1) : name;
  const [value, setValue] = useState(base);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const v = value.trim();
    if (!v) { toast("File name is required", "error"); return; }
    if (v.includes("/") || v.includes("\\")) { toast("Name can't contain slashes", "error"); return; }
    setBusy(true);
    try {
      await onSubmit(v);
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Rename failed", "error");
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Rename file"
      onClose={onClose}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy} onClick={submit}>Save</button>
        </>
      }
    >
      <div>
        <label className="label">File name</label>
        <input className="input font-mono text-sm" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
        <p className="text-xs text-gray-600 mt-1.5">
          Keep the extension. Renaming the video keeps playback working; its subtitle/nfo
          sidecars are matched by name, so rename those too if you change the video.
        </p>
      </div>
    </Modal>
  );
}
