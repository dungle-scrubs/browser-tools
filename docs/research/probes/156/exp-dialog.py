#!/usr/bin/env python3
"""Prototype the two candidate fixes for a click that raises a JavaScript dialog.

Shape A (--no-wait click): dispatch the click and do not await its response,
then wait for the dialog event and answer it.

Shape B (run-level dialog policy): subscribe to Page.javascriptDialogOpening
for the whole session and answer every dialog per a policy, so an ordinary
awaited click returns normally.

Also measures the control: an ordinary awaited click on a dialog-raising
button, which is the shape that deadlocks today.

Usage: exp-dialog.py <port> <shape:control|a|b> <button-id> [policy]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient, get_ws_url_async  # noqa: E402

URL = "http://127.0.0.1:17542/dialog.html"
DISPATCH_TIMEOUT = 6.0


async def centre_of(cdp: CDPClient, el: str) -> tuple[float, float]:
    r = await cdp.send("Runtime.evaluate", {
        "expression": f"(()=>{{const b=document.getElementById('{el}').getBoundingClientRect();"
                      "return {x:b.x+b.width/2, y:b.y+b.height/2};})()",
        "returnByValue": True,
    })
    v = r["result"]["value"]
    return v["x"], v["y"]


async def press(cdp: CDPClient, x: float, y: float) -> list:
    """Return the two dispatch coroutines as tasks, so a caller may await or not."""
    tasks = []
    for kind in ("mousePressed", "mouseReleased"):
        tasks.append(asyncio.ensure_future(cdp.send("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "left",
            "clickCount": 1, "buttons": 1 if kind == "mousePressed" else 0,
        }, timeout=DISPATCH_TIMEOUT)))
        await asyncio.sleep(0.02)
    return tasks


async def main(port: int, shape: str, button: str, policy: str) -> int:
    ws = await get_ws_url_async(port=port, target_type="page")
    seen: list[dict] = []
    answered: list[str] = []

    async with CDPClient(ws_url=ws) as cdp:
        await cdp.send("Page.enable", {})
        await cdp.send("Runtime.enable", {})

        opening: asyncio.Queue = asyncio.Queue()
        cdp.on("Page.javascriptDialogOpening", lambda p: (seen.append(p), opening.put_nowait(p)))

        if shape == "b":
            # The run-level policy: answer every dialog the moment it opens.
            accept = not policy.startswith("dismiss")
            text = policy.split(":", 1)[1] if ":" in policy else None

            def auto(p: dict) -> None:
                params = {"accept": accept}
                if text is not None and p.get("type") == "prompt":
                    params["promptText"] = text
                t = asyncio.ensure_future(cdp.send("Page.handleJavaScriptDialog", params))
                answered.append(p.get("type", "?"))
                t.add_done_callback(lambda _f: None)

            cdp.on("Page.javascriptDialogOpening", auto)

        await cdp.send("Page.navigate", {"url": URL})
        await asyncio.sleep(0.8)

        x, y = await centre_of(cdp, button)
        t0 = time.monotonic()
        outcome = {"shape": shape, "button": button}

        if shape == "control":
            tasks = await press(cdp, x, y)
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=DISPATCH_TIMEOUT)
                outcome["clickReturned"] = True
            except (TimeoutError, asyncio.TimeoutError, Exception) as exc:
                outcome["clickReturned"] = False
                outcome["clickError"] = type(exc).__name__
            outcome["clickSeconds"] = round(time.monotonic() - t0, 2)

        elif shape == "a":
            tasks = await press(cdp, x, y)
            outcome["clickReturned"] = "not awaited"
            outcome["dispatchSeconds"] = round(time.monotonic() - t0, 2)
            try:
                ev = await asyncio.wait_for(opening.get(), timeout=8)
                outcome["dialogSeen"] = {"type": ev.get("type"), "message": ev.get("message")}
                params = {"accept": True}
                if ev.get("type") == "prompt":
                    params["promptText"] = "typed-answer"
                await cdp.send("Page.handleJavaScriptDialog", params)
                outcome["handled"] = True
            except (TimeoutError, asyncio.TimeoutError):
                outcome["dialogSeen"] = None
                outcome["handled"] = False
            for t in tasks:
                t.cancel()

        elif shape == "b":
            tasks = await press(cdp, x, y)
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=DISPATCH_TIMEOUT)
                outcome["clickReturned"] = True
            except Exception as exc:
                outcome["clickReturned"] = False
                outcome["clickError"] = type(exc).__name__
            outcome["clickSeconds"] = round(time.monotonic() - t0, 2)
            outcome["autoAnswered"] = answered

        await asyncio.sleep(0.4)
        try:
            r = await cdp.send("Runtime.evaluate",
                               {"expression": "window.__r || 'none'", "returnByValue": True},
                               timeout=5)
            outcome["pageResult"] = r["result"]["value"]
        except Exception as exc:
            outcome["pageResult"] = f"unreadable: {type(exc).__name__}"

        outcome["dialogEventsSeen"] = len(seen)
        print(json.dumps(outcome, indent=2))
    return 0


if __name__ == "__main__":
    pol = sys.argv[4] if len(sys.argv) > 4 else "accept"
    sys.exit(asyncio.run(main(int(sys.argv[1]), sys.argv[2], sys.argv[3], pol)))
