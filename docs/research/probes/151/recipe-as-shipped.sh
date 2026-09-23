#!/bin/bash
# Re-run the Lighthouse recipe exactly as GUIDE.txt ships it, and check the
# report it writes. This is the probe behind the manual's "Measured against
# Chrome 151" line in PERFORMANCE CAPTURES AND CPU PROFILING.
#
# Run it from a checkout:  bash docs/research/probes/151/recipe-as-shipped.sh
#
# Three lines of isolation come first, and none of them is part of the recipe:
#
#   BROWSER_TOOLS_REGISTRY    a registry of its own, so this cannot see, stop
#                             or reap an instance somebody else launched.
#   BROWSER_TOOLS_CHROME_BINARY  a plain Chrome binary. Executing a macOS
#                             application bundle's inner binary aborts in
#                             _RegisterApplication under repeated launches and
#                             throws a crash dialog on the developer's screen.
#                             `python -m playwright install chromium` puts a
#                             headless shell where this looks.
#   a held 9222               `registry.allocate_port` probes upward from 9222,
#                             which is the DevTools default port and on a
#                             developer's machine is their own browser. Holding
#                             it makes the allocator step past it.
set -u

REPO=$(cd -- "$(dirname -- "$0")/../../../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

export BROWSER_TOOLS_REGISTRY="$WORK/registry.json"
export BROWSER_TOOLS_PROFILES_DIR="$WORK/profiles"
if [ -z "${BROWSER_TOOLS_CHROME_BINARY:-}" ]; then
  BROWSER_TOOLS_CHROME_BINARY=$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell 2>/dev/null | tail -1)
  export BROWSER_TOOLS_CHROME_BINARY
fi
[ -x "${BROWSER_TOOLS_CHROME_BINARY:-}" ] || {
  echo "no plain Chrome binary; run: python -m playwright install chromium"
  exit 1
}

lsof -nP -iTCP:9222 -sTCP:LISTEN >/dev/null 2>&1 && {
  echo "something is listening on 9222; refusing to run beside it"
  exit 1
}
python3 -c '
import socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 9222))
s.listen(1)
print("holding 9222", flush=True)
time.sleep(600)
' &
HOLDER=$!
trap 'kill "$HOLDER" 2>/dev/null; rm -rf "$WORK"' EXIT
sleep 1

bt() { (cd "$REPO" && uv run bt "$@"); }
cd "$WORK" || exit 1

# ---- the recipe, verbatim from GUIDE.txt ---------------------------------
bt launch --headless > instance.json
NAME=$(python3 -c 'import json;print(json.load(open("instance.json"))["name"])')
PORT=$(bt status "$NAME" |
  python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["port"])')
npx -y lighthouse@latest https://example.com --port="$PORT" \
  --output=json --output-path=./lighthouse.json
lh_status=$?
bt stop "$NAME"
# --------------------------------------------------------------------------

echo
echo "instance=$NAME port=$PORT lighthouse exit=$lh_status"
python3 - <<'PY'
import json
report = json.load(open("./lighthouse.json"))
print("lighthouseVersion:", report["lighthouseVersion"])
print("runtimeError:", report.get("runtimeError"))
print("categories:", {k: v["score"] for k, v in report["categories"].items()})
print("audits:", len(report["audits"]))
# The manual's "what it does not report": a navigation run drives no
# interaction, so the INP breakdown has nothing to break down.
print("inp-breakdown-insight:", report["audits"]["inp-breakdown-insight"]["scoreDisplayMode"])
PY
