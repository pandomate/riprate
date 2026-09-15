import { useEffect, useRef, useState } from "react";
import { admin, BulkAction, HistoryPage, HistoryStatus, Job, openvpn, OpenVpnStatus, Stats } from "../../api/client";
import { fmtBytes, fmtElapsed, Modal, ProgressBar, StatusBadge, useToast } from "../../components/ui";
import {
  IBox, IClose, IDownload, IFolder, IPause, IPlay, IRetry, ISignal, ITrash,
} from "../../components/icons";

const RUNNING = new Set(["downloading", "processing", "paused"]);
const PER = 10;
const emptyPage: HistoryPage = { items: [], total: 0, page: 0, per: PER };

export default function Downloads() {
  const toast = useToast();
  const [active, setActive] = useState<Job[]>([]);
  const [completed, setCompleted] = useState<HistoryPage>(emptyPage);
  const [failed, setFailed] = useState<HistoryPage>(emptyPage);
  const [cPage, setCPage] = useState(0);
  const [fPage, setFPage] = useState(0);
  const [qPage, setQPage] = useState(0);
  const [failedQ, setFailedQ] = useState("");
  const [stats, setStats] = useState<Stats | null>(null);
  const [openVpn, setOpenVpn] = useState<OpenVpnStatus | null>(null);
  const [rotatingVpn, setRotatingVpn] = useState(false);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [order, setOrder] = useState<number[]>([]);
  const [dragId, setDragId] = useState<number | null>(null);
  const [trashTarget, setTrashTarget] = useState<Job | null>(null);
  const [swapTarget, setSwapTarget] = useState<Job | null>(null);
  const pollRef = useRef<number | null>(null);

  // Reorder robustness: serialize reorder POSTs so out-of-order delivery can't
  // diverge, and track in-flight ones so applyActive knows when it's safe to
  // re-adopt the server's authoritative order (self-heals a failed/raced move).
  const pendingReorders = useRef(0);
  const reorderChain = useRef<Promise<unknown>>(Promise.resolve());
  const dragIdRef = useRef<number | null>(null);
  useEffect(() => { dragIdRef.current = dragId; }, [dragId]);

  const sendReorder = (ids: number[]) => {
    pendingReorders.current += 1;
    reorderChain.current = reorderChain.current
      .then(() => admin.reorder(ids))
      .catch(() => {})
      .finally(() => { pendingReorders.current -= 1; });
  };

  const applyActive = (a: Job[]) => {
    setActive(a); setLoading(false);
    const serverQueued = a.filter((j) => j.status === "queued").map((j) => j.id);
    setOrder((prev) => {
      // Mid-drag or with a reorder still settling on the server, keep the local
      // optimistic order; otherwise adopt the server order so any divergence
      // (a failed or raced reorder) heals within one tick instead of sticking.
      if (dragIdRef.current == null && pendingReorders.current === 0) return serverQueued;
      const kept = prev.filter((id) => serverQueued.includes(id));
      return [...kept, ...serverQueued.filter((id) => !kept.includes(id))];
    });
  };

  const loadHistory = async () => {
    try {
      const [comp, fail] = await Promise.all([
        admin.history("completed", cPage, PER),
        admin.history("failed", fPage, PER, failedQ),
      ]);
      setCompleted(comp); setFailed(fail);
    } catch { /* ignore */ }
  };
  const refreshStats = () => admin.stats().then(setStats).catch(() => {});
  const refreshOpenVpn = () => openvpn.status().then(setOpenVpn).catch(() => setOpenVpn(null));

  // Prune selection to jobs currently loaded (fixes ghost "N selected")
  useEffect(() => {
    const present = new Set([...active, ...completed.items, ...failed.items].map((j) => j.id));
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => present.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [active, completed, failed]);

  // Reset the failed page when the error filter changes
  useEffect(() => { setFPage(0); }, [failedQ]);

  // History load — NO periodic polling. It's re-fetched only when it can
  // actually change: on mount, when you switch page/filter, when a job leaves
  // the active queue (the effect below), and after any queue action (`after`).
  useEffect(() => {
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cPage, fPage, failedQ]);

  // A job leaving the active queue (completed/failed) is the only thing that
  // adds to history — refresh then, so history stays live without hard polling.
  const prevActiveLen = useRef(0);
  useEffect(() => {
    if (active.length < prevActiveLen.current) loadHistory();
    prevActiveLen.current = active.length;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active.length]);

  // Populate queue + stats once on mount (no stream held open while idle).
  useEffect(() => {
    admin.queue().then(applyActive).catch(() => setLoading(false));
    refreshStats();
    refreshOpenVpn();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Stream while downloading, while a queued job is about to start, or during a
  // network outage (the system auto-pauses then retries) so recovery shows live.
  const hasRunning = active.some((j) => j.status === "downloading" || j.status === "processing");
  const mayStart = !!stats && stats.queued > 0 && !stats.globally_paused
    && !stats.vpn_gated && stats.free_slots > 0;
  const netDown = !!stats && !stats.network_ok && active.length > 0;
  const streamOn = hasRunning || mayStart || netDown;
  useEffect(() => {
    if (!streamOn) { setLive(false); return; }
    const es = new EventSource("/api/admin/stream", { withCredentials: true });
    es.onmessage = (e) => {
      const d = JSON.parse(e.data) as { active: Job[]; stats?: Stats };
      applyActive(d.active); setLive(true);
      if (d.stats) setStats(d.stats);
    };
    es.onerror = () => {
      setLive(false); es.close();
      admin.queue().then(applyActive).catch(() => {});
      refreshStats();
      if (pollRef.current == null) pollRef.current = window.setInterval(() => {
        admin.queue().then(applyActive).catch(() => {});
        refreshStats();
      }, 8000);
    };
    return () => {
      es.close();
      if (pollRef.current != null) { clearInterval(pollRef.current); pollRef.current = null; }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streamOn]);

  // Auto-scroll while dragging near the viewport edges
  useEffect(() => {
    if (dragId == null) return;
    let y = 0;
    const onDragOver = (e: DragEvent) => { y = e.clientY; e.preventDefault(); };
    window.addEventListener("dragover", onDragOver);
    let raf = 0;
    const tick = () => {
      const m = 100, s = 18, h = window.innerHeight;
      if (y > 0 && y < m) window.scrollBy(0, -s * (1 - y / m));
      else if (y > h - m) window.scrollBy(0, s * (1 - (h - y) / m));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    // Clear the drag if it ends anywhere (the source row may have unmounted
    // during a cross-page flip, so its own onDragEnd won't fire).
    const onEnd = () => setDragId(null);
    window.addEventListener("dragend", onEnd);
    return () => {
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragend", onEnd);
      cancelAnimationFrame(raf);
    };
  }, [dragId]);

  const after = () => {
    if (!live) admin.queue().then(applyActive).catch(() => {});
    loadHistory(); refreshStats(); refreshOpenVpn();
  };
  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    try { await fn(); if (ok) toast(ok, "success"); }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Action failed", "error"); }
    after();
  };

  const toggleSel = (id: number) =>
    setSelected((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const setSel = (ids: number[], on: boolean) =>
    setSelected((prev) => { const n = new Set(prev); ids.forEach((id) => (on ? n.add(id) : n.delete(id))); return n; });
  const clearSel = () => setSelected(new Set());

  const bulk = async (action: BulkAction) => {
    const ids = [...selected];
    if (!ids.length) return;
    try { const res = await admin.bulk(action, ids); toast(`${action}: ${res.count} item(s)`, "success"); }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Bulk action failed", "error"); }
    clearSel(); after();
  };

  const toggleGlobalPause = async () => {
    try {
      if (stats?.globally_paused) { await admin.resumeAll(); toast("Queue resumed", "success"); }
      else { await admin.pauseAll(); toast("Queue paused", "success"); }
    } catch (err: unknown) { toast(err instanceof Error ? err.message : "Failed", "error"); }
    after();
  };

  const rotateVpn = async () => {
    setRotatingVpn(true);
    try {
      const response = await openvpn.rotate();
      setOpenVpn(response.status);
      toast(response.result.message || "Connected to a new VPN", "success");
      refreshStats();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Could not get a new VPN", "error");
      refreshOpenVpn();
    } finally {
      setRotatingVpn(false);
    }
  };

  const onDrop = (targetId: number) => {
    if (dragId == null || dragId === targetId) return;
    let next: number[] | null = null;
    setOrder((prev) => {
      const from = prev.indexOf(dragId), to = prev.indexOf(targetId);
      if (from < 0 || to < 0) return prev;
      const arr = [...prev]; arr.splice(from, 1); arr.splice(to, 0, dragId);
      next = arr;
      return arr;
    });
    setDragId(null);
    if (next) sendReorder(next);
  };

  // Move to top/bottom of the *waiting* queue (never into an active slot) —
  // optimistic local reorder + persist, so it's instant.
  const moveInOrder = (id: number, dir: "top" | "bottom") => {
    let next: number[] | null = null;
    setOrder((prev) => {
      const idx = prev.indexOf(id);
      if (idx < 0) return prev;
      const arr = [...prev];
      arr.splice(idx, 1);
      if (dir === "top") arr.unshift(id); else arr.push(id);
      next = arr;
      return arr;
    });
    if (next) sendReorder(next);
  };

  // Flip pages while a drag is in progress (so you can drop onto another page).
  const flipRef = useRef(0);
  const dragFlip = (dir: -1 | 1) => {
    if (dragId == null) return;
    const now = Date.now();
    if (now - flipRef.current < 550) return;
    flipRef.current = now;
    setQPage((p) => Math.max(0, p + dir));
  };

  const runNow = async (job: Job) => {
    let s = stats;
    try { s = await admin.stats(); setStats(s); } catch { /* keep cached */ }
    if ((s?.free_slots ?? 0) > 0) { await act(() => admin.promote(job.id), "Starting now"); return; }
    setSwapTarget(job);
  };

  const byId = new Map(active.map((j) => [j.id, j]));
  const running = active.filter((j) => RUNNING.has(j.status));
  const queued = order.map((id) => byId.get(id)).filter((j): j is Job => !!j && j.status === "queued");
  const activeIds = active.map((j) => j.id);
  const allSel = (ids: number[]) => ids.length > 0 && ids.every((id) => selected.has(id));

  // Paginate the active queue (running first, then queued) at PER per page
  const activeList = [...running, ...queued];
  const qPages = Math.max(1, Math.ceil(activeList.length / PER));
  const safeQPage = Math.min(qPage, qPages - 1);
  const pagedActive = activeList.slice(safeQPage * PER, safeQPage * PER + PER);
  useEffect(() => {
    if (qPage > qPages - 1) setQPage(qPages - 1);
  }, [qPages, qPage]);

  if (loading) return <p className="text-gray-500">Loading…</p>;

  return (
    <div className="space-y-6">
      <header className="flex items-center gap-3 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold">Downloads</h1>
          <p className="text-gray-500 text-sm mt-1">
            Queue and download activity{stats ? ` · ${stats.active_slots}/${stats.max_slots} slots busy` : ""}.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3">
          {active.length > 0 && (
            <button className={`inline-flex items-center gap-1.5 ${stats?.globally_paused ? "btn-primary btn-sm" : "btn-secondary btn-sm"}`} onClick={toggleGlobalPause}>
              {stats?.globally_paused ? <><IPlay size={14} /> Resume all</> : <><IPause size={14} /> Pause all</>}
            </button>
          )}
          {openVpn?.configured && (
            <button className="btn-secondary btn-sm inline-flex items-center gap-1.5" onClick={rotateVpn} disabled={rotatingVpn}>
              <ISignal size={14} /> {rotatingVpn ? "Getting VPN…" : "Get new VPN"}
            </button>
          )}
          <span className={`text-xs flex items-center gap-1.5 ${live ? "text-green-500" : "text-gray-500"}`}>
            <span className={`w-1.5 h-1.5 rounded-full inline-block ${live ? "bg-green-500 animate-pulse" : "bg-gray-600"}`} />
            {live ? "Live" : "Idle"}
          </span>
        </div>
      </header>

      {stats?.vpn_gated && (
        <div className="bg-amber-900/30 border border-amber-700/60 rounded-lg px-4 py-2 text-amber-300 text-sm">
          Downloads are paused — no active VPN. All traffic must route through the VPN;
          enable one in <a href="/settings" className="underline">Settings</a> to start downloading.
        </div>
      )}

      {stats && !stats.network_ok && (
        <div className={`${stats.vpn_active ? "bg-amber-900/30 border-amber-700/60 text-amber-300" : "bg-red-900/30 border-red-700/60 text-red-300"} border rounded-lg px-4 py-2 text-sm`}>
          {stats.vpn_active
            ? "VPN is connected, but download traffic is not progressing — downloads are paused and will retry automatically."
            : "No download network activity detected — downloads are paused and will retry automatically when connectivity returns."}
        </div>
      )}

      {stats?.globally_paused && (
        <div className="bg-orange-900/30 border border-orange-700/60 rounded-lg px-4 py-2 text-orange-300 text-sm">
          Queue is globally paused — no new downloads will start until you resume.
        </div>
      )}

      {stats && (
        <div className="card flex items-center gap-3 py-3">
          <span className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${stats.vpn_active ? "bg-green-500" : "bg-gray-500"}`} />
          <div className="min-w-0">
            <div className="text-xs text-gray-500 uppercase tracking-wide">
              Public IP {stats.vpn_active ? "· via VPN" : stats.public_ip ? "· no VPN" : ""}
            </div>
            <div className="text-lg font-mono font-semibold text-gray-100 truncate">
              {stats.public_ip || "Unknown"}
            </div>
          </div>
        </div>
      )}

      {stats && (
        <div className="grid grid-cols-3 gap-3">
          <StatCard label="Downloading" value={stats.downloading} accent="text-sky-400" />
          <StatCard label="Queued" value={stats.queued} accent="text-yellow-400" />
          <StatCard label="Failed" value={stats.failed} accent="text-red-400" />
        </div>
      )}

      {selected.size > 0 && (
        <div className="sticky top-2 z-10 flex items-center gap-2 flex-wrap card py-2 border-sky-800 bg-gray-900/95 backdrop-blur">
          <span className="text-sm font-medium">{selected.size} selected</span>
          <div className="flex gap-1.5 ml-auto flex-wrap">
            <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={() => bulk("retry")}><IRetry size={13} /> Retry</button>
            <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={() => bulk("pause")}><IPause size={13} /> Pause</button>
            <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={() => bulk("resume")}><IPlay size={13} /> Resume</button>
            <button className="btn-danger btn-sm inline-flex items-center gap-1" onClick={() => bulk("cancel")}><IClose size={13} /> Cancel</button>
            <button className="btn-danger btn-sm inline-flex items-center gap-1" onClick={() => bulk("delete")}><ITrash size={13} /> Remove</button>
            <button className="btn-ghost btn-sm" onClick={clearSel}>Clear</button>
          </div>
        </div>
      )}

      <section>
        <div className="flex items-center gap-3 mb-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">
            Active queue{active.length > 0 ? ` (${active.length})` : ""}
          </h2>
          {active.length > 0 && (
            <button className="btn-ghost btn-xs text-gray-400" onClick={() => setSel(activeIds, !allSel(activeIds))}>
              {allSel(activeIds) ? "Unselect all" : "Select all"}
            </button>
          )}
          {qPages > 1 && (
            <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
              <button className="btn-secondary btn-xs" disabled={safeQPage === 0}
                onClick={() => setQPage(safeQPage - 1)}
                onDragOver={(e) => { if (dragId != null) { e.preventDefault(); dragFlip(-1); } }}>‹ Prev</button>
              <span>Page {safeQPage + 1} / {qPages}</span>
              <button className="btn-secondary btn-xs" disabled={safeQPage >= qPages - 1}
                onClick={() => setQPage(safeQPage + 1)}
                onDragOver={(e) => { if (dragId != null) { e.preventDefault(); dragFlip(1); } }}>Next ›</button>
            </div>
          )}
          {dragId != null && qPages > 1 && (
            <span className="text-xs text-sky-400">↔ hover Prev/Next to move across pages</span>
          )}
        </div>
        {active.length === 0 ? (
          <div className="card text-gray-500 text-sm">Nothing in the queue.</div>
        ) : (
          <div className="space-y-2">
            {pagedActive.map((job) => {
              if (RUNNING.has(job.status)) {
                return (
                  <JobRow key={job.id} job={job} selected={selected.has(job.id)} onSelect={() => toggleSel(job.id)}
                    draggable={false} onPause={() => act(() => admin.pause(job.id))}
                    onResume={() => act(() => admin.resume(job.id))}
                    onNextSource={() => act(() => admin.skipSource(job.id), "Skipping to the next source…")}
                    onTrash={() => setTrashTarget(job)} />
                );
              }
              const qi = queued.findIndex((q) => q.id === job.id);
              return (
                <JobRow key={job.id} job={job} selected={selected.has(job.id)} onSelect={() => toggleSel(job.id)}
                  draggable isFirst={qi === 0} isLast={qi === queued.length - 1} dragging={dragId === job.id}
                  onDragStart={() => setDragId(job.id)} onDragEnd={() => setDragId(null)} onDropRow={() => onDrop(job.id)}
                  onMove={(dir) => moveInOrder(job.id, dir)} onRunNow={() => runNow(job)}
                  onTrash={() => setTrashTarget(job)} />
              );
            })}
          </div>
        )}
      </section>

      <HistorySection title="Completed" page={completed} pageIndex={cPage} onPage={setCPage}
        selected={selected} setSel={setSel} allSel={allSel}
        onRetry={() => {}} onRetrySubtitles={(id) => act(() => admin.retrySubtitles(id), "Subtitles downloaded")}
        onTrash={setTrashTarget} toggleSel={toggleSel} />
      <HistorySection title="Failed" page={failed} pageIndex={fPage} onPage={setFPage}
        selected={selected} setSel={setSel} allSel={allSel}
        onRetry={(id) => act(() => admin.retry(id), "Re-queued")} onTrash={setTrashTarget} toggleSel={toggleSel}
        filter={failedQ} onFilter={setFailedQ} filterPlaceholder="Filter by error or title…"
        onRetryAll={async () => {
          try { const r = await admin.retryFailed(failedQ); toast(`Re-queued ${r.count} download(s)`, "success"); }
          catch (err: unknown) { toast(err instanceof Error ? err.message : "Retry failed", "error"); }
          after();
        }} />

      {trashTarget && (
        <TrashModal job={trashTarget} onClose={() => setTrashTarget(null)} onDone={() => { setTrashTarget(null); after(); }} />
      )}
      {swapTarget && (
        <SwapModal job={swapTarget} running={running} onClose={() => setSwapTarget(null)} onDone={() => { setSwapTarget(null); after(); }} />
      )}
    </div>
  );
}

function HistorySection({
  title, page, pageIndex, onPage, selected, setSel, allSel, onRetry, onRetrySubtitles, onTrash, toggleSel,
  filter, onFilter, filterPlaceholder, onRetryAll,
}: {
  title: string; page: HistoryPage; pageIndex: number; onPage: (p: number) => void;
  selected: Set<number>; setSel: (ids: number[], on: boolean) => void; allSel: (ids: number[]) => boolean;
  onRetry: (id: number) => void; onRetrySubtitles?: (id: number) => void;
  onTrash: (j: Job) => void; toggleSel: (id: number) => void;
  filter?: string; onFilter?: (v: string) => void; filterPlaceholder?: string; onRetryAll?: () => void;
}) {
  const pages = Math.max(1, Math.ceil(page.total / page.per));
  // Keep the page index in range (e.g. after retrying-all shrinks the list)
  useEffect(() => {
    if (pageIndex > pages - 1) onPage(pages - 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pages, pageIndex]);

  if (page.total === 0 && !filter) return null;
  const ids = page.items.map((j) => j.id);

  return (
    <section>
      <div className="flex items-center gap-3 mb-3 flex-wrap">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">{title} ({page.total})</h2>
        {onFilter && (
          <input className="input py-1 h-8 text-sm max-w-xs" placeholder={filterPlaceholder}
            value={filter} onChange={(e) => onFilter(e.target.value)} />
        )}
        {page.total > 0 && (
          <button className="btn-ghost btn-xs text-gray-400" onClick={() => setSel(ids, !allSel(ids))}>
            {allSel(ids) ? "Unselect all" : "Select all"}
          </button>
        )}
        {onRetryAll && page.total > 0 && (
          <button className="btn-secondary btn-xs inline-flex items-center gap-1" onClick={onRetryAll}>
            <IRetry size={12} /> Retry all {filter ? "matching" : ""} ({page.total})
          </button>
        )}
        {pages > 1 && (
          <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
            <button className="btn-secondary btn-xs" disabled={pageIndex === 0} onClick={() => onPage(pageIndex - 1)}>‹ Prev</button>
            <span>Page {pageIndex + 1} / {pages}</span>
            <button className="btn-secondary btn-xs" disabled={pageIndex >= pages - 1} onClick={() => onPage(pageIndex + 1)}>Next ›</button>
          </div>
        )}
      </div>
      {page.items.length === 0 ? (
        <p className="text-gray-600 text-sm">No matches.</p>
      ) : (
        <div className="space-y-2">
          {page.items.map((job) => (
            <HistoryRow key={job.id} job={job} selected={selected.has(job.id)} onSelect={() => toggleSel(job.id)}
              onRetry={() => onRetry(job.id)}
              onRetrySubtitles={onRetrySubtitles ? () => onRetrySubtitles(job.id) : undefined}
              onTrash={() => onTrash(job)} />
          ))}
        </div>
      )}
    </section>
  );
}

function StatCard({ label, value, accent }: { label: string; value: number; accent: string }) {
  return (
    <div className="card py-3">
      <div className={`text-2xl font-bold ${accent}`}>{value}</div>
      <div className="text-xs text-gray-500 uppercase tracking-wide mt-0.5">{label}</div>
    </div>
  );
}

function phaseColor(job: Job): string {
  if (job.status === "processing") return "purple";
  if (job.status === "paused") return "orange";
  return "sky";
}

function Check({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <input type="checkbox" checked={on} onChange={onClick}
      className="w-4 h-4 rounded accent-sky-500 flex-shrink-0 mt-0.5" onClick={(e) => e.stopPropagation()} />
  );
}

function JobRow({
  job, selected, onSelect, draggable, isFirst, isLast, dragging,
  onDragStart, onDragEnd, onDropRow, onMove, onPause, onResume, onRunNow, onNextSource, onTrash,
}: {
  job: Job; selected: boolean; onSelect: () => void; draggable: boolean;
  isFirst?: boolean; isLast?: boolean; dragging?: boolean;
  onDragStart?: () => void; onDragEnd?: () => void; onDropRow?: () => void;
  onMove?: (dir: "top" | "bottom") => void;
  onPause?: () => void; onResume?: () => void; onRunNow?: () => void;
  onNextSource?: () => void; onTrash: () => void;
}) {
  const [showLog, setShowLog] = useState(false);
  const running = RUNNING.has(job.status);
  const isDownloading = job.status === "downloading";
  const isProcessing = job.status === "processing";
  const resolving = isDownloading && !job.source_url;
  const phaseLabel = job.status === "paused" ? "Paused"
    : isProcessing ? "Processing (encoding to MP4)"
    : resolving ? (job.current_source ? `Trying ${job.current_source}…` : "Finding a stream source…")
    : isDownloading ? "Downloading stream" : "Waiting in queue";
  const hasLog = (job.log?.length ?? 0) > 0;

  return (
    <div className={`card flex gap-2 ${dragging ? "opacity-50 border-sky-500" : ""}`}
      draggable={draggable} onDragStart={onDragStart} onDragEnd={onDragEnd}
      onDragOver={draggable ? (e) => e.preventDefault() : undefined} onDrop={draggable ? onDropRow : undefined}>
      <div className="flex-1 min-w-0 space-y-2">
        <div className="flex items-start gap-2 flex-wrap">
          <Check on={selected} onClick={onSelect} />
          <span className="font-medium text-sm">{job.label}</span>
          <StatusBadge status={job.status} />
          {job.requested_by && <span className="text-xs text-gray-500">· {job.requested_by}</span>}
          {running && job.started_at && <span className="text-xs text-gray-500 ml-auto">{fmtElapsed(job.started_at)}</span>}
        </div>
        {running && (
          <div>
            <div className="flex justify-between text-xs text-gray-400 mb-1"><span>{phaseLabel}</span><span>{job.progress.toFixed(0)}%</span></div>
            <ProgressBar value={job.progress} color={phaseColor(job)} />
          </div>
        )}
        {running && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
            {isDownloading && job.total_bytes ? <span className="inline-flex items-center gap-1"><IDownload size={12} /> {fmtBytes(job.downloaded_bytes)} / ~{fmtBytes(job.total_bytes)}</span>
              : job.downloaded_bytes ? <span className="inline-flex items-center gap-1">{isProcessing ? <IBox size={12} /> : <IDownload size={12} />} {fmtBytes(job.downloaded_bytes)}</span> : null}
            {job.download_bitrate && <span>{job.download_bitrate}</span>}
            {job.current_source && job.source_url && <span className="inline-flex items-center gap-1"><ISignal size={12} /> {job.current_source}</span>}
            {job.source_url && <span className="truncate max-w-xs" title={job.source_url}>{safeHost(job.source_url)}</span>}
            {hasLog && <button className="text-gray-400 hover:text-sky-400 ml-auto" onClick={() => setShowLog((s) => !s)}>{showLog ? "▾ Hide details" : "▸ Details"}</button>}
          </div>
        )}
        {showLog && hasLog && (
          <pre className="text-[11px] leading-relaxed text-gray-400 bg-gray-950 rounded-md p-2.5 max-h-44 overflow-y-auto font-mono whitespace-pre-wrap">{job.log!.join("\n")}</pre>
        )}
        <div className="flex gap-2 flex-wrap pt-0.5">
          {job.status === "queued" && onRunNow && <button className="btn-primary btn-sm inline-flex items-center gap-1" onClick={onRunNow} title="Start this download now"><IPlay size={14} /> Download now</button>}
          {job.status === "queued" && onMove && (
            <>
              <button className="btn-secondary btn-xs" disabled={isFirst} onClick={() => onMove("top")} title="Move to top">Top</button>
              <button className="btn-secondary btn-xs" disabled={isLast} onClick={() => onMove("bottom")} title="Move to bottom">Bottom</button>
            </>
          )}
          {(isDownloading || isProcessing) && onPause && <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={onPause}><IPause size={14} /> Pause</button>}
          {isDownloading && onNextSource && <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={onNextSource} title="Skip this source and try the next provider for this download only"><ISignal size={14} /> Next source</button>}
          {job.status === "paused" && onResume && <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={onResume}><IPlay size={14} /> Resume</button>}
          <button className="btn-ghost btn-sm text-red-400 inline-flex items-center gap-1" onClick={onTrash} title="Remove / delete"><ITrash size={14} /> Trash</button>
        </div>
      </div>
      {draggable && (
        <div className="flex items-center text-gray-600 hover:text-gray-300 cursor-grab active:cursor-grabbing pl-1 select-none" title="Drag to reorder">⠿</div>
      )}
    </div>
  );
}

function HistoryRow({ job, selected, onSelect, onRetry, onRetrySubtitles, onTrash }: {
  job: Job; selected: boolean; onSelect: () => void; onRetry: () => void;
  onRetrySubtitles?: () => void; onTrash: () => void;
}) {
  return (
    <div className="card py-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <Check on={selected} onClick={onSelect} />
        <span className="font-medium text-sm">{job.label}</span>
        <StatusBadge status={job.status} />
          {job.status === "completed" && job.total_bytes && <span className="text-xs text-gray-500">{fmtBytes(job.total_bytes)}</span>}
          <div className="ml-auto flex gap-2">
            {job.status === "completed" && !job.has_subtitle && onRetrySubtitles &&
              <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={onRetrySubtitles}>
                <IRetry size={13} /> Retry subtitles
              </button>}
            {job.status === "completed" && job.has_subtitle &&
              <span className="text-xs text-green-500 self-center">Subtitles available</span>}
            {job.status === "failed" && <button className="btn-secondary btn-sm inline-flex items-center gap-1" onClick={onRetry}><IRetry size={13} /> Retry</button>}
          <button className="btn-ghost btn-sm text-red-400 inline-flex items-center gap-1" onClick={onTrash} title="Remove / delete"><ITrash size={13} /> Trash</button>
        </div>
      </div>
      {job.status === "failed" && job.error_message && (
        <details className="mt-1">
          <summary className="text-xs text-red-400 cursor-pointer">Error details</summary>
          <pre className="text-xs text-red-300 mt-1 bg-gray-950 rounded p-2 overflow-x-auto whitespace-pre-wrap">{job.error_message}</pre>
        </details>
      )}
    </div>
  );
}

function TrashModal({ job, onClose, onDone }: { job: Job; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const isCompleted = job.status === "completed";
  const run = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true);
    try { await fn(); toast(msg, "success"); onDone(); }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Failed", "error"); setBusy(false); }
  };
  return (
    <Modal title={`Trash "${job.label}"`} onClose={onClose}
      footer={<button className="btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>}>
      <p className="text-sm text-gray-400">Choose what to do with this item.</p>
      <div className="space-y-2">
        {isCompleted ? (
          <button className="btn-secondary w-full justify-start inline-flex items-center gap-1.5" disabled={busy}
            onClick={() => run(() => admin.delete(job.id), "Removed from history — file kept")}>
            <IFolder size={14} /> Remove from history <span className="text-gray-500 ml-1">— keeps the file in your Library</span>
          </button>
        ) : (
          <button className="btn-secondary w-full justify-start inline-flex items-center gap-1.5" disabled={busy}
            onClick={() => run(() => admin.fail(job.id), "Marked as failed")}>
            <IClose size={14} /> Mark as failed <span className="text-gray-500 ml-1">— stops it, keeps a retryable entry</span>
          </button>
        )}
        <button className="btn-danger w-full justify-start inline-flex items-center gap-1.5" disabled={busy}
          onClick={() => run(() => admin.purge(job.id), "Permanently deleted")}>
          <ITrash size={14} /> Permanently delete <span className="text-red-200/70 ml-1">— wipes it and its files completely</span>
        </button>
      </div>
    </Modal>
  );
}

function SwapModal({ job, running, onClose, onDone }: {
  job: Job; running: Job[]; onClose: () => void; onDone: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  // Only actively-downloading jobs can yield their slot promptly. A processing
  // job is mid-encode (ffmpeg) and can't be interrupted without throwing the
  // encode away, so it is never offered as a swap target.
  const swappable = running.filter((j) => j.status === "downloading" || j.status === "paused");
  const doSwap = async (activeId: number) => {
    setBusy(true);
    try {
      await admin.swap(job.id, activeId);
      toast("Swapping — your download starts in a few seconds", "success");
      onDone();
    }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Swap failed", "error"); setBusy(false); }
  };
  return (
    <Modal title="All slots are busy" onClose={onClose}
      footer={<button className="btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>}>
      <p className="text-sm text-gray-400">
        Pick a running download to send back to the queue (its progress is kept) so
        <span className="text-gray-200"> {job.label}</span> can take its place. The swap
        takes a few seconds — the running download finishes its current chunk first.
      </p>
      <div className="space-y-2">
        {swappable.map((j) => (
          <button key={j.id} className="btn-secondary w-full justify-between" disabled={busy} onClick={() => doSwap(j.id)}>
            <span className="truncate">{j.label}</span>
            <span className="text-xs text-gray-500 ml-2">
              {j.status === "paused" ? "paused · " : ""}{j.progress.toFixed(0)}%
            </span>
          </button>
        ))}
        {swappable.length === 0 && (
          <p className="text-sm text-gray-500">
            No swappable downloads right now — the busy slots are finishing up (encoding).
          </p>
        )}
      </div>
    </Modal>
  );
}

function safeHost(url: string): string {
  try { return new URL(url).hostname; } catch { return "source"; }
}
