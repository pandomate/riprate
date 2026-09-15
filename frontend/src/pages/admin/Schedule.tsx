import { useEffect, useState } from "react";
import {
  RunItemStatus, Schedule as Sched, ScheduleRun, ScheduleRunDetail, SeasonGap, schedules,
} from "../../api/client";
import { fmtDate, ProgressBar, useToast } from "../../components/ui";
import ScheduleForm from "../../components/ScheduleForm";
import NewScheduleModal from "../../components/NewScheduleModal";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function Schedule() {
  const toast = useToast();
  const [list, setList] = useState<Sched[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<Sched | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);

  const load = () => schedules.list()
    .then((l) => { setList(l); })
    .catch(() => {})
    .finally(() => setLoading(false));
  useEffect(() => { load(); }, []);

  const act = async (id: number, fn: () => Promise<unknown>, ok: string) => {
    setBusy(id);
    try { await fn(); toast(ok, "success"); load(); }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Failed", "error"); }
    finally { setBusy(null); }
  };

  if (loading) return <p className="text-gray-500">Loading…</p>;

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Schedule</h1>
          <p className="text-gray-500 text-sm mt-1">
            Shows checked automatically for new episodes. Add one here or from the Search page's
            <span className="text-gray-300"> Schedule</span> button.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setCreating(true)}>+ New schedule</button>
      </header>

      {list.length === 0 ? (
        <div className="card text-gray-500 text-center py-10">
          No scheduled shows yet. Search for a TV series and click <span className="text-gray-300">Schedule</span>.
        </div>
      ) : (
        <div className="space-y-3">
          {list.map((s) => (
            <ScheduleCard key={s.id} sched={s} busy={busy === s.id}
              onEdit={() => setEditing(s)}
              onToggle={() => act(s.id, () => schedules.update(s.id, { is_active: !s.is_active }), s.is_active ? "Paused" : "Resumed")}
              onCheck={() => act(s.id, async () => { const r = await schedules.check(s.id); toast(`Checked — queued ${r.queued}`, "success"); }, "Checked")}
              onDelete={() => confirm(`Remove schedule for "${s.title}"?`) && act(s.id, () => schedules.remove(s.id), "Schedule removed")} />
          ))}
        </div>
      )}

      {editing && (
        <ScheduleForm imdb_id={editing.imdb_id} title={editing.title} year={editing.year}
          availableSeasons={editing.seasons} existing={editing}
          onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
      )}
      {creating && (
        <NewScheduleModal onClose={() => setCreating(false)} onSaved={() => { setCreating(false); load(); }} />
      )}
    </div>
  );
}

function cadence(s: Sched): string {
  let base = s.frequency.charAt(0).toUpperCase() + s.frequency.slice(1);
  if (s.frequency === "weekly" && s.day_of_week != null) base += ` · ${DAYS[s.day_of_week]}`;
  if (s.frequency !== "monthly" && s.hour != null) base += ` · ${String(s.hour).padStart(2, "0")}:00`;
  return base;
}

