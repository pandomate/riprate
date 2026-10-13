# RipRate

RipRate is a self-hosted media acquisition and library manager. It searches
movie and TV metadata, queues downloads, stores Jellyfin-compatible library
files, and provides browser playback with role-based access control.

The application runs as two Docker services:

- a FastAPI backend for metadata, providers, downloads, storage, VPNs,
  authentication, and the database;
- an Nginx-served React frontend on port 5173.

## Quick start

Requirements:

- Docker Engine;
- Docker Compose v2, using the docker compose command;
- enough host storage for the media library.

From a packaged release:

~~~bash
tar xzf riprate-YYYYMMDD.tar.gz
cd riprate
./start.sh
~~~

start.sh creates .env from .env.example when necessary, generates a strong
SECRET_KEY, creates the host media directory, builds both images, and starts
the stack. Open http://127.0.0.1:5173. The frontend is bound to loopback only,
and the backend API remains on Docker's private network behind the frontend
proxy.

The first visit opens the setup wizard. It configures:

1. the administrator account;
2. media categories and folders;
3. preferred video quality;
4. the metadata/search source;
5. subtitle language and download concurrency;
6. optional Wyzie subtitle downloads;
7. stream providers;
8. transcoding limits;
9. optional VPN settings.

The administrator password is stored as a bcrypt hash in the database. Other
settings are stored in the database and mirrored to .env where applicable.
There is no public sign-up; administrators create user accounts from Settings.

### Reset the application database

~~~bash
./start.sh --fresh
~~~

This removes the Compose database volume and reopens the setup wizard. It does
not remove files under MEDIA_DIR, but it does remove users, settings, download
history, requests, and schedules stored in the database.

### Apply source changes to a running installation

The frontend is compiled into its Docker image. Rebuild the services after
updating the source:

~~~bash
docker compose up --build -d
~~~

start.sh also rebuilds the images. A normal docker compose up -d may continue
serving an older frontend image.

## Roles

### Administrator

Administrators can search and queue downloads directly, approve or deny user
requests, manage the queue, retry or skip stream sources, manage the library,
configure categories/providers/VPNs, schedule recurring episode checks, and
manage users.

### User

Users can search for titles, submit movie or TV requests, browse and watch the
library, and track their own requests. They cannot see other users' requests,
administrator pages, file paths, or provider details.

## Metadata and media types

TMDB is the default metadata source. IMDB scraping can be selected instead in
Settings, but Nova still requires a TMDB API key because it resolves streams
with TMDB IDs. An optional IMDB browser cookie can be configured if IMDB blocks
ordinary requests.

TMDB results are classified as:

- Movies;
- TV Shows;
- Anime Movies;
- Anime Shows.

TMDB does not expose Anime as a separate media type. RipRate detects Anime from
TMDB's Animation genre (genre_id 16) and keeps the original movie/TV type for
seasons, episodes, and provider requests. It does not assume that an Anime
title is Japanese or infer Anime from spoken language or country.

The original audio language comes from TMDB's original_language metadata. If a
stream exposes multiple audio renditions, RipRate prefers that original
language when available. It does not hard-code Japanese audio.

## Categories and folders

Four built-in categories are always present and cannot be removed:

- movies → Movies;
- shows → Shows;
- anime_movies → Anime Movies;
- anime_shows → Anime Shows.

Administrators can add custom categories in Settings. Every category is
available as a manual destination when queueing. When no category is selected,
RipRate routes by media type and TMDB Anime classification:

~~~text
movie + regular       → Movies
TV + regular          → Shows
movie + Anime         → Anime Movies
TV + Anime            → Anime Shows
~~~

The old single anime category is no longer supported. Existing settings are
cleaned during startup and it is filtered from the categories API/UI. Existing
media files are not deleted or moved automatically.

Example layout:

