import { RefObject, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "../App";
import { EpisodeMeta, search, subtitleTiming, watch, TitleMeta, WatchItem } from "../api/client";
import { fmtBytes, useToast } from "../components/ui";
import { ICalendar, IHeart, IRestart } from "../components/icons";
import { useNavCounts } from "../components/Layout";
import ScheduleForm from "../components/ScheduleForm";

/* Watch page flow: browse (hero + carousels of poster cards, built from the
   locally-cached TMDB metadata) → title detail page (backdrop, plot, genres)
   → seasons → episodes → play. Continue watching / Favorites rows jump
   straight in. Hovering a poster card plays a muted preview of the actual
   file. The player is a full-screen overlay with its own built-in controls. */

const fmtTime = (s: number) => {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
           : `${m}:${String(sec).padStart(2, "0")}`;
};

const epLabel = (i: WatchItem) =>
  `S${String(i.season ?? 0).padStart(2, "0")}E${String(i.episode ?? 0).padStart(2, "0")}`;

const inProgress = (i: WatchItem) => !i.watched && i.position > 5;

/** One movie or one whole show — the unit the browse cards represent. */
interface TitleGroup {
  imdb_id: string;
  media_type: string;
  title: string;
  year: number | null;
  eps: WatchItem[];          // 1 item for movies, all episodes for shows
  meta?: TitleMeta;
}

/** The episode a "Play" on the whole show starts: the in-progress one,
    else the first unwatched, else the first episode. */
function upNext(eps: WatchItem[]): WatchItem {
  const cur = eps.filter(inProgress)
    .sort((a, b) => (b.progress_updated_at ?? "").localeCompare(a.progress_updated_at ?? ""))[0];
  return cur ?? eps.find((e) => !e.watched) ?? eps[0];
}

/** Next episode after `item` within the same show (season/episode order). */
function nextEpisode(items: WatchItem[], item: WatchItem): WatchItem | null {
  if (item.media_type !== "tvSeries") return null;
  const eps = items.filter((i) => i.imdb_id === item.imdb_id && i.media_type === "tvSeries")
    .sort((a, b) => (a.season! - b.season!) || (a.episode! - b.episode!));
  const idx = eps.findIndex((e) => e.id === item.id);
  return idx >= 0 && idx + 1 < eps.length ? eps[idx + 1] : null;
}

export default function Watch() {
  const [items, setItems] = useState<WatchItem[]>([]);
  const [titles, setTitles] = useState<Record<string, TitleMeta>>({});
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [playing, setPlaying] = useState<WatchItem | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  // Stable across next-video (the Player remounts, this doesn't) so fullscreen persists.
  const fsRef = useRef<HTMLDivElement>(null);

  const reload = () =>
    watch.list()
      .then((l) => { setItems(l.items); setTitles(l.titles); setLoading(false); })
      .catch(() => setLoading(false));

  useEffect(() => {
    reload();
    const timer = window.setInterval(reload, 15000);
    return () => window.clearInterval(timer);
  }, []);

  // While something is downloading, refresh the library so finished files show
  // up live; also refresh once when the last download completes.
  const { downloading = 0 } = useNavCounts();
  const prevDownloading = useRef(0);
  useEffect(() => {
    const wasDownloading = prevDownloading.current;
    prevDownloading.current = downloading;
    if (downloading > 0) {
      const t = window.setInterval(reload, 8000);
      return () => window.clearInterval(t);
    }
    if (wasDownloading > 0) reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloading]);

  const onProgressSaved = (id: number, position: number, duration: number, watched: boolean) =>
    setItems((prev) => prev.map((i) => i.id === id
      ? { ...i, position, duration, watched, progress_updated_at: new Date().toISOString() }
      : i));

  const removeProgress = async (id: number) => {
    await watch.clearProgress(id).catch(() => {});
    setItems((prev) => prev.map((i) => i.id === id
      ? { ...i, position: 0, duration: 0, watched: false, progress_updated_at: null } : i));
  };

  const toggleFav = async (imdb_id: string) => {
    // optimistic — flip every row of the title, revert on failure
    setItems((prev) => prev.map((i) => i.imdb_id === imdb_id ? { ...i, favorite: !i.favorite } : i));
    try { await watch.toggleFavorite(imdb_id); }
    catch { setItems((prev) => prev.map((i) => i.imdb_id === imdb_id ? { ...i, favorite: !i.favorite } : i)); }
  };

  const q = query.trim().toLowerCase();

  /** All titles grouped (movie = 1 item, show = its episodes) with meta attached. */
  const groups = useMemo(() => {
    const map = new Map<string, TitleGroup>();
    for (const i of items) {
      const g = map.get(i.imdb_id) ?? {
        imdb_id: i.imdb_id, media_type: i.media_type, title: i.title,
        year: i.year, eps: [], meta: titles[i.imdb_id],
      };
      g.eps.push(i);
      map.set(i.imdb_id, g);
    }
    for (const g of map.values())
      g.eps.sort((a, b) => ((a.season ?? 0) - (b.season ?? 0)) || ((a.episode ?? 0) - (b.episode ?? 0)));
    return Array.from(map.values()).sort((a, b) => a.title.localeCompare(b.title));
  }, [items, titles]);

  const movieGroups = useMemo(() => groups.filter((g) => g.media_type === "movie"), [groups]);
  const showGroups = useMemo(() => groups.filter((g) => g.media_type === "tvSeries"), [groups]);

  const searchResults = useMemo(() => {
    if (!q) return [];
    return groups.filter((g) =>
      g.title.toLowerCase().includes(q) ||
      (g.meta?.title ?? "").toLowerCase().includes(q) ||
      g.eps.some((e) => (e.episode_title ?? "").toLowerCase().includes(q)));
  }, [groups, q]);

  /** Continue watching: the last 5 in-progress movies + last 5 in-progress
      shows (each show collapsed to its most recent episode), newest first. */
  const continueRow = useMemo(() => {
    const inProg = items.filter(inProgress)
      .sort((a, b) => (b.progress_updated_at ?? "").localeCompare(a.progress_updated_at ?? ""));
    const movies: WatchItem[] = [];
    const shows: WatchItem[] = [];
    const seenShow = new Set<string>();
    for (const i of inProg) {
      if (i.media_type === "movie") {
        if (movies.length < 5) movies.push(i);
      } else if (!seenShow.has(i.imdb_id)) {
        seenShow.add(i.imdb_id);
        if (shows.length < 5) shows.push(i);
      }
    }
    return [...movies, ...shows]
      .sort((a, b) => (b.progress_updated_at ?? "").localeCompare(a.progress_updated_at ?? ""));
  }, [items]);

  const favoriteGroups = useMemo(() => groups.filter((g) => g.eps[0].favorite), [groups]);

  /** Genre carousels from the cached metadata (movies + shows mixed). */
  const genreRows = useMemo(() => {
    const byGenre = new Map<string, TitleGroup[]>();
    for (const g of groups) {
      for (const genre of g.meta?.genres ?? [])
        byGenre.set(genre, [...(byGenre.get(genre) ?? []), g]);
    }
    return Array.from(byGenre.entries())
      .filter(([, list]) => list.length >= 2)
      .sort((a, b) => b[1].length - a[1].length)
      .slice(0, 8);
  }, [groups]);

  /** Hero: what you're in the middle of, else the top-rated title with art. */
  const hero = useMemo(() => {
    for (const i of continueRow) {
      const g = groups.find((x) => x.imdb_id === i.imdb_id);
      if (g?.meta?.backdrop) return { group: g, resume: i };
    }
    const withArt = groups.filter((g) => g.meta?.backdrop)
      .sort((a, b) => (b.meta?.rating ?? 0) - (a.meta?.rating ?? 0));
    return withArt.length ? { group: withArt[0], resume: null } : null;
  }, [groups, continueRow]);

  if (loading) return <p className="text-gray-500">Loading…</p>;

  const detailGroup = detailId ? groups.find((g) => g.imdb_id === detailId) : null;

  const openDetail = (g: TitleGroup) => setDetailId(g.imdb_id);
  const playGroup = (g: TitleGroup) => setPlaying(upNext(g.eps));

  return (
    <div className="space-y-8">
      {detailGroup ? (
        <DetailPage group={detailGroup}
          onBack={() => setDetailId(null)}
          onPlay={setPlaying}
          onToggleFav={() => toggleFav(detailGroup.imdb_id)} />
      ) : (
        <>
          <header className="flex items-end gap-4 flex-wrap">
            <div>
              <h1 className="text-2xl font-bold">Watch</h1>
              <p className="text-gray-500 text-sm mt-1">Play your downloaded movies and shows right here.</p>
            </div>
            <input className="input py-1.5 h-9 text-sm max-w-xs ml-auto" placeholder="Search titles or episodes…"
              value={query} onChange={(e) => setQuery(e.target.value)} />
          </header>

          {items.length === 0 ? (
            <div className="card text-gray-500 text-center py-10">Nothing to watch yet — finish a download first.</div>
          ) : q ? (
            <section>
              <SectionTitle title={`Results for “${query}”`} count={searchResults.length} />
              {searchResults.length === 0 ? (
                <p className="text-gray-600 text-sm">Nothing matches “{query}”.</p>
              ) : (
                <div className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-3">
                  {searchResults.map((g) => (
                    <PosterCard key={g.imdb_id} group={g} onOpen={() => openDetail(g)}
                      onFav={() => toggleFav(g.imdb_id)} />
                  ))}
                </div>
              )}
            </section>
          ) : (
            <>
              {hero && (
                <Hero group={hero.group} resume={hero.resume}
                  onPlay={() => hero.resume ? setPlaying(hero.resume) : playGroup(hero.group)}
                  onInfo={() => openDetail(hero.group)} />
              )}

              {continueRow.length > 0 && (
                <Carousel title="Continue watching">
                  {continueRow.map((i) => (
                    <ContinueCard key={i.id} item={i} meta={titles[i.imdb_id]}
                      onPlay={() => setPlaying(i)}
                      onInfo={() => setDetailId(i.imdb_id)}
                      onRemove={() => removeProgress(i.id)} />
                  ))}
                </Carousel>
              )}

              {favoriteGroups.length > 0 && (
                <Carousel title="My favorites">
                  {favoriteGroups.map((g) => (
                    <PosterCard key={g.imdb_id} group={g} inCarousel
                      onOpen={() => openDetail(g)} onFav={() => toggleFav(g.imdb_id)} />
                  ))}
                </Carousel>
              )}

              {movieGroups.length > 0 && (
                <Carousel title="Movies" count={movieGroups.length}>
                  {movieGroups.map((g) => (
                    <PosterCard key={g.imdb_id} group={g} inCarousel
                      onOpen={() => openDetail(g)} onFav={() => toggleFav(g.imdb_id)} />
                  ))}
                </Carousel>
              )}

              {showGroups.length > 0 && (
                <Carousel title="TV Shows" count={showGroups.length}>
                  {showGroups.map((g) => (
                    <PosterCard key={g.imdb_id} group={g} inCarousel
                      onOpen={() => openDetail(g)} onFav={() => toggleFav(g.imdb_id)} />
                  ))}
                </Carousel>
              )}

              {genreRows.map(([genre, list]) => (
                <Carousel key={genre} title={genre} count={list.length}>
                  {list.map((g) => (
                    <PosterCard key={g.imdb_id} group={g} inCarousel
                      onOpen={() => openDetail(g)} onFav={() => toggleFav(g.imdb_id)} />
                  ))}
                </Carousel>
              ))}
            </>
          )}
        </>
      )}

      {playing && (
        <div ref={fsRef}>
          <Player key={playing.id} item={playing}
            next={nextEpisode(items, playing)}
            onClose={() => setPlaying(null)}
            onPlayNext={(n) => setPlaying(n)}
            onProgressSaved={onProgressSaved}
            fsRef={fsRef}
          />
        </div>
      )}
    </div>
  );
}

/* ── Browse: hero, carousels & cards ──────────────────────────────────────── */

function SectionTitle({ title, count }: { title: string; count?: number }) {
  return (
    <div className="flex items-center gap-2 mb-2">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400">{title}</h2>
      {count !== undefined && <span className="text-xs text-gray-600">· {count}</span>}
    </div>
  );
}

function rowKeyNav(e: React.KeyboardEvent<HTMLDivElement>) {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const cards = Array.from(e.currentTarget.querySelectorAll<HTMLElement>("[data-card]"));
  const idx = cards.indexOf(document.activeElement as HTMLElement);
  if (idx === -1) return;
  const next = cards[idx + (e.key === "ArrowRight" ? 1 : -1)];
  if (next) {
    e.preventDefault();
    next.focus();
    next.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
  }
}

/** Horizontal scroll row with hover arrows — the Netflix-style shelf. */
function Carousel({ title, count, children }: {
  title: string; count?: number; children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [canL, setCanL] = useState(false);
  const [canR, setCanR] = useState(false);

  const check = () => {
    const el = ref.current;
    if (!el) return;
    setCanL(el.scrollLeft > 4);
    setCanR(el.scrollLeft + el.clientWidth < el.scrollWidth - 4);
  };
  useEffect(() => { check(); }, [children]);

  const scroll = (dir: -1 | 1) =>
    ref.current?.scrollBy({ left: dir * ref.current.clientWidth * 0.8, behavior: "smooth" });

  const arrow = "absolute top-0 bottom-2 w-10 z-10 flex items-center justify-center " +
    "from-gray-950/90 to-transparent text-white/90 opacity-0 group-hover:opacity-100 transition-opacity";

  return (
    <section>
      <SectionTitle title={title} count={count} />
      <div className="relative group -mx-1">
        {canL && (
          <button className={`${arrow} left-0 bg-gradient-to-r rounded-l-lg`}
            onClick={() => scroll(-1)} aria-label="Scroll left">
            <IChevron dir="left" />
          </button>
        )}
        {canR && (
          <button className={`${arrow} right-0 bg-gradient-to-l rounded-r-lg`}
            onClick={() => scroll(1)} aria-label="Scroll right">
            <IChevron dir="right" />
          </button>
        )}
        <div ref={ref} onScroll={check} onKeyDown={rowKeyNav}
          className="flex gap-3 overflow-x-auto pb-2 px-1 scroll-smooth snap-x [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {children}
        </div>
      </div>
    </section>
  );
}

function Hero({ group, resume, onPlay, onInfo }: {
  group: TitleGroup; resume: WatchItem | null; onPlay: () => void; onInfo: () => void;
}) {
  const m = group.meta;
  const displayTitle = m?.title || group.title;
  return (
    <section className="relative rounded-2xl overflow-hidden border border-gray-800 min-h-[300px] flex items-end">
      {m?.backdrop && (
        <img src={watch.metaImageUrl(m.backdrop)} alt=""
          className="absolute inset-0 w-full h-full object-cover" />
      )}
      <div className="absolute inset-0 bg-gradient-to-t from-gray-950 via-gray-950/60 to-transparent" />
      <div className="absolute inset-0 bg-gradient-to-r from-gray-950/80 via-transparent to-transparent" />
      <div className="relative p-6 sm:p-8 max-w-2xl space-y-3">
        <h2 className="text-3xl sm:text-4xl font-bold drop-shadow">{displayTitle}</h2>
        <MetaLine group={group} />
        {m?.overview && (
          <p className="text-sm text-gray-300 leading-relaxed line-clamp-3 drop-shadow">{m.overview}</p>
        )}
        <div className="flex items-center gap-2 pt-1">
          <button className="btn-primary inline-flex items-center gap-1.5" onClick={onPlay}>
            <IPlay size={16} />
            {resume
              ? (resume.media_type === "tvSeries" ? `Resume ${epLabel(resume)}` : `Resume ${fmtTime(resume.position)}`)
              : "Play"}
          </button>
          <button className="btn-secondary" onClick={onInfo}>More info</button>
        </div>
      </div>
    </section>
  );
}

function MetaLine({ group }: { group: TitleGroup }) {
  const m = group.meta;
  const isShow = group.media_type === "tvSeries";
  const seasons = isShow ? new Set(group.eps.map((e) => e.season ?? 1)).size : 0;
  const bits: string[] = [];
  const year = m?.year ?? group.year;
  if (year) bits.push(String(year));
  if (isShow) bits.push(`${seasons} season${seasons !== 1 ? "s" : ""} · ${group.eps.length} eps`);
  else if (m?.runtime) bits.push(`${m.runtime} min`);
  if (m?.original_language) bits.push(`Original audio: ${m.original_language.toUpperCase()}`);
  return (
    <div className="flex items-center gap-2 text-sm text-gray-400 flex-wrap">
      {m?.rating ? <span className="text-amber-400 font-medium">★ {m.rating.toFixed(1)}</span> : null}
      <span>{bits.join(" · ")}</span>
      {(m?.genres ?? []).slice(0, 3).map((g) => (
        <span key={g} className="px-2 py-0.5 rounded-full bg-white/10 text-xs text-gray-300">{g}</span>
      ))}
    </div>
  );
}

/** Muted inline preview of the actual file, used on card hover. */
function HoverPreview({ item }: { item: WatchItem }) {
  const ref = useRef<HTMLVideoElement>(null);
  return (
    <video ref={ref}
      className="absolute inset-0 w-full h-full object-cover"
      src={watch.streamUrl(item.id)}
      muted autoPlay playsInline preload="metadata"
      onLoadedMetadata={() => {
        const v = ref.current;
        if (v && v.duration) v.currentTime = Math.min(90, v.duration * 0.1);
      }}
    />
  );
}

const PREVIEW_DELAY_MS = 700;

function usePreview() {
  const [on, setOn] = useState(false);
  const timer = useRef<number | null>(null);
  const start = () => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOn(true), PREVIEW_DELAY_MS);
  };
  const stop = () => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
    setOn(false);
  };
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
  return { on, start, stop };
}

