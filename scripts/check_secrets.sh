#!/usr/bin/env bash
# Fail if real site addresses, denylisted terms or media files are in the working tree.
# Usage:
#   scripts/check_secrets.sh                      tracked + untracked (not ignored) files
#   scripts/check_secrets.sh --commits all        ... plus every commit message in history
#   scripts/check_secrets.sh --commits A..B       ... plus commit messages in a rev range
# Rules and rationale: scripts/_check_secrets.py, AGENT.md 3.
set -euo pipefail

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

git -C "$repo" ls-files -z --cached --others --exclude-standard > "$tmp/paths"
args=(--repo "$repo" --paths-file "$tmp/paths")

if [ "${1:-}" = "--commits" ]; then
  range=${2:?"--commits needs 'all' or a rev range"}
  if [ "$range" = "all" ]; then
    git -C "$repo" log -z --format='%h%n%B' > "$tmp/messages"
  else
    git -C "$repo" log -z --format='%h%n%B' "$range" > "$tmp/messages"
  fi
  args+=(--messages-file "$tmp/messages")
elif [ $# -gt 0 ]; then
  echo "usage: $0 [--commits all|<rev-range>]" >&2
  exit 2
fi

"${PYTHON:-python3}" "$repo/scripts/_check_secrets.py" "${args[@]}"
