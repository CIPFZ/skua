#!/usr/bin/env bash
# Download model weights listed in models/manifest.tsv and verify their SHA256.
# Usage:
#   scripts/fetch_models.sh            download missing files, verify all
#   scripts/fetch_models.sh --check    verify only, never touch the network
# Weights are never committed (.gitignore) and never downloaded at runtime (AGENT.md 2.5).
# Manifest format: see the header of models/manifest.tsv. M0 has no entries.
set -euo pipefail

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
models="$repo/models"
manifest="$models/manifest.tsv"
check_only=0
case "${1:-}" in
  "") ;;
  --check) check_only=1 ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

sha256_of() {
  if command -v sha256sum > /dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1  # macOS
  fi
}

total=0
bad=0
while IFS=$'\t' read -r name file sha url license || [ -n "${name:-}" ]; do
  case "$name" in ""|\#*) continue ;; esac
  if [ -z "${file:-}" ] || [ -z "${sha:-}" ] || [ -z "${url:-}" ] || [ -z "${license:-}" ]; then
    echo "manifest: malformed entry '$name' (need 5 tab-separated columns)" >&2
    exit 2
  fi
  total=$((total + 1))
  dest="$models/$file"
  if [ ! -f "$dest" ]; then
    if [ "$check_only" = 1 ]; then
      echo "missing  $name ($file)" >&2
      bad=$((bad + 1))
      continue
    fi
    echo "fetch    $name ($license)"
    mkdir -p "$(dirname "$dest")"
    # .part + rename: an interrupted download never looks like a complete weight file.
    curl --fail --location --retry 3 --continue-at - --output "$dest.part" "$url"
    mv "$dest.part" "$dest"
  fi
  got=$(sha256_of "$dest")
  if [ "$got" = "$sha" ]; then
    echo "ok       $name"
  else
    echo "MISMATCH $name: expected $sha, got $got" >&2
    bad=$((bad + 1))
  fi
done < "$manifest"

echo "fetch_models: $total entr(y/ies), $bad problem(s)"
[ "$bad" -eq 0 ]