function PosterCard({ group, onOpen, onFav, inCarousel = false }: {
  group: TitleGroup; onOpen: () => void; onFav: () => void; inCarousel?: boolean;
}) {
  const m = group.meta;
  const preview = usePreview();
  const isShow = group.media_type === "tvSeries";
  const allWatched = group.eps.every((e) => e.watched);
  const favorite = group.eps[0].favorite;
  const movie = !isShow ? group.eps[0] : null;
  const previewItem = isShow ? upNext(group.eps) : group.eps[0];
  const displayTitle = m?.title || group.title;
  const year = m?.year ?? group.year;

  return (
    <div className={`relative ${inCarousel ? "flex-none w-36 sm:w-40 snap-start" : ""}`}
      onMouseEnter={preview.start} onMouseLeave={preview.stop}>
      <button data-card onClick={onOpen}
        className="w-full text-left rounded-xl overflow-hidden border border-gray-800 bg-gray-800/60
          hover:border-sky-700 focus:outline-none focus:border-sky-600 transition-transform hover:scale-[1.03]">
        <div className="relative aspect-[2/3] bg-gradient-to-br from-gray-800 to-gray-900">
          {m?.poster ? (
            <img src={watch.metaImageUrl(m.poster)} alt={displayTitle} loading="lazy"
              className="absolute inset-0 w-full h-full object-cover" />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center p-3 text-center">
              <span className="text-sm font-semibold text-gray-400">{displayTitle}</span>
            </div>
          )}
          {preview.on && <HoverPreview item={previewItem} />}
          {/* bottom gradient + name */}
          <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 to-transparent pt-8 pb-1.5 px-2">
            <div className="text-xs font-medium text-white truncate">{displayTitle}</div>
            <div className="text-[10px] text-gray-400">
              {year ?? ""}{isShow ? ` · ${group.eps.length} eps` : ""}
              {m?.rating ? ` · ★ ${m.rating.toFixed(1)}` : ""}
            </div>
          </div>
          {allWatched && (
            <span className="absolute top-1.5 left-1.5 text-[10px] font-semibold bg-emerald-600/90 text-white rounded-full px-1.5 py-0.5"
              title="Watched">✓</span>
          )}
          {movie && inProgress(movie) && movie.duration > 0 && (
            <div className="absolute inset-x-0 bottom-0 h-1 bg-gray-900/70">
              <div className="h-full bg-sky-500"
                style={{ width: `${Math.min(100, (movie.position / movie.duration) * 100)}%` }} />
            </div>
          )}
        </div>
      </button>
      <button
        className={`absolute top-1.5 right-1.5 px-1 py-0.5 drop-shadow ${favorite ? "text-rose-400" : "text-gray-400/80 hover:text-rose-400"}`}
        title={favorite ? "Remove from favorites" : "Add to favorites"}
        onClick={(e) => { e.stopPropagation(); onFav(); }}>
        <IHeart size={15} filled={favorite} />
      </button>
    </div>
  );
}

