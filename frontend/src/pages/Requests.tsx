import { useEffect, useState } from "react";
import { useAuth } from "../App";
import { MediaRequest, requests } from "../api/client";
import { fmtDate, Modal, ProgressBar, StatusBadge, useToast } from "../components/ui";

export default function Requests() {
  const user = useAuth()!;
  return user.role === "admin" ? <AdminRequests /> : <MyRequests />;
}

function seasonsLabel(r: MediaRequest): string {
  if (r.media_type === "movie") return "Movie";
  if (!r.seasons.length) return "TV";
  return r.seasons.length === 1 ? `Season ${r.seasons[0]}` : `Seasons ${r.seasons.join(", ")}`;
}

function QueueProgress({ r }: { r: MediaRequest }) {
  if (!r.queue || r.status !== "approved") return null;
  const q = r.queue;
  const color = q.processing > 0 ? "purple" : "sky";
  return (
    <div className="mt-2">
      <div className="flex justify-between text-xs text-gray-500 mb-1">
        <span>
          {q.completed === q.total_jobs ? "All downloads complete"
            : q.processing > 0 ? "Processing…"
            : q.downloading > 0 ? "Downloading…"
            : q.in_queue ? "Waiting in queue" : "Done"}
        </span>
        <span>{q.completed}/{q.total_jobs}</span>
      </div>
      <ProgressBar value={q.progress} color={color} />
    </div>
  );
}

function DecisionInfo({ r }: { r: MediaRequest }) {
  if (r.status === "approved") {
    return (
      <p className="text-xs text-gray-500 mt-1">
        Approved{r.decided_by ? ` by ${r.decided_by}` : ""}{r.decided_at ? ` · ${fmtDate(r.decided_at)}` : ""}
      </p>
    );
  }
  if (r.status === "denied") {
    return (
      <div className="mt-1">
        <p className="text-xs text-red-300/80">
          Denied{r.decided_by ? ` by ${r.decided_by}` : ""}{r.decided_at ? ` · ${fmtDate(r.decided_at)}` : ""}
        </p>
        {r.deny_reason && <p className="text-xs text-gray-400 mt-0.5">{r.deny_reason}</p>}
      </div>
    );
  }
  return null;
}

function cardTone(r: MediaRequest): string {
  if (r.status === "denied") return "border-red-800/60 bg-red-950/20";
  if (r.status === "approved") return "border-green-800/40 bg-green-950/10";
  return "";
}

function RequestCard({ r, forAdmin, actions }: {
  r: MediaRequest; forAdmin: boolean; actions?: React.ReactNode;
}) {
  return (
    <div className={`card ${cardTone(r)}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold">{r.title}</span>
            {r.year && <span className="text-gray-500 text-sm">({r.year})</span>}
            <StatusBadge status={r.status} />
            <span className="badge-neutral">{seasonsLabel(r)}</span>
            {forAdmin && r.requested_by && <span className="text-xs text-gray-500">from {r.requested_by}</span>}
          </div>
          {r.status === "pending"
            ? <p className="text-xs text-gray-500 mt-1">Requested {fmtDate(r.created_at)}</p>
            : <DecisionInfo r={r} />}
          <QueueProgress r={r} />
        </div>
        {actions && <div className="flex gap-2 flex-shrink-0">{actions}</div>}
      </div>
    </div>
  );
}

// ── User view ────────────────────────────────────────────────────────────────

function MyRequests() {
  const toast = useToast();
  const [reqs, setReqs] = useState<MediaRequest[]>([]);
  const [loading, setLoading] = useState(true);

  const load = () => requests.mine()
    .then((r) => { setReqs(r); })
    .catch(() => {})
    .finally(() => setLoading(false));
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const cancel = async (r: MediaRequest) => {
    try { await requests.cancel(r.id); toast("Request cancelled", "success"); load(); }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Could not cancel", "error"); }
  };

  if (loading) return <p className="text-gray-500">Loading…</p>;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">My Requests</h1>
        <p className="text-gray-500 text-sm mt-1">Track the titles you've requested and their download status.</p>
      </header>

      {reqs.length === 0 ? (
        <div className="card text-center text-gray-500 py-10">
          You haven't requested anything yet. Head to Search to find something.
        </div>
      ) : (
        <div className="space-y-3">
          {reqs.map((r) => (
            <RequestCard key={r.id} r={r} forAdmin={false}
              actions={r.status === "pending"
                ? <button className="btn-secondary btn-sm" onClick={() => cancel(r)}>Cancel</button>
                : undefined} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Admin view ───────────────────────────────────────────────────────────────

function AdminRequests() {
  const toast = useToast();
  const [pending, setPending] = useState<MediaRequest[]>([]);
  const [decided, setDecided] = useState<MediaRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [denyTarget, setDenyTarget] = useState<MediaRequest | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = () =>
    requests.all()
      .then((r) => { setPending(r.pending); setDecided(r.decided); })
      .catch(() => {})
      .finally(() => setLoading(false));
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const approve = async (r: MediaRequest) => {
    setBusy(r.id);
    try {
      const res = await requests.approve(r.id);
      toast(`Approved — queued ${res.queued} download(s)`, "success");
      if (res.errors?.length) toast(res.errors.join("; "), "error");
      load();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Approve failed", "error");
    } finally { setBusy(null); }
  };

  if (loading) return <p className="text-gray-500">Loading…</p>;

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-bold">Requests</h1>
        <p className="text-gray-500 text-sm mt-1">Approve to queue the download, or deny with a reason.</p>
      </header>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500 mb-3">
          Pending ({pending.length})
        </h2>
        {pending.length === 0 ? (
          <div className="card text-gray-500 text-sm">No pending requests.</div>
        ) : (
          <div className="space-y-3">
            {pending.map((r) => (
              <RequestCard key={r.id} r={r} forAdmin
                actions={<>
                  <button className="btn-primary btn-sm" disabled={busy === r.id} onClick={() => approve(r)}>
                    {busy === r.id ? "…" : "Approve"}
                  </button>
                  <button className="btn-danger btn-sm" onClick={() => setDenyTarget(r)}>Deny</button>
                </>} />
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500 mb-3">Decided</h2>
        {decided.length === 0 ? (
          <div className="card text-gray-500 text-sm">Nothing decided yet.</div>
        ) : (
          <div className="space-y-3">
            {decided.map((r) => <RequestCard key={r.id} r={r} forAdmin />)}
          </div>
        )}
      </section>

      {denyTarget && (
        <DenyModal request={denyTarget} onClose={() => setDenyTarget(null)}
          onDone={() => { setDenyTarget(null); load(); }} />
      )}
    </div>
  );
}

function DenyModal({ request, onClose, onDone }: {
  request: MediaRequest; onClose: () => void; onDone: () => void;
}) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try { await requests.deny(request.id, reason); toast("Request denied", "success"); onDone(); }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Deny failed", "error"); setBusy(false); }
  };

  return (
    <Modal title={`Deny "${request.title}"`} onClose={onClose}
      footer={<>
        <button className="btn-secondary" onClick={onClose}>Cancel</button>
        <button className="btn-danger" disabled={busy} onClick={submit}>Deny request</button>
      </>}>
      <div>
        <label className="label">Reason (shown to the requester)</label>
        <textarea className="input min-h-[90px]" placeholder="e.g. Not available in acceptable quality"
          value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      </div>
    </Modal>
  );
}
