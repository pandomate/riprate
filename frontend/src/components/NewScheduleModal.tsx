import { FormEvent, useEffect, useState } from "react";
import { LibraryShow, schedules, search, SearchResult } from "../api/client";
import { Modal, useToast } from "./ui";
import { ITv } from "./icons";
import ScheduleForm from "./ScheduleForm";

interface Props {
  onClose: () => void;
  onSaved: () => void;
}

interface Pick { imdb_id: string; title: string; year: number | null; seasons: number[] }

export default function NewScheduleModal({ onClose, onSaved }: Props) {
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [noShow, setNoShow] = useState(false);
  const [loading, setLoading] = useState(false);
  const [libShows, setLibShows] = useState<LibraryShow[]>([]);
  const [picked, setPicked] = useState<Pick | null>(null);

  useEffect(() => { schedules.libraryShows().then(setLibShows).catch(() => {}); }, []);

  const doSearch = async (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true); setNoShow(false);
    try {
      const res = await search.query(query);
      const shows = res.filter((r) => r.media_type === "tvSeries");
      setResults(shows);
      setNoShow(shows.length === 0);
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Search failed", "error");
    } finally { setLoading(false); }
  };

  const pick = async (item: { imdb_id: string; title: string; year: number | null }) => {
    setLoading(true);
    try {
      const res = await search.seasons(item.imdb_id);
      setPicked({ ...item, seasons: res.seasons });
    } catch {
      setPicked({ ...item, seasons: [] });
      toast("Couldn't load seasons — you can still schedule all seasons", "info");
    } finally { setLoading(false); }
  };

  if (picked) {
    return (
      <ScheduleForm imdb_id={picked.imdb_id} title={picked.title} year={picked.year}
        availableSeasons={picked.seasons} onClose={onClose} onSaved={onSaved} />
    );
  }

  return (
    <Modal title="Schedule a new show" onClose={onClose}
      footer={<button className="btn-secondary" onClick={onClose}>Cancel</button>}>
      <form onSubmit={doSearch} className="flex gap-2">
        <input className="input" placeholder="Search for a TV show…" value={query}
          autoFocus onChange={(e) => setQuery(e.target.value)} />
        <button className="btn-primary" type="submit" disabled={loading}>
          {loading ? "…" : "Search"}
        </button>
      </form>

      {noShow && (
        <p className="text-sm text-yellow-400">
          No TV shows matched. Only shows can be scheduled — movies can't have new episodes.
        </p>
      )}

      {results.length > 0 && (
        <div className="max-h-56 overflow-y-auto space-y-1.5">
          {results.map((r) => (
            <ShowRow key={r.imdb_id} imdb_id={r.imdb_id} title={r.title} year={r.year}
              poster={r.poster_url} onPick={() => pick(r)} disabled={loading} />
          ))}
        </div>
      )}

      {results.length === 0 && !noShow && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">
            Shows already in your library
          </p>
          {libShows.length === 0 ? (
            <p className="text-sm text-gray-500">No shows downloaded yet — search above.</p>
          ) : (
            <div className="max-h-56 overflow-y-auto space-y-1">
              {libShows.map((s) => (
                <ShowRow key={s.imdb_id} imdb_id={s.imdb_id} title={s.title} year={s.year}
                  scheduled={s.scheduled} onPick={() => pick(s)} disabled={loading} />
              ))}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

function ShowRow({ imdb_id, title, year, poster, scheduled, onPick, disabled }: {
  imdb_id: string; title: string; year: number | null; poster?: string | null;
  scheduled?: boolean; onPick: () => void; disabled: boolean;
}) {
  return (
    <button className="w-full flex items-center gap-3 p-2 rounded-lg hover:bg-gray-800 text-left disabled:opacity-60"
      onClick={onPick} disabled={disabled || scheduled}>
      {poster ? (
        <img src={poster} alt="" className="w-9 h-13 object-cover rounded flex-shrink-0" loading="lazy" />
      ) : (
        <div className="w-9 h-13 bg-gray-800 rounded flex items-center justify-center text-gray-600 flex-shrink-0"><ITv size={16} /></div>
      )}
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium truncate">{title}</span>
        <span className="block text-xs text-gray-500">{year ?? "Unknown"} · {imdb_id}</span>
      </span>
      {scheduled && <span className="badge-neutral flex-shrink-0">scheduled</span>}
    </button>
  );
}
