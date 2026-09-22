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

run 'pages: open a second tab again, to test how a CURATED verb resolves the page' <<'CMD'
bt 148-01 Target.createTarget '{"url": "http://127.0.0.1:17490/second.html", "newWindow": true}'
CMD

run 'pages: a curated verb with two tabs open and no --target' <<'CMD'
bt 148-01 snapshot | head -5
CMD

run 'pages: the same shape as a raw passthrough' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
CMD

run 'pages: close the second tab again' <<'CMD'
bt 148-01 stop --target 2
CMD

run 'run semantics: bt run cannot branch on a step result - the whole decision goes into one eval instead' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "(() => { const n = document.querySelectorAll(\"input\").length; if (n > 2) { document.getElementById(\"go\").click(); return \"clicked, inputs=\" + n; } return \"skipped, inputs=\" + n; })()", "returnByValue": true}'
CMD

run 'help: the live protocol schema for one method' <<'CMD'
bt 148-01 help Page.handleJavaScriptDialog
CMD

run 'help: Input.dispatchKeyEvent signature, the parameter table a press verb would hide' <<'CMD'
bt 148-01 help Input.dispatchKeyEvent
CMD
