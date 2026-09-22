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

run 'dialog: open a confirm in step 1 of a run, wait for the event in step 2, handle it in step 3, read the answer in step 4' <<'CMD'
printf '%s\n' \
 'Runtime.evaluate '"'"'{"expression": "setTimeout(() => { window.__dlg = String(confirm(\"CONFIRM-FIXTURE\")); }, 1500)"}'"'"'' \
 'wait --event Page.javascriptDialogOpening --timeout 10' \
 'Page.handleJavaScriptDialog '"'"'{"accept": true}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "window.__dlg", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 25
CMD
