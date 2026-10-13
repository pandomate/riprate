import { useEffect, useMemo, useState } from "react";
import {
  setup, MediaCategory, SearchSource, Quality, SetupConfig, SetupPayload,
} from "../api/client";
import { useToast } from "../components/ui";
import { ICheck, IChevron, ITrash } from "../components/icons";
import { OpenVpnProfiles } from "../components/OpenVpnProfiles";

const QUALITIES: Array<[Quality, string]> = [
  [480, "480p"], [720, "720p"], [1080, "1080p"], [1440, "1440p"], [2160, "4K"],
];

const SUBTITLE_OPTIONS: Array<[string, string]> = [
  ["en", "English"], ["es", "Spanish"], ["fr", "French"], ["de", "German"],
  ["pt", "Portuguese"], ["it", "Italian"], ["ja", "Japanese"], ["ko", "Korean"],
  ["zh", "Chinese"], ["ar", "Arabic"], ["ru", "Russian"], ["hi", "Hindi"],
];

const SEARCH_OPTIONS: Array<{ value: SearchSource; label: string; hint: string }> = [
  { value: "tmdb", label: "TMDB", hint: "Search titles and episodes by TMDB ID. Required by Nova." },
  { value: "imdb", label: "IMDB", hint: "Search with IMDB scraping. Nova still requires a TMDB key to resolve streams." },
];

// libx264 -preset values, fastest → smallest
const PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"];

interface Form {
  admin_username: string;
  admin_password: string;
  admin_password2: string;
  categories: MediaCategory[];
  preferred_quality: Quality;
  search_source: SearchSource;
  tmdb_api_key: string;
  imdb_cookie: string;
  wyzie_api_key: string;
  wyzie_enabled: boolean;
  subtitle_language: string;
  max_concurrent_downloads: number;
  segment_connections: number;
  history_retention_days: number;
  video_crf: number;
  video_preset: string;
  max_file_gb: number;
  providers: Array<{ key: string; label: string; enabled: boolean }>;
}

const STEP_TITLES = [
  "Admin account", "Media folders", "Download quality", "Search source",
  "Downloads", "Subtitles", "Providers", "VPN", "Review",
];

const builtin = (key: string) =>
  key === "movies" || key === "shows" || key === "anime_movies" || key === "anime_shows";

