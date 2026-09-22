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

run 'eval: simplest raw form, no quotes inside the JS' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
CMD

run 'eval: JS carrying a single quote (axi: eval "document.querySelectorAll(\047a\047).length")' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.querySelectorAll('"'"'a'"'"').length", "returnByValue": true}'
CMD

run 'eval: same, double quotes inside the JS instead (needs JSON backslash escaping)' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.querySelectorAll(\"a\").length", "returnByValue": true}'
CMD

run 'eval: without returnByValue, an object comes back as a remote handle' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "({a: 1, b: 2})"}'
CMD

run 'eval: with returnByValue, the object comes back as a value' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "({a: 1, b: 2})", "returnByValue": true}'
CMD

run 'eval: multi-statement IIFE, the axi "() => {...}" shape' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "(() => { const rows = [...document.querySelectorAll(\"p\")]; return rows.map(r => r.textContent); })()", "returnByValue": true}'
CMD

run 'eval: a JS exception is reported in exceptionDetails, exit 0' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "nope.nope", "returnByValue": true}'
CMD

run 'eval: awaitPromise for async JS' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "fetch(\"/data.json\").then(r => r.json())", "returnByValue": true, "awaitPromise": true}'
CMD
