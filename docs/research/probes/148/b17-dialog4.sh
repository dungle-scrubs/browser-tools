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

run 'dialog: the workaround - defer the click so the step returns, then handle the prompt with text' <<'CMD'
printf '%s\n' \
 'Runtime.evaluate '"'"'{"expression": "setTimeout(() => document.getElementById(\"promptbtn\").click(), 300)"}'"'"'' \
 'wait --event Page.javascriptDialogOpening --timeout 8' \
 'Page.handleJavaScriptDialog '"'"'{"accept": true, "promptText": "typed-answer"}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "window.__dialogResult", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 20
CMD

run 'dialog: dismiss a confirm the same way' <<'CMD'
printf '%s\n' \
 'Runtime.evaluate '"'"'{"expression": "setTimeout(() => document.getElementById(\"confirmbtn\").click(), 300)"}'"'"'' \
 'wait --event Page.javascriptDialogOpening --timeout 8' \
 'Page.handleJavaScriptDialog '"'"'{"accept": false}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "window.__dialogResult", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 20
CMD
