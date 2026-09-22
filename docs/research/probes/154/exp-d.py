#!/usr/bin/env python3
"""A trace that WRAPS a sequence of driven actions, in one invocation.

This is the third candidate shape for the trace verb: rather than trace steps
living inside a Step Run (proved impossible, experiments A/A2/A3), the run
lives inside the trace. One invocation starts tracing, drives the page, ends
tracing, and writes the file.

The actions here stand in for a Step List. A real verb would hand them to
step_run.execute(steps, handler) between the start and the end; the CDP
traffic is the same either way.

Usage: exp-d.py <port> <out.json>
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient, get_ws_url_async  # noqa: E402

URL = "http://127.0.0.1:17541/interact.html"

CATEGORIES = [
    "-*", "blink.console", "blink.user_timing", "devtools.timeline",
    "disabled-by-default-devtools.screenshot",
    "disabled-by-default-devtools.timeline",
    "disabled-by-default-devtools.timeline.frame",
    "disabled-by-default-devtools.timeline.stack",
    "disabled-by-default-v8.cpu_profiler",
    "disabled-by-default-v8.cpu_profiler.hires",
    "latencyInfo", "loading", "disabled-by-default-lighthouse",
    "v8.execute", "v8",
]


def trace_config() -> dict:
    return {
        "includedCategories": [c for c in CATEGORIES if not c.startswith("-")],
        "excludedCategories": [c[1:] for c in CATEGORIES if c.startswith("-")],
        "recordMode": "recordUntilFull",
    }


async def click_at(cdp: CDPClient, x: float, y: float) -> None:
    """A real pointer press and release, the way `bt click` dispatches one."""
    for kind in ("mousePressed", "mouseReleased"):
        await cdp.send("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "left",
            "clickCount": 1, "buttons": 1 if kind == "mousePressed" else 0,
        })


async def main(port: int, out: Path) -> int:
    ws = await get_ws_url_async(port=port, target_type="page")
    complete: asyncio.Future = asyncio.get_running_loop().create_future()

    async with CDPClient(ws_url=ws) as cdp:
        def on_complete(p: dict) -> None:
            if not complete.done():
                complete.set_result(p)

        cdp.on("Tracing.tracingComplete", on_complete)

        t0 = time.monotonic()
        await cdp.send("Tracing.start", {
            "traceConfig": trace_config(),
            "transferMode": "ReturnAsStream",
            "streamFormat": "json",
        })

        # ---- the wrapped "step list" starts here ----
        await cdp.send("Page.navigate", {"url": URL})
        await asyncio.sleep(1.2)

        box = await cdp.send("Runtime.evaluate", {
            "expression": "(() => {const r=document.getElementById('slow').getBoundingClientRect();"
                          "return {x: r.x + r.width/2, y: r.y + r.height/2};})()",
            "returnByValue": True,
        })
        pt = box["result"]["value"]
        await click_at(cdp, pt["x"], pt["y"])
        await asyncio.sleep(1.2)

        log = await cdp.send("Runtime.evaluate", {
            "expression": "document.getElementById('log').textContent",
            "returnByValue": True,
        })
        # ---- wrapped step list ends here ----

        await cdp.send("Tracing.end", {})
        done = await asyncio.wait_for(complete, timeout=30)

        chunks: list[str] = []
        while True:
            r = await cdp.send("IO.read", {"handle": done["stream"], "size": 1 << 20})
            chunks.append(r.get("data", ""))
            if r.get("eof"):
                break
        await cdp.send("IO.close", {"handle": done["stream"]})

        payload = "".join(chunks)
        out.write_text(payload)
        parsed = json.loads(payload)
        evs = parsed["traceEvents"]
        names = [e.get("name") for e in evs]

        print(json.dumps({
            "pageLog": log["result"]["value"],
            "dataLossOccurred": done.get("dataLossOccurred"),
            "events": len(evs),
            "bytes": out.stat().st_size,
            "seconds": round(time.monotonic() - t0, 2),
            "eventTimingEvents": sum(1 for n in names if n == "EventTiming"),
            "layoutShiftEvents": sum(1 for n in names if n == "LayoutShift"),
            "out": str(out),
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2]))))
