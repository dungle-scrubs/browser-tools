"""The edges auto-attach has to survive, measured.

1. Does the child's `parentId` name a frame in the parent's own tree?
2. What is that second `page` attach event?
3. Does a child navigation keep the session, or replace it?
4. Does removing the iframe detach the session, and do we hear about it?
5. Does a parent navigation take the child session with it?
6. What does auto-attach cost in wall-clock?
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient  # noqa: E402


def frame_ids(node, out=None):
    out = [] if out is None else out
    out.append(node["frame"]["id"])
    for kid in node.get("childFrames", []):
        frame_ids(kid, out)
    return out


async def main(ws_url: str, parent_url: str) -> None:
    client = CDPClient(ws_url)
    await client.connect()
    events: list[tuple[float, str, dict]] = []
    start = time.monotonic()

    def note(name):
        return lambda p: events.append((time.monotonic() - start, name, p))

    for name in ("Target.attachedToTarget", "Target.detachedFromTarget", "Target.targetInfoChanged"):
        client.on(name, note(name))

    targets = await client.send(method="Target.getTargets")
    page = next(t for t in targets["targetInfos"] if t["type"] == "page")
    session = (await client.send(
        method="Target.attachToTarget",
        params={"targetId": page["targetId"], "flatten": True},
    ))["sessionId"]
    await client.send(method="Page.enable", session_id=session)

    tree = (await client.send(method="Page.getFrameTree", session_id=session))["frameTree"]
    parent_ids = frame_ids(tree)
    print("1. parent tree frame ids:", parent_ids)

    t0 = time.monotonic()
    await client.send(
        method="Target.setAutoAttach",
        params={"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        session_id=session,
    )
    cost = (time.monotonic() - t0) * 1000
    await asyncio.sleep(0.8)
    print(f"6. setAutoAttach returned in {cost:.1f} ms")

    print("\n2. every attach event:")
    for when, name, params in events:
        info = params.get("targetInfo", {})
        print(f"   {when * 1000:7.1f}ms {name:28} session={params.get('sessionId', '')[:8]} "
              f"type={info.get('type', '')} url={info.get('url', '')} "
              f"opener={info.get('openerId', '')[:8]} attached={info.get('attached')}")

    child = next(
        (p for _, n, p in events
         if n == "Target.attachedToTarget" and p["targetInfo"]["type"] == "iframe"),
        None,
    )
    if child is None:
        print("no child session"); await client.close(); return
    child_session = child["sessionId"]
    child_tree = (await client.send(method="Page.getFrameTree", session_id=child_session))["frameTree"]
    parent_of_child = child_tree["frame"].get("parentId")
    print(f"\n1. child parentId={parent_of_child}  in the parent's tree? "
          f"{parent_of_child in parent_ids}")

    events.clear(); start = time.monotonic()
    print("\n3. navigate the child within its own origin")
    await client.send(
        method="Page.navigate",
        session_id=child_session,
        params={"url": "http://localhost:8128/index.html?v=2"},
    )
    await asyncio.sleep(0.8)
    for when, name, params in events:
        print(f"   {when * 1000:7.1f}ms {name} session={params.get('sessionId', '')[:8]}")
    still = await client.send(method="Page.getFrameTree", session_id=child_session)
    print("   child session still answers:", still["frameTree"]["frame"]["url"])

    events.clear(); start = time.monotonic()
    print("\n4. remove the iframe from the parent")
    await client.send(
        method="Runtime.evaluate",
        session_id=session,
        params={"expression": "document.querySelector('iframe').remove()"},
    )
    await asyncio.sleep(0.8)
    for when, name, params in events:
        print(f"   {when * 1000:7.1f}ms {name} session={params.get('sessionId', '')[:8]} "
              f"reason={params.get('reason', '')}")
    try:
        await client.send(method="Page.getFrameTree", session_id=child_session, timeout=2)
        print("   child session STILL answers after removal")
    except Exception as exc:
        print("   child session is gone:", type(exc).__name__, str(exc)[:80])

    events.clear(); start = time.monotonic()
    print("\n5. navigate the parent back, then away")
    await client.send(method="Page.navigate", session_id=session, params={"url": parent_url})
    await asyncio.sleep(1.2)
    for when, name, params in events:
        info = params.get("targetInfo", {})
        print(f"   {when * 1000:7.1f}ms {name} session={params.get('sessionId', '')[:8]} "
              f"type={info.get('type', '')} url={info.get('url', '')}")
    print("   auto-attach survived the parent navigation:",
          any(n == "Target.attachedToTarget" for _, n, _ in events))

    await client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
