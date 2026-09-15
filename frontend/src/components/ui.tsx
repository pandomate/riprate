import { createContext, ReactNode, useCallback, useContext, useState } from "react";

// ── Toasts ──────────────────────────────────────────────────────────────────

type ToastKind = "success" | "error" | "info";
interface Toast { id: number; kind: ToastKind; msg: string }

const ToastCtx = createContext<(msg: string, kind?: ToastKind) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((msg: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, msg }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-6 right-6 z-[100] flex flex-col gap-2 max-w-sm">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`px-4 py-3 rounded-lg shadow-lg text-sm border animate-in ${
              t.kind === "success" ? "bg-green-900/90 border-green-700 text-green-100"
              : t.kind === "error" ? "bg-red-900/90 border-red-700 text-red-100"
              : "bg-gray-800/95 border-gray-700 text-gray-100"
            }`}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ── Status badge ────────────────────────────────────────────────────────────

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  downloading: "Downloading",
  processing: "Processing",
  paused: "Paused",
  completed: "Completed",
  failed: "Failed",
  pending: "Pending",
  approved: "Approved",
  denied: "Denied",
};

const KNOWN_STATUS = new Set(Object.keys(STATUS_LABEL));

export function StatusBadge({ status }: { status: string }) {
  const cls = KNOWN_STATUS.has(status) ? `badge-${status}` : "badge-neutral";
  return <span className={cls}>{STATUS_LABEL[status] ?? status}</span>;
}

// ── Progress bar ────────────────────────────────────────────────────────────

export function ProgressBar({ value, color = "sky" }: { value: number; color?: string }) {
  const colorCls =
    color === "purple" ? "bg-purple-500"
    : color === "orange" ? "bg-orange-500"
    : color === "green" ? "bg-green-500"
    : "bg-sky-500";
  return (
    <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
      <div
        className={`h-full rounded-full transition-all duration-500 ${colorCls}`}
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  );
}

// ── Modal ───────────────────────────────────────────────────────────────────

export function Modal({
  title, children, onClose, footer,
}: {
  title: string; children: ReactNode; onClose: () => void; footer?: ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-[90] bg-black/60 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="card w-full max-w-md shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold mb-4">{title}</h3>
        <div className="space-y-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 mt-6">{footer}</div>}
      </div>
    </div>
  );
}

// ── Utility formatters ──────────────────────────────────────────────────────

export function fmtBytes(bytes: number | null | undefined): string {
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function fmtElapsed(startedAt: string | null): string {
  if (!startedAt) return "";
  const secs = Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000);
  if (secs < 0) return "";
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}
