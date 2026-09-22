#!/usr/bin/env python3
"""Single-invocation trace capture, both transfer modes.

Mimics what a `bt trace --duration N` verb would do: connect, start tracing,
drive the page, end, collect, write the file. Everything inside one process,
the Bounded Capture shape.

Usage: exp-b.py <port> <mode:events|stream> <out.json>
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient, get_ws_url_async  # noqa: E402

URL = "http://127.0.0.1:17541/fixture.html"

# The DevTools-default set chrome-devtools-mcp v1.9.0 uses, per ticket 150.
CATEGORIES = [
    "-*",
    "blink.console",
    "blink.user_timing",
    "devtools.timeline",
    "disabled-by-default-devtools.screenshot",
    "disabled-by-default-devtools.timeline",
    "disabled-by-default-devtools.timeline.frame",
    "disabled-by-default-devtools.timeline.stack",
    "disabled-by-default-v8.cpu_profiler",
    "disabled-by-default-v8.cpu_profiler.hires",
    "latencyInfo",
    "loading",
    "disabled-by-default-lighthouse",
    "v8.execute",
    "v8",
]


def trace_config() -> dict:
    included = [c for c in CATEGORIES if not c.startswith("-")]
    excluded = [c[1:] for c in CATEGORIES if c.startswith("-")]
    return {
        "includedCategories": included,
        "excludedCategories": excluded,
        "recordMode": "recordUntilFull",
    }


async def main(port: int, mode: str, out: Path) -> int:
    ws = await get_ws_url_async(port=port, target_type="page")
    events: list[dict] = []
    complete: asyncio.Future = asyncio.get_running_loop().create_future()

    async with CDPClient(ws_url=ws) as cdp:
        cdp.on("Tracing.dataCollected", lambda p: events.extend(p.get("value", [])))

        def on_complete(p: dict) -> None:
            if not complete.done():
                complete.set_result(p)

        cdp.on("Tracing.tracingComplete", on_complete)

        t0 = time.monotonic()
        params = {
            "traceConfig": trace_config(),
            "transferMode": "ReturnAsStream" if mode == "stream" else "ReportEvents",
            "bufferUsageReportingInterval": 0,
        }
        if mode == "stream":
            params["streamFormat"] = "json"
        await cdp.send("Tracing.start", params)

        await cdp.send("Page.navigate", {"url": URL})
        await asyncio.sleep(3.0)  # the "--duration" of this capture

        await cdp.send("Tracing.end", {})
        done = await asyncio.wait_for(complete, timeout=30)

        data_loss = done.get("dataLossOccurred")
        stream = done.get("stream")

        if mode == "stream":
            chunks: list[str] = []
            while True:
                r = await cdp.send("IO.read", {"handle": stream, "size": 1 << 20})
                chunks.append(r.get("data", ""))
                if r.get("eof"):
                    break
            await cdp.send("IO.close", {"handle": stream})
            payload = "".join(chunks)
            out.write_text(payload)
            parsed = json.loads(payload)
            count = len(parsed["traceEvents"]) if isinstance(parsed, dict) else len(parsed)
        else:
            out.write_text(json.dumps({"traceEvents": events}))
            count = len(events)

        elapsed = time.monotonic() - t0
        names = {e.get("name") for e in (events if mode != "stream" else parsed.get("traceEvents", []))}
        print(json.dumps({
            "mode": mode,
            "dataLossOccurred": data_loss,
            "streamHandle": stream,
            "events": count,
            "bytes": out.stat().st_size,
            "seconds": round(elapsed, 2),
            "hasLargestContentfulPaint": any("largestContentfulPaint" in str(n).lower() for n in names),
            "hasLayoutShift": any("layoutshift" in str(n).lower().replace("_", "") for n in names),
            "hasNavigationStart": "navigationStart" in names,
            "out": str(out),
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))))
