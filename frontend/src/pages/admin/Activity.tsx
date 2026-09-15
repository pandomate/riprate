import { useEffect, useState } from "react";
import { admin, AuditEntry } from "../../api/client";
import { fmtDate } from "../../components/ui";

const ACTION_STYLE: Record<string, string> = {
  "login.success": "text-green-400",
  "login.failed": "text-yellow-400",
  "login.lockout": "text-red-400",
  "login.locked": "text-red-400",
  "login.rate_limited": "text-red-400",
  "login.disabled": "text-red-400",
  "logout": "text-gray-400",
  "password.changed": "text-sky-400",
  "password.change_failed": "text-yellow-400",
  "user.created": "text-green-400",
  "user.deleted": "text-red-400",
  "user.updated": "text-sky-400",
  "user.password_reset": "text-sky-400",
  "request.created": "text-sky-400",
  "request.approved": "text-green-400",
  "request.denied": "text-yellow-400",
  "request.cancelled": "text-gray-400",
  "library.file_deleted": "text-red-400",
  "library.season_deleted": "text-red-400",
  "library.show_deleted": "text-red-400",
  "library.renamed": "text-sky-400",
  "library.show_renamed": "text-sky-400",
  "schedule.created": "text-green-400",
  "settings.updated": "text-sky-400",
  "job.failed": "text-yellow-400",
};

const PER_OPTIONS = [10, 50, 100];

export default function Activity() {
  const [log, setLog] = useState<AuditEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [per, setPer] = useState(50);
  const [loading, setLoading] = useState(true);

  const load = () => {
    admin.audit(page, per).then((a) => {
      setLog(a.items); setTotal(a.total); setLoading(false);
    }).catch(() => setLoading(false));
  };
  useEffect(() => {
    load();
    const id = setInterval(load, 8000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, per]);

  const pages = Math.ceil(total / per);

  if (loading) return <p className="text-gray-500">Loading…</p>;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Activity</h1>
        <p className="text-gray-500 text-sm mt-1">Security audit log.</p>
      </header>

      <section>
        <div className="flex items-center gap-3 mb-3 flex-wrap">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">
            Audit log ({total})
          </h2>
          <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
            <span>Per page</span>
            <select className="input py-1 w-20" value={per}
              onChange={(e) => { setPer(Number(e.target.value)); setPage(0); }}>
              {PER_OPTIONS.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            {pages > 1 && (
              <>
                <button className="btn-secondary btn-xs" disabled={page === 0} onClick={() => setPage(page - 1)}>‹ Prev</button>
                <span>Page {page + 1} / {pages}</span>
                <button className="btn-secondary btn-xs" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next ›</button>
              </>
            )}
          </div>
        </div>
        <div className="card p-0 overflow-hidden">
          <table className="w-full">
            <thead className="border-b border-gray-800">
              <tr>
                <th className="th">When</th>
                <th className="th">User</th>
                <th className="th">Action</th>
                <th className="th">Detail</th>
                <th className="th">IP</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/60">
              {log.map((e, i) => (
                <tr key={i}>
                  <td className="td text-gray-500 whitespace-nowrap">{fmtDate(e.ts)}</td>
                  <td className="td">{e.username ?? "—"}</td>
                  <td className={`td font-mono text-xs ${ACTION_STYLE[e.action] ?? "text-gray-300"}`}>{e.action}</td>
                  <td className="td text-gray-400 max-w-xs truncate" title={e.detail ?? ""}>{e.detail ?? "—"}</td>
                  <td className="td text-gray-600 font-mono text-xs">{e.ip ?? "—"}</td>
                </tr>
              ))}
              {log.length === 0 && (
                <tr><td className="td text-gray-500" colSpan={5}>No activity yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
