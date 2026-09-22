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

run 'hover: step 2 of 2, mouseMoved to the box centre computed from step 1' <<'CMD'
bt 148-01 Input.dispatchMouseEvent '{"type": "mouseMoved", "x": 62, "y": 188, "button": "none"}'
CMD

run 'hover: did the mouseover handler and the :hover rule take effect?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
CMD

run 'hover: does the :hover state survive into a separate invocation?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor", "returnByValue": true}'
CMD

run 'hover: the whole thing as one bt run (2 steps, one invocation) - still needs the coordinates from step 1' <<'CMD'
printf 'DOM.getBoxModel {"backendNodeId": 48}\nInput.dispatchMouseEvent {"type": "mouseMoved", "x": 62, "y": 188, "button": "none"}\n' | bt 148-01 run -
CMD

# ---------- upload ----------
run 'upload: the GUIDE recipe, one CDP call with the backendNodeId out of the UID' <<'CMD'
bt 148-01 DOM.setFileInputFiles '{"files": ["/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148/upload.txt"], "backendNodeId": 4}'
CMD

run 'upload: read the file input back' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"file\").files.length, document.getElementById(\"file\").files[0] && document.getElementById(\"file\").files[0].name]", "returnByValue": true}'
CMD

# ---------- pages / newpage / selectpage / closepage ----------
run 'pages: bt status already lists the page targets' <<'CMD'
bt status 148-01
CMD

run 'newpage: one CDP call (GUIDE recipe)' <<'CMD'
bt 148-01 Target.createTarget '{"url": "http://127.0.0.1:17490/second.html", "newWindow": true}'
CMD

run 'pages: status after the new tab' <<'CMD'
bt status 148-01
CMD

run 'pages: the raw CDP form, browser-level over a page session' <<'CMD'
bt 148-01 Target.getTargets '{}' --target 1
CMD

run 'selectpage: there is no mode to change - the page is a per-invocation flag' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}' --url second.html
CMD

run 'selectpage: same flag by index' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}' --target 2
CMD
