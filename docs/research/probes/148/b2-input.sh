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

# ---------- scroll ----------
run 'scroll: read the starting scrollY' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "window.scrollY", "returnByValue": true}'
CMD

run 'scroll down: one mouseWheel dispatch, no JS' <<'CMD'
bt 148-01 Input.dispatchMouseEvent '{"type": "mouseWheel", "x": 100, "y": 300, "deltaX": 0, "deltaY": 600}'
CMD

run 'scroll: scrollY after the wheel' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "window.scrollY", "returnByValue": true}'
CMD

run 'scroll bottom: one Input.dispatchKeyEvent End press (keyDown only)' <<'CMD'
bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "End", "code": "End", "windowsVirtualKeyCode": 35, "nativeVirtualKeyCode": 35}'
CMD

run 'scroll: scrollY after End' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[window.scrollY, document.body.scrollHeight]", "returnByValue": true}'
CMD

run 'scroll top: the JS form (JavaScript inside JSON inside a shell line)' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "window.scrollTo(0, 0)", "returnByValue": true}'
CMD

run 'scroll: scrollY after scrollTo(0,0)' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "window.scrollY", "returnByValue": true}'
CMD

# ---------- type ----------
run 'type: focus the field first with the curated fill, then insertText' <<'CMD'
bt 148-01 fill --uid A16B214C022B-2 --text seed
CMD

run 'type: Input.insertText at the focused element, one call' <<'CMD'
bt 148-01 Input.insertText '{"text": "TYPED"}'
CMD

run 'type: read the field value back' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"one\").value", "returnByValue": true}'
CMD

# ---------- press ----------
run 'press Enter: keyDown alone (the GUIDE recipe), does it reach the handler?' <<'CMD'
bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "Enter"}'
CMD

run 'press: what the keydown handler recorded' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent", "returnByValue": true}'
CMD

run 'press Enter: the full keyDown/keyUp pair with the virtual key code, step 1' <<'CMD'
bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r"}'
CMD

run 'press Enter: step 2, keyUp' <<'CMD'
bt 148-01 Input.dispatchKeyEvent '{"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}'
CMD

run 'press: did the form submit (submit handler writes SUBMITTED:...)' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent", "returnByValue": true}'
CMD
