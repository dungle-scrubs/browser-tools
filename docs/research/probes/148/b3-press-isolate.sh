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

run 'press: reset the page state and refocus the field' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent = \"\"; document.getElementById(\"one\").value = \"x\"; document.getElementById(\"one\").focus(); \"reset\"", "returnByValue": true}'
CMD

run 'press: keyDown alone WITH the virtual key code and text, no keyUp' <<'CMD'
bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r"}'
CMD

run 'press: did that one call alone submit the form?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent", "returnByValue": true}'
CMD

run 'type: does Input.insertText fire key events? reset the recorder first' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent = \"NO-KEYDOWN\"; document.getElementById(\"one\").focus(); \"reset\"", "returnByValue": true}'
CMD

run 'type: insertText into the focused field' <<'CMD'
bt 148-01 Input.insertText '{"text": "abc"}'
CMD

run 'type: did the keydown handler fire during insertText?' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"submitted\").textContent, document.getElementById(\"one\").value]", "returnByValue": true}'
CMD

run 'type: char-by-char real keys, the puppeteer shape, is 2 CDP calls per character' <<'CMD'
bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "q", "code": "KeyQ", "windowsVirtualKeyCode": 81, "nativeVirtualKeyCode": 81, "text": "q"}'
CMD

run 'type: and its keyUp' <<'CMD'
bt 148-01 Input.dispatchKeyEvent '{"type": "keyUp", "key": "q", "code": "KeyQ", "windowsVirtualKeyCode": 81, "nativeVirtualKeyCode": 81}'
CMD

run 'type: value and keydown record after the real key' <<'CMD'
bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"submitted\").textContent, document.getElementById(\"one\").value]", "returnByValue": true}'
CMD
