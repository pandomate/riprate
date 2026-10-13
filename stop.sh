#!/usr/bin/env bash
# Stop RipRate without deleting its database or media.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

DOCKER=(docker)
DOCKER_USES_SUDO=0

if ! command -v docker &>/dev/null; then
  echo "[riprate] Docker is not installed."
  exit 1
fi

if ! "${DOCKER[@]}" info &>/dev/null 2>&1; then
  if [[ "$EUID" -ne 0 ]] && command -v sudo &>/dev/null \
      && sudo docker info &>/dev/null 2>&1; then
    DOCKER=(sudo docker)
    DOCKER_USES_SUDO=1
  else
    echo "[riprate] Docker is not running or cannot be accessed."
    exit 1
  fi
fi

if docker compose version &>/dev/null 2>&1; then
  COMPOSE=("${DOCKER[@]}" compose)
elif command -v docker-compose &>/dev/null; then
  if [[ "$DOCKER_USES_SUDO" == "1" ]]; then
    COMPOSE=(sudo docker-compose)
  else
    COMPOSE=(docker-compose)
  fi
else
  echo "[riprate] Docker Compose is not installed."
  exit 1
fi

echo "[riprate] Stopping RipRate…"
"${COMPOSE[@]}" down --remove-orphans
echo "[riprate] ✓ RipRate stopped. Database and media were preserved."
