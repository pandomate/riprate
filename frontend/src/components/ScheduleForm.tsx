import { useEffect, useMemo, useState } from "react";
import { ApiError, Frequency, Schedule, ScheduleConflict, schedules } from "../api/client";
import { Modal, useToast } from "./ui";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

interface Props {
  imdb_id: string;
  title: string;
  year: number | null;
  is_anime?: boolean;
  availableSeasons?: number[];
  existing?: Schedule;
  onClose: () => void;
  onSaved: () => void;
}

export default function ScheduleForm({
  imdb_id, title, year, is_anime = false, availableSeasons, existing, onClose, onSaved,
}: Props) {
  const toast = useToast();

  // The schedule this form is actually editing: either the `existing` prop
  // (an explicit edit entry point), or one discovered below because it
  // conflicts with what the admin is trying to create — in which case we
  // pivot straight into editing it instead of a dead-end error.
  const [effectiveExisting, setEffectiveExisting] = useState<Schedule | undefined>(existing);
  const [pivoted, setPivoted] = useState(false);   // true only when we pivoted ourselves
  // Other schedules for this show (excluding effectiveExisting) — used to
  // disable "All seasons" / already-taken season buttons before submit.
  const [otherSchedules, setOtherSchedules] = useState<Schedule[]>([]);
  const [loadingConflicts, setLoadingConflicts] = useState(!existing);

  const [freq, setFreq] = useState<Frequency>(existing?.frequency ?? "weekly");
  const [dow, setDow] = useState<number>(existing?.day_of_week ?? 0);
  const [hour, setHour] = useState<number>(existing?.hour ?? 3);
  const [allSeasons, setAllSeasons] = useState<boolean>(
    existing ? existing.seasons.length === 0 : true
  );
  const [picked, setPicked] = useState<Set<number>>(new Set(existing?.seasons ?? []));
  const [busy, setBusy] = useState(false);

  // On first mount for a NEW schedule (no `existing`), check what's already
  // scheduled for this show so we can pre-empt the 409s below.
  useEffect(() => {
    if (existing) return;
    let cancelled = false;
    schedules.list().then((all) => {
      if (cancelled) return;
      const mine = all.filter((s) => s.imdb_id === imdb_id);
      const whole = mine.find((s) => s.seasons.length === 0);
      if (whole) {
        setEffectiveExisting(whole);
        setPivoted(true);
        setOtherSchedules(mine.filter((s) => s.id !== whole.id));
      } else {
        setOtherSchedules(mine);
      }
      setLoadingConflicts(false);
    }).catch(() => setLoadingConflicts(false));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imdb_id]);

  // Sync form fields whenever we (re)target a different schedule to edit —
  // covers both the initial whole-show pivot above and the submit-time 409
  // pivot below.
  useEffect(() => {
    if (!effectiveExisting) return;
    setFreq(effectiveExisting.frequency);
    setDow(effectiveExisting.day_of_week ?? 0);
    setHour(effectiveExisting.hour ?? 3);
    setAllSeasons(effectiveExisting.seasons.length === 0);
    setPicked(new Set(effectiveExisting.seasons));
  }, [effectiveExisting]);

  const takenSeasons = useMemo(
    () => new Set(otherSchedules.flatMap((s) => s.seasons)),
    [otherSchedules]
  );
  const blockAllSeasons = !effectiveExisting && otherSchedules.some((s) => s.seasons.length > 0);

  const editOther = (sched: Schedule) => {
    setEffectiveExisting(sched);
    setPivoted(true);
    setOtherSchedules((prev) => prev.filter((s) => s.id !== sched.id));
  };

  const toggleSeason = (s: number) => {
    if (!effectiveExisting && takenSeasons.has(s)) {
      const other = otherSchedules.find((sc) => sc.seasons.includes(s));
      if (other) { editOther(other); toast(`Season ${s} is already scheduled — editing that schedule instead`, "info"); }
      return;
    }
    setPicked((prev) => { const n = new Set(prev); n.has(s) ? n.delete(s) : n.add(s); return n; });
  };

  const submit = async () => {
    const seasons = allSeasons ? [] : Array.from(picked).sort((a, b) => a - b);
    if (!allSeasons && seasons.length === 0) { toast("Pick at least one season, or choose all", "error"); return; }
    setBusy(true);
    try {
      if (effectiveExisting) {
        await schedules.update(effectiveExisting.id, {
          frequency: freq, day_of_week: freq === "weekly" ? dow : null, hour, seasons,
        });
        toast("Schedule updated", "success");
      } else {
        await schedules.create({
          imdb_id, title, year, seasons, frequency: freq,
          is_anime,
          day_of_week: freq === "weekly" ? dow : null, hour,
        });
        toast(`Scheduled ${title}`, "success");
      }
      onSaved();
    } catch (err: unknown) {
      const apiErr = err as ApiError;
      const detail = apiErr.detail as ScheduleConflict | undefined;
      if (apiErr.status === 409 && detail?.existing) {
        setEffectiveExisting(detail.existing);
        setPivoted(true);
        toast(detail.message, "info");
      } else {
        toast(err instanceof Error ? err.message : "Failed to save schedule", "error");
      }
      setBusy(false);
    }
  };

  const isEditing = !!effectiveExisting;

  return (
    <Modal
      title={isEditing ? `Edit schedule — ${title}` : `Schedule ${title}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn-primary" onClick={submit} disabled={busy || loadingConflicts}>
            {isEditing ? "Save" : "Schedule show"}
          </button>
        </>
      }
    >
      {pivoted && (
        <p className="text-sm text-amber-400 bg-amber-950/40 border border-amber-800/60 rounded-lg px-3 py-2">
          This show already has a schedule that covers what you picked — editing it below
          instead of creating a duplicate.
        </p>
      )}

      <p className="text-sm text-gray-500">
        RipRate will check for new episodes on this cadence and automatically queue any
        that aren't already downloaded.
      </p>

      <div>
        <label className="label">How often to check</label>
        <select className="input" value={freq} onChange={(e) => setFreq(e.target.value as Frequency)}>
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
        </select>
      </div>

      <div className="flex gap-3">
        {freq === "weekly" && (
          <div className="flex-1">
            <label className="label">Day of week</label>
            <select className="input" value={dow} onChange={(e) => setDow(Number(e.target.value))}>
              {DAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
            </select>
          </div>
        )}
        {freq !== "monthly" && (
          <div className="flex-1">
            <label className="label">Time of day</label>
            <select className="input" value={hour} onChange={(e) => setHour(Number(e.target.value))}>
              {Array.from({ length: 24 }).map((_, h) => (
                <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div>
        <label className="label">Seasons to track</label>
        <label className={`flex items-center gap-2 text-sm mb-2 ${blockAllSeasons ? "opacity-50" : "cursor-pointer"}`}>
          <input type="checkbox" className="w-4 h-4 accent-sky-500" checked={allSeasons}
            disabled={blockAllSeasons}
            onChange={(e) => setAllSeasons(e.target.checked)} />
          All seasons (including future ones)
        </label>
        {blockAllSeasons && (
          <p className="text-xs text-yellow-500 mb-2">
            This show already has per-season schedule(s) — a whole-show schedule can't
            coexist with them. Pick specific seasons instead, or edit/delete the existing
            one(s) from the Schedule page first.
          </p>
        )}
        {!allSeasons && (
          availableSeasons && availableSeasons.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {availableSeasons.map((s) => {
                const taken = !effectiveExisting && takenSeasons.has(s);
                return (
                  <button key={s} type="button"
                    title={taken ? "Already scheduled — click to edit that schedule" : undefined}
                    className={`px-2.5 py-1 rounded-lg border text-sm ${
                      picked.has(s) ? "border-sky-500 bg-sky-900/40 text-sky-300"
                      : taken ? "border-yellow-800 bg-yellow-950/30 text-yellow-600"
                      : "border-gray-700 text-gray-300 hover:bg-gray-800"}`}
                    onClick={() => toggleSeason(s)}>
                    S{s}{taken ? " ✓" : ""}
                  </button>
                );
              })}
            </div>
          ) : (
            <p className="text-xs text-gray-500">
              Uncheck "All seasons" is only useful when specific seasons are known — leave it checked here.
            </p>
          )
        )}
      </div>
    </Modal>
  );
}