~~~text
$MEDIA_DIR/
├── Movies/
│   └── V for Vendetta (2005)/
│       ├── V for Vendetta (2005).mp4
│       ├── V for Vendetta (2005).nfo
│       └── poster.jpg
├── Shows/
│   └── Family Guy (1999)/
│       └── Season 01/
│           └── Family Guy - S01E01 - Death Has a Shadow.mp4
├── Anime Movies/
└── Anime Shows/
~~~

Folders are created under the host path configured by MEDIA_DIR. Existing files
stay where they are when a category folder is changed; new downloads use the
updated folder. Settings includes a tool for moving an existing folder into
another category.

The library automatically scans the mounted media folder every 15 seconds.
MP4, M4V, MKV, and WebM files are indexed after two unchanged scans; local NFO
metadata and standard movie/episode filenames identify imported media. Moved
files are matched using metadata or an unambiguous filename and size. Missing
files disappear from the available library after two scans, while their database
records and watch history are retained. Restoring a file restores its availability.
Deleting a file outside RipRate does not automatically download it again. Library
and Watch pages refresh automatically; changes normally appear within 45 seconds.

For network storage, set `MEDIA_MOUNT` to the host mountpoint (for example
`/mnt/smb`) and `MEDIA_EXPECTED_FSTYPE` to `9p` for a WSL network drive or `cifs`
for native Linux SMB. Mount the share before running `./start.sh`. The launcher
checks that the host and container see the same directory, and the backend refuses
to start on the wrong filesystem. Docker uses a bind mount, so files are shared
directly rather than copied into a separate Docker volume. After changing or
remounting the host drive, recreate the containers with `./start.sh` (without
`--fresh`). Mount failures skip database reconciliation and block new downloads.

## Downloads, providers, and subtitles

Nova is the current stream provider. Its resolver may return a variable number
of stream URLs and subtitle tracks. RipRate does not impose a fixed source
limit: it tries the returned URLs in quality order, validates that each result
is playable, and continues through the remaining URLs when one is empty,
stalls, unauthorized, or produces invalid media. At least one stream provider
must remain enabled in Settings.

The queue has two major phases:

1. Downloading — a Python HLS/DASH downloader selects the preferred video
   quality, downloads segments with parallel connections, supports AES-128 HLS,
   and resumes or fails over when a source cannot complete.
2. Processing — ffmpeg creates the final MP4. Compatible streams are remuxed
   without re-encoding; incompatible or oversized results are re-encoded using
   the configured CRF and preset.

Subtitle behavior is independent from original audio:

- the subtitle language defaults to English (en);
- the language can be changed in Setup or Settings → Downloads;
- enable or disable Wyzie in Setup or Settings → Wyzie Subs (disabled by default);
- when enabled with a key, Wyzie is tried first for downloads and subtitle retries;
- stream provider subtitles (including HLS tracks during downloads) are used when
  Wyzie is disabled, has no key, finds no match, or fails;
- selected subtitles are cleaned and saved as Jellyfin-friendly .vtt sidecars;
- subtitle timing is aligned locally against the downloaded audio by default;
- the RipRate player provides ±0.1s/±1s preview controls, a saved fixed offset,
  audio-based auto alignment, and restoration of the original timing;
- Wyzie is optional and requires an API key from store.wyzie.io/redeem;
- changes to the Wyzie switch apply to subsequent subtitle requests without a restart.

## Scheduling and requests

Administrators can schedule recurring checks for new episodes. Scheduled jobs
retain the show's media type and Anime classification so they use the same
category routing as manual downloads.

For a non-administrator request:

1. the user searches and requests a movie or TV season/episode selection;
2. the administrator approves or denies it from Requests;
3. approved requests enter the download queue and remain attributed to the
   requesting user;
4. the user can track the result from My Requests.

Titles already downloaded or queued are identified before duplicate work is
created.

## VPN support

OpenVPN runs inside the backend container. Docker Compose grants the backend
NET_ADMIN and /dev/net/tun; these capabilities are inactive until a VPN is
configured.

