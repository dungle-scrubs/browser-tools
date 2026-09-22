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

run 'dialog: the realistic shape - a CLICK raises the dialog synchronously, then a later step handles it' <<'CMD'
printf '%s\n' \
 'click --uid AB5DFB3B758F-200' \
 'wait --event Page.javascriptDialogOpening --timeout 8' \
 'Page.handleJavaScriptDialog '"'"'{"accept": true}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "document.title", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 20
CMD