export default function Setup({ onComplete }: { onComplete: () => void }) {
  const toast = useToast();
  const [cfg, setCfg] = useState<SetupConfig | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [step, setStep] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState<Form | null>(null);

  useEffect(() => {
    setup.status()
      .then((s) => {
        if (!s.needs_setup || !s.config) { onComplete(); return; }
        const c = s.config;
        setCfg(c);
        setForm({
          admin_username: c.admin_username || "admin",
          admin_password: "",
          admin_password2: "",
          categories: c.media_categories?.length
            ? c.media_categories
            : [{ key: "movies", label: "Movies", folder: "Movies" },
               { key: "shows", label: "Shows", folder: "Shows" },
               { key: "anime_movies", label: "Anime Movies", folder: "Anime Movies" },
               { key: "anime_shows", label: "Anime Shows", folder: "Anime Shows" }],
          preferred_quality: c.preferred_quality || 1080,
          search_source: c.search_source || "tmdb",
          tmdb_api_key: "",
          imdb_cookie: "",
          wyzie_api_key: "",
          wyzie_enabled: c.wyzie_enabled ?? false,
          subtitle_language: c.subtitle_language || "en",
          max_concurrent_downloads: c.max_concurrent_downloads || 3,
          segment_connections: c.segment_connections || 6,
          history_retention_days: c.history_retention_days ?? 0,
          video_crf: c.video_crf ?? 20,
          video_preset: c.video_preset || "medium",
          max_file_gb: c.max_file_gb ?? 10,
          providers: (c.stream_providers || []).map((p) => ({ key: p.key, label: p.label, enabled: p.enabled })),
        });
      })
      .catch((e) => setLoadErr(e instanceof Error ? e.message : "Could not load setup"));
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const up = (patch: Partial<Form>) => setForm((f) => (f ? { ...f, ...patch } : f));

  const wantsTmdb = form?.search_source === "tmdb";
  const tmdbReady = !!(form?.tmdb_api_key.trim() || cfg?.tmdb_api_key_set);

  const canNext = useMemo(() => {
    if (!form) return false;
    switch (step) {
      case 0: {
        if (form.admin_username.trim().length < 3) return false;
        if (!cfg?.admin_password_set || form.admin_password) {
          if (form.admin_password.length < 12) return false;
          if (form.admin_password !== form.admin_password2) return false;
        }
        return true;
      }
      case 1:
        return form.categories.every((c) => c.label.trim() && c.folder.trim());
      case 3:
        return !((form.search_source === "tmdb" || form.providers.some((p) => p.key === "nova" && p.enabled)) && !tmdbReady);
      default:
        return true;
    }
  }, [form, step, cfg]);

  const submit = async () => {
    if (!form) return;
    setSubmitting(true);
    try {
      const payload: SetupPayload = {
        admin_username: form.admin_username.trim(),
        media_categories: form.categories,
        preferred_quality: form.preferred_quality,
        search_source: form.search_source,
        wyzie_enabled: form.wyzie_enabled,
        subtitle_language: form.subtitle_language,
        max_concurrent_downloads: form.max_concurrent_downloads,
        segment_connections: form.segment_connections,
        history_retention_days: form.history_retention_days,
        video_crf: form.video_crf,
        video_preset: form.video_preset,
        max_file_gb: form.max_file_gb,
        stream_providers: form.providers.map((p) => ({ key: p.key, enabled: p.enabled })),
      };
      if (form.admin_password) payload.admin_password = form.admin_password;
      if (wantsTmdb && form.tmdb_api_key.trim()) payload.tmdb_api_key = form.tmdb_api_key.trim();
      if (form.search_source === "imdb" && form.imdb_cookie.trim()) payload.imdb_cookie = form.imdb_cookie.trim();
      if (form.wyzie_api_key.trim()) payload.wyzie_api_key = form.wyzie_api_key.trim();
      const res = await setup.submit(payload);
      toast("Setup complete — you can now log in.", "success");
      setTimeout(onComplete, 700);
    } catch (err: unknown) {
      // A previous submission may have completed on the server even if this
      // browser did not receive its response (for example after a restart).
      // Leave the stale wizard instead of showing a second, misleading error.
      const apiErr = err as { status?: number; message?: string };
      if (apiErr.status === 409 && apiErr.message === "Setup has already been completed") {
        onComplete();
        return;
      }
      toast(err instanceof Error ? err.message : "Setup failed", "error");
      setSubmitting(false);
    }
  };

  if (loadErr) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-950 px-4">
        <div className="card max-w-md text-center space-y-2">
          <p className="text-red-400 font-medium">Couldn't start setup</p>
          <p className="text-sm text-gray-500">{loadErr}</p>
          <button className="btn-secondary" onClick={() => window.location.reload()}>Retry</button>
        </div>
      </div>
    );
  }
  if (!form || !cfg) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="text-gray-500 text-lg animate-pulse">Loading…</div>
      </div>
    );
  }

  const last = STEP_TITLES.length - 1;

  return (
    <div className="min-h-screen bg-gray-950 px-4 py-10 flex justify-center">
      <div className="w-full max-w-xl">
        <div className="text-center mb-6">
          <h1 className="text-3xl font-bold text-sky-400 tracking-tight">RipRate</h1>
          <p className="text-gray-500 text-sm mt-1">First-time setup</p>
        </div>

        {/* Step rail */}
        <div className="flex items-center gap-1.5 mb-5 flex-wrap justify-center">
          {STEP_TITLES.map((t, i) => (
            <button key={t} onClick={() => i < step && setStep(i)}
              className={`text-[11px] px-2 py-1 rounded-full border transition-colors ${
                i === step ? "border-sky-500 text-sky-300 bg-sky-500/10"
                : i < step ? "border-gray-700 text-gray-400 hover:text-gray-200"
                : "border-gray-800 text-gray-600"}`}>
              {i < step ? <span className="inline-flex items-center gap-1"><ICheck size={11} /> {t}</span> : `${i + 1}. ${t}`}
            </button>
          ))}
        </div>

        <div className="card space-y-4">
          <h2 className="font-semibold text-lg">{STEP_TITLES[step]}</h2>

          {/* ── 0. Admin account ── */}
          {step === 0 && (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">Create the administrator account. It has full control of the app.</p>
              <div>
                <label className="label">Admin username</label>
                <input className="input" value={form.admin_username} autoFocus autoComplete="off"
                  onChange={(e) => up({ admin_username: e.target.value })} />
              </div>
              {cfg.admin_password_set && (
                <p className="text-xs text-amber-400/90 rounded-md bg-amber-500/10 border border-amber-800/50 px-3 py-2">
                  A password is already configured in <span className="font-mono">.env</span>. Leave the fields blank to keep it, or enter a new one to update it.
                </p>
              )}
              <div>
                <label className="label">Password {!cfg.admin_password_set && <span className="text-red-400">*</span>}</label>
                <input className="input" type="password" value={form.admin_password} autoComplete="new-password"
                  placeholder={cfg.admin_password_set ? "(keep existing)" : "At least 12 characters"}
                  onChange={(e) => up({ admin_password: e.target.value })} />
              </div>
              <div>
                <label className="label">Confirm password</label>
                <input className="input" type="password" value={form.admin_password2} autoComplete="new-password"
                  onChange={(e) => up({ admin_password2: e.target.value })} />
              </div>
              {form.admin_password && form.admin_password.length < 12 &&
                <p className="text-xs text-red-400">Password must be at least 12 characters.</p>}
              {form.admin_password2 && form.admin_password !== form.admin_password2 &&
                <p className="text-xs text-red-400">Passwords don't match.</p>}
            </div>
          )}

          {/* ── 1. Media folders ── */}
          {step === 1 && (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">
                Categories tell RipRate where downloads are saved (as subfolders of the media root). Movies, Shows, Anime Movies, and Anime Shows are the automatic defaults.
              </p>
              <div className="space-y-1.5">
                {form.categories.map((c, i) => (
                  <div key={c.key} className="flex items-center gap-2 rounded-lg border border-gray-800 bg-gray-800/30 px-3 py-2">
                    <input className="input h-8 text-sm py-1 w-32" value={c.label}
                      placeholder="Name"
                      onChange={(e) => up({ categories: form.categories.map((x, idx) => idx === i ? { ...x, label: e.target.value } : x) })} />
                    <span className="text-gray-600">→</span>
                    <input className="input h-8 text-sm py-1 flex-1 font-mono" value={c.folder}
                      placeholder="Folder"
                      onChange={(e) => up({ categories: form.categories.map((x, idx) => idx === i ? { ...x, folder: e.target.value } : x) })} />
                    {builtin(c.key)
                      ? <span className="badge-neutral flex-shrink-0">default</span>
                      : <button className="p-1 rounded text-red-400/80 hover:text-red-300 hover:bg-gray-700/60"
                          title="Remove category"
                          onClick={() => up({ categories: form.categories.filter((_, idx) => idx !== i) })}>
                          <ITrash size={14} />
                        </button>}
                  </div>
                ))}
              </div>
              <button className="btn-secondary btn-sm"
                onClick={() => {
                  const key = `cat-${Date.now().toString(36)}`;
                  up({ categories: [...form.categories, { key, label: "", folder: "" }] });
                }}>
                + Add category
              </button>
            </div>
          )}

          {/* ── 2. Download quality ── */}
          {step === 2 && (
            <div className="space-y-4">
              <div>
                <p className="text-sm text-gray-500 mb-2">Preferred resolution. RipRate picks the closest available, stepping down if needed.</p>
                <div className="flex flex-wrap gap-2">
                  {QUALITIES.map(([q, label]) => (
                    <button key={q}
                      className={`px-4 py-2 rounded-lg border text-sm ${
                        form.preferred_quality === q ? "border-sky-500 bg-sky-500/10 text-sky-300" : "border-gray-700 text-gray-400 hover:text-gray-200"}`}
                      onClick={() => up({ preferred_quality: q })}>
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="border-t border-gray-800 pt-3 space-y-3">
                <p className="text-sm text-gray-400 font-medium">Re-encoding</p>
                <p className="text-xs text-gray-500 -mt-1.5">
                  Streams are copied to MP4 as-is when possible. These only apply when a
                  re-encode is needed (incompatible codec, or the file is over the size limit).
                </p>
                <div>
                  <label className="label">Max file size before shrinking: {form.max_file_gb} GB</label>
                  <input type="number" min={0.5} max={200} step={0.5} value={form.max_file_gb}
                    className="input w-32"
                    onChange={(e) => up({ max_file_gb: Math.max(0.5, Math.min(200, Number(e.target.value) || 10)) })} />
                  <p className="text-xs text-gray-500">Over this, re-encode with libx264 to shrink. A 2h 1080p movie is ~6–8 GB.</p>
                </div>
                <div>
                  <label className="label">Quality (CRF): {form.video_crf}</label>
                  <input type="range" min={16} max={30} value={form.video_crf}
                    className="w-full accent-sky-500"
                    onChange={(e) => up({ video_crf: Number(e.target.value) })} />
                  <p className="text-xs text-gray-500">Lower = better quality, bigger file. 18–23 is the sweet spot.</p>
                </div>
                <div>
                  <label className="label">Encoding speed (preset)</label>
                  <select className="input w-44" value={form.video_preset}
                    onChange={(e) => up({ video_preset: e.target.value })}>
                    {PRESETS.map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                  <p className="text-xs text-gray-500">Slower presets = smaller file, same quality, more CPU time.</p>
                </div>
              </div>
            </div>
          )}

          {/* ── 3. Search source ── */}
          {step === 3 && (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">Choose the source used for title, season, and episode searches.</p>
              <div className="space-y-2">
                {SEARCH_OPTIONS.map((o) => (
                  <button key={o.value}
                    className={`w-full text-left px-3 py-2.5 rounded-lg border ${
                      form.search_source === o.value ? "border-sky-500 bg-sky-500/10" : "border-gray-700 hover:border-gray-600"}`}
                    onClick={() => up({ search_source: o.value })}>
                    <div className="flex items-center gap-2">
                      <span className={`w-3.5 h-3.5 rounded-full border flex-shrink-0 ${form.search_source === o.value ? "border-sky-400 bg-sky-400" : "border-gray-500"}`} />
                      <span className="text-sm font-medium">{o.label}</span>
                    </div>
                    <p className="text-xs text-gray-500 mt-0.5 ml-5">{o.hint}</p>
                  </button>
                ))}
              </div>
              {(wantsTmdb || form.providers.some((p) => p.key === "nova" && p.enabled)) && (
                <div>
                  <label className="label">TMDB API key (v4 bearer token)</label>
                  <input className="input font-mono text-sm" value={form.tmdb_api_key}
                    placeholder={cfg.tmdb_api_key_set ? "(already set — leave blank to keep)" : "eyJhbGciOi…"}
                    onChange={(e) => up({ tmdb_api_key: e.target.value })} />
                  {!tmdbReady && (
                    <p className="text-xs text-amber-400 mt-1">
                      A TMDB API key is required for the selected search/provider configuration.
                    </p>
                  )}
                </div>
              )}
              {form.search_source === "imdb" && (
                <div>
                  <label className="label">IMDB cookie <span className="text-gray-600">(optional — not required)</span></label>
                  <input className="input font-mono text-sm" value={form.imdb_cookie}
                    placeholder="Only if IMDB blocks requests"
                    onChange={(e) => up({ imdb_cookie: e.target.value })} />
                </div>
              )}
            </div>
          )}

          {/* ── 4. Downloads ── */}
          {step === 4 && (
            <div className="space-y-4">
              <div>
                <label className="label">Subtitle language</label>
                <select className="input w-48" value={form.subtitle_language}
                  onChange={(e) => up({ subtitle_language: e.target.value })}>
                  {SUBTITLE_OPTIONS.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
                </select>
                <p className="text-xs text-gray-500">Used when choosing embedded or external subtitles. English is the default.</p>
              </div>
              <div>
                <label className="label">Concurrent downloads: {form.max_concurrent_downloads}</label>
                <input type="range" min={1} max={10} value={form.max_concurrent_downloads}
                  className="w-full accent-sky-500"
                  onChange={(e) => up({ max_concurrent_downloads: Number(e.target.value) })} />
                <p className="text-xs text-gray-500">How many titles download at once.</p>
              </div>
              <div>
                <label className="label">Connections per download: {form.segment_connections}</label>
                <input type="range" min={1} max={16} value={form.segment_connections}
                  className="w-full accent-sky-500"
                  onChange={(e) => up({ segment_connections: Number(e.target.value) })} />
                <p className="text-xs text-gray-500">Parallel segment connections. 6–10 is a good range.</p>
              </div>
              <div>
                <label className="label">Download history retention (days)</label>
                <input type="number" min={0} max={3650} value={form.history_retention_days}
                  className="input w-32"
                  onChange={(e) => up({ history_retention_days: Math.max(0, Math.min(3650, Number(e.target.value) || 0)) })} />
                <p className="text-xs text-gray-500">
                  Auto-clean finished/failed history older than this. <span className="font-mono">0</span> = keep forever.
                </p>
              </div>
            </div>
          )}

          {/* ── 5. Subtitles (Wyzie) ── */}
          {step === 5 && (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">
                When enabled with an API key, Wyzie Subs is the preferred subtitle source.
                Stream provider subtitles are used if Wyzie is disabled, has no key, or cannot find subtitles.
                A free key comes from{" "}
                <span className="font-mono text-gray-400">store.wyzie.io/redeem</span>.
              </p>
              <div>
                <label className="label">Wyzie API key <span className="text-gray-600">(leave blank to keep the current key)</span></label>
                <input className="input font-mono text-sm" value={form.wyzie_api_key}
                  placeholder={cfg.wyzie_api_key_set ? "(already set — leave blank to keep)" : "wyzie key…"}
                  onChange={(e) => up({ wyzie_api_key: e.target.value })} />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" className="w-4 h-4 rounded accent-sky-500"
                  checked={form.wyzie_enabled} onChange={(e) => up({ wyzie_enabled: e.target.checked })} />
                Enable Wyzie subtitle downloads
              </label>
            </div>
          )}

          {/* ── 6. Providers ── */}
          {step === 6 && (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">Enable the stream sources to try when downloading. You can reorder them later in Settings.</p>
              <div className="space-y-1.5">
                {form.providers.map((p, i) => (
                  <label key={p.key} className="flex items-center gap-3 rounded-lg border border-gray-800 bg-gray-800/30 px-3 py-2 cursor-pointer">
                    <input type="checkbox" className="w-4 h-4 rounded accent-sky-500"
                      checked={p.enabled}
                      onChange={(e) => up({ providers: form.providers.map((x, idx) => idx === i ? { ...x, enabled: e.target.checked } : x) })} />
                    <span className="text-sm font-medium">{p.label}</span>
                  </label>
                ))}
                {form.providers.length === 0 && <p className="text-sm text-gray-600">No providers available.</p>}
              </div>
              {form.providers.every((p) => !p.enabled) &&
                <p className="text-xs text-amber-400">At least one provider should be enabled to download.</p>}
            </div>
          )}

          {/* ── 7. VPN (optional) ── */}
          {step === 7 && (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">
                Optional. Upload one or more OpenVPN <span className="font-mono">.ovpn</span> profiles.
                RipRate verifies the tunnel and exit IP before allowing downloads. Leave it off to
                configure it later in Settings.
              </p>
              <OpenVpnProfiles setupMode />
            </div>
          )}

          {/* ── 8. Review ── */}
          {step === 8 && (
            <div className="space-y-2 text-sm">
              <p className="text-gray-500">Review your setup, then finish. Everything is saved and mirrored to <span className="font-mono">.env</span>.</p>
              <Row k="Admin" v={form.admin_username} />
              <Row k="Password" v={form.admin_password ? "will be set" : "keep existing"} />
              <Row k="Categories" v={form.categories.map((c) => c.label).join(", ")} />
              <Row k="Quality" v={`${form.preferred_quality}p`} />
              <Row k="Re-encode" v={`CRF ${form.video_crf} · ${form.video_preset} · shrink over ${form.max_file_gb} GB`} />
              <Row k="Search source" v={form.search_source} />
              <Row k="Downloads" v={`${form.max_concurrent_downloads} concurrent · ${form.segment_connections} connections · ${form.subtitle_language} subtitles`} />
              <Row k="History retention" v={form.history_retention_days === 0 ? "keep forever" : `${form.history_retention_days} days`} />
              <Row k="Subtitles" v={form.wyzie_enabled && (form.wyzie_api_key.trim() || cfg.wyzie_api_key_set) ? "Wyzie first · stream fallback" : "Stream provider"} />
              <Row k="Providers" v={form.providers.filter((p) => p.enabled).map((p) => p.label).join(", ") || "none"} />
              <Row k="VPN" v="OpenVPN profile pool — optional" />
            </div>
          )}

          {/* Nav */}
          <div className="flex items-center justify-between pt-2 border-t border-gray-800">
            <button className="btn-ghost btn-sm inline-flex items-center gap-1" disabled={step === 0 || submitting}
              onClick={() => setStep((s) => Math.max(0, s - 1))}>
              <IChevron dir="left" size={14} /> Back
            </button>
            {step < last ? (
              <button className="btn-primary inline-flex items-center gap-1" disabled={!canNext}
                onClick={() => setStep((s) => s + 1)}>
                Next <IChevron dir="right" size={14} />
              </button>
            ) : (
              <button className="btn-primary" disabled={submitting} onClick={submit}>
                {submitting ? "Finishing…" : "Finish setup"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3 border-b border-gray-800/60 py-1">
      <span className="text-gray-500">{k}</span>
      <span className="text-gray-200 text-right truncate">{v}</span>
    </div>
  );
}
