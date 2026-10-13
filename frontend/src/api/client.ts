const BASE = "";  // same-origin — Vite proxies /api to :8000 in dev

// A broken proxy, backend restart, or stale connection must not leave a page
// showing its initial spinner forever. Reads fail quickly enough to render a
// useful error/empty state; write operations get longer because VPN changes
// and queue actions can legitimately take a while.
// Metadata providers are allowed up to 25 seconds. Keep the browser deadline
// above the backend deadline so users receive the backend's useful error
// instead of a generic AbortError first.
const GET_TIMEOUT_MS = 30_000;
const WRITE_TIMEOUT_MS = 120_000;

/** Thrown by `request()`. `detail` is the raw parsed error payload — usually
 * a string, but some endpoints (e.g. schedule conflicts) send a structured
 * object; `.message` is always a display-ready string either way. */
export interface ApiError extends Error {
  status?: number;
  detail?: unknown;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(
    () => controller.abort(),
    method === "GET" ? GET_TIMEOUT_MS : WRITE_TIMEOUT_MS,
  );
  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, {
      method,
      credentials: "include",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (err: unknown) {
    if (controller.signal.aborted) {
      throw new Error("Request timed out. Check that RipRate is running and reachable.");
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const message = typeof err.detail === "string"
      ? err.detail
      : (err.detail && typeof err.detail === "object" && "message" in err.detail
          ? String((err.detail as { message?: unknown }).message)
          : "Request failed");
    const e = new Error(message) as ApiError;
    e.status = resp.status;
    e.detail = err.detail;
    throw e;
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

const encodeRelPath = (rel: string) => rel.split("/").map(encodeURIComponent).join("/");

export const api = {
  get:    <T>(path: string) => request<T>("GET", path),
  post:   <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch:  <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
};

// ── Auth ──────────────────────────────────────────────────────────────────

export type Role = "admin" | "user";

export interface User {
  username: string;
  role: Role;
}

export const auth = {
  login: (username: string, password: string) =>
    api.post<{ ok: boolean; username: string; role: Role }>("/api/auth/login", { username, password }),
  logout: () => api.post<{ ok: boolean }>("/api/auth/logout"),
  me: () =>
    api.get<{ username: string | null; role: Role | null; authenticated: boolean }>("/api/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    api.post<{ ok: boolean }>("/api/auth/change-password", { current_password, new_password }),
};

// ── Search ────────────────────────────────────────────────────────────────

export interface SearchResult {
  imdb_id: string;
  title: string;
  media_type: "movie" | "tvSeries";
  year: number | null;
  poster_url: string | null;
  cast: string;
  is_anime: boolean;
  original_language: string | null;
}

export interface RecentTitles { movies: SearchResult[]; shows: SearchResult[] }

export const search = {
  query: (q: string) => api.get<SearchResult[]>(`/api/search?q=${encodeURIComponent(q)}`),
  seasons: (imdb_id: string) => api.get<{ seasons: number[] }>(`/api/seasons/${imdb_id}`),
  episodes: (imdb_id: string, season: number) =>
    api.get<{ season: number; episodes: Array<{ episode: number; title: string }> }>(
      `/api/episodes/${imdb_id}/${season}`
    ),
  // Recent clicked titles (per-user, DB-backed search history)
  recent:       () => api.get<RecentTitles>("/api/search/recent"),
  recordRecent: (item: SearchResult) => api.post<RecentTitles>("/api/search/recent", item),
  removeRecent: (imdb_id: string) => api.delete<RecentTitles>(`/api/search/recent/${encodeURIComponent(imdb_id)}`),
  clearRecent:  () => api.delete<RecentTitles>("/api/search/recent"),
};

// ── Media categories (Movies/Shows/Anime Movies/Anime Shows + custom) ───────

/** A category is a download destination: it tells the program which folder
    under /media/library a title's files belong in. Movies, Shows, Anime
    Movies, and Anime Shows are the built-in defaults; every category is
    offered for every download. */
export interface MediaCategory {
  key: string;
  label: string;
  folder: string;                    // folder under /media/library
}

export const media = {
  categories: () =>
    api.get<{ categories: MediaCategory[]; defaults: { movie: string; tvSeries: string } }>("/api/categories"),
};

// Map of imdb_id → { type, episodes: [{season, episode}] }
export type DownloadedMap = Record<
  string,
  { type: string; episodes: Array<{ season: number; episode: number }> }
>;

export const collection = {
  downloaded: () => api.get<DownloadedMap>("/api/downloaded"),
};

// ── Watch (local playback) ──────────────────────────────────────────────────

export interface WatchItem {
  id: number;
  imdb_id: string;
  title: string;
  year: number | null;
  media_type: string;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  size: number | null;
  has_subtitle: boolean;
  mine: boolean;
  position: number;              // seconds, this user's saved position
  duration: number;              // seconds, 0 = never played
  watched: boolean;              // past the server-side watched threshold
  progress_updated_at: string | null;
  favorite: boolean;             // title-level (imdb_id) favorite
}

/** Locally-cached TMDB metadata for a title (never triggers a TMDB call). */
export interface TitleMeta {
  imdb_id: string;
  tmdb_id: number | null;
  media_type: string;
  title: string | null;
  year: number | null;
  overview: string;
  tagline: string;
  genres: string[];
  rating: number | null;
  runtime: number | null;
  original_language: string | null;
  original_title: string | null;
  poster: string | null;      // filename — render via watch.metaImageUrl
  backdrop: string | null;
}

export interface WatchList {
  items: WatchItem[];
  titles: Record<string, TitleMeta>;
}

/** Cached per-episode TMDB metadata, keyed "season:episode". */
export interface EpisodeMeta {
  season: number;
  episode: number;
  title: string | null;
  overview: string;
  air_date: string | null;
  rating: number | null;
  runtime: number | null;
  still: string | null;   // filename — render via watch.metaImageUrl
}

export interface FolderFile {
  name: string;
  size: number;
  season: number | null;    // parsed from SxxEyy in the filename (null = shared file)
  episode: number | null;
}
export interface FolderFiles { folder: string; files: FolderFile[] }

export const watch = {
  list:         () => api.get<WatchList>("/api/watch/list"),
  streamUrl:    (id: number) => `/api/watch/${id}`,
  subtitleUrl:  (id: number) => `/api/watch/${id}/subtitle`,
  downloadUrl:  (id: number) => `/api/watch/${id}/download`,
  metaImageUrl: (name: string) => `/api/watch/meta-image/${name}`,
  episodeMeta:  (imdb_id: string) => api.get<Record<string, EpisodeMeta>>(`/api/watch/episode-meta/${imdb_id}`),
  saveProgress: (id: number, position: number, duration: number) =>
    api.post<{ ok: boolean; watched: boolean }>(`/api/watch/${id}/progress`, { position, duration }),
  // keepalive lets the final save survive the player/tab closing mid-request
  saveProgressBeacon: (id: number, position: number, duration: number) => {
    void fetch(`/api/watch/${id}/progress`, {
      method: "POST", credentials: "include", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position, duration }),
    }).catch(() => {});
  },
  clearProgress: (id: number) => api.delete<{ ok: boolean }>(`/api/watch/${id}/progress`),
  toggleFavorite: (imdb_id: string) =>
    api.post<{ favorited: boolean }>("/api/watch/favorite", { imdb_id }),

  // Files + zip downloads at three scopes: one item (movie/episode), one
  // season, the whole show. Every zip takes mp4=true to grab just the videos.
  files:            (jobId: number) => api.get<FolderFiles>(`/api/watch/${jobId}/files`),
  itemFiles:        (jobId: number) => api.get<FolderFiles>(`/api/watch/${jobId}/item-files`),
  fileDownloadUrl:  (jobId: number, relPath: string) => `/api/watch/${jobId}/files/${encodeRelPath(relPath)}`,
  zipUrl:           (jobId: number, mp4 = false) => `/api/watch/${jobId}/zip${mp4 ? "?mp4=true" : ""}`,
  itemZipUrl:       (jobId: number) => `/api/watch/${jobId}/item-zip`,

  seasonFiles:  (imdb_id: string, season: number) =>
    api.get<FolderFiles>(`/api/watch/season-files/${imdb_id}/${season}`),
  seasonZipUrl: (imdb_id: string, season: number, mp4 = false) =>
    `/api/watch/season-zip/${imdb_id}/${season}${mp4 ? "?mp4=true" : ""}`,

  showFiles:           (imdb_id: string) => api.get<FolderFiles>(`/api/watch/show-files/${imdb_id}`),
  showFileDownloadUrl: (imdb_id: string, relPath: string) => `/api/watch/show-files/${imdb_id}/${encodeRelPath(relPath)}`,
  showZipUrl:          (imdb_id: string, mp4 = false) => `/api/watch/show-zip/${imdb_id}${mp4 ? "?mp4=true" : ""}`,
};

export type SubtitleSyncState = {
  status: "idle" | "running" | "completed" | "failed";
  message: string;
};

export const subtitleTiming = {
  startAutoSync: (id: number) =>
    api.post<SubtitleSyncState>(`/api/admin/subtitles/${id}/sync`),
  syncStatus: (id: number) =>
    api.get<SubtitleSyncState>(`/api/admin/subtitles/${id}/sync`),
  saveOffset: (id: number, offset: number) =>
    api.post<{ ok: boolean }>(`/api/admin/subtitles/${id}/timing`, { offset, scale: 1 }),
  restore: (id: number) =>
    api.post<{ ok: boolean }>(`/api/admin/subtitles/${id}/restore`),
};

// ── Sidebar badge counts (role-scoped server-side) ──────────────────────────

export interface NavCounts { requests: number; downloads: number; downloading?: number }

export const nav = {
  counts: () => api.get<NavCounts>("/api/nav/counts"),
};

// ── Requests (users create, admin decides) ─────────────────────────────────

export type RequestStatus = "pending" | "approved" | "denied";

export interface RequestQueueSummary {
  total_jobs: number;
  completed: number;
  failed: number;
  in_queue: boolean;
  downloading: number;
  processing: number;
  queued: number;
  progress: number;
}

export interface MediaRequest {
  id: number;
  imdb_id: string;
  title: string;
  year: number | null;
  media_type: "movie" | "tvSeries";
  is_anime: boolean;
  seasons: number[];
  status: RequestStatus;
  deny_reason: string | null;
  created_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  queue: RequestQueueSummary | null;
  requested_by?: string;
}

export const requests = {
  create: (payload: {
    imdb_id: string; title: string; year: number | null;
    media_type: "movie" | "tvSeries"; seasons?: number[];
    is_anime?: boolean;
  }) => api.post<MediaRequest>("/api/requests", payload),
  mine: () => api.get<MediaRequest[]>("/api/requests/mine"),
  cancel: (id: number) => api.delete<{ ok: boolean }>(`/api/requests/${id}`),
  // admin
  all: () => api.get<{ pending: MediaRequest[]; decided: MediaRequest[] }>("/api/admin/requests"),
  approve: (id: number) =>
    api.post<{ ok: boolean; queued: number; errors: string[] }>(`/api/admin/requests/${id}/approve`),
  deny: (id: number, reason: string) =>
    api.post<{ ok: boolean }>(`/api/admin/requests/${id}/deny`, { reason }),
};

// ── Download (admin only) ───────────────────────────────────────────────────

export const download = {
  single:  (payload: object) =>
    api.post<{ job_id: number; status: string; skipped: boolean; reason?: string }>("/api/download", payload),
  bulk:    (payload: object) =>
    api.post<{ queued: number; skipped: number }>("/api/download/bulk", payload),
  seasons: (payload: object) =>
    api.post<{ queued: number; skipped: number; errors: string[] }>("/api/download/seasons", payload),
};

// ── Admin: queue / library / users ──────────────────────────────────────────

export type JobStatus =
  | "queued" | "downloading" | "processing" | "paused" | "completed" | "failed" | "missing";

export interface Job {
  id: number;
  label: string;
  title: string;
  year: number | null;
  imdb_id: string;
  media_type: string;
  category: string | null;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  status: JobStatus;
  progress: number;
  position: number;
  file_path: string | null;
  has_subtitle: boolean;
  error_message: string | null;
  downloaded_bytes: number | null;
  total_bytes: number | null;
  download_bitrate: string | null;
  source_url: string | null;
  current_source: string | null;
  request_id: number | null;
  requested_by: string | null;
  added_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  log?: string[];
}

export interface Stats {
  total: number;
  completed: number;
  failed: number;
  queued: number;
  downloading: number;
  processing: number;
  movies: number;
  shows: number;
  episodes: number;
  globally_paused: boolean;
  network_ok: boolean;
  max_slots: number;
  active_slots: number;
  free_slots: number;
  vpn_gated: boolean;          // VPN not active → downloads paused
  vpn_active: boolean;         // VPN configured + enabled + tunnel up
  public_ip: string | null;    // cached exit/public IP (not pinged per tick)
}

export type MoveDir = "up" | "down" | "top" | "bottom";
export type BulkAction = "pause" | "resume" | "cancel" | "delete" | "retry";

export interface HistoryPage { items: Job[]; total: number; page: number; per: number }
export type HistoryStatus = "all" | "completed" | "failed";

export const admin = {
  queue:   () => api.get<Job[]>("/api/admin/queue"),
  history: (status: HistoryStatus = "all", page = 0, per = 10, q = "") =>
    api.get<HistoryPage>(`/api/admin/history?status=${status}&page=${page}&per=${per}&q=${encodeURIComponent(q)}`),
  retryFailed: (q = "") => api.post<{ ok: boolean; count: number }>("/api/admin/queue/retry-failed", { q }),
  library: () => api.get<Job[]>("/api/admin/library"),
  gaps:    (imdb_id: string) => api.get<{ seasons: SeasonGap[] }>(`/api/admin/library/show/${imdb_id}/gaps`),
  stats:   () => api.get<Stats>("/api/admin/stats"),
  audit:   (page = 0, per = 50) => api.get<AuditPage>(`/api/admin/audit?page=${page}&per=${per}`),
  move:    (id: number, direction: MoveDir) => api.post<{ ok: boolean }>(`/api/admin/queue/move/${id}`, { direction }),
  reorder: (job_ids: number[]) => api.post<{ ok: boolean }>("/api/admin/queue/reorder", { job_ids }),
  bulk:    (action: BulkAction, job_ids: number[]) =>
    api.post<{ ok: boolean; count: number }>("/api/admin/queue/bulk", { action, job_ids }),
  pauseAll:  () => api.post<{ ok: boolean; globally_paused: boolean }>("/api/admin/queue/pause-all"),
  resumeAll: () => api.post<{ ok: boolean; globally_paused: boolean }>("/api/admin/queue/resume-all"),
  promote: (id: number) => api.post<{ ok: boolean }>(`/api/admin/promote/${id}`),
  swap:    (queued_id: number, active_id: number) =>
    api.post<{ ok: boolean }>("/api/admin/queue/swap", { queued_id, active_id }),
  fail:    (id: number) => api.post<{ ok: boolean }>(`/api/admin/fail/${id}`),
  purge:   (id: number) => api.post<{ ok: boolean }>(`/api/admin/purge/${id}`),
  retry:   (id: number) => api.post<{ ok: boolean }>(`/api/admin/retry/${id}`),
  retrySubtitles: (id: number) =>
    api.post<{ ok: boolean; language: string; source: string; filename: string }>(`/api/admin/subtitles/${id}/retry`),
  pause:   (id: number) => api.post<{ ok: boolean }>(`/api/admin/pause/${id}`),
  resume:  (id: number) => api.post<{ ok: boolean }>(`/api/admin/resume/${id}`),
  cancel:  (id: number) => api.post<{ ok: boolean }>(`/api/admin/cancel/${id}`),
  skipSource: (id: number) => api.post<{ ok: boolean }>(`/api/admin/skip-source/${id}`),
  delete:  (id: number) => api.delete<{ ok: boolean; kept_file: boolean; archived: boolean }>(`/api/admin/job/${id}`),
  mediaTree:  () => api.get<MediaTree>("/api/admin/media-tree"),
  moveFolder: (source: string, category: string) =>
    api.post<{ ok: boolean; started: boolean; target: string }>("/api/admin/library/move-folder", { source, category }),
  moveStatus: () => api.get<MoveStatus>("/api/admin/library/move-status"),
  fsList:  (path = "") => api.get<FsListing>(`/api/admin/fs?path=${encodeURIComponent(path)}`),
  fsMkdir: (path: string) => api.post<{ ok: boolean; path: string }>("/api/admin/fs/mkdir", { path }),
  fsDelete: (path: string, confirmName: string) =>
    api.post<{ ok: boolean; path: string }>("/api/admin/fs/delete", { path, confirm_name: confirmName }),
};

export interface FsDir {
  name: string;
  path: string;                  // rel path under the media root
  is_mount: boolean;
  category: string | null;       // category label already mapped to this folder
  free_gb?: number;
  total_gb?: number;
}

export interface FsListing {
  root: string;                  // absolute media root inside the container
  path: string;                  // current rel path ("" = root)
  parent: string | null;         // rel path of parent (null at root)
  dirs: FsDir[];
}

export interface MediaTreeFolder {
  name: string;
  is_mount: boolean;              // a dedicated host mount (MOUNT_* in .env)
  category: string | null;        // category key currently mapped to this folder
  free_gb: number;
  total_gb: number;
  children: string[];             // title folders inside
}

export interface MediaTree {
  root: string;
  root_is_mount: boolean;
  root_free_gb: number;
  root_total_gb: number;
  folders: MediaTreeFolder[];
}

export interface MoveStatus {
  running: boolean;
  source: string | null;
  target: string | null;
  moved_jobs: number;
  error: string | null;
}

export interface AuditEntry {
  ts: string | null;
  username: string | null;
  action: string;
  detail: string | null;
  ip: string | null;
}

export interface AuditPage { items: AuditEntry[]; total: number; page: number; per: number }

// ── Library file management (admin) ─────────────────────────────────────────

export interface LibraryItem {
  id: number;                 // job id — usable with watch.downloadUrl()
  imdb_id: string;
  title: string;
  year: number | null;
  media_type: string;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  added_at: string | null;
  requested_by_me: boolean;
}

export const userLibrary = {
  browse: () => api.get<LibraryItem[]>("/api/library/browse"),
};

export const library = {
  deleteFile:   (id: number) =>
    api.delete<{ ok: boolean }>(`/api/admin/job/${id}?delete_file=true`),
  renameOneFile: (job_id: number, rel_path: string, new_name: string) =>
    api.post<{ ok: boolean; name: string }>("/api/admin/library/file/rename", { job_id, rel_path, new_name }),
  deleteOneFile: (job_id: number, rel_path: string) =>
    api.post<{ ok: boolean; deleted_job: boolean }>("/api/admin/library/file/delete", { job_id, rel_path }),
  deleteSeason: (imdb_id: string, season: number) =>
    api.delete<{ ok: boolean; deleted: number }>(`/api/admin/library/show/${imdb_id}/season/${season}`),
  deleteShow:   (imdb_id: string) =>
    api.delete<{ ok: boolean; deleted: number }>(`/api/admin/library/show/${imdb_id}`),
  renameJob:    (id: number, body: { title?: string; episode_title?: string; year?: number }) =>
    api.patch<{ ok: boolean; new_path: string }>(`/api/admin/library/job/${id}`, body),
  renameShow:   (imdb_id: string, body: { title: string; year?: number }) =>
    api.patch<{ ok: boolean }>(`/api/admin/library/show/${imdb_id}`, body),
};

// ── Users (admin) ───────────────────────────────────────────────────────────

export interface ManagedUser {
  id: number;
  username: string;
  role: Role;
  is_active: boolean;
  locked: boolean;
  last_login_at: string | null;
  created_at: string | null;
}

// ── Settings (admin) ────────────────────────────────────────────────────────

export interface StreamProvider {
  key: string;
  label: string;
  enabled: boolean;
  requires_source?: SearchSource | null;
  available?: boolean;
}

export type SearchSource = "tmdb" | "imdb";

export type Quality = 480 | 720 | 1080 | 1440 | 2160;

export interface AppSettings {
  history_retention_days: number;
  failed_temp_retention_hours: number;
  max_concurrent_downloads: number;
  imdb_cookie_set: boolean;
  wyzie_api_key_set: boolean;
  wyzie_enabled: boolean;
  subtitle_language: string;
  subtitle_auto_sync: boolean;
  tmdb_api_key_set: boolean;
  search_source: SearchSource;
  watched_threshold_percent: number;
  stream_providers: StreamProvider[];
  media_root: string;          // read-only (set via .env MEDIA_DIR)
  movies_folder: string;       // legacy — mirrors media_categories
  shows_folder: string;
  media_categories: MediaCategory[];
  preferred_quality: Quality;
  segment_connections: number;   // parallel connections per download (1-16)
  video_crf: number;             // libx264 CRF used when a re-encode is needed
  video_preset: string;          // libx264 preset (ultrafast…veryslow)
  max_file_gb: number;           // over this size → re-encode to shrink
}

// ── OpenVPN profile pool ───────────────────────────────────────────────────

export interface OpenVpnRemote { host: string; port: number; protocol: string }
export interface OpenVpnProfile {
  id: string; name: string; profile_type: "country" | "individual"; group: string;
  remotes: OpenVpnRemote[]; enabled: boolean;
  has_credentials: boolean; created_at: string | null;
  last_connected_at: string | null; last_error: string | null;
}
export interface OpenVpnStatus {
  configured: boolean; enabled: boolean; running: boolean;
  connection_state: "off" | "connecting" | "connected";
  active_profile_id: string | null; active_profile_name: string | null;
  active_source: OpenVpnRemote | null;
  public_ip: string | null; kill_switch: boolean; last_error: string | null;
  profiles: OpenVpnProfile[];
}
export interface OpenVpnResult {
  ok: boolean; message: string; profile_id: string | null;
  profile_name: string | null; public_ip: string | null;
  source: OpenVpnRemote | null;
  attempts: Array<{ profile_id: string; profile_name: string; source?: OpenVpnRemote; ok: boolean; message: string }>;
  at: string; pending: boolean;
}

function openvpnApi(base: string) {
  return {
    status: () => api.get<OpenVpnStatus>(base),
    add: (body: { name: string; config: string; username?: string; password?: string;
      profile_type?: "auto" | "country" | "individual"; group?: string }) =>
      api.post<{ profile: OpenVpnProfile; status: OpenVpnStatus }>(`${base}/profiles`, body),
    setEnabled: (id: string, enabled: boolean) =>
      api.patch<OpenVpnStatus>(`${base}/profiles/${encodeURIComponent(id)}`, { enabled }),
    remove: (id: string) => api.delete<OpenVpnStatus>(`${base}/profiles/${encodeURIComponent(id)}`),
    enable: () => api.post<{ result: OpenVpnResult; status: OpenVpnStatus }>(`${base}/enable`),
    rotate: () => api.post<{ result: OpenVpnResult; status: OpenVpnStatus }>(`${base}/rotate`),
    disable: () => api.post<OpenVpnStatus>(`${base}/disable`),
  };
}

export const openvpn = openvpnApi("/api/admin/vpn/openvpn");
export const setupOpenvpn = openvpnApi("/api/setup/vpn/openvpn");

export interface SettingsUpdate {
  history_retention_days?: number;
  failed_temp_retention_hours?: number;
  max_concurrent_downloads?: number;
  imdb_cookie?: string;   // write-only; never returned
  wyzie_api_key?: string; // write-only; never returned
  wyzie_enabled?: boolean;
  subtitle_language?: string;
  subtitle_auto_sync?: boolean;
  tmdb_api_key?: string;  // write-only; never returned
  search_source?: SearchSource;
  watched_threshold_percent?: number;
  stream_providers?: Array<{ key: string; enabled: boolean }>;
  movies_folder?: string;
  shows_folder?: string;
  media_categories?: MediaCategory[];
  preferred_quality?: Quality;
  segment_connections?: number;
  video_crf?: number;
  video_preset?: string;
  max_file_gb?: number;
}

export const settings = {
  get:    () => api.get<AppSettings>("/api/admin/settings"),
  update: (body: SettingsUpdate) => api.patch<AppSettings>("/api/admin/settings", body),
};

// ── First-run setup wizard ──────────────────────────────────────────────────

export interface SetupConfig extends AppSettings {
  admin_username: string;
  admin_password_set: boolean;
}

export interface SetupStatus {
  needs_setup: boolean;
  config?: SetupConfig;
}

export interface SetupPayload {
  admin_username: string;
  admin_password?: string;         // blank = keep existing
  media_categories: MediaCategory[];
  preferred_quality: Quality;
  search_source: SearchSource;
  tmdb_api_key?: string;
  imdb_cookie?: string;
  wyzie_api_key?: string;
  wyzie_enabled: boolean;
  subtitle_language: string;
  max_concurrent_downloads: number;
  segment_connections: number;
  history_retention_days: number;
  video_crf: number;
  video_preset: string;
  max_file_gb: number;
  stream_providers: Array<{ key: string; enabled: boolean }>;
}

export const setup = {
  status: () => api.get<SetupStatus>("/api/setup/status"),
  submit: (body: SetupPayload) => api.post<{ ok: boolean }>("/api/setup", body),
};

export interface LibraryShow { imdb_id: string; title: string; year: number | null; scheduled: boolean }

// ── Schedules (admin) ───────────────────────────────────────────────────────

export type Frequency = "daily" | "weekly" | "monthly";

export interface Schedule {
  id: number;
  imdb_id: string;
  title: string;
  year: number | null;
  is_anime: boolean;
  seasons: number[];
  frequency: Frequency;
  day_of_week: number | null;
  hour: number | null;
  is_active: boolean;
  last_checked: string | null;
  last_result: string | null;
  last_queued: number;
  due: boolean;
  created_at: string | null;
}

export interface SeasonGap { season: number; total: number; downloaded: number; missing: number[] }

/** Structured 409 error body from create/update when a conflicting schedule
 * already exists for the show — read via `(err as ApiError).detail`. */
export interface ScheduleConflict { message: string; existing: Schedule }

export type RunItemStatus = "queued" | "downloading" | "completed" | "failed";

export interface ScheduleRunItem {
  id: number;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  job_id: number | null;
  status: RunItemStatus;
  error_message: string | null;
}

export interface ScheduleRun {
  id: number;
  started_at: string | null;
  finished_at: string | null;
  status: "running" | "done";
  checked: number;
  queued: number;
  summary: string | null;
  counts?: Partial<Record<RunItemStatus, number>>;
}

export interface ScheduleRunDetail extends ScheduleRun {
  items: ScheduleRunItem[];
}

export interface ScheduleRunPage { items: ScheduleRun[]; total: number; page: number; per: number }

export const schedules = {
  list:   () => api.get<Schedule[]>("/api/schedules"),
  libraryShows: () => api.get<LibraryShow[]>("/api/schedules/library-shows"),
  create: (body: {
    imdb_id: string; title: string; year: number | null;
    is_anime?: boolean;
    seasons?: number[]; frequency: Frequency; day_of_week?: number | null; hour?: number | null;
  }) => api.post<Schedule>("/api/schedules", body),
  update: (id: number, body: {
    seasons?: number[]; frequency?: Frequency; day_of_week?: number | null; hour?: number | null; is_active?: boolean;
  }) => api.patch<Schedule>(`/api/schedules/${id}`, body),
  remove: (id: number) => api.delete<{ ok: boolean }>(`/api/schedules/${id}`),
  check:  (id: number) => api.post<{ ok: boolean; queued: number; checked?: number }>(`/api/schedules/${id}/check`),
  status: (id: number) => api.get<{ seasons: SeasonGap[] }>(`/api/schedules/${id}/status`),
  runs:   (id: number, page = 0, per = 10) =>
    api.get<ScheduleRunPage>(`/api/schedules/${id}/runs?page=${page}&per=${per}`),
  runDetail: (id: number, runId: number) =>
    api.get<ScheduleRunDetail>(`/api/schedules/${id}/runs/${runId}`),
};

// ── Users (admin) ───────────────────────────────────────────────────────────

export const users = {
  list:   () => api.get<ManagedUser[]>("/api/users"),
  create: (body: { username: string; password: string; role: Role }) =>
    api.post<ManagedUser>("/api/users", body),
  update: (id: number, body: { is_active?: boolean; role?: Role }) =>
    api.patch<ManagedUser>(`/api/users/${id}`, body),
  resetPassword: (id: number, password: string) =>
    api.post<{ ok: boolean }>(`/api/users/${id}/reset-password`, { password }),
  remove: (id: number) => api.delete<{ ok: boolean }>(`/api/users/${id}`),
};
