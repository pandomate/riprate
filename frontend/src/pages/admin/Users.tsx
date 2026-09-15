import { useEffect, useState } from "react";
import { useAuth } from "../../App";
import { ManagedUser, Role, users } from "../../api/client";
import { fmtDate, Modal, useToast } from "../../components/ui";

export default function Users() {
  const me = useAuth()!;
  const toast = useToast();
  const [list, setList] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [resetTarget, setResetTarget] = useState<ManagedUser | null>(null);

  const load = () => users.list()
    .then((l) => { setList(l); })
    .catch(() => {})
    .finally(() => setLoading(false));
  useEffect(() => { load(); }, []);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try { await fn(); toast(ok, "success"); load(); }
    catch (err: unknown) { toast(err instanceof Error ? err.message : "Action failed", "error"); }
  };

  if (loading) return <p className="text-gray-500">Loading…</p>;

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Users</h1>
          <p className="text-gray-500 text-sm mt-1">Create and manage accounts.</p>
        </div>
        <button className="btn-primary" onClick={() => setCreating(true)}>+ New user</button>
      </header>

      <div className="card p-0 overflow-hidden">
        <table className="w-full">
          <thead className="border-b border-gray-800">
            <tr>
              <th className="th">Username</th>
              <th className="th">Role</th>
              <th className="th">Status</th>
              <th className="th">Last login</th>
              <th className="th text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {list.map((u) => {
              const self = u.username === me.username;
              return (
                <tr key={u.id}>
                  <td className="td font-medium">
                    {u.username}
                    {self && <span className="text-xs text-gray-500 ml-1">(you)</span>}
                  </td>
                  <td className="td">
                    <span className={u.role === "admin" ? "badge-approved" : "badge-neutral"}>
                      {u.role}
                    </span>
                  </td>
                  <td className="td">
                    {!u.is_active ? <span className="badge-failed">disabled</span>
                      : u.locked ? <span className="badge-paused">locked</span>
                      : <span className="badge-completed">active</span>}
                  </td>
                  <td className="td text-gray-500">{fmtDate(u.last_login_at)}</td>
                  <td className="td">
                    <div className="flex gap-1 justify-end">
                      {!self && (
                        <>
                          <button
                            className="btn-ghost btn-xs"
                            onClick={() => act(
                              () => users.update(u.id, { role: u.role === "admin" ? "user" : "admin" }),
                              u.role === "admin" ? "Demoted to user" : "Promoted to admin",
                            )}
                          >
                            {u.role === "admin" ? "Make user" : "Make admin"}
                          </button>
                          <button
                            className="btn-ghost btn-xs"
                            onClick={() => act(
                              () => users.update(u.id, { is_active: !u.is_active }),
                              u.is_active ? "Account disabled" : "Account enabled",
                            )}
                          >
                            {u.is_active ? "Disable" : "Enable"}
                          </button>
                          <button className="btn-ghost btn-xs" onClick={() => setResetTarget(u)}>
                            Reset pw
                          </button>
                          <button
                            className="btn-ghost btn-xs text-red-400"
                            onClick={() => confirm(`Delete user "${u.username}"?`) &&
                              act(() => users.remove(u.id), "User deleted")}
                          >
                            Delete
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {creating && (
        <CreateUserModal onClose={() => setCreating(false)} onDone={() => { setCreating(false); load(); }} />
      )}
      {resetTarget && (
        <ResetPasswordModal user={resetTarget} onClose={() => setResetTarget(null)} />
      )}
    </div>
  );
}

function CreateUserModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("user");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await users.create({ username, password, role });
      toast(`Created ${username}`, "success");
      onDone();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Create failed", "error");
      setBusy(false);
    }
  };

  return (
    <Modal
      title="New user"
      onClose={onClose}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy} onClick={submit}>Create</button>
        </>
      }
    >
      <div>
        <label className="label">Username</label>
        <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
      </div>
      <div>
        <label className="label">Temporary password</label>
        <input className="input" value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="At least 10 characters" />
      </div>
      <div>
        <label className="label">Role</label>
        <select className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="user">User — search &amp; request only</option>
          <option value="admin">Admin — full control</option>
        </select>
      </div>
    </Modal>
  );
}

function ResetPasswordModal({ user, onClose }: { user: ManagedUser; onClose: () => void }) {
  const toast = useToast();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await users.resetPassword(user.id, password);
      toast(`Password reset for ${user.username}`, "success");
      onClose();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Reset failed", "error");
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Reset password — ${user.username}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy} onClick={submit}>Reset</button>
        </>
      }
    >
      <p className="text-sm text-gray-500">
        This signs the user out everywhere. They'll log in with the new password.
      </p>
      <div>
        <label className="label">New password</label>
        <input className="input" value={password} onChange={(e) => setPassword(e.target.value)}
          placeholder="At least 10 characters" autoFocus />
      </div>
    </Modal>
  );
}
