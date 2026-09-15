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
if ! command -v docker &>/dev/null; then
  echo "[riprate] Docker not found — installing…"
  if command -v apt-get &>/dev/null; then
    # Debian/Ubuntu
    $_SUDO apt-get update -q
    $_SUDO apt-get install -y -q ca-certificates curl gnupg lsb-release
    $_SUDO install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg \
      | $_SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    $_SUDO chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
      $(lsb_release -cs) stable" \
      | $_SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null
    $_SUDO apt-get update -q
    $_SUDO apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
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
  # Add current user to docker group so they can run docker without sudo
  $_SUDO usermod -aG docker "$USER" 2>/dev/null || true
  echo "[riprate] ✓ Docker installed."
fi

# ── Start Docker daemon if it's not already running ──────────────────────
if ! docker info &>/dev/null 2>&1; then
  echo "[riprate] Docker daemon not running — starting it…"
  if command -v service &>/dev/null; then
    $_SUDO service docker start
  elif command -v systemctl &>/dev/null; then
    $_SUDO systemctl start docker
  else
    # WSL2 / no init system fallback
    $_SUDO dockerd > /tmp/dockerd.log 2>&1 &
    echo "[riprate] Started dockerd in background (log: /tmp/dockerd.log)"
  fi
  # Wait up to 15 s for the daemon to become ready
  for i in $(seq 1 15); do
    docker info &>/dev/null 2>&1 && break
    sleep 1
  done
  if ! docker info &>/dev/null 2>&1; then
    echo "[riprate] ⚠  Docker daemon did not start in time."
    echo "           Try: sudo service docker start   (or: sudo systemctl start docker)"
    echo "           Then re-run this script."
    exit 1
  fi
  echo "[riprate] ✓ Docker daemon is running."
fi

# ── Install docker-compose if neither plugin nor standalone exists ─────────
if ! docker compose version &>/dev/null 2>&1 && ! command -v docker-compose &>/dev/null; then
  echo "[riprate] docker compose not found — installing plugin…"
  if command -v apt-get &>/dev/null; then
    $_SUDO apt-get install -y -q docker-compose-plugin
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
  DC="docker compose"
elif command -v docker-compose &>/dev/null; then
  DC="docker-compose"
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
# Defaults to ./media/library (under the project) to match docker-compose.yml.
cd "$ROOT"
MEDIA_DIR="${MEDIA_DIR:-./media/library}"
mkdir -p "$MEDIA_DIR"
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
  $DC down -v --remove-orphans
  # Admin accounts live ONLY in the database, so wiping it is all it takes to
  # land back on the setup wizard — nothing in .env to clear. Also drop any
  # stale on-disk SQLite left by a mis-set DATABASE_URL, just in case.
  rm -f "$ROOT/backend/riprate.db" "$ROOT/backend/riprate.db-shm" "$ROOT/backend/riprate.db-wal"
else
  echo "[riprate] Removing old containers (database and media are kept)…"
  $DC down --remove-orphans
fi
docker image prune -f > /dev/null 2>&1 || true

echo "[riprate] Building and starting containers…"
$DC up --build -d

echo ""
echo "[riprate] ✓ RipRate is running."
echo "           Frontend → http://localhost:5173"
echo "           Backend  → http://localhost:8000"
echo ""
echo "  Tail logs: $DC logs -f"
echo "  Stop:      $DC down"
