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

# color-scheme, discriminating direction: the browser default here is dark, so emulate LIGHT
run 'emulate color-scheme: set LIGHT, then read it back in a SEPARATE invocation' <<'CMD'
bt 148-01 Emulation.setEmulatedMedia '{"features": [{"name": "prefers-color-scheme", "value": "light"}]}'
CMD

run 'emulate color-scheme: separate invocation - is it still light?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "matchMedia(\"(prefers-color-scheme: light)\").matches", "returnByValue": true}'
CMD

run 'emulate color-scheme: set LIGHT and read it inside ONE invocation' <<'CMD'
printf '%s\n' 'Emulation.setEmulatedMedia '"'"'{"features": [{"name": "prefers-color-scheme", "value": "light"}]}'"'"'' 'Runtime.evaluate '"'"'{"expression": "matchMedia(\"(prefers-color-scheme: light)\").matches", "returnByValue": true}'"'"'' | bt 148-01 run -
CMD

# network conditions
run 'emulate network: Network.emulateNetworkConditions without Network.enable first' <<'CMD'
bt 148-01 Network.emulateNetworkConditions '{"offline": true, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0}'
CMD

run 'emulate network: is the page offline in a SEPARATE invocation?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "fetch(\"/data.json\", {cache: \"no-store\"}).then(r => \"OK \" + r.status).catch(e => \"FAILED \" + e.message)", "returnByValue": true, "awaitPromise": true}'
CMD

run 'emulate network: offline + the fetch inside ONE invocation' <<'CMD'
printf '%s\n' 'Network.enable '"'"'{}'"'"'' 'Network.emulateNetworkConditions '"'"'{"offline": true, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0}'"'"'' 'Runtime.evaluate '"'"'{"expression": "fetch(\"/data.json\", {cache: \"no-store\"}).then(r => \"OK \" + r.status).catch(e => \"FAILED \" + e.message)", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
CMD

run 'emulate network: back online, then confirm the page fetches again' <<'CMD'
printf '%s\n' 'Network.enable '"'"'{}'"'"'' 'Network.emulateNetworkConditions '"'"'{"offline": false, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1}'"'"'' 'Runtime.evaluate '"'"'{"expression": "fetch(\"/data.json\", {cache: \"no-store\"}).then(r => \"OK \" + r.status).catch(e => \"FAILED \" + e.message)", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
CMD

# user agent
run 'emulate user-agent: one CDP call' <<'CMD'
bt 148-01 Emulation.setUserAgentOverride '{"userAgent": "bt-verb-gap-probe/1.0"}'
CMD

run 'emulate user-agent: read navigator.userAgent in a SEPARATE invocation' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "navigator.userAgent", "returnByValue": true}'
CMD

run 'emulate user-agent: set and read inside ONE invocation' <<'CMD'
printf '%s\n' 'Emulation.setUserAgentOverride '"'"'{"userAgent": "bt-verb-gap-probe/1.0"}'"'"'' 'Runtime.evaluate '"'"'{"expression": "navigator.userAgent", "returnByValue": true}'"'"'' | bt 148-01 run -
CMD

# geolocation
run 'emulate geolocation: grant the permission and set the override, one invocation each' <<'CMD'
bt 148-01 Browser.grantPermissions '{"origin": "http://127.0.0.1:17490", "permissions": ["geolocation"]}'
CMD

run 'emulate geolocation: set the override' <<'CMD'
bt 148-01 Emulation.setGeolocationOverride '{"latitude": 37.7749, "longitude": -122.4194, "accuracy": 10}'
CMD

run 'emulate geolocation: read the position in a SEPARATE invocation' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'
CMD

run 'emulate geolocation: set and read inside ONE invocation' <<'CMD'
printf '%s\n' 'Emulation.setGeolocationOverride '"'"'{"latitude": 48.8566, "longitude": 2.3522, "accuracy": 10}'"'"'' 'Runtime.evaluate '"'"'{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
CMD
