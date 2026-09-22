#!/usr/bin/env python3
"""Does Input.insertText carry multi-line prose into a textarea, and what
events does the page see? The measured axi `type` usage is bulk prose into a
chat input, so this is the case the verb has to serve.

Usage: exp-type.py <port>
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient, get_ws_url_async  # noqa: E402

PROSE = """Replace the hand-written append path.
It's got an apostrophe, a "quoted phrase", and a {"json": "looking"} bit.
Third line."""

SETUP = """
(() => {
  document.body.innerHTML = '';
  const ta = document.createElement('textarea');
  ta.id = 'box'; ta.rows = 8; ta.cols = 60;
  document.body.appendChild(ta);
  window.__ev = [];
  for (const k of ['keydown', 'keypress', 'input', 'change', 'beforeinput']) {
    ta.addEventListener(k, e => window.__ev.push(k));
  }
  ta.focus();
  return document.activeElement.id;
})()
"""


async def main(port: int) -> int:
    ws = await get_ws_url_async(port=port, target_type="page")
    async with CDPClient(ws_url=ws) as cdp:
        await cdp.send("Page.navigate", {"url": "about:blank"})
        await asyncio.sleep(0.5)
        r = await cdp.send("Runtime.evaluate", {"expression": SETUP, "returnByValue": True})
        focused = r["result"]["value"]

        await cdp.send("Input.insertText", {"text": PROSE})
        await asyncio.sleep(0.3)

        back = await cdp.send("Runtime.evaluate", {
            "expression": "({value: document.getElementById('box').value, "
                          "events: window.__ev, lines: document.getElementById('box').value.split('\\n').length})",
            "returnByValue": True,
        })
        v = back["result"]["value"]
        print(json.dumps({
            "focusedAtSetup": focused,
            "calls": 1,
            "charsSent": len(PROSE),
            "roundTripExact": v["value"] == PROSE,
            "linesReceived": v["lines"],
            "eventsPageSaw": sorted(set(v["events"])),
            "eventCount": len(v["events"]),
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]))))
