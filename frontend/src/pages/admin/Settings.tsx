import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { useAuth } from "../../App";
import {
  admin, auth, settings as settingsApi, FsDir, FsListing,
  MediaCategory, Quality, SearchSource, StreamProvider,
} from "../../api/client";
import { Modal, useToast } from "../../components/ui";
import { IChevron, IFolder, ITrash } from "../../components/icons";
import { OpenVpnProfiles } from "../../components/OpenVpnProfiles";

const QUALITIES: Array<[Quality, string]> = [
  [480, "480p (SD)"], [720, "720p (HD)"], [1080, "1080p (Full HD)"],
  [1440, "1440p (QHD)"], [2160, "2160p (4K)"],
];

const SUBTITLE_OPTIONS: Array<[string, string]> = [
  ["en", "English"], ["es", "Spanish"], ["fr", "French"], ["de", "German"],
  ["pt", "Portuguese"], ["it", "Italian"], ["ja", "Japanese"], ["ko", "Korean"],
  ["zh", "Chinese"], ["ar", "Arabic"], ["ru", "Russian"], ["hi", "Hindi"],
];

// libx264 -preset values, fastest → smallest file
const PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"];

/** Section divider that groups related setting cards together. */
function GroupHeading({ children }: { children: ReactNode }) {
  return (
    <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 pt-4 pb-1 border-b border-gray-800/70">
      {children}
    </h2>
  );
}

export default function Settings() {
  const user = useAuth()!;
  const isAdmin = user.role === "admin";

  return (
    <div className="space-y-6 max-w-xl">
      <header>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-gray-500 text-sm mt-1">Your account and preferences.</p>
      </header>

      <AccountSection />
      <PasswordSection />
      {isAdmin && <AdminSettingsSection />}
    </div>
  );
}

function AccountSection() {
  const user = useAuth()!;
  const isAdmin = user.role === "admin";
  return (
    <div className="card space-y-1">
      <h2 className="font-semibold mb-2">Account</h2>
      <div className="flex justify-between text-sm">
        <span className="text-gray-500">Username</span>
        <span className="font-medium">{user.username}</span>
      </div>
      <div className="flex justify-between text-sm">
        <span className="text-gray-500">Role</span>
        <span className={isAdmin ? "text-amber-400 font-medium" : "font-medium"}>
          {isAdmin ? "Administrator" : "User"}
        </span>
      </div>
    </div>
  );
}

function PasswordSection() {
  const user = useAuth()!;
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const minLen = user.role === "admin" ? 12 : 10;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== confirm) { toast("New passwords do not match", "error"); return; }
    if (next.length < minLen) { toast(`Password must be at least ${minLen} characters`, "error"); return; }
    setBusy(true);
    try {
      await auth.changePassword(current, next);
      toast("Password changed", "success");
      setCurrent(""); setNext(""); setConfirm("");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Change failed", "error");
    } finally { setBusy(false); }
  };

  return (
    <form onSubmit={submit} className="card space-y-4">
      <h2 className="font-semibold">Change password</h2>
      <div>
        <label className="label">Current password</label>
        <input className="input" type="password" autoComplete="current-password"
          value={current} onChange={(e) => setCurrent(e.target.value)} required />
      </div>
      <div>
        <label className="label">New password</label>
        <input className="input" type="password" autoComplete="new-password"
          value={next} onChange={(e) => setNext(e.target.value)}
          placeholder={`At least ${minLen} characters`} required />
      </div>
      <div>
        <label className="label">Confirm new password</label>
        <input className="input" type="password" autoComplete="new-password"
          value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
      </div>
      <button className="btn-primary" type="submit" disabled={busy}>
        {busy ? "Saving…" : "Update password"}
      </button>
    </form>
  );
}

