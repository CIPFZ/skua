#!/usr/bin/env bash
# Fail if av / subprocess / process-spawning APIs are used outside their single allowed file.
# Usage: scripts/check_imports.sh            (checks skua/, scripts/, tests/ of this repo)
# Rules and rationale: scripts/_check_imports.py, AGENT.md 5.3.
set -euo pipefail

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
exec "${PYTHON:-python3}" "$repo/scripts/_check_imports.py" "$repo"
