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

run 'run vs bare verb with two pages open: which page does a run with no --target drive?' <<'CMD'
printf '%s\n' 'Runtime.evaluate '"'"'{"expression": "document.title", "returnByValue": true}'"'"'' | bt 148-01 run -
CMD

run 'run vs bare verb: the same call as a bare verb refuses to guess' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
CMD

run 'closepage: the curated form is stop --target' <<'CMD'
bt 148-01 stop --target 2
CMD

run 'closepage: status after' <<'CMD'
bt status 148-01
CMD
