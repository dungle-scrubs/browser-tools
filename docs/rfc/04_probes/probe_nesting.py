"""Does auto-attach reach a grandchild, and does a same-origin frame get a target?

A embeds B embeds C, three origins. `Target.setAutoAttach` on the page
session reaches B. Whether it reaches C without being re-sent on B's session
decides whether the implementation recurses.
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient  # noqa: E402


async def main(ws_url: str, _parent_url: str) -> None:
    client = CDPClient(ws_url)
    await client.connect()
    attached: list[dict] = []
    client.on("Target.attachedToTarget", lambda p: attached.append(p))

    targets = await client.send(method="Target.getTargets")
    page = next(t for t in targets["targetInfos"] if t["type"] == "page")
    session = (await client.send(
        method="Target.attachToTarget",
        params={"targetId": page["targetId"], "flatten": True},
    ))["sessionId"]
    await client.send(method="Page.enable", session_id=session)

    await client.send(
        method="Target.setAutoAttach",
        params={"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        session_id=session,
    )
    await asyncio.sleep(1.0)

    def show(label):
        print(f"{label}: {len(attached)} attach events")
        for event in attached:
            info = event["targetInfo"]
            same = info["targetId"] == page["targetId"]
            print(f"   type={info['type']:8} url={info['url']:32} "
                  f"session={event['sessionId'][:8]} isOurPage={same}")

    show("after setAutoAttach on the page session")
    iframes = [e for e in attached if e["targetInfo"]["type"] == "iframe"]
    if not iframes:
        print("no iframe session"); await client.close(); return

    child_session = iframes[0]["sessionId"]
    tree = (await client.send(method="Page.getFrameTree", session_id=child_session))["frameTree"]

    def walk(node, depth=0):
        print("   " + "  " * depth + f"{node['frame']['id'][:8]} {node['frame']['url']}")
        for kid in node.get("childFrames", []):
            walk(kid, depth + 1)

    print("\nchild session's own frame tree (a same-origin frame of the child shows here):")
    walk(tree)

    attached.clear()
    t0 = time.monotonic()
    await client.send(
        method="Target.setAutoAttach",
        params={"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        session_id=child_session,
    )
    await asyncio.sleep(1.0)
    print(f"\nsetAutoAttach re-sent on the child session ({(time.monotonic() - t0) * 1000:.1f} ms)")
    show("  grandchild")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