function AdminSettingsSection() {
  const toast = useToast();
  const [retention, setRetention] = useState("0");
  const [failedTempMode, setFailedTempMode] = useState<"immediate" | "timed">("immediate");
  const [failedTempAmount, setFailedTempAmount] = useState("24");
  const [failedTempUnit, setFailedTempUnit] = useState<"hours" | "days">("hours");
  const [concurrency, setConcurrency] = useState(3);
  const [cookieSet, setCookieSet] = useState(false);
  const [cookie, setCookie] = useState("");
  const [watchedPct, setWatchedPct] = useState(90);
  const [providers, setProviders] = useState<StreamProvider[]>([]);
  const [wyzieSet, setWyzieSet] = useState(false);
  const [wyzieOn, setWyzieOn] = useState(false);
  const [savingWyzieToggle, setSavingWyzieToggle] = useState(false);
  const [subtitleLanguage, setSubtitleLanguage] = useState("en");
  const [subtitleAutoSync, setSubtitleAutoSync] = useState(true);
  const [wyzieKey, setWyzieKey] = useState("");
  const [tmdbSet, setTmdbSet] = useState(false);
  const [tmdbKey, setTmdbKey] = useState("");
  const [searchSource, setSearchSource] = useState<SearchSource>("tmdb");
  const [mediaRoot, setMediaRoot] = useState("");
  const [cats, setCats] = useState<MediaCategory[]>([]);
  const [quality, setQuality] = useState<Quality>(1080);
  const [segConns, setSegConns] = useState(6);
  const [savingSC, setSavingSC] = useState(false);
  const [crf, setCrf] = useState(20);
  const [preset, setPreset] = useState("medium");
  const [maxGb, setMaxGb] = useState(10);
  const [savingEnc, setSavingEnc] = useState(false);
  const [loading, setLoading] = useState(true);
  const [savingR, setSavingR] = useState(false);
  const [savingFailedTemp, setSavingFailedTemp] = useState(false);
  const [savingC, setSavingC] = useState(false);
  const [savingK, setSavingK] = useState(false);
  const [savingW, setSavingW] = useState(false);
  const [savingWz, setSavingWz] = useState(false);
  const [savingT, setSavingT] = useState(false);
  const [savingF, setSavingF] = useState(false);

  useEffect(() => {
    settingsApi.get().then((s) => {
      setRetention(String(s.history_retention_days));
      if (s.failed_temp_retention_hours === 0) {
        setFailedTempMode("immediate");
      } else {
        setFailedTempMode("timed");
        if (s.failed_temp_retention_hours % 24 === 0) {
          setFailedTempUnit("days");
          setFailedTempAmount(String(s.failed_temp_retention_hours / 24));
        } else {
          setFailedTempUnit("hours");
          setFailedTempAmount(String(s.failed_temp_retention_hours));
        }
      }
      setConcurrency(s.max_concurrent_downloads);
      setCookieSet(s.imdb_cookie_set);
      setWatchedPct(s.watched_threshold_percent);
      setProviders(s.stream_providers);
      setWyzieSet(s.wyzie_api_key_set);
      setWyzieOn(s.wyzie_enabled);
      setSubtitleLanguage(s.subtitle_language || "en");
      setSubtitleAutoSync(s.subtitle_auto_sync ?? true);
      setTmdbSet(s.tmdb_api_key_set);
      setSearchSource(s.search_source);
      setMediaRoot(s.media_root);
      setCats(s.media_categories);
      setQuality(s.preferred_quality);
      setSegConns(s.segment_connections);
      setCrf(s.video_crf);
      setPreset(s.video_preset);
      setMaxGb(s.max_file_gb);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  const saveCats = async (list: MediaCategory[]) => {
    setSavingF(true);
    try {
      const s = await settingsApi.update({ media_categories: list });
      setCats(s.media_categories);
      toast("Categories saved", "success");
      return true;
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
      return false;
    } finally { setSavingF(false); }
  };

  const saveSegConns = async () => {
    setSavingSC(true);
    try {
      const s = await settingsApi.update({ segment_connections: segConns });
      setSegConns(s.segment_connections);
      toast(`Downloads will use ${s.segment_connections} parallel connections`, "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingSC(false); }
  };

  const saveSubtitleLanguage = async (value: string) => {
    const prev = subtitleLanguage;
    setSubtitleLanguage(value);
    try {
      const s = await settingsApi.update({ subtitle_language: value });
      setSubtitleLanguage(s.subtitle_language);
      toast("Subtitle language saved", "success");
    } catch (err: unknown) {
      setSubtitleLanguage(prev);
      toast(err instanceof Error ? err.message : "Save failed", "error");
    }
  };

  const toggleSubtitleAutoSync = async () => {
    const next = !subtitleAutoSync;
    setSubtitleAutoSync(next);
    try {
      const s = await settingsApi.update({ subtitle_auto_sync: next });
      setSubtitleAutoSync(s.subtitle_auto_sync);
      toast(next ? "Automatic subtitle alignment enabled" : "Automatic subtitle alignment disabled", "success");
    } catch (err: unknown) {
      setSubtitleAutoSync(!next);
      toast(err instanceof Error ? err.message : "Save failed", "error");
    }
  };

  const saveEncoding = async () => {
    setSavingEnc(true);
    try {
      const s = await settingsApi.update({ video_crf: crf, video_preset: preset, max_file_gb: maxGb });
      setCrf(s.video_crf); setPreset(s.video_preset); setMaxGb(s.max_file_gb);
      toast("Encoding settings saved", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingEnc(false); }
  };

  const saveQuality = async (q: Quality) => {
    const prev = quality;
    setQuality(q);   // optimistic
    try {
      const s = await settingsApi.update({ preferred_quality: q });
      setQuality(s.preferred_quality);
      toast(`New downloads will prefer ${q}p`, "success");
    } catch (err: unknown) {
      setQuality(prev);
      toast(err instanceof Error ? err.message : "Save failed", "error");
    }
  };

  const saveCookie = async (value: string) => {
    setSavingK(true);
    try {
      const s = await settingsApi.update({ imdb_cookie: value });
      setCookieSet(s.imdb_cookie_set);
      setCookie("");
      toast(value.trim() ? "IMDB cookie saved" : "IMDB cookie cleared", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingK(false); }
  };

  const saveWyzieKey = async (value: string) => {
    setSavingWz(true);
    try {
      const s = await settingsApi.update({ wyzie_api_key: value });
      setWyzieSet(s.wyzie_api_key_set);
      setWyzieKey("");
      toast(value.trim() ? "Wyzie API key saved" : "Wyzie API key cleared", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingWz(false); }
  };

  const toggleWyzie = async () => {
    if (savingWyzieToggle) return;
    const on = !wyzieOn;
    setSavingWyzieToggle(true);
    setWyzieOn(on);   // optimistic
    try {
      const s = await settingsApi.update({ wyzie_enabled: on });
      setWyzieOn(s.wyzie_enabled);
      toast(on ? "Wyzie enabled — preferred subtitle source" : "Wyzie disabled — using stream subtitles", "success");
    } catch (err: unknown) {
      setWyzieOn(!on);
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingWyzieToggle(false); }
  };

  const saveTmdbKey = async (value: string) => {
    setSavingT(true);
    try {
      const s = await settingsApi.update({ tmdb_api_key: value });
      setTmdbSet(s.tmdb_api_key_set);
      setTmdbKey("");
      toast(value.trim() ? "TMDB API key saved" : "TMDB API key cleared", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingT(false); }
  };

  const saveSearchSource = async (src: SearchSource) => {
    const prev = searchSource;
    setSearchSource(src);   // optimistic
    try {
      const s = await settingsApi.update({ search_source: src });
      setSearchSource(s.search_source);
      toast("Search source saved", "success");
    } catch (err: unknown) {
      setSearchSource(prev);
      toast(err instanceof Error ? err.message : "Save failed", "error");
    }
  };

  const saveRetention = async () => {
    const days = Math.max(0, Math.floor(Number(retention) || 0));
    setSavingR(true);
    try {
      const s = await settingsApi.update({ history_retention_days: days });
      setRetention(String(s.history_retention_days));
      toast("Retention saved", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingR(false); }
  };

  const saveFailedTempRetention = async () => {
    const amount = Math.max(1, Math.floor(Number(failedTempAmount) || 1));
    const hours = failedTempMode === "immediate" ? 0
      : Math.min(87600, amount * (failedTempUnit === "days" ? 24 : 1));
    setSavingFailedTemp(true);
    try {
      const s = await settingsApi.update({ failed_temp_retention_hours: hours });
      const saved = s.failed_temp_retention_hours;
      setFailedTempMode(saved === 0 ? "immediate" : "timed");
      if (saved > 0) {
        if (saved % 24 === 0) {
          setFailedTempUnit("days");
          setFailedTempAmount(String(saved / 24));
        } else {
          setFailedTempUnit("hours");
          setFailedTempAmount(String(saved));
        }
      }
      toast("Failed download cleanup saved", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingFailedTemp(false); }
  };

  const saveConcurrency = async (n: number) => {
    setSavingC(true);
    try {
      const s = await settingsApi.update({ max_concurrent_downloads: n });
      setConcurrency(s.max_concurrent_downloads);
      toast(`Now running up to ${s.max_concurrent_downloads} downloads at once`, "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingC(false); }
  };

  const saveWatched = async () => {
    const pct = Math.max(50, Math.min(100, Math.floor(watchedPct) || 90));
    setSavingW(true);
    try {
      const s = await settingsApi.update({ watched_threshold_percent: pct });
      setWatchedPct(s.watched_threshold_percent);
      toast("Watched threshold saved", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Save failed", "error");
    } finally { setSavingW(false); }
  };

  const saveProviders = async (list: StreamProvider[]) => {
    const prev = providers;
    setProviders(list);   // optimistic
    try {
      const s = await settingsApi.update({
        stream_providers: list.map((p) => ({ key: p.key, enabled: p.enabled })),
      });
      setProviders(s.stream_providers);
    } catch (err: unknown) {
      setProviders(prev);
      toast(err instanceof Error ? err.message : "Save failed", "error");
    }
  };

  const moveProvider = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= providers.length) return;
    const list = [...providers];
    [list[i], list[j]] = [list[j], list[i]];
    saveProviders(list);
  };

  const toggleProvider = (i: number) =>
    saveProviders(providers.map((p, idx) => idx === i ? { ...p, enabled: !p.enabled } : p));

  if (loading) return null;

  return (
    <>
      <GroupHeading>Privacy &amp; network</GroupHeading>

      <OpenVpnProfiles />

      <GroupHeading>Library &amp; folders</GroupHeading>

      <CategoriesWizard cats={cats} mediaRoot={mediaRoot}
        saving={savingF} onSave={saveCats} />

      <MoveFolderCard cats={cats} />

      <GroupHeading>Downloads</GroupHeading>

      <div className="card space-y-3">
        <h2 className="font-semibold">Subtitle language</h2>
        <p className="text-sm text-gray-500">
          RipRate chooses this language from Nova's subtitle tracks, embedded HLS tracks, and Wyzie.
          Audio stays in the title's original TMDB language when the stream provides that choice.
        </p>
        <select className="input w-48" value={subtitleLanguage}
          onChange={(e) => saveSubtitleLanguage(e.target.value)}>
          {SUBTITLE_OPTIONS.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
        </select>
        <div className="flex items-center gap-3 pt-3 border-t border-gray-800">
          <div className="min-w-0">
            <div className="text-sm font-medium text-gray-200">Align subtitles to audio</div>
            <p className="text-xs text-gray-500 mt-0.5">
              Corrects fixed delays and release timing differences locally before new downloads are saved.
            </p>
          </div>
          <button role="switch" aria-label="Automatically align subtitles to audio"
            aria-checked={subtitleAutoSync} onClick={toggleSubtitleAutoSync}
            className={`relative w-11 h-6 rounded-full flex-shrink-0 ml-auto transition-colors ${subtitleAutoSync ? "bg-sky-600" : "bg-gray-700"}`}>
            <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all ${subtitleAutoSync ? "left-[22px]" : "left-0.5"}`} />
          </button>
        </div>
      </div>

      <div className="card space-y-3">
        <h2 className="font-semibold">Preferred download quality</h2>
        <p className="text-sm text-gray-500">
          The downloader picks the stream closest to this resolution — exact match first, then
          the next one below it (it never grabs more than you asked for). Applies to new
          downloads only.
        </p>
        <div className="flex flex-wrap gap-1.5">
          {QUALITIES.map(([q, label]) => (
            <button key={q}
              onClick={() => saveQuality(q)}
              className={`px-3 py-1.5 rounded-lg border text-sm transition-colors ${
                quality === q
                  ? "border-sky-600 bg-sky-950/40 text-gray-100 font-medium"
                  : "border-gray-800 text-gray-400 hover:border-gray-600"}`}>
              {label}
            </button>
          ))}
        </div>

        <div className="border-t border-gray-800 pt-3 space-y-3">
          <h3 className="text-sm font-medium text-gray-300">Re-encoding</h3>
          <p className="text-sm text-gray-500">
            Streams are copied into an MP4 untouched when they can be. These only apply when a
            re-encode is unavoidable — an incompatible codec, or a file over the size limit.
          </p>
          <div className="flex items-end gap-3 flex-wrap">
            <div>
              <label className="label">Shrink over (GB)</label>
              <input className="input w-28" type="number" min={0.5} max={200} step={0.5} value={maxGb}
                onChange={(e) => setMaxGb(Math.max(0.5, Math.min(200, Number(e.target.value) || 10)))} />
            </div>
            <div>
              <label className="label">Quality — CRF {crf}</label>
              <input type="range" min={16} max={30} value={crf} className="w-40 accent-sky-500 block"
                onChange={(e) => setCrf(Number(e.target.value))} />
            </div>
            <div>
              <label className="label">Preset</label>
              <select className="input w-36" value={preset} onChange={(e) => setPreset(e.target.value)}>
                {PRESETS.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <button className="btn-primary" onClick={saveEncoding} disabled={savingEnc}>
              {savingEnc ? "Saving…" : "Save"}
            </button>
          </div>
          <p className="text-xs text-gray-500">
            Lower CRF = better quality, bigger file (18–23 is the sweet spot). A slower preset makes a
            smaller file at the same quality, using more CPU time.
          </p>
        </div>
      </div>

      <div className="card space-y-3">
        <h2 className="font-semibold">Download providers</h2>
        <p className="text-sm text-gray-500">
          Where RipRate looks for a stream to download, tried top-to-bottom until one works. Reorder
          with the arrows, or turn a source off if it's broken or too slow. Nova returns multiple
          CDN sources; the queue tries them in quality order and validates that each one decodes
          to real video before continuing.
        </p>
        <div className="space-y-1.5">
          {providers.map((p, i) => (
            <div key={p.key}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2 ${p.enabled ? "border-gray-700 bg-gray-800/40" : "border-gray-800 bg-transparent"}`}>
              <span className="text-xs text-gray-600 w-4 text-center">{i + 1}</span>
              <span className={`text-sm ${p.enabled ? "text-gray-200" : "text-gray-500 line-through"}`}>
                {p.label}
                {p.requires_source && !p.available && (
                  <span className="ml-2 text-xs text-amber-500">requires TMDB key</span>
                )}
              </span>
              <div className="ml-auto flex items-center gap-1">
                <button className="btn-secondary btn-sm px-2 disabled:opacity-30"
                  disabled={i === 0} onClick={() => moveProvider(i, -1)} title="Move up">↑</button>
                <button className="btn-secondary btn-sm px-2 disabled:opacity-30"
                  disabled={i === providers.length - 1} onClick={() => moveProvider(i, 1)} title="Move down">↓</button>
                <button
                  role="switch" aria-checked={p.enabled}
                  onClick={() => toggleProvider(i)}
                  className={`relative w-11 h-6 rounded-full flex-shrink-0 transition-colors ml-1 ${p.enabled ? "bg-sky-600" : "bg-gray-700"}`}>
                  <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all ${p.enabled ? "left-[22px]" : "left-0.5"}`} />
                </button>
              </div>
            </div>
          ))}
        </div>
        {providers.every((p) => !p.enabled) && (
          <p className="text-xs text-amber-500">Every provider is off — downloads will fail until you enable one.</p>
        )}
      </div>

      <div className="card space-y-3">
        <h2 className="font-semibold">Concurrent downloads</h2>
        <p className="text-sm text-gray-500">
          How many downloads run at the same time. Increasing starts the next queued items right
          away; decreasing sends the extra running downloads back to the front of the queue (their
          progress is kept). Saved to the server's <span className="font-mono text-gray-400">.env</span>.
        </p>
        <div className="flex items-end gap-2">
          <div>
            <label className="label">Simultaneous downloads (1–10)</label>
            <input className="input w-40" type="number" min={1} max={10} value={concurrency}
              onChange={(e) => setConcurrency(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} />
          </div>
          <button className="btn-primary" onClick={() => saveConcurrency(concurrency)} disabled={savingC}>
            {savingC ? "Applying…" : "Apply"}
          </button>
        </div>

        <div className="border-t border-gray-800 pt-3 space-y-2">
          <h3 className="text-sm font-medium text-gray-300">Connections per download</h3>
          <p className="text-sm text-gray-500">
            How many pieces of one video are fetched in parallel. More connections = faster
            downloads, until the source starts rate-limiting (it backs off automatically if that
            happens). Applies to downloads started after saving.
          </p>
          <div className="flex items-end gap-2">
            <div>
              <label className="label">Parallel connections (1–16)</label>
              <input className="input w-40" type="number" min={1} max={16} value={segConns}
                onChange={(e) => setSegConns(Math.max(1, Math.min(16, Number(e.target.value) || 6)))} />
            </div>
            <button className="btn-primary" onClick={saveSegConns} disabled={savingSC}>
              {savingSC ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </div>

      <div className="card space-y-3">
        <h2 className="font-semibold">Download history retention</h2>
        <p className="text-sm text-gray-500">
          Automatically clean up old items in the Downloads history. Failed entries are deleted;
          completed entries are archived (they stay in your Library, just leave the history list).
          Set to <span className="text-gray-300">0</span> to keep everything forever. Saved to the
          server's <span className="font-mono text-gray-400">.env</span>.
        </p>
        <div className="flex items-end gap-2">
          <div>
            <label className="label">Keep history for (days)</label>
            <input className="input w-40" type="number" min={0} value={retention}
              onChange={(e) => setRetention(e.target.value)} />
          </div>
          <button className="btn-primary" onClick={saveRetention} disabled={savingR}>
            {savingR ? "Saving…" : "Save"}
          </button>
        </div>
        <p className="text-xs text-gray-600">
          {Number(retention) > 0
            ? `History older than ${retention} day(s) will be cleaned automatically.`
            : "History is kept indefinitely."}
        </p>
      </div>

      <div className="card space-y-3">
        <h2 className="font-semibold">Failed download temp files</h2>
        <p className="text-sm text-gray-500">
          Choose how long incomplete video pieces are kept after a download fails. Retained files
          can be reused if the job is retried. Cancelling or deleting a job still removes its temp files.
        </p>
        <div className="space-y-2">
          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input type="radio" name="failed_temp_mode" className="accent-sky-500"
              checked={failedTempMode === "immediate"}
              onChange={() => setFailedTempMode("immediate")} />
            Delete all temp files immediately when a download fails
          </label>
          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input type="radio" name="failed_temp_mode" className="accent-sky-500"
              checked={failedTempMode === "timed"}
              onChange={() => setFailedTempMode("timed")} />
            Delete temp files after
          </label>
          {failedTempMode === "timed" && (
            <div className="flex gap-2 pl-6">
              <input className="input w-28" type="number" min={1}
                max={failedTempUnit === "days" ? 3650 : 87600}
                value={failedTempAmount} onChange={(e) => setFailedTempAmount(e.target.value)} />
              <select className="input w-28" value={failedTempUnit}
                onChange={(e) => setFailedTempUnit(e.target.value as "hours" | "days")}>
                <option value="hours">Hours</option>
                <option value="days">Days</option>
              </select>
            </div>
          )}
        </div>
        <button className="btn-primary" onClick={saveFailedTempRetention}
          disabled={savingFailedTemp}>
          {savingFailedTemp ? "Saving…" : "Save"}
        </button>
        <p className="text-xs text-gray-600">
          Restarting RipRate with start.sh always removes failed jobs and their temp files. During
          a server run, cleanup also runs when this setting changes and every 15 minutes.
        </p>
      </div>

      <GroupHeading>Metadata &amp; subtitles</GroupHeading>

      <div className="card space-y-3">
        <h2 className="font-semibold">Search source</h2>
        <p className="text-sm text-gray-500">
          Choose one source for title, season, and episode searches. TMDB requires the API key below;
          IMDB does not. Nova still requires a TMDB key because it resolves streams by TMDB ID.
        </p>
        <div className="space-y-1.5">
          {([
            ["tmdb", "TMDB", "Recommended. Fast API search and the IDs Nova uses."],
            ["imdb", "IMDB", "Scraped search and episode data; Nova still needs TMDB configured."],
          ] as Array<[SearchSource, string, string]>).map(([value, label, hint]) => (
            <label key={value}
              className={`flex items-start gap-2.5 rounded-lg border px-3 py-2 cursor-pointer ${searchSource === value ? "border-sky-700 bg-sky-950/30" : "border-gray-800 hover:border-gray-700"}`}>
              <input type="radio" name="search_source" className="mt-1 accent-sky-500"
                checked={searchSource === value} onChange={() => saveSearchSource(value)} />
              <span>
                <span className={`text-sm block ${searchSource === value ? "text-gray-100 font-medium" : "text-gray-300"}`}>{label}</span>
                <span className="text-xs text-gray-500">{hint}</span>
              </span>
            </label>
          ))}
        </div>
        {searchSource === "tmdb" && !tmdbSet && (
          <p className="text-xs text-amber-500">TMDB search is selected but no API key is saved.</p>
        )}
      </div>

      <div className="card space-y-3">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold">TMDB (metadata &amp; artwork)</h2>
          {tmdbSet
            ? <span className="badge-completed">configured</span>
            : <span className="badge-neutral">not configured</span>}
        </div>
        <p className="text-sm text-gray-500">
          Powers posters, plots and ratings on the Watch page, richer Jellyfin NFO files, and Nova's
          stream lookup. Paste a TMDB <span className="text-gray-400">API Read Access
          Token</span> (the long v4 bearer token from themoviedb.org → Settings → API). Metadata is
          fetched once per title and cached locally — TMDB is not called on every page view.
        </p>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <label className="label">API read access token {tmdbSet && <span className="text-gray-600">(leave blank to keep current)</span>}</label>
            <input className="input" type="password" autoComplete="off"
              placeholder={tmdbSet ? "•••••••••• (a token is saved)" : "eyJhbGci…"}
              value={tmdbKey} onChange={(e) => setTmdbKey(e.target.value)} />
          </div>
          <button className="btn-primary" onClick={() => saveTmdbKey(tmdbKey)} disabled={savingT || !tmdbKey.trim()}>
            {savingT ? "Saving…" : "Save"}
          </button>
          {tmdbSet && (
            <button className="btn-secondary" onClick={() => saveTmdbKey("")} disabled={savingT}>
              Clear
            </button>
          )}
        </div>

      </div>

      <div className="card space-y-3">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold">IMDB access cookie</h2>
          {cookieSet
            ? <span className="badge-completed">configured</span>
            : <span className="badge-neutral">optional</span>}
        </div>
        <p className="text-sm text-gray-500">
          IMDB sometimes blocks automated requests, causing search/season lookups to time out.
          IMDB may block ordinary HTTP requests. If that happens, paste your browser's
          <span className="font-mono text-gray-400"> Cookie</span> header from
          <span className="font-mono text-gray-400"> imdb.com</span>. It's stored securely and never shown again.
        </p>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <label className="label">Cookie header {cookieSet && <span className="text-gray-600">(leave blank to keep current)</span>}</label>
            <input className="input" type="password" autoComplete="off"
              placeholder={cookieSet ? "•••••••••• (a cookie is saved)" : "session-id=…; aws-waf-token=…"}
              value={cookie} onChange={(e) => setCookie(e.target.value)} />
          </div>
          <button className="btn-primary" onClick={() => saveCookie(cookie)} disabled={savingK || !cookie.trim()}>
            {savingK ? "Saving…" : "Save"}
          </button>
          {cookieSet && (
            <button className="btn-secondary" onClick={() => saveCookie("")} disabled={savingK}>
              Clear
            </button>
          )}
        </div>
      </div>

      <div className="card space-y-3">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold">Wyzie Subs (subtitle downloads)</h2>
          {wyzieSet
            ? <span className="badge-completed">key configured</span>
            : <span className="badge-neutral">no key</span>}
          <button
            role="switch" aria-label="Enable Wyzie subtitles" aria-checked={wyzieOn}
            disabled={savingWyzieToggle} aria-busy={savingWyzieToggle}
            onClick={toggleWyzie} title={wyzieOn ? "Disable Wyzie" : "Enable Wyzie"}
            className={`relative w-11 h-6 rounded-full flex-shrink-0 transition-colors ml-auto ${wyzieOn ? "bg-sky-600" : "bg-gray-700"}`}>
            <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all ${wyzieOn ? "left-[22px]" : "left-0.5"}`} />
          </button>
        </div>
        <p className="text-sm text-gray-300" role="status">
          {wyzieOn && wyzieSet ? "Active: Wyzie first, with stream provider fallback."
            : wyzieOn ? "Add an API key to activate Wyzie. Currently using stream provider subtitles."
            : "Wyzie disabled. Using stream provider subtitles."}
        </p>
        <p className="text-sm text-gray-500">
          When enabled with an API key, Wyzie is tried first for subtitles in your selected language,
          including subtitle retries. If Wyzie has no result or fails, RipRate uses stream provider
          subtitles. When disabled or without a key, only stream provider subtitles are used.
          Get a free API key from <span className="font-mono text-gray-400">store.wyzie.io/redeem</span>.
        </p>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <label className="label">API key {wyzieSet && <span className="text-gray-600">(leave blank to keep current)</span>}</label>
            <input className="input" type="password" autoComplete="off"
              placeholder={wyzieSet ? "•••••••••• (a key is saved)" : "wyzie-…"}
              value={wyzieKey} onChange={(e) => setWyzieKey(e.target.value)} />
          </div>
          <button className="btn-primary" onClick={() => saveWyzieKey(wyzieKey)} disabled={savingWz || !wyzieKey.trim()}>
            {savingWz ? "Saving…" : "Save"}
          </button>
          {wyzieSet && (
            <button className="btn-secondary" onClick={() => saveWyzieKey("")} disabled={savingWz}>
              Clear
            </button>
          )}
        </div>
      </div>

      <GroupHeading>Playback</GroupHeading>

      <div className="card space-y-3">
        <h2 className="font-semibold">Watched threshold</h2>
        <p className="text-sm text-gray-500">
          When someone has watched this percentage of a video, it counts as watched and drops
          out of their <span className="text-gray-300">Continue watching</span> row on the Watch
          page. 90% means the credits don't keep it hanging around.
        </p>
        <div className="flex items-end gap-2">
          <div>
            <label className="label">Watched at (50–100%)</label>
            <input className="input w-40" type="number" min={50} max={100} value={watchedPct}
              onChange={(e) => setWatchedPct(Math.max(50, Math.min(100, Number(e.target.value) || 90)))} />
          </div>
          <button className="btn-primary" onClick={saveWatched} disabled={savingW}>
            {savingW ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

    </>
  );
}

/* ── Media categories wizard ─────────────────────────────────────────────── */

/** Jellyfin-style folder picker: browse the container's real media tree,
    drill into folders, create one, and select the current location. */
function FolderBrowserModal({ title, initialPath, onClose, onSelect }: {
  title: string;
  initialPath?: string;
  onClose: () => void;
  onSelect: (relPath: string) => void;
}) {
  const toast = useToast();
  const [listing, setListing] = useState<FsListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<FsDir | null>(null);

  const load = (path: string) => {
    setLoading(true);
    admin.fsList(path)
      .then(setListing)
      .catch(() => admin.fsList("").then(setListing).catch(() => {}))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(initialPath ?? ""); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const crumbs: Array<{ label: string; path: string }> = [{ label: listing?.root ?? "…", path: "" }];
  if (listing?.path) {
    const parts = listing.path.split("/");
    parts.forEach((p, i) => crumbs.push({ label: p, path: parts.slice(0, i + 1).join("/") }));
  }

  const createFolder = async () => {
    const name = newName.trim();
    if (!name || !listing) return;
    setCreating(true);
    try {
      const rel = listing.path ? `${listing.path}/${name}` : name;
      await admin.fsMkdir(rel);
      setNewName("");
      load(listing.path);
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Could not create folder", "error");
    } finally { setCreating(false); }
  };

  return (
    <Modal title={title} onClose={onClose}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={!listing || listing.path === ""}
            title={listing?.path === "" ? "Pick a folder inside the media root" : undefined}
            onClick={() => listing && onSelect(listing.path)}>
            Select this folder
          </button>
        </>
      }>
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 flex-wrap text-sm font-mono bg-gray-950 rounded-lg px-3 py-2">
        {crumbs.map((c, i) => (
          <span key={c.path || "__root"} className="flex items-center gap-1">
            {i > 0 && <span className="text-gray-600">/</span>}
            <button
              className={i === crumbs.length - 1 ? "text-gray-100" : "text-sky-400 hover:underline"}
              onClick={() => load(c.path)}>
              {c.label}
            </button>
          </span>
        ))}
      </div>

      {/* Directory list */}
      <div className="border border-gray-800 rounded-lg divide-y divide-gray-800/70 max-h-72 overflow-y-auto">
        {listing?.parent !== null && listing !== null && (
          <button className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-400 hover:bg-gray-800/50"
            onClick={() => load(listing.parent!)}>
            <IChevron dir="up" size={14} /> Up one level
          </button>
        )}
        {loading ? (
          <div className="px-3 py-4 text-sm text-gray-500">Loading…</div>
        ) : listing && listing.dirs.length === 0 ? (
          <div className="px-3 py-4 text-sm text-gray-600">No folders in here.</div>
        ) : (
          listing?.dirs.map((d) => (
            <div key={d.path}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-gray-800/50">
              <button className="flex items-center gap-2 flex-1 min-w-0 text-left"
                onClick={() => load(d.path)} title={`Open ${d.name}`}>
                <span className="text-gray-500 flex-shrink-0"><IFolder size={15} /></span>
                <span className="truncate">{d.name}</span>
                {d.category && <span className="badge-completed flex-shrink-0">{d.category}</span>}
                {d.is_mount && (
                  <span className="badge-neutral flex-shrink-0">
                    mount{d.free_gb != null ? ` · ${d.free_gb} GB free` : ""}
                  </span>
                )}
              </button>
              <button
                className="p-1 rounded text-red-400/80 hover:text-red-300 hover:bg-gray-700/60 flex-shrink-0"
                title={`Delete “${d.name}” and everything inside it`}
                onClick={(e) => { e.stopPropagation(); setDeleteTarget(d); }}>
                <ITrash size={14} />
              </button>
              <span className="text-gray-600 flex-shrink-0"><IChevron dir="right" size={13} /></span>
            </div>
          ))
        )}
      </div>

      {/* New folder */}
      <div className="flex items-center gap-2">
        <input className="input h-8 text-sm py-1 flex-1" placeholder="New folder name…"
          value={newName} onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") createFolder(); }} />
        <button className="btn-secondary btn-sm" onClick={createFolder}
          disabled={creating || !newName.trim()}>
          {creating ? "Creating…" : "Create here"}
        </button>
      </div>

      {listing && (
        <p className="text-xs text-gray-600 font-mono truncate">
          Selected: {listing.root}{listing.path ? `/${listing.path}` : ""}
        </p>
      )}

      {deleteTarget && (
        <DeleteFolderConfirm
          dir={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => { setDeleteTarget(null); load(listing?.path ?? ""); }}
        />
      )}
    </Modal>
  );
}

/** Type-to-confirm permanent deletion of a folder and its contents. */
function DeleteFolderConfirm({ dir, onClose, onDeleted }: {
  dir: FsDir; onClose: () => void; onDeleted: () => void;
}) {
  const toast = useToast();
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const armed = typed === dir.name;

  const del = async () => {
    if (!armed || busy) return;
    setBusy(true);
    try {
      await admin.fsDelete(dir.path, dir.name);
      toast(`Deleted “${dir.name}”`, "success");
      onDeleted();
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Could not delete folder", "error");
    } finally { setBusy(false); }
  };

  return (
    <Modal title="Delete folder" onClose={onClose}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-danger" disabled={!armed || busy} onClick={del}>
            {busy ? "Deleting…" : "Delete permanently"}
          </button>
        </>
      }>
      <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2.5 text-sm text-red-200 space-y-1">
        <p className="font-medium">This permanently deletes the folder and everything inside it.</p>
        <p className="text-red-300/90 font-mono break-all">{dir.path}</p>
        <p className="text-red-300/80 text-xs">
          This cannot be undone — files are removed from disk (and the host drive).
          {dir.is_mount && " This folder is a mounted drive; its contents will be erased."}
          {dir.category && ` It is currently assigned to the “${dir.category}” category.`}
        </p>
      </div>
      <div>
        <label className="label">Type <span className="font-mono text-gray-300">{dir.name}</span> to confirm</label>
        <input className="input" value={typed} autoFocus
          placeholder={dir.name}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") del(); }} />
      </div>
    </Modal>
  );
}

/** Read-only folder path + Browse button that opens the picker dialog. */
function FolderField({ value, onChange, browseTitle }: {
  value: string; onChange: (v: string) => void; browseTitle: string;
}) {
  const [browsing, setBrowsing] = useState(false);
  return (
    <div className="flex items-center gap-1.5">
      <span className={`input h-8 text-sm py-1 flex-1 truncate font-mono ${value ? "" : "text-gray-600"}`}
        title={value || undefined}>
        {value || "— none —"}
      </span>
      <button className="btn-secondary btn-sm flex-shrink-0" onClick={() => setBrowsing(true)}>Browse</button>
      {browsing && (
        <FolderBrowserModal title={browseTitle} initialPath={value}
          onClose={() => setBrowsing(false)}
          onSelect={(p) => { onChange(p); setBrowsing(false); }} />
      )}
    </div>
  );
}

function CategoriesWizard({ cats, mediaRoot, saving, onSave }: {
  cats: MediaCategory[]; mediaRoot: string;
  saving: boolean; onSave: (list: MediaCategory[]) => Promise<boolean>;
}) {
  const [list, setList] = useState<MediaCategory[]>(cats);
  const [adding, setAdding] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [newFolder, setNewFolder] = useState("");
  useEffect(() => { setList(cats); }, [cats]);

  const builtin = (key: string) =>
    key === "movies" || key === "shows" || key === "anime_movies" || key === "anime_shows";
  const setFolder = (i: number, folder: string) =>
    setList((l) => l.map((c, idx) => idx === i ? { ...c, folder } : c));
  const remove = (i: number) => setList((l) => l.filter((_, idx) => idx !== i));

  const addCategory = () => {
    const label = newLabel.trim();
    if (!label) return;
    const key = label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 30);
    if (!key || list.some((c) => c.key === key)) return;
    setList((l) => [...l, { key, label, folder: newFolder.trim() || label }]);
    setNewLabel(""); setNewFolder(""); setAdding(false);
  };

  const dirty = JSON.stringify(list) !== JSON.stringify(cats);

  return (
    <div className="card space-y-3">
      <h2 className="font-semibold">Media folders &amp; categories</h2>
      <p className="text-sm text-gray-500">
        A category tells the program where a download's files belong — every category is
        offered when queueing, and <span className="text-gray-300">Movies</span>,
        <span className="text-gray-300">Shows</span>, <span className="text-gray-300">Anime Movies</span>,
        and <span className="text-gray-300">Anime Shows</span> are the automatic destinations when detected.
        Each one saves into a folder under the media root{" "}
        <span className="font-mono text-gray-400">{mediaRoot}</span>. A folder marked{" "}
        <span className="text-sky-400">mount</span> is a dedicated host location from{" "}
        <span className="font-mono text-gray-400">.env</span>; anything else is a plain subfolder
        (created automatically). Existing files stay put when you change a folder — only new
        downloads follow it.
      </p>

      <div className="space-y-1.5">
        {list.map((c, i) => (
          <div key={c.key} className="flex items-center gap-2 rounded-lg border border-gray-800 bg-gray-800/30 px-3 py-2 flex-wrap">
            <span className="text-sm font-medium w-28 truncate" title={c.key}>{c.label}</span>
            {builtin(c.key) && <span className="badge-neutral flex-shrink-0">default</span>}
            <div className="ml-auto w-64">
              <FolderField value={c.folder} onChange={(v) => setFolder(i, v)}
                browseTitle={`Folder for ${c.label}`} />
            </div>
            {!builtin(c.key) ? (
              <button className="p-1 rounded text-red-400/80 hover:text-red-300 hover:bg-gray-700/60"
                title="Remove category (files are not deleted)" onClick={() => remove(i)}>
                <ITrash size={14} />
              </button>
            ) : <span className="w-6" />}
          </div>
        ))}
      </div>

      {adding ? (
        <div className="flex items-end gap-2 flex-wrap rounded-lg border border-sky-900/60 bg-sky-950/20 px-3 py-2">
          <div>
            <label className="label">Name</label>
            <input className="input h-8 text-sm py-1 w-36" value={newLabel} placeholder="Music"
              onChange={(e) => setNewLabel(e.target.value)} autoFocus />
          </div>
          <div className="w-64">
            <label className="label">Folder</label>
            <FolderField value={newFolder} onChange={setNewFolder}
              browseTitle={`Folder for ${newLabel.trim() || "new category"}`} />
          </div>
          <button className="btn-primary btn-sm" onClick={addCategory} disabled={!newLabel.trim()}>Add</button>
          <button className="btn-ghost btn-sm" onClick={() => setAdding(false)}>Cancel</button>
        </div>
      ) : (
        <button className="btn-secondary btn-sm" onClick={() => setAdding(true)}>+ Add category</button>
      )}

      <div className="flex items-center gap-3">
        <button className="btn-primary" disabled={!dirty || saving}
          onClick={async () => { await onSave(list); }}>
          {saving ? "Saving…" : "Save categories"}
        </button>
        {dirty && <span className="text-xs text-amber-500">Unsaved changes</span>}
      </div>
    </div>
  );
}

/* ── Move a folder into a category ───────────────────────────────────────── */

function MoveFolderCard({ cats }: { cats: MediaCategory[] }) {
  const toast = useToast();
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<number | null>(null);
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const move = async () => {
    if (!source || !target) return;
    setBusy(true);
    try {
      await admin.moveFolder(source, target);
      toast("Moving… this can take a while across drives", "info");
      pollRef.current = window.setInterval(async () => {
        const st = await admin.moveStatus().catch(() => null);
        if (st && !st.running) {
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
          setBusy(false);
          if (st.error) toast(`Move failed: ${st.error}`, "error");
          else { toast(`Moved to ${st.target} (${st.moved_jobs} item(s) updated)`, "success"); setSource(""); }
        }
      }, 2000);
    } catch (err: unknown) {
      setBusy(false);
      toast(err instanceof Error ? err.message : "Move failed", "error");
    }
  };

  return (
    <div className="card space-y-3">
      <h2 className="font-semibold">Move a folder into a category</h2>
      <p className="text-sm text-gray-500">
        Relocate an already-downloaded title (or any folder under the media root) into another
        category's folder — e.g. move a show from <span className="text-gray-300">Shows</span> into{" "}
        <span className="text-gray-300">Anime Shows</span>. Playback keeps working; the library records
        are updated to the new location and category.
      </p>
      <div className="flex items-end gap-2 flex-wrap">
        <div className="min-w-64 flex-1">
          <label className="label">Folder to move</label>
          <FolderField value={source} onChange={setSource} browseTitle="Choose the folder to move" />
        </div>
        <div>
          <label className="label">Into category</label>
          <select className="input h-9 text-sm py-1" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">— choose —</option>
            {cats.map((c) => <option key={c.key} value={c.key}>{c.label} ({c.folder}/)</option>)}
          </select>
        </div>
        <button className="btn-primary" onClick={move} disabled={busy || !source || !target}>
          {busy ? "Moving…" : "Move"}
        </button>
      </div>
    </div>
  );
}
