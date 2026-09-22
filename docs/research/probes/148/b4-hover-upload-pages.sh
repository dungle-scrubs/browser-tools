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

# ---------- hover ----------
run 'hover: scroll back to the top so the target is in the viewport' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "window.scrollTo(0,0); document.getElementById(\"hoverstate\").textContent", "returnByValue": true}'
CMD

run 'hover: step 1 of 2, box model for the UID backendNodeId 48 (span#hovertarget)' <<'CMD'
bt 148-01 DOM.getBoxModel '{"backendNodeId": 48}'
CMD
