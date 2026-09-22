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

run 'pages: note what the previous two commands did - snapshot drove second.html (target-id order), and stop --target 2 closed the fixture tab. Back to the fixture.' <<'CMD'
bt 148-01 Page.navigate '{"url": "http://127.0.0.1:17490/fixture.html"}'
CMD

run 'run semantics: the branch demo, this time on the fixture page' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "(() => { const n = document.querySelectorAll(\"input\").length; if (n > 2) { document.getElementById(\"go\").click(); return \"clicked, inputs=\" + n; } return \"skipped, inputs=\" + n; })()", "returnByValue": true}'
CMD

run 'run semantics: the same decision as a bt run cannot be written - a step cannot read step 1 output' <<'CMD'
printf '%s\n' 'Runtime.evaluate '"'"'{"expression": "document.querySelectorAll(\"input\").length", "returnByValue": true}'"'"'' 'click --uid THERE-IS-NO-WAY-TO-PUT-THE-ANSWER-HERE' | bt 148-01 run -
CMD
