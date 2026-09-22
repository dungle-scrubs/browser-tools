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

# ---------- resize / emulate --viewport ----------
run 'resize: baseline innerWidth/innerHeight/devicePixelRatio' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[innerWidth, innerHeight, devicePixelRatio]", "returnByValue": true}'
CMD

run 'resize: one CDP call (GUIDE recipe)' <<'CMD'
bt 148-01 Emulation.setDeviceMetricsOverride '{"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": true}'
CMD

run 'resize: read it back in a SEPARATE invocation - does the override outlive its session?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[innerWidth, innerHeight, devicePixelRatio]", "returnByValue": true}'
CMD

run 'resize: put it back (clearDeviceMetricsOverride cannot, per the GUIDE)' <<'CMD'
bt 148-01 Emulation.setDeviceMetricsOverride '{"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": false}'
CMD

# ---------- emulate --color-scheme ----------
run 'emulate color-scheme: baseline' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "matchMedia(\"(prefers-color-scheme: dark)\").matches", "returnByValue": true}'
CMD

run 'emulate color-scheme: one CDP call' <<'CMD'
bt 148-01 Emulation.setEmulatedMedia '{"features": [{"name": "prefers-color-scheme", "value": "dark"}]}'
CMD

run 'emulate color-scheme: read back in a SEPARATE invocation' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "matchMedia(\"(prefers-color-scheme: dark)\").matches", "returnByValue": true}'
CMD

run 'emulate color-scheme: set and read inside ONE invocation (bt run)' <<'CMD'
printf '%s\n' 'Emulation.setEmulatedMedia '"'"'{"features": [{"name": "prefers-color-scheme", "value": "dark"}]}'"'"'' 'Runtime.evaluate '"'"'{"expression": "matchMedia(\"(prefers-color-scheme: dark)\").matches", "returnByValue": true}'"'"'' | bt 148-01 run -
CMD

# ---------- emulate --cpu ----------
run 'emulate cpu: baseline spin-loop iterations in 200ms' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "(() => { const t = performance.now(); let n = 0; while (performance.now() - t < 200) n++; return n; })()", "returnByValue": true}'
CMD

run 'emulate cpu: one CDP call, rate 20' <<'CMD'
bt 148-01 Emulation.setCPUThrottlingRate '{"rate": 20}'
CMD

run 'emulate cpu: same spin loop in a SEPARATE invocation' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "(() => { const t = performance.now(); let n = 0; while (performance.now() - t < 200) n++; return n; })()", "returnByValue": true}'
CMD

run 'emulate cpu: throttle and measure inside ONE invocation (bt run)' <<'CMD'
printf '%s\n' 'Emulation.setCPUThrottlingRate '"'"'{"rate": 20}'"'"'' 'Runtime.evaluate '"'"'{"expression": "(() => { const t = performance.now(); let n = 0; while (performance.now() - t < 200) n++; return n; })()", "returnByValue": true}'"'"'' | bt 148-01 run -
CMD

run 'emulate cpu: back to rate 1' <<'CMD'
bt 148-01 Emulation.setCPUThrottlingRate '{"rate": 1}'
CMD
