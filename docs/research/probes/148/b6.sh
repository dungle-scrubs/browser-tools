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

run 'run: a raw step needs its JSON quoted, as at the prompt (my previous unquoted line was my error, not bt refusing the method)' <<'CMD'
printf '%s\n' 'DOM.getBoxModel '"'"'{"backendNodeId": 48}'"'"'' | bt 148-01 run -
CMD

run 'hover: move the pointer away first, so the next test starts un-hovered' <<'CMD'
bt 148-01 Input.dispatchMouseEvent '{"type": "mouseMoved", "x": 600, "y": 600, "button": "none"}'
CMD

run 'hover: confirm the :hover rule has dropped' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
CMD

run 'hover: the JS substitute - dispatch a synthetic MouseEvent instead of a real pointer move' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"hoverstate\").textContent = \"reset\"; document.getElementById(\"hovertarget\").dispatchEvent(new MouseEvent(\"mouseover\", {bubbles: true})); [document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
CMD

# ---------- fillform ----------
run 'fillform: N fields in one invocation is a bt run with N fill steps' <<'CMD'
printf 'fill --uid A16B214C022B-2 --text alpha\nfill --uid A16B214C022B-3 --text beta\n' | bt 148-01 run -
CMD

run 'fillform: read both fields back' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"one\").value, document.getElementById(\"two\").value]", "returnByValue": true}'
CMD

run 'fillform: a value with a space, quoted in the step list the way a shell quotes it' <<'CMD'
printf '%s\n' 'fill --uid A16B214C022B-3 --text "two words"' | bt 148-01 run -
CMD

run 'fillform: read the spaced value back' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"two\").value", "returnByValue": true}'
CMD