/** Landscape resume card (backdrop art + progress) for Continue watching.
    Clicking the card plays; clicking the NAME opens the title's detail page. */
function ContinueCard({ item, meta, onPlay, onInfo, onRemove }: {
  item: WatchItem; meta?: TitleMeta; onPlay: () => void; onInfo: () => void; onRemove: () => void;
}) {
  const preview = usePreview();
  const isEp = item.media_type === "tvSeries";
  const pct = item.duration > 0 ? Math.min(100, (item.position / item.duration) * 100) : 0;
  return (
    <div className="relative flex-none w-64 snap-start"
      onMouseEnter={preview.start} onMouseLeave={preview.stop}>
      <button data-card onClick={onPlay}
        className="w-full text-left rounded-xl overflow-hidden border border-gray-800 bg-gray-800/60
          hover:border-sky-700 focus:outline-none focus:border-sky-600">
        <div className="relative aspect-video bg-gradient-to-br from-gray-800 to-gray-900">
          {meta?.backdrop ? (
            <img src={watch.metaImageUrl(meta.backdrop)} alt="" loading="lazy"
              className="absolute inset-0 w-full h-full object-cover" />
          ) : meta?.poster ? (
            <img src={watch.metaImageUrl(meta.poster)} alt="" loading="lazy"
              className="absolute inset-0 w-full h-full object-cover" />
          ) : null}
          {preview.on && <HoverPreview item={item} />}
          <div className="absolute inset-0 bg-gradient-to-t from-black/85 via-transparent to-transparent" />
          <div className="absolute inset-0 flex items-center justify-center opacity-0 hover:opacity-100 transition-opacity">
            <span className="w-11 h-11 rounded-full bg-black/60 border border-white/25 flex items-center justify-center text-white">
              <IPlay size={22} />
            </span>
          </div>
          <div className="absolute inset-x-0 bottom-0 px-2.5 pb-1.5">
            <span role="link" tabIndex={0}
              className="block text-xs font-medium text-white truncate hover:underline cursor-pointer"
              title="Open details"
              onClick={(e) => { e.stopPropagation(); onInfo(); }}
              onKeyDown={(e) => { if (e.key === "Enter") { e.stopPropagation(); onInfo(); } }}>
              {item.title}
            </span>
            <div className="text-[10px] text-gray-400 truncate">
              {isEp ? `${epLabel(item)}${item.episode_title ? ` · ${item.episode_title}` : ""}`
                    : fmtTime(item.position) + (item.duration > 0 ? ` / ${fmtTime(item.duration)}` : "")}
            </div>
          </div>
          <div className="absolute inset-x-0 bottom-0 h-1 bg-gray-900/70">
            <div className="h-full bg-sky-500" style={{ width: `${pct}%` }} />
          </div>
        </div>
      </button>
      <button className="absolute top-1 right-1 text-gray-400/90 hover:text-white text-sm px-1 drop-shadow"
        title="Remove from Continue watching"
        onClick={(e) => { e.stopPropagation(); onRemove(); }}>✕</button>
    </div>
  );
}

