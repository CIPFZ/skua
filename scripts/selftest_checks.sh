#!/usr/bin/env bash
# Prove that check_imports.sh and check_secrets.sh fail on deliberately violating commits.
# Copies the working tree into a throwaway git repo, verifies the clean copy passes, then
# commits one violation at a time and asserts the matching checker exits with status 1.
# Usage: scripts/selftest_checks.sh   (AGENT.md 10.6: "两个检查脚本在故意违规的提交上各失败一次")
set -euo pipefail

src=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
work="$tmp/repo"
out="$tmp/out"
mkdir "$work"

(cd "$src" && git ls-files -z --cached --others --exclude-standard | tar --null -T - -cf -) \
  | tar -xf - -C "$work"

g() { git -C "$work" -c user.name=selftest -c user.email=selftest@example.com "$@"; }
g init -q
g add -A
g commit -qm "selftest: baseline"

failures=0
run_checker() {
  case "$1" in
    imports) "$work/scripts/check_imports.sh" ;;
    secrets) "$work/scripts/check_secrets.sh" --commits all ;;
  esac
}

expect() {  # expect <imports|secrets> <0|1> <label>
  local checker=$1 want=$2 label=$3 got=0
  run_checker "$checker" > "$out" 2>&1 || got=$?
  if [ "$got" = "$want" ]; then
    echo "ok   [$checker] $label (exit $got)"
  else
    echo "FAIL [$checker] $label: want exit $want, got $got" >&2
    sed 's/^/     /' "$out" >&2
    failures=$((failures + 1))
  fi
}

# Commit whatever is staged in the work tree, expect the checker to fail, then roll back.
commit_and_expect_failure() {  # <checker> <label> [commit message]
  g add -A
  g commit -qm "${3:-selftest: inject violation}"
  expect "$1" 1 "$2"
  g reset -q --hard HEAD~1
  g clean -qfdx
}

violate() {  # violate <checker> <label> <file> <content> [commit message]
  mkdir -p "$work/$(dirname "$3")"
  printf '%s\n' "$4" > "$work/$3"
  # -f: a violator can bypass .gitignore (e.g. *.mp4), the checkers must still catch it.
  g add -f -- "$3"
  commit_and_expect_failure "$1" "$2" "${5:-}"
}

# The clean copy must pass, otherwise every "fails on violation" below proves nothing.
expect imports 0 "clean tree passes"
expect secrets 0 "clean tree passes"

# --- check_imports: one commit per rule -------------------------------------------------------
violate imports "import av outside decode.py" skua/evidence/_bad.py "import av"
violate imports "import av inside adapter but not decode.py" skua/adapter/_bad.py "import av as _av"
violate imports "subprocess in tests" tests/_bad.py "import subprocess
subprocess.run(['true'], check=True)"
violate imports "from os import system" scripts/_bad.py "from os import system"
violate imports "dynamic import of subprocess" skua/_bad.py "import importlib
importlib.import_module('subprocess')"
violate imports "shell=True" skua/_bad.py "def f(run): return run(['x'], shell=True)"
violate imports "asyncio subprocess" skua/_bad.py "import asyncio
asyncio.create_subprocess_exec('ffmpeg')"

# --- check_secrets: one commit per rule -------------------------------------------------------
# The fake host and show name are assembled at runtime so this script itself stays clean.
host="cdn.$(printf '%s' 'streamsite').com"
term="Fake$(printf '%s' 'showname')"

violate secrets "real URL in docs" docs/_bad.md "see https://${host}/v/index.m3u8"
violate secrets "scheme-less m3u8 in source" skua/_bad.py "SRC = '${host}/v/index.m3u8'"
violate secrets "real URL in commit message" docs/_ok.md "harmless" \
  "adapter: tested against https://${host}/v"
violate secrets "media file extension" tests/fixtures/_bad.mp4 "not really a video"

mkdir -p "$work/tests/fixtures"
"${PYTHON:-python3}" -c 'import sys; open(sys.argv[1], "wb").write((b"\x47" + bytes(187)) * 4)' \
  "$work/tests/fixtures/_bad.bin"
commit_and_expect_failure secrets "MPEG-TS sync bytes behind an innocent extension"

# .env.local is gitignored, so it survives the commit but is removed by `git clean -x`.
printf 'SKUA_DENYLIST=%s\nSKUA_SRC=https://%s/v/index.m3u8\n' "$term" "$host" > "$work/.env.local"
expect secrets 0 "denylist alone does not trip a clean tree"
printf 'SKUA_DENYLIST=%s\n' "$term" > "$work/.env.local"
violate secrets "denylisted term from .env.local" docs/_bad.md "notes about ${term} episode 3"
printf 'SKUA_SRC=https://%s/v/index.m3u8\n' "$host" > "$work/.env.local"
violate secrets "scheme-less host from .env.local" docs/_bad.md "mirrors ${host} as well"

if [ "$failures" -gt 0 ]; then
  echo "selftest_checks: $failures failure(s)" >&2
  exit 1
fi
echo "selftest_checks: ok"
