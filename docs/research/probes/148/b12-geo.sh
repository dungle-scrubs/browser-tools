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

run 'emulate geolocation: grant, override and read all inside ONE invocation' <<'CMD'
printf '%s\n' 'Browser.grantPermissions '"'"'{"origin": "http://127.0.0.1:17490", "permissions": ["geolocation"]}'"'"'' 'Emulation.setGeolocationOverride '"'"'{"latitude": 48.8566, "longitude": 2.3522, "accuracy": 10}'"'"'' 'Runtime.evaluate '"'"'{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
CMD

run 'emulate geolocation: after that run, read the position again in a SEPARATE invocation' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'
CMD
