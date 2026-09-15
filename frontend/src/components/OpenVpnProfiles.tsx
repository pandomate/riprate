import { useEffect, useState } from "react";
import { openvpn, setupOpenvpn, OpenVpnProfile, OpenVpnStatus, OpenVpnResult } from "../api/client";
import { useToast } from "./ui";

function ResultBanner({ result }: { result: OpenVpnResult | null }) {
  if (!result) return null;
  return (
    <div className={`rounded-lg border px-3 py-2 text-sm ${result.ok
      ? "bg-green-900/30 border-green-700/60 text-green-200"
      : "bg-red-900/30 border-red-700/60 text-red-200"}`}>
      <div className="font-medium">{result.ok ? "VPN connection works" : "VPN connection failed"}</div>
      <div className="text-xs opacity-90 mt-0.5">{result.message}</div>
      {result.attempts.length > 1 && (
        <div className="text-xs opacity-75 mt-1">Tried {result.attempts.length} profiles.</div>
      )}
    </div>
  );
}

export function OpenVpnProfiles({ setupMode = false }: { setupMode?: boolean }) {
  const toast = useToast();
  const vpnApi = setupMode ? setupOpenvpn : openvpn;
  const [status, setStatus] = useState<OpenVpnStatus | null>(null);
  const [name, setName] = useState("");
  const [config, setConfig] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [profileType, setProfileType] = useState<"auto" | "country" | "individual">("auto");
  const [group, setGroup] = useState("");
  const [result, setResult] = useState<OpenVpnResult | null>(null);
  const [busy, setBusy] = useState("");

  const load = () => vpnApi.status().then(setStatus).catch(() => {});
  useEffect(() => {
    load();
    // Boot reconnect can continue after the page loads. Polling keeps the
    // status honest instead of leaving the user with a stale "Selecting…" UI.
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const readFiles = async (selected: FileList | undefined) => {
    const next = selected ? Array.from(selected) : [];
    setFiles(next);
    if (!next.length) { setConfig(""); return; }
    setConfig(await next[0].text());
    if (next.length === 1 && !name.trim()) setName(next[0].name.replace(/\.ovpn$/i, ""));
  };

  const add = async () => {
    setBusy("add"); setResult(null);
    try {
      const selected = files.length ? files : [];
      let latest = status;
      for (const file of selected) {
        const fileName = file.name.replace(/\.ovpn$/i, "");
        const response = await vpnApi.add({
          name: selected.length === 1 ? name.trim() : fileName,
          config: await file.text(),
          username: username.trim() || undefined,
          password: password || undefined,
          profile_type: profileType,
          group: group.trim() || (selected.length === 1 ? name.trim() : fileName),
        });
        latest = response.status;
      }
      if (!selected.length) throw new Error("Choose at least one .ovpn file");
      setStatus(latest);
      setName(""); setConfig(""); setFiles([]); setUsername(""); setPassword(""); setGroup(""); setProfileType("auto");
      toast(`${selected.length} OpenVPN profile${selected.length === 1 ? "" : "s"} added`, "success");
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Could not add profile", "error");
    } finally { setBusy(""); }
  };

  const action = async (kind: "enable" | "rotate" | "disable") => {
    setBusy(kind); setResult(null);
    try {
      const response = kind === "enable" ? await vpnApi.enable()
        : kind === "rotate" ? await vpnApi.rotate() : await vpnApi.disable();
      if ("result" in response) {
        setResult(response.result);
        setStatus(response.status);
      } else {
        setStatus(response);
      }
      toast(kind === "enable" ? "VPN enabled" : kind === "rotate" ? "VPN rotated" : "VPN disabled", "success");
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "VPN action failed", "error");
    } finally { setBusy(""); }
  };

  const remove = async (profile: OpenVpnProfile) => {
    setBusy(`remove:${profile.id}`);
    try {
      setStatus(await vpnApi.remove(profile.id));
      toast("Profile removed", "success");
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Could not remove profile", "error");
    } finally { setBusy(""); }
  };

  const toggle = async (profile: OpenVpnProfile) => {
    setBusy(`toggle:${profile.id}`);
    try {
      setStatus(await vpnApi.setEnabled(profile.id, !profile.enabled));
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Could not update profile", "error");
    } finally { setBusy(""); }
  };

  const hasProfiles = Boolean(status?.profiles.length);
  const eligibleProfiles = status?.profiles.filter((profile) => profile.enabled).length || 0;
  const state = status?.connection_state || (status?.enabled
    ? status.running ? "connected" : "connecting"
    : "off");
  const connected = state === "connected";
  const connecting = state === "connecting";

  return (
    <div className="card space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold">VPN (OpenVPN profiles)</h2>
          <p className="text-sm text-gray-500">
            Upload Proton `.ovpn` files. RipRate randomly selects an included profile,
            verifies the exit IP, and rotates to another profile when OpenVPN fails.
            Downloads remain paused while no tunnel is verified.
          </p>
        </div>
        {status && (
          <span className={`${connected ? "badge-completed" : connecting ? "badge-downloading" : "badge-neutral"} whitespace-nowrap`}>
            {connected ? "Connected" : connecting ? "Starting / reconnecting" : "VPN off"}
          </span>
        )}
      </div>

      {connecting && (
        <div className="rounded-lg border border-sky-800/60 bg-sky-950/30 p-3 text-sm text-sky-200">
          <div className="font-medium">VPN is connecting</div>
          <div className="text-xs opacity-80 mt-0.5">Trying an included profile. Downloads stay paused until the tunnel and exit IP are verified.</div>
        </div>
      )}

      {connected && status?.active_profile_name && (
        <div className="rounded-lg border border-gray-800 bg-gray-950/40 p-3 text-sm grid grid-cols-2 gap-y-1 gap-x-4">
          <span className="text-gray-500">Connected profile</span><span className="text-green-300">{status.active_profile_name}</span>
          <span className="text-gray-500">Active source</span><span className="font-mono text-gray-300">{status.active_source ? `${status.active_source.host}:${status.active_source.port}/${status.active_source.protocol}` : "—"}</span>
          <span className="text-gray-500">Exit IP</span><span className="font-mono text-gray-300">{status.public_ip || "checking…"}</span>
          <span className="text-gray-500">Kill switch</span><span className={status.kill_switch ? "text-green-300" : "text-gray-400"}>{status.kill_switch ? "Active" : "Inactive"}</span>
        </div>
      )}

      {status?.last_error && <p className="text-sm text-amber-300">{status.last_error}</p>}
      <ResultBanner result={result} />

      <div className="space-y-2">
        <h3 className="text-sm font-medium text-gray-300">Profiles</h3>
        {status?.profiles.map((profile) => (
          <div key={profile.id} className={`flex items-center gap-2 rounded-lg border px-3 py-2 ${status.active_profile_id === profile.id ? "border-green-700/70 bg-green-950/20" : "border-gray-800"}`}>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className="text-sm text-gray-200 truncate">{profile.name}</div>
                {status.active_profile_id === profile.id && <span className="badge-completed whitespace-nowrap">Connected now</span>}
              </div>
              <div className="text-xs text-gray-500 truncate">{profile.enabled ? "Included in selection" : "Excluded from selection"} · {profile.profile_type} · {profile.group} · {profile.remotes.length} source{profile.remotes.length === 1 ? "" : "s"}: {profile.remotes.map((r) => `${r.host}:${r.port}/${r.protocol}`).join(", ")}</div>
            </div>
            <div className="ml-auto flex items-center gap-1">
              <button className={`btn-sm ${profile.enabled ? "btn-primary" : "btn-secondary"}`} disabled={busy !== ""}
                aria-pressed={profile.enabled} title={profile.enabled ? "This profile can be selected" : "This profile will not be selected"}
                onClick={() => toggle(profile)}>{profile.enabled ? "Included" : "Excluded"}</button>
              <button className="btn-danger btn-sm" disabled={busy !== "" || status.active_profile_id === profile.id}
                onClick={() => remove(profile)}>Remove</button>
            </div>
          </div>
        ))}
        {!hasProfiles && <p className="text-sm text-gray-500">No OpenVPN profiles uploaded yet.</p>}
      </div>

      <div className="border-t border-gray-800 pt-3 space-y-3">
        <h3 className="text-sm font-medium text-gray-300">Upload a profile</h3>
        <div>
          <label className="label">OpenVPN configuration (.ovpn)</label>
          <input className="input" type="file" multiple accept=".ovpn,.conf,text/plain"
            disabled={busy !== ""} onChange={(e) => readFiles(e.target.files || undefined)} />
          {files.length > 0 && <p className="text-xs text-gray-500 mt-1">{files.length} profile file{files.length === 1 ? "" : "s"} selected.</p>}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div><label className="label">Profile name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Proton US 1" /></div>
          <div><label className="label">Profile type</label><select className="input" value={profileType} onChange={(e) => setProfileType(e.target.value as typeof profileType)}><option value="auto">Auto-detect</option><option value="country">Country bundle</option><option value="individual">Individual server</option></select></div>
          <div><label className="label">Country/group label (optional)</label><input className="input" value={group} onChange={(e) => setGroup(e.target.value)} placeholder="United States" /></div>
          <div><label className="label">VPN username (if required)</label><input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" /></div>
          <div><label className="label">VPN password (if required)</label><input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" /></div>
        </div>
        <button className="btn-primary btn-sm" disabled={busy !== "" || !files.length}
          onClick={add}>{busy === "add" ? "Adding…" : files.length > 1 ? "Add profiles" : "Add profile"}</button>
      </div>

      <div className="flex flex-wrap gap-2">
        <button className="btn-primary btn-sm" disabled={busy !== "" || !eligibleProfiles}
          onClick={() => action("enable")}>{busy === "enable" ? "Connecting…" : connected ? "Reconnect / choose profile" : eligibleProfiles ? "Connect VPN" : "Include a profile first"}</button>
        <button className="btn-secondary btn-sm" disabled={busy !== "" || !connected || (status?.profiles.length || 0) < 2}
          onClick={() => action("rotate")}>{busy === "rotate" ? "Rotating…" : "Rotate now"}</button>
        {status?.enabled && <button className="btn-secondary btn-sm" disabled={busy !== ""}
          onClick={() => action("disable")}>{busy === "disable" ? "Disabling…" : "Disable"}</button>}
      </div>
    </div>
  );
}
