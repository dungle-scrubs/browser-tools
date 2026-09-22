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

# ---------- wait <ms> ----------
run 'wait <ms>: outside a run it is the shell, no bt verb and no CDP call' <<'CMD'
s=$(date +%s); sleep 2; e=$(date +%s); echo "slept $((e-s))s"
CMD

run 'wait <ms>: inside a run there is no sleep step. wait --event with a timeout FAILS the run.' <<'CMD'
printf '%s\n' 'wait --event Nothing.happensHere --timeout 2' 'snapshot' | bt 148-01 run -
CMD

run 'wait <ms>: inside a run, console-list --duration N is the fixed-duration wait that exists' <<'CMD'
s=$(date +%s); printf '%s\n' 'console-list --duration 3' 'Runtime.evaluate '"'"'{"expression": "1+1", "returnByValue": true}'"'"'' | bt 148-01 run - > /dev/null; e=$(date +%s); echo "run took $((e-s))s"
CMD

# ---------- wait <text> ----------
run 'wait <text>: reload so the LATE TEXT is not there yet, then check immediately' <<'CMD'
printf '%s\n' 'Page.reload '"'"'{}'"'"'' 'Runtime.evaluate '"'"'{"expression": "document.body.innerText.includes(\"LATE TEXT APPEARED\")", "returnByValue": true}'"'"'' | bt 148-01 run -
CMD

run 'wait <text>: is there a single CDP call for it? A MutationObserver promise with awaitPromise is one call - and this is what it costs to write' <<'CMD'
printf '%s\n' 'Page.reload '"'"'{}'"'"'' 'Runtime.evaluate '"'"'{"expression": "new Promise((res, rej) => { const t = \"LATE TEXT APPEARED\"; const hit = () => document.body && document.body.innerText.includes(t); if (hit()) return res(\"already there\"); const obs = new MutationObserver(() => { if (hit()) { obs.disconnect(); res(\"appeared\"); } }); obs.observe(document.documentElement, {childList: true, subtree: true, characterData: true}); setTimeout(() => { obs.disconnect(); rej(new Error(\"timeout waiting for text\")); }, 5000); })", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
CMD

run 'wait <text>: the polling alternative, a shell loop of separate invocations' <<'CMD'
bt 148-01 Page.reload '{}' > /dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do
  v=$(bt 148-01 Runtime.evaluate '{"expression": "document.body.innerText.includes(\"LATE TEXT APPEARED\")", "returnByValue": true}' | grep -c 'true')
  if [ "$v" -gt 0 ]; then echo "found after $i polls"; break; fi
  sleep 0.5
done
CMD