function ScheduleCard({ sched, busy, onEdit, onToggle, onCheck, onDelete }: {
  sched: Sched; busy: boolean;
  onEdit: () => void; onToggle: () => void; onCheck: () => void; onDelete: () => void;
}) {
  const [gaps, setGaps] = useState<SeasonGap[] | null>(null);
  const [open, setOpen] = useState(false);
  const [runsOpen, setRunsOpen] = useState(false);

  const loadStatus = () => {
    if (gaps) { setOpen((o) => !o); return; }
    setOpen(true);
    schedules.status(sched.id).then((r) => setGaps(r.seasons)).catch(() => setGaps([]));
  };

  const totalEps = gaps?.reduce((a, g) => a + g.total, 0) ?? 0;
  const totalDone = gaps?.reduce((a, g) => a + g.downloaded, 0) ?? 0;

  return (
    <div className="card space-y-2">
      <div className="flex items-start gap-2 flex-wrap">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{sched.title}</span>
            {sched.year && <span className="text-gray-500 text-sm">({sched.year})</span>}
            {sched.is_active
              ? <span className="badge-approved">Active</span>
              : <span className="badge-neutral">Paused</span>}
            {sched.due && sched.is_active && <span className="badge-pending">Due</span>}
          </div>
          <p className="text-xs text-gray-500 mt-0.5">
            {cadence(sched)} · {sched.seasons.length ? `Seasons ${sched.seasons.join(", ")}` : "All seasons"}
            {" · "}last checked {fmtDate(sched.last_checked)}
          </p>
          {sched.last_result && <p className="text-xs text-gray-400 mt-0.5">{sched.last_result}</p>}
        </div>
        <div className="ml-auto flex gap-1.5 flex-shrink-0 flex-wrap">
          <button className="btn-secondary btn-sm" disabled={busy} onClick={onCheck}>Check now</button>
          <button className="btn-ghost btn-sm" onClick={loadStatus}>{open ? "Hide" : "Coverage"}</button>
          <button className="btn-ghost btn-sm" onClick={() => setRunsOpen((o) => !o)}>
            {runsOpen ? "Hide runs" : "Run history"}
          </button>
          <button className="btn-ghost btn-sm" onClick={onToggle}>{sched.is_active ? "Pause" : "Resume"}</button>
          <button className="btn-ghost btn-sm" onClick={onEdit}>Edit</button>
          <button className="btn-ghost btn-sm text-red-400" onClick={onDelete}>Delete</button>
        </div>
      </div>

      {open && (
        <div className="border-t border-gray-800 pt-2">
          {gaps == null ? (
            <p className="text-sm text-gray-500">Checking coverage…</p>
          ) : gaps.length === 0 ? (
            <p className="text-sm text-gray-500">No season data available.</p>
          ) : (
            <>
              <div className="flex items-center justify-between text-xs text-gray-400 mb-2">
                <span>Overall coverage</span>
                <span>{totalDone}/{totalEps} episodes</span>
              </div>
              <ProgressBar value={totalEps ? (totalDone / totalEps) * 100 : 0} color="green" />
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 mt-3">
                {gaps.map((g) => (
                  <div key={g.season} className="flex items-center justify-between text-sm">
                    <span className="text-gray-300">Season {g.season}</span>
                    <span className={g.missing.length ? "text-yellow-400" : "text-green-400"}>
                      {g.downloaded}/{g.total}
                      {g.missing.length > 0 && ` · ${g.missing.length} missing`}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {runsOpen && (
        <div className="border-t border-gray-800 pt-2">
          <RunHistoryPanel schedId={sched.id} />
        </div>
      )}
    </div>
  );
}

/** Run-attempt history — separate from Downloads history. Each run is one
    execution of check_schedule() (automatic tick or "Check now"); expanding
    a run shows every episode it considered with a clear completed/failed/
    downloading/queued status. */
function RunHistoryPanel({ schedId }: { schedId: number }) {
  const per = 10;
  const [runs, setRuns] = useState<ScheduleRun[] | null>(null);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<ScheduleRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    schedules.runs(schedId, page, per)
      .then((r) => { setRuns(r.items); setTotal(r.total); })
      .catch(() => { setRuns([]); setTotal(0); });
  }, [schedId, page]);

  const openRun = (runId: number) => {
    if (selected === runId) { setSelected(null); return; }
    setSelected(runId);
    setDetail(null);
    setDetailLoading(true);
    schedules.runDetail(schedId, runId)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setDetailLoading(false));
  };

  if (runs === null) return <p className="text-sm text-gray-500">Loading run history…</p>;
  if (runs.length === 0) {
    return (
      <p className="text-sm text-gray-500">
        No run attempts yet — click "Check now" above, or wait for this schedule's next tick.
      </p>
    );
  }

  const pages = Math.ceil(total / per);

  return (
    <div className="space-y-2">
      {runs.map((r) => (
        <div key={r.id} className="border border-gray-800 rounded-lg overflow-hidden">
          <button className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-800/40"
            onClick={() => openRun(r.id)}>
            <span className="text-gray-500 w-4">{selected === r.id ? "▾" : "▸"}</span>
            <span className="text-sm">{fmtDate(r.started_at)}</span>
            <span className="text-xs text-gray-500">· {r.checked} checked · {r.queued} queued</span>
            {r.status === "running" && <span className="badge-pending">Running…</span>}
            <span className="ml-auto flex gap-2 text-xs flex-shrink-0">
              {!!r.counts?.completed && <span className="text-emerald-400">✓ {r.counts.completed}</span>}
              {!!r.counts?.failed && <span className="text-red-400">✗ {r.counts.failed}</span>}
              {!!r.counts?.downloading && <span className="text-sky-400">↓ {r.counts.downloading}</span>}
              {!!r.counts?.queued && <span className="text-gray-400">… {r.counts.queued}</span>}
            </span>
          </button>
          {selected === r.id && (
            <div className="border-t border-gray-800 px-3 py-2 space-y-1.5">
              {r.summary && <p className="text-xs text-gray-500">{r.summary}</p>}
              {detailLoading ? (
                <p className="text-sm text-gray-500">Loading…</p>
              ) : !detail || detail.items.length === 0 ? (
                <p className="text-sm text-gray-500">No episodes were considered in this run.</p>
              ) : (
                <div className="space-y-1 max-h-72 overflow-y-auto">
                  {detail.items.map((it) => (
                    <div key={it.id} className="flex items-center gap-2 text-sm">
                      <RunItemStatusDot status={it.status} />
                      <span className="font-mono text-xs text-gray-500 w-16 flex-shrink-0">
                        {it.season != null
                          ? `S${String(it.season).padStart(2, "0")}${it.episode != null ? `E${String(it.episode).padStart(2, "0")}` : ""}`
                          : "—"}
                      </span>
                      <span className="truncate flex-1">
                        {it.episode_title || (it.episode == null ? "Season fetch" : "—")}
                      </span>
                      {it.error_message && (
                        <span className="text-xs text-red-400 truncate max-w-[40%]" title={it.error_message}>
                          {it.error_message}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ))}
      {pages > 1 && (
        <div className="flex items-center justify-end gap-2 text-xs text-gray-500 pt-1">
          <button className="btn-secondary btn-xs" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>‹ Prev</button>
          <span>Page {page + 1} / {pages}</span>
          <button className="btn-secondary btn-xs" disabled={page >= pages - 1} onClick={() => setPage((p) => p + 1)}>Next ›</button>
        </div>
      )}
    </div>
  );
}

const RUN_ITEM_STATUS_META: Record<RunItemStatus, { icon: string; cls: string; label: string }> = {
  completed:   { icon: "✓", cls: "text-emerald-400", label: "Completed" },
  failed:      { icon: "✗", cls: "text-red-400", label: "Failed" },
  downloading: { icon: "↓", cls: "text-sky-400", label: "Downloading" },
  queued:      { icon: "…", cls: "text-gray-400", label: "Queued" },
};

function RunItemStatusDot({ status }: { status: RunItemStatus }) {
  const m = RUN_ITEM_STATUS_META[status] ?? RUN_ITEM_STATUS_META.queued;
  return <span className={`${m.cls} w-4 text-center flex-shrink-0`} title={m.label}>{m.icon}</span>;
}