/* ── Title detail page (backdrop + metadata + play/download) ──────────────── */

function DetailPage({ group, onBack, onPlay, onToggleFav }: {
  group: TitleGroup;
  onBack: () => void;
  onPlay: (i: WatchItem) => void;
  onToggleFav: () => void;
}) {
  const user = useAuth();
  const toast = useToast();
  const isAdmin = user?.role === "admin";
  const m = group.meta;
  const isShow = group.media_type === "tvSeries";
  const favorite = group.eps[0].favorite;
  const displayTitle = m?.title || group.title;
  const [episodeMeta, setEpisodeMeta] = useState<Record<string, EpisodeMeta> | null>(null);
  const [scheduling, setScheduling] = useState(false);
  const [scheduleSeasons, setScheduleSeasons] = useState<number[]>([]);

  useEffect(() => {
    if (isShow) watch.episodeMeta(group.imdb_id).then(setEpisodeMeta).catch(() => setEpisodeMeta({}));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [group.imdb_id, isShow]);

  const openSchedule = async () => {
    try {
      const res = await search.seasons(group.imdb_id);
      setScheduleSeasons(res.seasons);
    } catch {
      setScheduleSeasons(Array.from(new Set(group.eps.map((e) => e.season ?? 1))).sort((a, b) => a - b));
    }
    setScheduling(true);
  };

  return (
    <div className="space-y-6">
      <button className="text-sm text-gray-400 hover:text-gray-200" onClick={onBack}>← Back to Watch</button>

      <section className="relative rounded-2xl overflow-hidden border border-gray-800">
        {m?.backdrop && (
          <img src={watch.metaImageUrl(m.backdrop)} alt=""
            className="absolute inset-0 w-full h-full object-cover opacity-35" />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-gray-950 via-gray-950/70 to-gray-950/30" />
        <div className="relative p-5 sm:p-7 flex gap-6">
          {m?.poster && (
            <img src={watch.metaImageUrl(m.poster)} alt={displayTitle}
              className="hidden sm:block w-36 lg:w-44 rounded-xl border border-gray-700 shadow-xl self-start" />
          )}
          <div className="min-w-0 space-y-3">
            <h1 className="text-2xl sm:text-3xl font-bold flex items-center gap-3 flex-wrap">
              <span>{displayTitle}</span>
              <button className={`leading-none ${favorite ? "text-rose-400" : "text-gray-500 hover:text-rose-400"}`}
                title={favorite ? "Remove from favorites" : "Add to favorites"}
                onClick={onToggleFav}>
                <IHeart size={22} filled={favorite} />
              </button>
            </h1>
            <MetaLine group={group} />
            {m?.tagline && <p className="text-sm italic text-gray-500">{m.tagline}</p>}
            {m?.overview && <p className="text-sm text-gray-300 leading-relaxed max-w-2xl">{m.overview}</p>}
            {isShow
              ? <ShowActions eps={group.eps} onPlay={onPlay} imdbId={group.imdb_id}
                  isAdmin={isAdmin} onSchedule={openSchedule} />
              : <MovieActions item={group.eps[0]} onPlay={onPlay} />}
          </div>
        </div>
      </section>

      {isShow && <SeasonList eps={group.eps} onPlay={onPlay} episodeMeta={episodeMeta} />}

      {scheduling && (
        <ScheduleForm imdb_id={group.imdb_id} title={displayTitle} year={m?.year ?? group.year}
          availableSeasons={scheduleSeasons}
          onClose={() => setScheduling(false)}
          onSaved={() => { setScheduling(false); toast(`Scheduled ${displayTitle}`, "success"); }} />
      )}
    </div>
  );
}

function MovieActions({ item, onPlay }: { item: WatchItem; onPlay: (i: WatchItem) => void }) {
  return (
    <div className="space-y-3 pt-1">
      <div className="text-sm text-gray-500">
        {item.size ? `${fmtBytes(item.size)} · ` : ""}
        {item.has_subtitle ? "Subtitles available · " : ""}
        {item.watched
          ? <span className="text-emerald-400">Watched ✓</span>
          : inProgress(item)
            ? `Stopped at ${fmtTime(item.position)}${item.duration > 0 ? ` of ${fmtTime(item.duration)}` : ""}`
            : "Not watched yet"}
      </div>
      <div className="flex gap-2 flex-wrap items-center">
        <button className="btn-primary inline-flex items-center gap-1.5" onClick={() => onPlay(item)}>
          <IPlay size={16} />
          {inProgress(item) ? `Resume from ${fmtTime(item.position)}` : item.watched ? "Watch again" : "Play"}
        </button>
        {inProgress(item) && (
          <button className="btn-secondary inline-flex items-center gap-1.5"
            onClick={() => onPlay({ ...item, position: 0, watched: false })}>
            <IRestart size={15} /> Start over
          </button>
        )}
        <a className="btn-secondary inline-flex items-center gap-1.5" href={watch.downloadUrl(item.id)} download
          title="Save this movie to your computer">
          <IDownload size={16} /> Download
        </a>
      </div>
    </div>
  );
}

function ShowActions({ eps, onPlay, imdbId, isAdmin, onSchedule }: {
  eps: WatchItem[]; onPlay: (i: WatchItem) => void; imdbId: string;
  isAdmin: boolean; onSchedule: () => void;
}) {
  const next = upNext(eps);
  const watchedCount = eps.filter((e) => e.watched).length;
  return (
    <div className="space-y-2 pt-1">
      <div className="flex items-center gap-3 flex-wrap">
        <button className="btn-primary inline-flex items-center gap-1.5" onClick={() => onPlay(next)}>
          <IPlay size={16} />
          {inProgress(next) ? `Resume ${epLabel(next)}` : watchedCount > 0 ? `Next up ${epLabel(next)}` : `Play ${epLabel(next)}`}
        </button>
        <span className="text-sm text-gray-500">{watchedCount}/{eps.length} episodes watched</span>
        <a className="btn-secondary inline-flex items-center gap-1.5"
          href={watch.showZipUrl(imdbId, true)} download
          title="Download every episode of the show as one .zip">
          <IDownload size={16} /> Download show
        </a>
        {isAdmin && (
          <button className="btn-secondary btn-sm inline-flex items-center gap-1.5"
            onClick={onSchedule} title="Auto-check for new episodes">
            <ICalendar size={14} /> Schedule
          </button>
        )}
      </div>
    </div>
  );
}

function SeasonList({ eps, onPlay, episodeMeta }: {
  eps: WatchItem[]; onPlay: (i: WatchItem) => void; episodeMeta: Record<string, EpisodeMeta> | null;
}) {
  const next = upNext(eps);

  const seasons = useMemo(() => {
    const bySeason = new Map<number, WatchItem[]>();
    for (const e of eps) bySeason.set(e.season ?? 1, [...(bySeason.get(e.season ?? 1) ?? []), e]);
    return Array.from(bySeason.entries()).sort((a, b) => a[0] - b[0]);
  }, [eps]);

  // open the season the "up next" episode is in by default
  const [openSeason, setOpenSeason] = useState<number | null>(next.season ?? null);
  const imdbId = eps[0]?.imdb_id;

  return (
    <div className="space-y-2">
      {seasons.map(([season, list]) => {
        const w = list.filter((e) => e.watched).length;
        const open = openSeason === season;
        return (
          <div key={season} className="card p-0 overflow-hidden">
            <div className="flex items-center gap-2 px-3 py-2.5">
              <button className="flex items-center gap-2 min-w-0 flex-1 text-left hover:opacity-80"
                onClick={() => setOpenSeason(open ? null : season)}>
                <span className="text-gray-500 w-4">{open ? "▾" : "▸"}</span>
                <span className="text-sm font-medium">Season {season}</span>
                <span className="text-xs text-gray-600">· {list.length} episode{list.length !== 1 ? "s" : ""}</span>
                {w > 0 && (
                  <span className={`text-xs ml-auto ${w === list.length ? "text-emerald-400" : "text-gray-500"}`}>
                    {w === list.length ? "✓ all watched" : `${w}/${list.length} watched`}
                  </span>
                )}
              </button>
              {imdbId && (
                <a className="btn-secondary btn-xs inline-flex items-center gap-1"
                  href={watch.seasonZipUrl(imdbId, season, true)} download
                  title={`Download every Season ${season} episode as one .zip`}>
                  <IDownload size={13} /> Download season
                </a>
              )}
            </div>
            {open && (
              <div className="border-t border-gray-800 p-2">
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
                  {list.map((e) => (
                    <EpisodeCard key={e.id} ep={e} meta={episodeMeta?.[`${season}:${e.episode}`]} onPlay={onPlay} />
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Episode card with a hover overlay (overview/air date/rating) — mirrors
    Library.tsx's episode cards so the same show looks consistent across
    Library and Watch. */
function EpisodeCard({ ep, meta, onPlay }: {
  ep: WatchItem; meta?: EpisodeMeta; onPlay: (i: WatchItem) => void;
}) {
  const [hover, setHover] = useState(false);
  const code = `E${String(ep.episode ?? 0).padStart(2, "0")}`;
  const hasOverlayText = !!(meta && (meta.overview || meta.air_date || meta.rating));
  const pct = inProgress(ep) && ep.duration > 0 ? Math.round((ep.position / ep.duration) * 100) : null;

  return (
    <div className="relative rounded-lg border border-gray-800 bg-gray-800/40 overflow-hidden group"
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <button className="w-full text-left" onClick={() => onPlay(ep)}>
        <div className="relative aspect-video bg-gray-900">
          {meta?.still ? (
            <img src={watch.metaImageUrl(meta.still)} alt="" loading="lazy"
              className="absolute inset-0 w-full h-full object-cover" />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-gray-600 text-xs font-mono">{code}</div>
          )}
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity bg-black/30">
            <span className="w-9 h-9 rounded-full bg-black/60 border border-white/25 flex items-center justify-center text-white">
              <IPlay size={18} />
            </span>
          </div>
          {hover && hasOverlayText && (
            <div className="absolute inset-0 bg-black/85 p-2 text-[11px] text-gray-200 overflow-y-auto">
              <div className="flex items-center gap-2 text-gray-400 mb-1">
                {meta?.rating ? <span className="text-amber-400 font-medium">★ {meta.rating.toFixed(1)}</span> : null}
                {meta?.air_date && <span>{meta.air_date}</span>}
              </div>
              {meta?.overview && <p className="leading-snug">{meta.overview}</p>}
            </div>
          )}
          {pct !== null && (
            <div className="absolute inset-x-0 bottom-0 h-1 bg-gray-900/70">
              <div className="h-full bg-sky-500" style={{ width: `${pct}%` }} />
            </div>
          )}
          {ep.watched && (
            <span className="absolute top-1 left-1 text-[10px] font-semibold bg-emerald-600/90 text-white rounded-full px-1.5 py-0.5"
              title="Watched">✓</span>
          )}
        </div>
      </button>
      <div className="p-1.5 space-y-0.5">
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] font-mono text-gray-500">{code}</span>
          <span className="ml-auto flex items-center gap-1.5 text-[10px] text-gray-600">
            {ep.has_subtitle && <span>CC</span>}
            {ep.size ? <span>{fmtBytes(ep.size)}</span> : null}
          </span>
        </div>
        <div className="text-xs truncate" title={ep.episode_title || undefined}>
          {ep.episode_title || `Episode ${ep.episode}`}
        </div>
        <a className="inline-flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-200 opacity-0 group-hover:opacity-100 transition-opacity pt-0.5"
          href={watch.downloadUrl(ep.id)} download title="Save this episode to your computer"
          onClick={(ev) => ev.stopPropagation()}>
          <IDownload size={12} /> Download
        </a>
      </div>
    </div>
  );
}

/* ── Player icons (monochrome SVG — no colored emoji glyphs) ──────────────── */

function Svg({ children, size = 22, stroke = false }: {
  children: React.ReactNode; size?: number; stroke?: boolean;
}) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size}
      fill={stroke ? "none" : "currentColor"}
      stroke={stroke ? "currentColor" : "none"}
      strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}
const IPlay = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M8 5v14l11-7z" /></Svg>;
const IPause = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M6 5h4v14H6zM14 5h4v14h-4z" /></Svg>;
const IBack10 = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z" />
    <text x="12" y="16.5" textAnchor="middle" fontSize="7" fontFamily="sans-serif" fontWeight="bold" stroke="none">10</text>
  </Svg>
);
const IFwd10 = ({ size }: { size?: number }) => (
  <Svg size={size}>
    <path d="M12 5V1l5 5-5 5V7c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6h2c0 4.42-3.58 8-8 8s-8-3.58-8-8 3.58-8 8-8z" />
    <text x="12" y="16.5" textAnchor="middle" fontSize="7" fontFamily="sans-serif" fontWeight="bold" stroke="none">10</text>
  </Svg>
);
const IVolume = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z" /></Svg>;
const IMuted = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z" /></Svg>;
const INext = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z" /></Svg>;
const IGear = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z" /></Svg>;
const IFullscreen = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z" /></Svg>;
const IExitFs = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z" /></Svg>;
const IClose = ({ size }: { size?: number }) =>
  <Svg size={size} stroke><path d="M6 6l12 12M18 6L6 18" /></Svg>;
const IDownload = ({ size }: { size?: number }) =>
  <Svg size={size}><path d="M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z" /></Svg>;
const IChevron = ({ dir, size = 26 }: { dir: "left" | "right"; size?: number }) =>
  <Svg size={size} stroke>
    {dir === "left" ? <path d="M15 6l-6 6 6 6" /> : <path d="M9 6l6 6-6 6" />}
  </Svg>;

/* ── Player (custom built-in controls) ────────────────────────────────────── */

const AUTONEXT_KEY = "riprate.autonext";
const VOLUME_KEY = "riprate.volume";
const RATE_KEY = "riprate.rate";
const COUNTDOWN_WINDOW = 20;   // seconds before the end when the overlay shows
const HIDE_AFTER_MS = 3000;    // controls auto-hide delay while playing
const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];

function Player({ item, next, onClose, onPlayNext, onProgressSaved, fsRef }: {
  item: WatchItem;
  next: WatchItem | null;
  onClose: () => void;
  onPlayNext: (n: WatchItem) => void;
  onProgressSaved: (id: number, position: number, duration: number, watched: boolean) => void;
  fsRef: RefObject<HTMLDivElement | null>;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const posRef = useRef({ position: item.position, duration: item.duration });
  const hideTimer = useRef<number | null>(null);
  const menuOpenRef = useRef(false);
  const originalCueTimes = useRef(new WeakMap<TextTrackCue, [number, number]>());

  const [paused, setPaused] = useState(true);
  const [cur, setCur] = useState(0);
  const [dur, setDur] = useState(0);
  // sound on by default: never start muted, and ignore a saved volume of 0
  const [volume, setVolume] = useState(() => {
    const v = Number(localStorage.getItem(VOLUME_KEY));
    return Number.isFinite(v) && v > 0 && v <= 1 ? v : 1;
  });
  const [muted, setMuted] = useState(false);
  const [ccOn, setCcOn] = useState(true);
  const [rate, setRate] = useState(() => {
    const r = Number(localStorage.getItem(RATE_KEY));
    return RATES.includes(r) ? r : 1;
  });
  const [isFs, setIsFs] = useState(() => !!document.fullscreenElement);
  const [ctlVisible, setCtlVisible] = useState(true);
  const [menuOpen, setMenuOpen] = useState(false);
  const [autoNext, setAutoNext] = useState(() => localStorage.getItem(AUTONEXT_KEY) !== "off");
  const [cancelled, setCancelled] = useState(false);   // per-playback autonext cancel
  const [countdown, setCountdown] = useState<number | null>(null);
  const [resumedFrom, setResumedFrom] = useState<number | null>(null);
  const [subtitleOffset, setSubtitleOffset] = useState(0);
  const [subtitleVersion, setSubtitleVersion] = useState(0);
  const [subtitleSyncing, setSubtitleSyncing] = useState(false);
  const [subtitleMessage, setSubtitleMessage] = useState("");
  const isAdmin = useAuth()?.role === "admin";

  useEffect(() => { menuOpenRef.current = menuOpen; }, [menuOpen]);

  const applySubtitleOffset = (offset: number) => {
    const track = videoRef.current?.textTracks[0];
    if (!track?.cues) return;
    for (let index = 0; index < track.cues.length; index += 1) {
      const cue = track.cues[index];
      let original = originalCueTimes.current.get(cue);
      if (!original) {
        original = [cue.startTime, cue.endTime];
        originalCueTimes.current.set(cue, original);
      }
      cue.startTime = Math.max(0, original[0] + offset);
      cue.endTime = Math.max(cue.startTime + 0.001, original[1] + offset);
    }
  };

  const changeSubtitleOffset = (offset: number) => {
    const rounded = Math.round(Math.max(-600, Math.min(600, offset)) * 10) / 10;
    setSubtitleOffset(rounded);
    applySubtitleOffset(rounded);
  };

  const reloadSubtitles = () => {
    originalCueTimes.current = new WeakMap();
    setSubtitleOffset(0);
    setSubtitleVersion((value) => value + 1);
  };

  const runSubtitleSync = async () => {
    setSubtitleSyncing(true);
    setSubtitleMessage("Starting audio alignment…");
    try {
      let state = await subtitleTiming.startAutoSync(item.id);
      setSubtitleMessage(state.message);
      while (state.status === "running") {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        state = await subtitleTiming.syncStatus(item.id);
        setSubtitleMessage(state.message);
      }
      if (state.status === "completed") reloadSubtitles();
    } catch (err: unknown) {
      setSubtitleMessage(err instanceof Error ? err.message : "Subtitle alignment failed");
    } finally {
      setSubtitleSyncing(false);
    }
  };

  const saveSubtitleOffset = async () => {
    if (!subtitleOffset) return;
    setSubtitleMessage("Saving subtitle timing…");
    try {
      await subtitleTiming.saveOffset(item.id, subtitleOffset);
      setSubtitleMessage("Timing saved for RipRate and external-subtitle players.");
      reloadSubtitles();
    } catch (err: unknown) {
      setSubtitleMessage(err instanceof Error ? err.message : "Could not save subtitle timing");
    }
  };

  const restoreSubtitles = async () => {
    try {
      await subtitleTiming.restore(item.id);
      setSubtitleMessage("Original subtitle timing restored.");
      reloadSubtitles();
    } catch (err: unknown) {
      setSubtitleMessage(err instanceof Error ? err.message : "No original backup is available");
    }
  };

  const label = item.media_type === "tvSeries"
    ? `${item.title} · ${epLabel(item)}${item.episode_title ? ` · ${item.episode_title}` : ""}`
    : `${item.title}${item.year ? ` (${item.year})` : ""}`;

  const persist = (position: number, duration: number) => {
    if (duration <= 0) return;
    posRef.current = { position, duration };
    watch.saveProgress(item.id, position, duration)
      .then((r) => onProgressSaved(item.id, position, duration, r.watched))
      .catch(() => {});
  };

  const seek = (delta: number) => {
    const v = videoRef.current;
    if (v && v.duration) v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + delta));
  };

  const togglePlay = () => {
    setMenuOpen(false);
    const v = videoRef.current;
    if (v) v.paused ? v.play() : v.pause();
  };

  const goNext = () => {
    const v = videoRef.current;
    if (v && v.duration) persist(v.duration, v.duration);   // leaving early counts as watched
    if (next) onPlayNext(next);
  };

  const toggleAutoNext = () => {
    const on = !autoNext;
    setAutoNext(on);
    localStorage.setItem(AUTONEXT_KEY, on ? "on" : "off");
    if (!on) setCountdown(null);
  };

  const toggleFullscreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void fsRef.current?.requestFullscreen();
  };

  const changeVolume = (v: number) => {
    v = Math.max(0, Math.min(1, v));
    setVolume(v);
    setMuted(v === 0);
    if (v > 0) localStorage.setItem(VOLUME_KEY, String(v));
  };

  const changeRate = (r: number) => {
    setRate(r);
    localStorage.setItem(RATE_KEY, String(r));
  };

  // show controls now; restart the auto-hide timer (hides only while playing)
  const poke = () => {
    setCtlVisible(true);
    if (hideTimer.current) window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => {
      const v = videoRef.current;
      if (v && !v.paused && !menuOpenRef.current) {
        setCtlVisible(false);
        setMenuOpen(false);
      }
    }, HIDE_AFTER_MS);
  };

  // keyboard: Esc close · space play/pause · ←/→ ±10s · ↑/↓ volume · m mute · f fullscreen
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLButtonElement || e.target instanceof HTMLInputElement) return;
      poke();
      if (e.key === "Escape" && !document.fullscreenElement) onClose();
      else if (e.key === "ArrowLeft") { e.preventDefault(); seek(-10); }
      else if (e.key === "ArrowRight") { e.preventDefault(); seek(10); }
      else if (e.key === "ArrowUp") { e.preventDefault(); changeVolume((videoRef.current?.volume ?? 1) + 0.1); }
      else if (e.key === "ArrowDown") { e.preventDefault(); changeVolume((videoRef.current?.volume ?? 1) - 0.1); }
      else if (e.key === "m") setMuted((m) => !m);
      else if (e.key === "f") toggleFullscreen();
      else if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onClose]);

  useEffect(() => {
    const onFsChange = () => setIsFs(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  // periodic save while playing + final keepalive save on unmount/tab close
  useEffect(() => {
    const t = window.setInterval(() => {
      const v = videoRef.current;
      if (v && !v.paused && v.duration) persist(v.currentTime, v.duration);
    }, 10_000);
    return () => {
      clearInterval(t);
      if (hideTimer.current) window.clearTimeout(hideTimer.current);
      const { position, duration } = posRef.current;
      if (duration > 0) watch.saveProgressBeacon(item.id, position, duration);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // apply volume / mute / speed / subtitle-visibility to the element
  useEffect(() => {
    const v = videoRef.current;
    if (v) { v.volume = volume; v.muted = muted; }
  }, [volume, muted]);
  useEffect(() => {
    const v = videoRef.current;
    if (v) v.playbackRate = rate;
  }, [rate]);
  useEffect(() => {
    const v = videoRef.current;
    if (v && v.textTracks.length > 0) v.textTracks[0].mode = ccOn ? "showing" : "hidden";
  }, [ccOn, dur]);

  const onLoadedMetadata = () => {
    const v = videoRef.current;
    if (!v) return;
    setDur(v.duration || 0);
    posRef.current.duration = v.duration || 0;
    v.volume = volume;
    v.muted = muted;
    v.playbackRate = rate;
    if (!item.watched && item.position > 5 && item.position < (v.duration || 0) * 0.95) {
      v.currentTime = item.position;
      setResumedFrom(item.position);
      window.setTimeout(() => setResumedFrom(null), 4000);
    }
    poke();
  };

  const onTimeUpdate = () => {
    const v = videoRef.current;
    if (!v || !v.duration) return;
    setCur(v.currentTime);
    posRef.current = { position: v.currentTime, duration: v.duration };
    if (next && autoNext && !cancelled) {
      const remaining = v.duration - v.currentTime;
      setCountdown(remaining <= COUNTDOWN_WINDOW ? Math.max(0, Math.ceil(remaining)) : null);
    }
  };

  const onEnded = () => {
    const v = videoRef.current;
    if (v && v.duration) persist(v.duration, v.duration);
    if (next && autoNext && !cancelled) onPlayNext(next);
  };

  const onPauseEvt = () => {
    setPaused(true);
    poke();
    const v = videoRef.current;
    if (v && v.duration && !v.ended) persist(v.currentTime, v.duration);
  };

  const barBtn = "p-2 rounded-full text-gray-200 hover:text-white hover:bg-white/10 flex items-center justify-center";
  const centerBtn = `rounded-full bg-black/55 hover:bg-black/80 text-white border border-white/15 shadow-lg
    flex items-center justify-center ${ctlVisible ? "pointer-events-auto" : ""}`;

  return (
    <div ref={wrapRef}
      className={`fixed inset-0 z-50 bg-black flex flex-col ${ctlVisible ? "" : "cursor-none"}`}
      onMouseMove={poke}>
      <video
        ref={videoRef}
        className="absolute inset-0 w-full h-full object-contain bg-black"
        src={watch.streamUrl(item.id)}
        autoPlay
        onClick={togglePlay}
        onDoubleClick={toggleFullscreen}
        onLoadedMetadata={onLoadedMetadata}
        onTimeUpdate={onTimeUpdate}
        onEnded={onEnded}
        onPlay={() => { setPaused(false); poke(); }}
        onPause={onPauseEvt}
      >
        {item.has_subtitle && (
          <track key={subtitleVersion}
            src={`${watch.subtitleUrl(item.id)}?v=${subtitleVersion}`}
            kind="subtitles" srcLang="en" label="English" default
            onLoad={() => applySubtitleOffset(subtitleOffset)} />
        )}
      </video>

      {/* center cluster: skip back · play/pause · skip forward */}
      <div className={`absolute inset-0 flex items-center justify-center gap-10 pointer-events-none transition-opacity ${ctlVisible ? "opacity-100" : "opacity-0"}`}>
        <button className={`${centerBtn} w-14 h-14`} onClick={() => seek(-10)} title="Back 10 seconds (←)">
          <IBack10 size={26} />
        </button>
        <button className={`${centerBtn} w-20 h-20`} onClick={togglePlay} title={paused ? "Play (space)" : "Pause (space)"}>
          {paused ? <IPlay size={40} /> : <IPause size={40} />}
        </button>
        <button className={`${centerBtn} w-14 h-14`} onClick={() => seek(10)} title="Forward 10 seconds (→)">
          <IFwd10 size={26} />
        </button>
      </div>

      {/* top bar */}
      <div className={`absolute top-0 inset-x-0 flex items-center gap-3 px-4 py-3 bg-gradient-to-b from-black/80 to-transparent transition-opacity ${ctlVisible ? "opacity-100" : "opacity-0 pointer-events-none"}`}>
        <span className="text-gray-100 font-medium truncate">{label}</span>
        <button className={`ml-auto ${barBtn}`} title="Close (Esc)" onClick={onClose}><IClose /></button>
      </div>

      {/* bottom control bar */}
      <div className={`absolute bottom-0 inset-x-0 px-4 pb-3 pt-10 bg-gradient-to-t from-black/90 to-transparent transition-opacity ${ctlVisible ? "opacity-100" : "opacity-0 pointer-events-none"}`}>
        {/* scrub */}
        <div className="flex items-center gap-3 text-xs text-gray-300 font-mono">
          <span>{fmtTime(cur)}</span>
          <input type="range" className="flex-1 accent-gray-200 h-1.5 cursor-pointer"
            min={0} max={dur || 0} step={0.1} value={cur}
            onChange={(e) => {
              const v = videoRef.current;
              if (v) v.currentTime = Number(e.target.value);
              setCur(Number(e.target.value));
            }} />
          <span>{fmtTime(dur)}</span>
        </div>

        {/* buttons */}
        <div className="flex items-center gap-1 mt-1.5">
          <button className={barBtn} onClick={togglePlay} title={paused ? "Play (space)" : "Pause (space)"}>
            {paused ? <IPlay /> : <IPause />}
          </button>
          <button className={barBtn} onClick={() => seek(-10)} title="Back 10 seconds (←)"><IBack10 /></button>
          <button className={barBtn} onClick={() => seek(10)} title="Forward 10 seconds (→)"><IFwd10 /></button>

          <button className={barBtn} onClick={() => setMuted((m) => !m)} title={muted ? "Unmute (m)" : "Mute (m)"}>
            {muted || volume === 0 ? <IMuted /> : <IVolume />}
          </button>
          <input type="range" className="w-20 accent-gray-200 h-1 cursor-pointer"
            min={0} max={1} step={0.05} value={muted ? 0 : volume}
            onChange={(e) => changeVolume(Number(e.target.value))} title="Volume (↑/↓)" />

          <span className="ml-auto" />

          {next && (
            <>
              <button className={barBtn} onClick={goNext} title={`Play ${epLabel(next)}`}><INext /></button>
              <button className={`px-2 py-1.5 rounded-full text-xs font-medium hover:bg-white/10 ${autoNext ? "text-white" : "text-gray-500"}`}
                onClick={toggleAutoNext}
                title="Automatically play the next episode when this one ends">
                Auto {autoNext ? "on" : "off"}
              </button>
            </>
          )}
          <button className={`${barBtn} ${menuOpen ? "bg-white/10 text-white" : ""}`}
            onClick={() => { setMenuOpen((o) => !o); poke(); }} title="Settings">
            <IGear />
          </button>
          <button className={barBtn} onClick={toggleFullscreen} title={isFs ? "Exit fullscreen (f)" : "Fullscreen (f)"}>
            {isFs ? <IExitFs /> : <IFullscreen />}
          </button>
        </div>
      </div>

      {/* settings menu */}
      {menuOpen && (
        <div className="absolute bottom-20 right-4 w-72 bg-gray-900/95 border border-gray-700 rounded-xl p-3 text-sm text-gray-200 shadow-xl space-y-3 max-h-[70vh] overflow-y-auto">
          <div>
            <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Subtitles</div>
            {item.has_subtitle ? (
              <div className="space-y-0.5">
                <MenuOpt label="Off" active={!ccOn} onClick={() => setCcOn(false)} />
                <MenuOpt label="English" active={ccOn} onClick={() => setCcOn(true)} />
                <div className="pt-2 mt-2 border-t border-gray-700 space-y-2">
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-gray-400">Timing offset</span>
                    <span className="font-mono text-gray-200">{subtitleOffset > 0 ? "+" : ""}{subtitleOffset.toFixed(1)}s</span>
                  </div>
                  <div className="grid grid-cols-4 gap-1">
                    <button className="btn-secondary btn-sm" onClick={() => changeSubtitleOffset(subtitleOffset - 1)}>−1s</button>
                    <button className="btn-secondary btn-sm" onClick={() => changeSubtitleOffset(subtitleOffset - .1)}>−.1s</button>
                    <button className="btn-secondary btn-sm" onClick={() => changeSubtitleOffset(subtitleOffset + .1)}>+.1s</button>
                    <button className="btn-secondary btn-sm" onClick={() => changeSubtitleOffset(subtitleOffset + 1)}>+1s</button>
                  </div>
                  <p className="text-[11px] text-gray-500">Use − if subtitles appear late; use + if they appear early.</p>
                  {isAdmin && (
                    <div className="grid grid-cols-2 gap-1">
                      <button className="btn-primary btn-sm" disabled={!subtitleOffset || subtitleSyncing}
                        onClick={saveSubtitleOffset}>Save offset</button>
                      <button className="btn-secondary btn-sm" disabled={subtitleSyncing}
                        onClick={runSubtitleSync}>{subtitleSyncing ? "Aligning…" : "Auto align"}</button>
                      <button className="btn-secondary btn-sm col-span-2" disabled={subtitleSyncing}
                        onClick={restoreSubtitles}>Restore original</button>
                    </div>
                  )}
                  {subtitleMessage && <p className="text-[11px] text-gray-300" role="status">{subtitleMessage}</p>}
                </div>
              </div>
            ) : (
              <div className="text-gray-500 px-2 py-1">No subtitles available</div>
            )}
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Playback speed</div>
            <div className="grid grid-cols-3 gap-1">
              {RATES.map((r) => (
                <button key={r}
                  className={`px-2 py-1 rounded-lg text-xs ${rate === r ? "bg-white/15 text-white font-medium" : "text-gray-400 hover:bg-white/10"}`}
                  onClick={() => changeRate(r)}>
                  {r}×
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {resumedFrom !== null && (
        <div className="absolute top-16 left-1/2 -translate-x-1/2 bg-gray-900/90 border border-gray-700 rounded-lg px-4 py-2 text-sm text-gray-200">
          Resumed from {fmtTime(resumedFrom)}
        </div>
      )}

      {countdown !== null && next && (
        <div className="absolute bottom-28 right-8 bg-gray-900/95 border border-gray-700 rounded-xl px-4 py-3 text-sm text-gray-200 shadow-xl space-y-2">
          <div>
            Up next: <span className="font-medium">{epLabel(next)}</span>
            {next.episode_title ? ` · ${next.episode_title}` : ""}
            <span className="text-gray-500"> — in {countdown}s</span>
          </div>
          <div className="flex gap-2">
            <button className="btn-primary btn-sm inline-flex items-center gap-1" onClick={goNext}>
              <IPlay size={14} /> Play now
            </button>
            <button className="btn-secondary btn-sm"
              onClick={() => { setCancelled(true); setCountdown(null); }}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function MenuOpt({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick}
      className={`w-full flex items-center gap-2 px-2 py-1 rounded-lg text-left ${active ? "bg-white/15 text-white" : "text-gray-400 hover:bg-white/10"}`}>
      <span className="w-4 text-center">{active ? "✓" : ""}</span>
      <span>{label}</span>
    </button>
  );
}
