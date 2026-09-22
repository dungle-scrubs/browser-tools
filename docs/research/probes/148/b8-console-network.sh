#!/bin/bash
cd /Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148 || exit 1
LOG=transcript.log
run() {
  local label="$1"; local cmd; cmd=$(cat)
  { printf '\n### %s\n$ %s\n' "$label" "$cmd"; } | tee -a "$LOG"
  local out rc
  out=$(bash -c "$cmd" 2>&1); rc=$?
  { printf '%s\n[exit %d]\n' "$out" "$rc"; } | tee -a "$LOG"
}

# ---------- console / console-get ----------
run 'console: the page logged two lines at load. Does console-list see them afterwards?' <<'CMD'
bt 148-01 console-list --duration 2
CMD

run 'console: the same window, but with the page reloading inside it (bt run, one invocation)' <<'CMD'
printf 'Page.reload '"'"'{}'"'"'\nconsole-list --duration 2\n' | bt 148-01 run -
CMD

run 'console-get: there is no per-message id to fetch; console-list carries the whole message' <<'CMD'
bt 148-01 console-list --duration 1 | head -40
CMD

# ---------- network / network-get ----------
run 'network: network-list over a window with a reload inside it' <<'CMD'
printf 'Page.reload '"'"'{}'"'"'\nnetwork-list --duration 3\n' | bt 148-01 run -
CMD

run 'network-get: a response body needs the requestId AND the Network domain on in the same session. Try it alone, with a requestId from the list above.' <<'CMD'
bt 148-01 Network.getResponseBody '{"requestId": "PLACEHOLDER"}'
CMD
