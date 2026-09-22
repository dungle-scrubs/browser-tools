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

run 'hover (re-run, the first attempt died on an ambiguous target): fresh instance and fixture' <<'CMD'
(nohup python3 -m http.server 17490 --bind 127.0.0.1 > server.log 2>&1 &) ; sleep 1; bt launch --headless
CMD

run 'hover: navigate' <<'CMD'
bt 148-01 Page.navigate '{"url": "http://127.0.0.1:17490/fixture.html"}'
CMD

run 'hover: baseline, nothing hovered' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
CMD

run 'hover: the JS substitute - a synthetic MouseEvent fires the handler but does NOT set the CSS :hover state' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"hovertarget\").dispatchEvent(new MouseEvent(\"mouseover\", {bubbles: true})); [document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
CMD

run 'hover: the real pointer move, for comparison (box centre from DOM.getBoxModel)' <<'CMD'
bt 148-01 DOM.getBoxModel '{"backendNodeId": 48}'
CMD
