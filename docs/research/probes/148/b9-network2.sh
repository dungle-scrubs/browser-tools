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

run 'network-get: a REAL requestId from the previous invocation, fetched in a new one' <<'CMD'
bt 148-01 Network.getResponseBody '{"requestId": "F0CAB589ACBDF60CFA428FF679111DD2"}'
CMD

run 'network: with the page already loaded and quiet, network-list sees nothing - it is a window, not a buffer' <<'CMD'
bt 148-01 network-list --duration 2
CMD

run 'console: with the page already loaded and quiet, console-list DOES see the old messages - Chrome replays them on enable' <<'CMD'
bt 148-01 console-list --duration 1
CMD

run 'network-get inside one run: enable, reload, list, then body - the requestId cannot be carried from the list step to the body step' <<'CMD'
printf '%s\n' 'Network.enable '"'"'{}'"'"'' 'Page.reload '"'"'{}'"'"'' 'network-list --duration 3' 'Network.getResponseBody '"'"'{"requestId": "CANNOT-BE-FILLED-IN-FROM-A-PREVIOUS-STEP"}'"'"'' | bt 148-01 run -
CMD
