#!/usr/bin/env bash
# ── RipRate — Docker Compose launcher ──────────────────────────────────────
# Installs Docker and Docker Compose if missing, then starts all containers.
# Media files are written to MEDIA_DIR on the host via a Docker volume mount,
# so Jellyfin (or any local player) can read them directly.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Must run as root or with sudo for Docker install ──────────────────────
_SUDO=""
if [[ "$EUID" -ne 0 ]]; then
  if command -v sudo &>/dev/null; then
    _SUDO="sudo"
  else
    echo "[riprate] ⚠  Run as root or install Docker manually, then re-run."
    exit 1
  fi
fi

# ── Install Docker if missing ─────────────────────────────────────────────
# Docker Desktop can add a Windows docker.exe to PATH inside WSL.  That is
# not a native Linux Docker installation, so do not treat it as Docker here.
DOCKER_BIN="$(command -v docker 2>/dev/null || true)"
if [[ -z "$DOCKER_BIN" || "$DOCKER_BIN" == /mnt/* || "$DOCKER_BIN" == *.exe ]]; then
  echo "[riprate] Docker not found — installing…"
  if command -v apt-get &>/dev/null; then
    # Debian/Ubuntu: install the native Linux engine and Compose.
    $_SUDO apt-get update -q
    $_SUDO apt-get install -y -q docker.io docker-compose
  elif command -v yum &>/dev/null || command -v dnf &>/dev/null; then
    # RHEL/CentOS/Fedora
    _PKG=$(command -v dnf &>/dev/null && echo dnf || echo yum)
    $_SUDO $_PKG install -y -q docker docker-compose-plugin
    $_SUDO systemctl enable --now docker
  else
    # Generic fallback via convenience script
    echo "[riprate] Using Docker's convenience install script…"
    curl -fsSL https://get.docker.com | $_SUDO sh
  fi
  hash -r
  # Add current user to docker group so they can run docker without sudo
  $_SUDO usermod -aG docker "$USER" 2>/dev/null || true
  echo "[riprate] ✓ Docker installed."
fi

# Use sudo for Docker during this run when the daemon is available but the
# current shell has not picked up its newly-added docker group membership yet.
DOCKER=(docker)
DOCKER_USES_SUDO=0
if ! "${DOCKER[@]}" info &>/dev/null 2>&1 \
    && [[ -n "$_SUDO" ]] && $_SUDO docker info &>/dev/null 2>&1; then
  DOCKER=($_SUDO docker)
  DOCKER_USES_SUDO=1
  echo "[riprate] Docker is running; using sudo until your next login refreshes group membership."
fi

# ── Start Docker daemon if it's not already running ──────────────────────
if ! "${DOCKER[@]}" info &>/dev/null 2>&1; then
  echo "[riprate] Docker daemon not running — starting it…"
  if [[ -d /run/systemd/system ]] && command -v systemctl &>/dev/null; then
    $_SUDO systemctl start docker
  elif command -v service &>/dev/null && [[ -x /usr/sbin/service ]] \
      && ! ( [[ -r /proc/version ]] && grep -qi microsoft /proc/version ); then
    $_SUDO service docker start
  else
    # Systems without an init system (including WSL without systemd).
    $_SUDO dockerd > /tmp/dockerd.log 2>&1 &
    echo "[riprate] Started dockerd in background (log: /tmp/dockerd.log)"
  fi
  # Wait up to 15 s for the daemon to become ready
  for i in $(seq 1 15); do
    if docker info &>/dev/null 2>&1; then
      break
    elif [[ -n "$_SUDO" ]] && $_SUDO docker info &>/dev/null 2>&1; then
      DOCKER=($_SUDO docker)
      DOCKER_USES_SUDO=1
      break
    fi
    sleep 1
  done
  if ! "${DOCKER[@]}" info &>/dev/null 2>&1; then
    echo "[riprate] ⚠  Docker daemon did not start in time."
    if [[ -s /tmp/dockerd.log ]]; then
      echo "           Last dockerd log lines:"
      tail -n 20 /tmp/dockerd.log | sed 's/^/           /'
    elif command -v systemctl &>/dev/null; then
      $_SUDO systemctl status docker --no-pager 2>/dev/null || true
    fi
    exit 1
  fi
  echo "[riprate] ✓ Docker daemon is running."
fi

# ── Install docker-compose if neither plugin nor standalone exists ─────────
if ! docker compose version &>/dev/null 2>&1 && ! command -v docker-compose &>/dev/null; then
  echo "[riprate] docker compose not found — installing plugin…"
  if command -v apt-get &>/dev/null; then
    $_SUDO apt-get install -y -q docker-compose
  else
    # Standalone binary fallback
    ARCH=$(uname -m)
    OS=$(uname -s)
    COMPOSE_URL="https://github.com/docker/compose/releases/latest/download/docker-compose-${OS}-${ARCH}"
    $_SUDO curl -L "$COMPOSE_URL" -o /usr/local/bin/docker-compose
    $_SUDO chmod +x /usr/local/bin/docker-compose
  fi
  echo "[riprate] ✓ docker compose installed."
fi

# Resolve compose command
if docker compose version &>/dev/null 2>&1; then
  DC=("${DOCKER[@]}" compose)
elif command -v docker-compose &>/dev/null; then
  if [[ "$DOCKER_USES_SUDO" == "1" ]]; then
    DC=($_SUDO docker-compose)
  else
    DC=(docker-compose)
  fi
else
  echo "[riprate] ⚠  Could not find docker compose. Please install it manually."
  exit 1
fi

# ── .env check ───────────────────────────────────────────────────────────
if [[ ! -f "$ROOT/.env" ]]; then
  echo "[riprate] No .env found — copying .env.example"
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

source "$ROOT/.env" 2>/dev/null || true

# ── SECRET_KEY: auto-generate if missing/placeholder/too short ────────────
# The app cannot start without a strong SECRET_KEY (session signing). We
# generate one automatically so a fresh install can boot straight into the
# web setup wizard — no manual .env editing required.
_need_secret=0
if [[ -z "${SECRET_KEY:-}" || "${#SECRET_KEY}" -lt 32 ]]; then _need_secret=1; fi
case "${SECRET_KEY:-}" in
  *change*|*changeme*|*your-secret*|*example*|*REPLACE*) _need_secret=1 ;;
esac
if [[ "$_need_secret" == "1" ]]; then
  NEW_SECRET="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if grep -qE '^[[:space:]]*SECRET_KEY=' "$ROOT/.env"; then
    # Replace the existing (weak) line in place
    sed -i.bak "s|^[[:space:]]*SECRET_KEY=.*|SECRET_KEY=$NEW_SECRET|" "$ROOT/.env" && rm -f "$ROOT/.env.bak"
  else
    printf '\nSECRET_KEY=%s\n' "$NEW_SECRET" >> "$ROOT/.env"
  fi
  export SECRET_KEY="$NEW_SECRET"
  echo "[riprate] ✓ Generated a SECRET_KEY into .env"
fi

# ── Ensure the host media directory exists ───────────────────────────────
# MEDIA_DIR is the HOST path bound to /media/library inside the container.
# Defaults to ./media (under the project) to match docker-compose.yml.
cd "$ROOT"
MEDIA_DIR="${MEDIA_DIR:-./media}"
# A missing network drive must not turn into a local library directory.
if [[ -n "${MEDIA_MOUNT:-}" ]] && ! mountpoint -q "$MEDIA_MOUNT"; then
  echo "[riprate] Media mount is unavailable: $MEDIA_MOUNT"
  echo "           Mount the drive before starting RipRate."
  exit 1
fi
mkdir -p "$MEDIA_DIR"
export MEDIA_DIR
if [[ -z "${MEDIA_EXPECTED_FSTYPE:-}" ]] && command -v findmnt &>/dev/null; then
  MEDIA_EXPECTED_FSTYPE="$(findmnt -n -o FSTYPE -T "$MEDIA_DIR")"
fi
export MEDIA_EXPECTED_FSTYPE="${MEDIA_EXPECTED_FSTYPE:-}"
echo "[riprate] Media directory (host): $MEDIA_DIR  →  /media/library in the container"

# Drop any stale auto-generated compose override left by the old MOUNT_* feature
OVERRIDE="$ROOT/docker-compose.override.yml"
if [[ -f "$OVERRIDE" ]] && head -1 "$OVERRIDE" | grep -q "AUTO-GENERATED by start.sh"; then
  rm -f "$OVERRIDE"
  echo "[riprate] Removed stale docker-compose.override.yml"
fi

# ── Clean up old containers, then build and launch ───────────────────────
# Default: remove this project's old/stopped/orphaned containers and dangling
# images before starting (volumes — i.e. the database — are KEPT).
# `./start.sh --fresh` ALSO removes the volumes: full reset, wipes the
# database (users, settings, download history). Media files are never touched
# either way — they live in MEDIA_DIR on the host, not in a volume.
cd "$ROOT"
if [[ "${1:-}" == "--fresh" ]]; then
  echo "[riprate] --fresh: removing containers AND volumes (database will be wiped)…"
  "${DC[@]}" down -v --remove-orphans
  # Admin accounts live ONLY in the database, so wiping it is all it takes to
  # land back on the setup wizard — nothing in .env to clear. Also drop any
  # stale on-disk SQLite left by a mis-set DATABASE_URL, just in case.
  rm -f "$ROOT/backend/riprate.db" "$ROOT/backend/riprate.db-shm" "$ROOT/backend/riprate.db-wal"
else
  echo "[riprate] Removing old containers (database and media are kept)…"
  "${DC[@]}" down --remove-orphans
fi
"${DOCKER[@]}" image prune -f > /dev/null 2>&1 || true

echo "[riprate] Building and starting containers…"
"${DC[@]}" up --build -d

# Recreating containers attaches the current host mount. Compare the actual
# directory identity so a stale Docker mount cannot pass unnoticed.
_MEDIA_HOST_ID="$(stat -c '%d:%i' "$MEDIA_DIR")"
_MEDIA_CONTAINER_ID="$("${DC[@]}" exec -T backend stat -c '%d:%i' /media/library)"
if [[ "$_MEDIA_HOST_ID" != "$_MEDIA_CONTAINER_ID" ]]; then
  echo "[riprate] Host MEDIA_DIR and Docker media directory do not match."
  echo "           Check the Docker context and host mount, then recreate the backend."
  "${DC[@]}" stop backend
  exit 1
fi

echo "[riprate] Waiting for the app at http://127.0.0.1:5173…"
_frontend_ready=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:5173/api/setup/status >/dev/null 2>&1; then
    _frontend_ready=1
    break
  fi
  sleep 1
done

if [[ "$_frontend_ready" != "1" ]]; then
  echo "[riprate] ⚠  RipRate did not become ready on port 5173."
  echo "[riprate] Container status:"
  "${DC[@]}" ps -a || true
  echo "[riprate] Recent frontend/backend logs:"
  "${DC[@]}" logs --tail=80 frontend backend || true
  exit 1
fi

echo ""
echo "[riprate] ✓ RipRate is running."
echo "           Frontend → http://127.0.0.1:5173"
echo "           API      → http://127.0.0.1:5173/api"
echo ""
echo "  Tail logs: ${DC[*]} logs -f"
echo "  Stop:      ${DC[*]} down"
