import { FormEvent, useEffect, useState } from "react";
import { useAuth } from "../App";
import {
  collection, download, DownloadedMap, media, MediaCategory, MediaRequest,
  requests, search, SearchResult,
} from "../api/client";
import { Modal, ProgressBar, useToast } from "../components/ui";
import { ICalendar, IFilm } from "../components/icons";
import ScheduleForm from "../components/ScheduleForm";

interface EpInfo { episode: number; title: string }

type Step =
  | { kind: "idle" }
  | { kind: "results"; items: SearchResult[] }
  | {
      kind: "tv_picker";
      item: SearchResult;
      seasons: number[];
      activeSeason: number;
      episodes: EpInfo[];
      loadingEps: boolean;
    };

export default function Search() {
  const user = useAuth()!;
  const isAdmin = user.role === "admin";
  const toast = useToast();

  const [query, setQuery] = useState("");
  const [step, setStep] = useState<Step>({ kind: "idle" });
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [checkedSeasons, setCheckedSeasons] = useState<Set<number>>(new Set());
  const [downloaded, setDownloaded] = useState<DownloadedMap>({});
  const [myReqs, setMyReqs] = useState<MediaRequest[]>([]);
  const [lastResults, setLastResults] = useState<SearchResult[]>([]);
  const [recentMovies, setRecentMovies] = useState<SearchResult[]>([]);
  const [recentShows, setRecentShows] = useState<SearchResult[]>([]);
  const [scheduling, setScheduling] = useState<{ item: SearchResult; seasons: number[] } | null>(null);
  const [cats, setCats] = useState<MediaCategory[]>([]);
  const [movieCatPick, setMovieCatPick] = useState<SearchResult | null>(null);
  const [tvCategory, setTvCategory] = useState("");   // "" = Auto (Anime detected from TMDB)

  // Every category is a valid destination — the category IS the routing choice
  const movieCats = cats;
  const tvCats = cats;

  const refreshState = () => {
    collection.downloaded().then(setDownloaded).catch(() => {});
    requests.mine().then(setMyReqs).catch(() => {});
  };
  useEffect(refreshState, []);
  useEffect(() => { media.categories().then((r) => setCats(r.categories)).catch(() => {}); }, []);

  const applyRecent = (r: { movies: SearchResult[]; shows: SearchResult[] }) => {
    setRecentMovies(r.movies); setRecentShows(r.shows);
  };
  useEffect(() => { search.recent().then(applyRecent).catch(() => {}); }, []);

  const removeRecent = (item: SearchResult) => {
    search.removeRecent(item.imdb_id).then(applyRecent).catch(() => {});
  };
  const clearRecent = (mt: "movie" | "tvSeries") => {
    // remove just this row's titles, one call each (keeps the other row intact)
    const list = mt === "movie" ? recentMovies : recentShows;
    Promise.allSettled(list.map((i) => search.removeRecent(i.imdb_id)))
      .then(() => search.recent().then(applyRecent).catch(() => {}));
  };

  const activeReq = (imdbId: string): MediaRequest | undefined =>
    myReqs.find((r) => r.imdb_id === imdbId && r.status !== "denied");

  // ── Search ────────────────────────────────────────────────────────────────

  const handleSearch = async (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true); setError("");
    try {
      const results = await search.query(query);
      setLastResults(results);
      setStep({ kind: "results", items: results });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally { setLoading(false); }
  };

  // ── Movie action ────────────────────────────────────────────────────────

  const actOnMovie = async (item: SearchResult, category?: string) => {
    if (downloaded[item.imdb_id]) { toast("Already in your library.", "info"); return; }
    // More than one place a movie can go (e.g. Movies vs Anime Movies) — ask which,
    // once, before queueing.
    const autoCategory = item.is_anime
      ? (item.media_type === "movie" ? "anime_movies" : "anime_shows")
      : undefined;
    if (isAdmin && !category && !autoCategory && movieCats.length > 1) { setMovieCatPick(item); return; }
    setBusy(true); setError("");
    try {
      if (isAdmin) {
        const res = await download.single({
          imdb_id: item.imdb_id, title: item.title, year: item.year, media_type: "movie",
          category: category || autoCategory,
          is_anime: item.is_anime,
        });
        toast(res.skipped ? `${item.title} is ${res.reason}` : `Download queued: ${item.title}`,
          res.skipped ? "info" : "success");
      } else {
        await requests.create({
          imdb_id: item.imdb_id, title: item.title, year: item.year, media_type: "movie",
          is_anime: item.is_anime,
        });
        toast(`Requested: ${item.title}`, "success");
      }
      refreshState();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Action failed", "error");
    } finally { setBusy(false); }
  };

  // ── Open TV picker (fast: show seasons immediately, load episodes after) ────

  const selectResult = async (item: SearchResult) => {
    // Record the click as recent history (server-side, per user)
    search.recordRecent(item).then(applyRecent).catch(() => {});

    if (item.media_type === "movie") { await actOnMovie(item); return; }

    setLoading(true); setError("");
    try {
      const res = await search.seasons(item.imdb_id);
      const seasons = res.seasons;
      if (seasons.length === 0) {
        setError("No seasons found. IMDB may be blocked — retry in a moment.");
        return;
      }
      const firstSeason = seasons[0];
      setSelected(new Set());
      setCheckedSeasons(new Set());
      setTvCategory("");
      // Render the picker right away with an episode-loading state
      setStep({ kind: "tv_picker", item, seasons, activeSeason: firstSeason, episodes: [], loadingEps: true });
      loadEpisodes(item.imdb_id, firstSeason);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load show data");
    } finally { setLoading(false); }
  };

  const loadEpisodes = async (imdbId: string, season: number) => {
    try {
      const res = await search.episodes(imdbId, season);
      setStep((prev) =>
        prev.kind === "tv_picker" && prev.item.imdb_id === imdbId && prev.activeSeason === season
          ? { ...prev, episodes: res.episodes, loadingEps: false }
          : prev
      );
    } catch (err: unknown) {
      setStep((prev) => (prev.kind === "tv_picker" ? { ...prev, loadingEps: false, episodes: [] } : prev));
      setError(err instanceof Error ? err.message : "Failed to load episodes");
    }
  };

  const changeActiveSeason = (season: number) => {
    if (step.kind !== "tv_picker" || season === step.activeSeason) return;
    setSelected(new Set());
    setStep({ ...step, activeSeason: season, loadingEps: true, episodes: [] });
    loadEpisodes(step.item.imdb_id, season);
  };

  const toggleCheckedSeason = (s: number) =>
    setCheckedSeasons((prev) => { const n = new Set(prev); n.has(s) ? n.delete(s) : n.add(s); return n; });
  const toggleEp = (ep: number) =>
    setSelected((prev) => { const n = new Set(prev); n.has(ep) ? n.delete(ep) : n.add(ep); return n; });

  // ── TV submit ──────────────────────────────────────────────────────────────

  const submitTv = async () => {
    if (step.kind !== "tv_picker") return;
    const { item, activeSeason, episodes } = step;
    setBusy(true); setError("");
    try {
      if (!isAdmin) {
        const seasonList = checkedSeasons.size > 0
          ? Array.from(checkedSeasons).sort((a, b) => a - b) : [activeSeason];
        await requests.create({
          imdb_id: item.imdb_id, title: item.title, year: item.year,
          is_anime: item.is_anime,
          media_type: "tvSeries", seasons: seasonList,
        });
        toast(`Requested ${item.title} — ${seasonList.map((s) => `S${s}`).join(", ")}`, "success");
        setCheckedSeasons(new Set());
      } else if (checkedSeasons.size > 0) {
        const seasonList = Array.from(checkedSeasons).sort((a, b) => a - b);
        const res = await download.seasons({
          imdb_id: item.imdb_id, title: item.title, year: item.year, seasons: seasonList,
          category: tvCategory || undefined,
          is_anime: item.is_anime,
        });
        toast(queuedMsg(res.queued, res.skipped, `${item.title} ${seasonList.map((s) => `S${s}`).join("+")}`),
          res.queued ? "success" : "info");
        if (res.errors?.length) setError(res.errors.join("; "));
        setCheckedSeasons(new Set());
      } else {
        const eps = selected.size === 0 ? episodes : episodes.filter((e) => selected.has(e.episode));
        if (eps.length === 0) { setError("No episodes selected."); return; }
        const res = await download.bulk({
          imdb_id: item.imdb_id, title: item.title, year: item.year,
          category: tvCategory || undefined,
          is_anime: item.is_anime,
          episodes: eps.map((e) => ({ season: activeSeason, episode: e.episode, title: e.title })),
        });
        toast(queuedMsg(res.queued, res.skipped, `${item.title} S${activeSeason}`),
          res.queued ? "success" : "info");
        setSelected(new Set());
      }
      refreshState();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Action failed", "error");
    } finally { setBusy(false); }
  };

  const isEpDone = (imdbId: string, season: number, ep: number) =>
    downloaded[imdbId]?.episodes.some((e) => e.season === season && e.episode === ep) ?? false;

  const handleBack = () => {
    setError(""); setCheckedSeasons(new Set());
    // Return to the search results if we came from a search; otherwise to the
    // home screen (recent titles) — e.g. when a show was opened from Recents.
    setStep(lastResults.length > 0 ? { kind: "results", items: lastResults } : { kind: "idle" });
  };

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Search</h1>
        <p className="text-gray-500 text-sm mt-1">
          {isAdmin ? "Find a title and queue it for download."
                   : "Find a title and request it. An admin will approve your request."}
        </p>
      </header>

      <form onSubmit={handleSearch} className="flex gap-2">
        <input className="input max-w-xl" placeholder="Search movies & TV shows…"
          value={query} onChange={(e) => setQuery(e.target.value)} />
        <button className="btn-primary" type="submit" disabled={loading}>
          {loading && step.kind !== "tv_picker" ? "…" : "Search"}
        </button>
      </form>

      {error && (
        <div className="bg-red-900/40 border border-red-700 rounded-lg px-4 py-3 text-red-300 text-sm">
          {error}
        </div>
      )}

      {step.kind === "idle" && (recentShows.length > 0 || recentMovies.length > 0) && (
        <div className="space-y-6">
          <RecentRow title="Recent shows" items={recentShows} isAdmin={isAdmin} downloaded={downloaded}
            activeReq={activeReq} busy={busy} onClick={selectResult}
            onRemove={removeRecent} onClear={() => clearRecent("tvSeries")} />
          <RecentRow title="Recent movies" items={recentMovies} isAdmin={isAdmin} downloaded={downloaded}
            activeReq={activeReq} busy={busy} onClick={selectResult}
            onRemove={removeRecent} onClear={() => clearRecent("movie")} />
        </div>
      )}

      {step.kind === "results" && (
        step.items.length === 0 ? (
          <p className="text-gray-500">No results found.</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {step.items.map((item) => (
              <ResultCard key={item.imdb_id} item={item} isAdmin={isAdmin}
                inLibrary={!!downloaded[item.imdb_id]} request={activeReq(item.imdb_id)}
                busy={busy || loading} onClick={() => selectResult(item)} />
            ))}
          </div>
        )
      )}

      {step.kind === "tv_picker" && (
        <TvPicker step={step} isAdmin={isAdmin} selected={selected} checkedSeasons={checkedSeasons}
          downloaded={downloaded} request={activeReq(step.item.imdb_id)} busy={busy} isEpDone={isEpDone}
          tvCats={tvCats} category={tvCategory} onCategory={setTvCategory}
          onBack={handleBack} onChangeActiveSeason={changeActiveSeason} onToggleSeason={toggleCheckedSeason}
          onToggleEp={toggleEp} onSelectAll={() => setSelected(new Set(step.episodes.map((e) => e.episode)))}
          onClear={() => setSelected(new Set())} onSubmit={submitTv}
          onSchedule={() => setScheduling({ item: step.item, seasons: step.seasons })} />
      )}

      {scheduling && (
        <ScheduleForm imdb_id={scheduling.item.imdb_id} title={scheduling.item.title} year={scheduling.item.year}
          is_anime={scheduling.item.is_anime}
          availableSeasons={scheduling.seasons} onClose={() => setScheduling(null)}
          onSaved={() => { setScheduling(null); toast("Show scheduled — manage it in the Schedule tab", "success"); }} />
      )}

      {movieCatPick && (
        <Modal title={`Queue "${movieCatPick.title}"`} onClose={() => setMovieCatPick(null)}
          footer={<button className="btn-secondary" onClick={() => setMovieCatPick(null)}>Cancel</button>}>
          <p className="text-sm text-gray-400">Which category should this movie go in?</p>
          <div className="space-y-2">
            {movieCats.map((c) => (
              <button key={c.key} className="btn-secondary w-full justify-start"
                onClick={() => { const item = movieCatPick; setMovieCatPick(null); actOnMovie(item, c.key); }}>
                {c.label} <span className="text-gray-500 ml-1">→ {c.folder}/</span>
              </button>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}

function queuedMsg(queued: number, skipped: number, label: string): string {
  if (queued === 0 && skipped > 0) return `${label}: all ${skipped} already in library/queue`;
  const skip = skipped > 0 ? ` (${skipped} already had)` : "";
  return `Queued ${queued} ep(s) — ${label}${skip}`;
}

// ── Recent row ──────────────────────────────────────────────────────────────

function RecentRow({ title, items, isAdmin, downloaded, activeReq, busy, onClick, onRemove, onClear }: {
  title: string; items: SearchResult[]; isAdmin: boolean; downloaded: DownloadedMap;
  activeReq: (id: string) => MediaRequest | undefined; busy: boolean;
  onClick: (i: SearchResult) => void; onRemove: (i: SearchResult) => void; onClear: () => void;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">{title}</h2>
        <button className="text-xs text-gray-500 hover:text-red-400" onClick={onClear}>Clear</button>
      </div>
      {/* Always a 2-column grid — a single recent card fills ONE column (half
          width), never the whole row. `grid-cols-2` keeps the columns even. */}
      <div className="grid grid-cols-2 gap-4">
        {items.map((item) => (
          <ResultCard key={item.imdb_id} item={item} isAdmin={isAdmin}
            inLibrary={!!downloaded[item.imdb_id]} request={activeReq(item.imdb_id)}
            busy={busy} onClick={() => onClick(item)} onRemove={() => onRemove(item)} />
        ))}
      </div>
    </div>
  );
}

// ── Result card ────────────────────────────────────────────────────────────

function RequestPill({ request }: { request: MediaRequest }) {
  if (request.status === "pending") return <span className="badge-pending">Requested</span>;
  if (request.status === "approved")
    return request.queue?.in_queue
      ? <span className="badge-downloading">In queue</span>
      : <span className="badge-approved">Approved</span>;
  return null;
}

function ResultCard({ item, isAdmin, inLibrary, request, busy, onClick, onRemove }: {
  item: SearchResult; isAdmin: boolean; inLibrary: boolean;
  request?: MediaRequest; busy: boolean; onClick: () => void; onRemove?: () => void;
}) {
  const isMovie = item.media_type === "movie";
  const disabled = busy || (inLibrary && isMovie);
  const cta = inLibrary && isMovie ? "In library"
    : isMovie ? (isAdmin ? "Queue download →" : "Request →")
    : "Browse seasons →";

  return (
    <button onClick={onClick} disabled={disabled}
      className={`card group w-full min-w-0 overflow-hidden text-left transition-colors flex gap-3 relative disabled:opacity-70 ${
        inLibrary ? "border-green-800/70" : "hover:border-sky-700 hover:bg-gray-800/60"
      } ${disabled ? "cursor-default" : "cursor-pointer"}`}>
      {/* Top-right corner overlay: the remove-✕ (recent cards only) stacked above
          the status badges, so it sits in the card's inner top-right corner. It's
          always laid out (just faded) → no shift when it brightens on hover. */}
      <div className="absolute top-2 right-2 flex flex-col items-end gap-1.5 z-10">
        {onRemove && (
          <span role="button" tabIndex={-1} title="Remove from recent" aria-label="Remove from recent"
            className="flex items-center justify-center w-5 h-5 rounded-full bg-black/50 text-gray-300 text-[11px] leading-none opacity-60 group-hover:opacity-100 hover:bg-red-600 hover:text-white transition cursor-pointer"
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRemove(); }}>
            ✕
          </span>
        )}
        {inLibrary && <span className="badge-completed">✓ In library</span>}
        {request && <RequestPill request={request} />}
      </div>
      {item.poster_url ? (
        <img src={item.poster_url} alt={item.title}
          className="w-16 h-24 object-cover rounded-md flex-shrink-0" loading="lazy" />
      ) : (
        <div className="w-16 h-24 bg-gray-800 rounded-md flex items-center justify-center text-gray-600 flex-shrink-0"><IFilm size={24} /></div>
      )}
      <div className="flex flex-col flex-1 min-w-0 pr-2">
        <span className="font-semibold text-sm leading-snug pr-16 truncate">{item.title}</span>
        <span className="text-gray-400 text-xs mt-0.5 truncate">
          {item.year ?? "Unknown"} · {isMovie ? "Movie" : "TV Series"}
          {item.is_anime && <span className="text-fuchsia-400"> · Anime</span>}
        </span>
        {/* Recents (onRemove set) stay compact — the long overview is what was
            forcing cards wider than their column and overlapping. */}
        {!onRemove && item.cast && <span className="text-gray-500 text-xs mt-1 truncate">{item.cast}</span>}
        <span className="mt-auto pt-2 text-sky-400 text-xs font-medium truncate">{cta}</span>
      </div>
    </button>
  );
}

// ── TV Picker (two-pane: seasons | episodes) ────────────────────────────────

function TvPicker({
  step, isAdmin, selected, checkedSeasons, downloaded, request, busy, isEpDone,
  tvCats, category, onCategory,
  onBack, onChangeActiveSeason, onToggleSeason, onToggleEp, onSelectAll, onClear, onSubmit, onSchedule,
}: {
  step: Extract<Step, { kind: "tv_picker" }>;
  isAdmin: boolean; selected: Set<number>; checkedSeasons: Set<number>;
  downloaded: DownloadedMap; request?: MediaRequest; busy: boolean;
  isEpDone: (id: string, s: number, ep: number) => boolean;
  tvCats: MediaCategory[]; category: string; onCategory: (v: string) => void;
  onBack: () => void; onChangeActiveSeason: (s: number) => void; onToggleSeason: (s: number) => void;
  onToggleEp: (ep: number) => void; onSelectAll: () => void; onClear: () => void; onSubmit: () => void;
  onSchedule: () => void;
}) {
  const { item, seasons, activeSeason, episodes, loadingEps } = step;
  const bulkMode = checkedSeasons.size > 0;
  const sorted = Array.from(checkedSeasons).sort((a, b) => a - b);
  const requestedSeasons = new Set(request?.seasons ?? []);
  const epsInSeason = (s: number) =>
    downloaded[item.imdb_id]?.episodes.filter((e) => e.season === s).length ?? 0;

  const btnLabel = () => {
    if (!isAdmin) return bulkMode ? `Request ${sorted.map((s) => `S${s}`).join("+")}` : `Request S${activeSeason}`;
    if (bulkMode) return `↓ Queue ${sorted.map((s) => `S${s}`).join("+")}`;
    const n = selected.size === 0 ? episodes.length : selected.size;
    return `↓ Queue ${selected.size === 0 ? `all ${episodes.length}` : selected.size} ep${n !== 1 ? "s" : ""}`;
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3 flex-wrap">
        <button className="btn-secondary btn-sm" onClick={onBack}>← Back</button>
        <div className="min-w-0">
          <h2 className="text-lg font-semibold leading-tight truncate">
            {item.title} {item.year && <span className="text-gray-500 font-normal text-sm">({item.year})</span>}
          </h2>
        </div>
        {request && <RequestPill request={request} />}
        <div className="ml-auto flex gap-2 items-center">
          {isAdmin && tvCats.length > 1 && (
            <select className="input h-8 text-sm py-1 w-auto" value={category}
              title="Which folder this show downloads into"
              onChange={(e) => onCategory(e.target.value)}>
              <option value="">Category: Auto (Anime detected)</option>
              {tvCats.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
            </select>
          )}
          {isAdmin && (
            <button className="btn-secondary btn-sm inline-flex items-center gap-1.5" onClick={onSchedule} title="Auto-check for new episodes">
              <ICalendar size={14} /> Schedule
            </button>
          )}
          <button className="btn-primary btn-sm" onClick={onSubmit} disabled={busy || loadingEps}>
            {busy ? "Submitting…" : btnLabel()}
          </button>
        </div>
      </div>

      {bulkMode && (
        <p className="text-xs text-sky-400">
          {isAdmin ? "Queueing" : "Requesting"} every episode of {sorted.map((s) => `S${s}`).join(", ")}.
          Episode selection is disabled while whole seasons are checked.
        </p>
      )}

      {/* Two-pane */}
      <div className="flex gap-4 items-start">
        {/* Seasons column */}
        <div className="w-40 flex-shrink-0 space-y-1 max-h-[68vh] overflow-y-auto pr-1">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 px-2 pb-1">
            Seasons
          </div>
          {seasons.map((s) => {
            const active = activeSeason === s;
            const checked = checkedSeasons.has(s);
            const nDone = epsInSeason(s);
            return (
              <div key={s}
                className={`flex items-center gap-2 pl-2 pr-2 py-1.5 rounded-lg border cursor-pointer transition-colors ${
                  active ? "border-sky-500 bg-sky-900/30"
                  : checked ? "border-sky-700 bg-sky-900/15"
                  : "border-transparent hover:bg-gray-800"}`}
                onClick={() => onChangeActiveSeason(s)}>
                <input type="checkbox" className="w-3.5 h-3.5 accent-sky-500 cursor-pointer flex-shrink-0"
                  checked={checked} onChange={() => onToggleSeason(s)} onClick={(e) => e.stopPropagation()} />
                <span className={`text-sm font-medium flex-1 ${active ? "text-sky-300" : "text-gray-200"}`}>
                  Season {s}
                </span>
                {nDone > 0 && <span className="text-[10px] text-green-500" title={`${nDone} in library`}>✓{nDone}</span>}
                {requestedSeasons.has(s) && <span className="text-[10px] text-amber-400" title="Requested">•</span>}
              </div>
            );
          })}
        </div>

        {/* Episodes pane */}
        <div className="flex-1 min-w-0 card p-3">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-sm font-semibold">Season {activeSeason}</span>
            <span className="text-xs text-gray-500">
              {loadingEps ? "loading…" : `${episodes.length} episode${episodes.length !== 1 ? "s" : ""}`}
            </span>
            {isAdmin && !bulkMode && !loadingEps && episodes.length > 0 && (
              <div className="ml-auto flex gap-1.5">
                <button className="btn-secondary btn-xs" onClick={onSelectAll}>All</button>
                <button className="btn-secondary btn-xs" onClick={onClear}>None</button>
                {selected.size > 0 && <span className="text-xs text-gray-500 self-center">{selected.size} selected</span>}
              </div>
            )}
          </div>

          {loadingEps ? (
            <div className="space-y-1.5">
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="h-7 rounded bg-gray-800/60 animate-pulse" />
              ))}
            </div>
          ) : episodes.length === 0 ? (
            <p className="text-gray-500 text-sm py-6 text-center">No episodes found for Season {activeSeason}.</p>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-4 gap-y-0.5 max-h-[62vh] overflow-y-auto">
              {episodes.map((ep) => {
                const done = isEpDone(item.imdb_id, activeSeason, ep.episode);
                const selectable = isAdmin && !bulkMode;
                return (
                  <label key={ep.episode}
                    className={`flex items-center gap-2.5 px-2 py-1.5 rounded-lg ${
                      !selectable ? "" : "cursor-pointer hover:bg-gray-800"} ${done ? "opacity-60" : ""}`}>
                    {isAdmin && (
                      <input type="checkbox" className="w-4 h-4 rounded accent-sky-500 flex-shrink-0"
                        checked={selected.has(ep.episode)} disabled={!selectable}
                        onChange={() => selectable && onToggleEp(ep.episode)} />
                    )}
                    <span className="text-gray-500 text-xs w-8 flex-shrink-0 font-mono">
                      E{ep.episode.toString().padStart(2, "0")}
                    </span>
                    <span className="text-sm flex-1 truncate">{ep.title}</span>
                    {done && <span className="text-green-500 text-[10px] flex-shrink-0">✓</span>}
                  </label>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {request?.queue && request.status === "approved" && request.queue.in_queue && (
        <div className="card">
          <p className="text-sm text-gray-400 mb-2">Your request is downloading:</p>
          <ProgressBar value={request.queue.progress} />
          <p className="text-xs text-gray-500 mt-1">
            {request.queue.completed}/{request.queue.total_jobs} episodes complete
          </p>
        </div>
      )}
    </div>
  );
}
