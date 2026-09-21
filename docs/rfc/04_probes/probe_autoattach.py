"""What `Target.setAutoAttach` with `flatten: true` actually gives us.

Run against a live headless Chrome with a cross-origin iframe on the page.
Prints, in order: the child session arriving, what `Page.getFrameTree` on it
returns, whether the child's frame id matches the parent's Iframe node, and
what a snapshot of the child costs.
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/Users/kevin/dev/browser-tools/src")

from browser_tools.core.cdp_client import CDPClient  # noqa: E402


async def main(ws_url: str) -> None:
    client = CDPClient(ws_url)
    await client.connect()

    attached: list[dict] = []
    client.on("Target.attachedToTarget", lambda p: attached.append(p))
    client.on("Target.detachedFromTarget", lambda p: attached.append({"detached": p}))

    targets = await client.send(method="Target.getTargets")
    page = next(t for t in targets["targetInfos"] if t["type"] == "page")
    print("page target:", page["targetId"], page["url"])

    session = (await client.send(
        method="Target.attachToTarget",
        params={"targetId": page["targetId"], "flatten": True},
    ))["sessionId"]
    print("page session:", session)

    await client.send(method="Page.enable", session_id=session)
    tree = await client.send(method="Page.getFrameTree", session_id=session)
    print("parent frame tree:", json.dumps(tree["frameTree"], indent=2)[:400])

    print("\n--- setAutoAttach on the page session ---")
    await client.send(
        method="Target.setAutoAttach",
        params={"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        session_id=session,
    )
    await asyncio.sleep(1.0)
    print("attach events:", len(attached))
    for event in attached:
        info = event.get("targetInfo", {})
        print(f"  sessionId={event.get('sessionId')} type={info.get('type')} url={info.get('url')}")

    child = next((e for e in attached if e.get("targetInfo", {}).get("type") == "iframe"), None)
    if child is None:
        print("NO CHILD SESSION - setAutoAttach did not reach the OOPIF")
        await client.close()
        return

    child_session = child["sessionId"]
    child_target = child["targetInfo"]["targetId"]
    print(f"\nchild session: {child_session}  target: {child_target}")

    await client.send(method="Page.enable", session_id=child_session)
    child_tree = await client.send(method="Page.getFrameTree", session_id=child_session)
    print("child frame tree:", json.dumps(child_tree["frameTree"], indent=2)[:400])

    child_frame_id = child_tree["frameTree"]["frame"]["id"]
    print("\nchild frame id == child target id?", child_frame_id == child_target)

    print("\n--- can we read the child's DOM? ---")
    await client.send(method="DOM.enable", session_id=child_session)
    doc = await client.send(method="DOM.getDocument", session_id=child_session, params={"depth": -1})
    html = json.dumps(doc)
    print("child document nodes:", html.count('"nodeId"'))
    print("child has the card input?", "card" in html)

    print("\n--- backendNodeId overlap between parent and child ---")
    await client.send(method="DOM.enable", session_id=session)
    parent_doc = await client.send(method="DOM.getDocument", session_id=session, params={"depth": -1})

    def backend_ids(node, out):
        out.add(node.get("backendNodeId"))
        for kid in node.get("children", []):
            backend_ids(kid, out)

    pset, cset = set(), set()
    backend_ids(parent_doc["root"], pset)
    backend_ids(doc["root"], cset)
    print(f"parent ids: {len(pset)}  child ids: {len(cset)}  overlapping: {len(pset & cset)}")

    print("\n--- child storage ---")
    got = await client.send(
        method="Runtime.evaluate",
        session_id=child_session,
        params={"expression": "location.origin", "returnByValue": True},
    )
    print("child origin:", got["result"]["value"])

    await client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
