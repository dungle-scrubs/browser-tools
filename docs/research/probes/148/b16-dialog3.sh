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

run 'dialog: the alert the timed-out click left open - can a NEW invocation handle it? (the GUIDE says no)' <<'CMD'
bt 148-01 Page.handleJavaScriptDialog '{"accept": true}'
CMD

run 'dialog: recover - navigate away from the wedged page' <<'CMD'
bt 148-01 Page.navigate '{"url": "http://127.0.0.1:17490/fixture.html"}'
CMD

run 'dialog: is the page answering again?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
CMD
