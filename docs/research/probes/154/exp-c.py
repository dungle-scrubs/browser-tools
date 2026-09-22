#!/usr/bin/env python3
"""Single-invocation heap snapshot.

Mimics what a `bt heap --out FILE` verb would do: connect, ask for the
snapshot, collect the chunk events, write the file, print a summary. One
process, the Bounded Capture shape.

Usage: exp-c.py <port> <out.heapsnapshot>
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient, get_ws_url_async  # noqa: E402


async def main(port: int, out: Path) -> int:
    ws = await get_ws_url_async(port=port, target_type="page")
    chunks: list[str] = []
    progress: list[dict] = []

    async with CDPClient(ws_url=ws) as cdp:
        cdp.on("HeapProfiler.addHeapSnapshotChunk", lambda p: chunks.append(p.get("chunk", "")))
        cdp.on("HeapProfiler.reportHeapSnapshotProgress", lambda p: progress.append(p))

        t0 = time.monotonic()
        await cdp.send("HeapProfiler.enable", {})
        await cdp.send(
            "HeapProfiler.takeHeapSnapshot",
            {"reportProgress": True, "captureNumericValue": False},
            timeout=120,
        )
        payload = "".join(chunks)
        out.write_text(payload)
        elapsed = time.monotonic() - t0
        await cdp.send("HeapProfiler.disable", {})

        parsed = json.loads(payload)
        meta = parsed.get("snapshot", {}).get("meta", {})
        print(json.dumps({
            "chunks": len(chunks),
            "progressEvents": len(progress),
            "bytes": out.stat().st_size,
            "seconds": round(elapsed, 2),
            "nodeCount": parsed.get("snapshot", {}).get("node_count"),
            "edgeCount": parsed.get("snapshot", {}).get("edge_count"),
            "hasMetaNodeFields": bool(meta.get("node_fields")),
            "out": str(out),
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2]))))