### OpenVPN profile pool

Upload one or more .ovpn files in Settings → VPN. A file containing multiple
remote entries is managed as multiple possible sources. Profiles can be enabled
or disabled, labeled, tested, and rotated. RipRate verifies tunnel
connectivity and public IPv4 egress, pauses downloads during rotation, and uses
a container-local kill switch while the VPN is active. While connected, a
background health check verifies direct internet access every 30 seconds and
automatically rotates after two consecutive failures. Admins can also click
Get new VPN on the Downloads page to rotate immediately.

The reusable OpenVPN profile package has its own documentation in
vpn_profiles/README.md.

## Configuration

Most settings should be changed in the setup wizard or the in-app Settings
page. .env is useful for values needed before the first launch and for
deployment-specific paths.

| Variable | Template value | Purpose |
|---|---:|---|
| MEDIA_DIR | ./media | Host directory mounted as /media/library in the backend. |
| DATABASE_URL | sqlite:////app/data/riprate.db | Database URL used inside the container. |
| SECRET_KEY | generated | Session-signing secret; start.sh generates it automatically. |
| SESSION_SECURE | false | Set true when the site is served over HTTPS. |
| MAX_CONCURRENT_DOWNLOADS | 3 | Maximum simultaneous downloads. |
| SEGMENT_CONNECTIONS | 3 | Parallel segment connections per download. |
| PREFERRED_QUALITY | 1080 | Preferred vertical video resolution. |
| SUBTITLE_LANGUAGE | en | Subtitle language code searched in stream tracks and Wyzie. |
| MAX_FILE_SIZE_GB | 10.0 | Results above this size are eligible for shrink re-encoding. |
| VIDEO_CRF | 20 | x264 quality setting used only when re-encoding. |
| VIDEO_PRESET | medium | x264 encoding preset used only when re-encoding. |
| HISTORY_RETENTION_DAYS | 30 | Automatic history cleanup; 0 keeps history indefinitely. |
| FAILED_TEMP_RETENTION_HOURS | 0 | Failed download temp cleanup; 0 deletes immediately, or set hours to retain until cleanup/restart. |
| SEARCH_SOURCE | tmdb | tmdb or imdb for title/season/episode searches. |
| TMDB_API_KEY | empty | TMDB v4 token; required for TMDB search and Nova. |
| IMDB_COOKIE | empty | Optional browser Cookie header for IMDB access. |
| WYZIE_API_KEY | empty | Optional subtitle API key. |
| WYZIE_ENABLED | false | Prefers Wyzie subtitles when a key is configured; otherwise uses stream subtitles. |
| VPN_OPENVPN_HEALTH_INTERVAL | 30 | Seconds between direct OpenVPN egress checks. |
| VPN_OPENVPN_HEALTH_FAILURES | 2 | Consecutive failed checks before rotation. |

The database is authoritative for settings changed in the UI. Settings mirrored
to .env are written there as a convenience for restarts and future containers.

## Useful commands

~~~bash
# Start or rebuild the stack
./start.sh
docker compose up --build -d

# Follow logs
docker compose logs -f

# Stop the stack without deleting the database volume
docker compose down

# Inspect service status
docker compose ps

# Build a distributable release archive
./package.sh
~~~

package.sh excludes local secrets, databases, media, build output, and provider
.ovpn files. Never commit .env, API keys, passwords, private VPN material, or
media files.

## Security

RipRate includes:

- bcrypt password hashing;
- admin-only management endpoints;
- per-user request and library visibility;
- login rate limiting and account lockout;
- signed HTTP-only SameSite session cookies;
- session revocation after password, role, or account changes;
- state-changing request origin/CSRF checks;
- restrictive browser security headers;
- audit logging for authentication, requests, user management, and library
  edits.

For HTTPS deployments, terminate TLS in a trusted reverse proxy and set
SESSION_SECURE=true.
