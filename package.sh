#!/usr/bin/env bash
# Create a distributable RipRate source archive using Git's ignore rules.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_PREFIX="riprate"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [[ $# -gt 1 ]]; then
  echo "Usage: ./package.sh [output.tar.gz]" >&2
  exit 2
fi

if [[ $# -eq 1 ]]; then
  if [[ "$1" = /* ]]; then
    OUTPUT="$1"
  else
    OUTPUT="$(pwd)/$1"
  fi
else
  OUTPUT="$ROOT/dist/${PACKAGE_PREFIX}-${STAMP}.tar.gz"
fi

case "$OUTPUT" in
  *.tar.gz) ;;
  *)
    echo "[package] Output must end in .tar.gz: $OUTPUT" >&2
    exit 2
    ;;
esac

for tool in git tar sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[package] Required command is unavailable: $tool" >&2
    exit 1
  fi
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
MANIFEST="$WORK/files"
TEMP_ARCHIVE="$WORK/package.tar.gz"

# A normal checkout uses its existing index so tracked and untracked source
# files are included. A source tree without .git gets a temporary empty index;
# Git still interprets every nested .gitignore with the same matching rules.
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT=(git -C "$ROOT")
else
  git init -q "$WORK/repository"
  GIT=(git --git-dir="$WORK/repository/.git" --work-tree="$ROOT")
fi

file_count=0
while IFS= read -r -d '' path; do
  # Never distribute repository or local agent/account metadata, even if a
  # checkout has an unusually permissive ignore file.
  case "$path" in
    .git|.git/*|.agents|.agents/*|.aws|.aws/*|.codex|.codex/*) continue ;;
  esac

  # --no-index also honors .gitignore for an accidentally tracked secret such
  # as .env. Missing tracked files are skipped instead of breaking packaging.
  if "${GIT[@]}" check-ignore --no-index -q -- "$path"; then
    continue
  fi
  if [[ ! -f "$ROOT/$path" && ! -L "$ROOT/$path" ]]; then
    continue
  fi
  printf '%s\0' "$path" >> "$MANIFEST"
  ((file_count += 1))
done < <("${GIT[@]}" ls-files --cached --others --exclude-standard -z)

if (( file_count == 0 )); then
  echo "[package] No files remained after applying .gitignore." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"
tar --create --gzip --file "$TEMP_ARCHIVE" \
  --directory "$ROOT" \
  --no-recursion \
  --null --verbatim-files-from --files-from "$MANIFEST" \
  --transform="flags=r;s|^|${PACKAGE_PREFIX}/|"
mv -f "$TEMP_ARCHIVE" "$OUTPUT"

OUTPUT_DIR="$(dirname "$OUTPUT")"
OUTPUT_NAME="$(basename "$OUTPUT")"
(
  cd "$OUTPUT_DIR"
  sha256sum "$OUTPUT_NAME" > "$OUTPUT_NAME.sha256"
)

SIZE="$(du -h "$OUTPUT" | cut -f1)"
echo "[package] Created $OUTPUT"
echo "[package] Included $file_count files after applying .gitignore ($SIZE)"
echo "[package] Checksum: $OUTPUT.sha256"
