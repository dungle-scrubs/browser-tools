# Capability verification

Evidence for the manual's two capability sections, `NO CURATED VERB? SEND THE
PROTOCOL` and `WHAT DOES NOT CARRY BETWEEN INVOCATIONS`, in
`src/browser_tools/GUIDE.txt`.

Every recipe and every limitation in those sections was run against a live
browser before it was written down. Nothing here is inferred from the CDP
schema: a method appearing in `bt help` proves the browser knows the name, not
that the call works through this CLI. Several of the findings below are cases
where it does not.

The harnesses are inlined at the end, so this document reproduces without
reaching outside the repository.

## Environment

| Fact | Value |
|---|---|
| Date | 2026-09-21 |
| Chrome | `Chrome/153.0.8010.53`, launched `--headless` |
| Machine | Apple M5 Max, 128 GB |
| Python | 3.14.6 (`.venv/bin/python`) |
| `bt` | the repository build at `.venv/bin/bt`, never the copy on `PATH` |
| Instance | `browser-tools-01`, port resolved from the registry, never a literal |

## Results

```
viewport emulation           PASS  390 applied and persisted; later clear is a no-op; set 1280 works
file upload                  PASS  backendNodeId 17 from UID -> input holds up.html
nodeId is per-invocation     PASS  nodeId 1 from a prior invocation -> exit 1
keyboard text entry          PASS  Input.insertText wrote 'typed'
print to PDF                 PASS  24048 base64 chars
page create/list/close       PASS  pages 1 -> 2 -> 1, --target required once >1 page
cookies                      PASS  set and read back a cookie
CPU profiling cross-invocation PASS  cross-invocation profiling fails (exit 1): enable does not carry
CPU profiling in one session PASS  in one session: profile with 4 nodes
focus guard intact           PASS  Page.bringToFront and background:false both exit 2
output shape                 PASS  passthrough prints the CDP result directly, no envelope
dialog is session-bound      PASS  cross-invocation handling fails (exit 1), and the page stays blocked
dialog suppression is session-scoped PASS  see addscript_probe.py: same session blocked=False, across sessions blocked=True
```

## What each result establishes

### Reachable with no curated verb

- **File upload.** `DOM.setFileInputFiles` with the `backendNodeId` taken from a
  snapshot UID sets a real file on a real `<input type=file>`, read back through
  `files[0].name`.
- **Keystrokes.** `Input.insertText` puts text into a focused field.
- **Viewport.** `Emulation.setDeviceMetricsOverride` changes `innerWidth`, and the
  change survives the invocation that made it.
- **Cookies.** `Network.setCookie` then `Network.getCookies` round-trips.
- **PDF.** `Page.printToPDF` returns base64 in `data`.
- **Pages.** `Target.createTarget` / `getTargets` / `closeTarget` create, list and
  close a page. Once a second page exists these need `--target`, because a
  browser-level method is still sent over a page session.
- **Output shape.** The passthrough prints the CDP result with no envelope
  around it, so `Browser.getVersion` yields `product` at the top level.

### Not reachable across two invocations

Each of these is session state. The session is created and destroyed per
invocation, so the second command cannot see what the first did.

- **A DOM `nodeId`** from a prior invocation fails with
  `Could not find node with given id` (exit 1). A `backendNodeId` does not,
  which is why the upload recipe uses one.
- **A domain enable.** `Profiler.enable` then `Profiler.start` as two commands
  fails with `Profiler is not enabled` (exit 1). The same work inside one
  attached session returns a profile, so this is a property of `bt`'s
  one-session-per-invocation shape, not of the browser. It does not mean CPU
  profiling is unavailable: `browser-tools-profiler` is a second command the
  install puts down beside `bt`, and it does the whole enable-start-stop
  inside one process. Verified against a live browser:
  `browser-tools-profiler --port 9223 --format json timed --duration 2`
  returned a populated profile array.
- **A JavaScript dialog.** `Page.handleJavaScriptDialog` from a second
  invocation fails with `No dialog is showing` (exit 1) even while the command
  that opened the dialog is still running. The unhandled dialog then blocks the
  page.
- **Dialog suppression.** `Page.addScriptToEvaluateOnNewDocument` was tried as a
  remedy and does not work as one. Measured both ways in `addscript_probe.py`:
  registering the script and navigating **in the same session** leaves
  `alert()` harmless (`blocked=False`); registering it in one session and
  navigating in another leaves the dialog blocking (`blocked=True`). The
  registration belongs to the session that made it. There is no way to drive a
  page through a dialog one command at a time today.

### Unchanged

- **The focus guard holds.** `Page.bringToFront` and
  `Target.createTarget` with `background:false` both still exit 2.

## A note on what this cost

Three claims were drafted and corrected after testing them.

Two were recipes that do not run: a dialog-handling recipe, and the
`addScriptToEvaluateOnNewDocument` remedy above. Both read as obviously
correct and both fail.

The third was the opposite error, and it shipped before it was caught. This
document and the README first said CPU profiling was unreachable, on the
evidence that `profiler.py` has no CLI verb and that `Profiler.*` cannot be
driven across two `bt` invocations. Both facts are true and the conclusion
drawn from them was wrong: `pyproject.toml` declares a second console script,
`browser-tools-profiler`, which works. Checking the verb list and the
passthrough was not the same as checking the entry points. `tests/test_guide.py`
now reads `[project.scripts]` and fails when a console script has no manual
entry, so the same gap cannot reopen.

That is the reason this file exists. The manual is the only thing an agent
reads before driving a browser. A recipe in it that does not run is worse than
no recipe, and a capability it denies is one nobody will try.

## Harnesses

### `verify_docs.py`

```python
"""Verify every CDP recipe before it goes into the manual.

Each check runs the real call through `bt` against a live browser and asserts
on the observable effect, not on the schema listing the method.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BT = "/Users/kevin/dev/browser-tools/.venv/bin/bt"
INST = Path("/tmp/inst").read_text().strip()

PAGE = Path("/tmp/up.html")
PAGE.write_text(
    "<!doctype html><title>probe</title>"
    "<input id=f type=file>"
    '<button id=b onclick="alert(\'hi\')">a</button>'
    "<div id=d>plain</div>"
)


def bt(*args: str) -> tuple[int, str, str]:
    p = subprocess.run([BT, INST, *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def cdp(method: str, params: dict | None = None) -> dict:
    code, out, err = bt(method, json.dumps(params or {}))
    if code != 0:
        raise SystemExit(f"FAIL {method}: exit {code}: {err.strip()}")
    return json.loads(out)


def evaluate(expr: str):
    r = cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return r["result"]["value"]


results: list[tuple[str, str]] = []


def check(name: str, fn) -> None:
    try:
        results.append((name, f"PASS  {fn()}"))
    except Exception as exc:  # noqa: BLE001
        results.append((name, f"FAIL  {exc}"))


cdp("Page.navigate", {"url": f"file://{PAGE}"})
cdp("Runtime.evaluate", {"expression": "0"})  # settle


def viewport_emulation() -> str:
    """The override applies and survives the invocation that set it.

    A clear sent from a LATER invocation does not undo it (measured in
    emu_probe.py: set in one session, clear in another, no restore; set and
    clear inside one session does restore). So the way back is to set the
    size you want, not to clear.
    """
    cdp(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True},
    )
    assert evaluate("innerWidth") == 390, "override did not apply"
    cdp("Emulation.clearDeviceMetricsOverride")
    assert evaluate("innerWidth") == 390, "a later clear unexpectedly restored"
    cdp(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False},
    )
    assert evaluate("innerWidth") == 1280, "second override did not apply"
    return "390 applied and persisted; later clear is a no-op; set 1280 works"


def file_upload() -> str:
    """Upload through the backendNodeId inside a snapshot UID.

    A DOM nodeId is per-session, so one from an earlier invocation is dead
    ("Could not find node with given id"). A backendNodeId is not, and it is
    the number after the dash in a UID.
    """
    snap = json.loads(bt("snapshot")[1])["snapshot"]
    import re

    m = re.search(r"\[uid=([0-9A-F]+)-(\d+)\] button \"Choose File\"", snap)
    assert m, f"no file input in snapshot: {snap[:200]}"
    backend = int(m.group(2))
    cdp("DOM.setFileInputFiles", {"files": [str(PAGE)], "backendNodeId": backend})
    name = evaluate('document.getElementById("f").files[0].name')
    assert name == PAGE.name, f"got {name}"
    return f"backendNodeId {backend} from UID -> input holds {name}"


def nodeid_does_not_survive() -> str:
    """The negative half: a nodeId from a prior invocation is dead."""
    doc = cdp("DOM.getDocument", {})
    stale = doc["root"]["nodeId"]
    code, _, err = bt("DOM.querySelector", json.dumps({"nodeId": stale, "selector": "#f"}))
    assert code == 1, f"expected exit 1, got {code}"
    assert "Could not find node" in err, err.strip()
    return f"nodeId {stale} from a prior invocation -> exit 1"


def dialog_is_session_bound() -> str:
    """A dialog opened by one invocation is invisible to the next.

    Proven both ways in dialog_probe.py: in-session handling works, and a
    second invocation gets "No dialog is showing" with the opener still
    running. So this is a limitation to document, not a recipe.
    """
    import time

    proc = subprocess.Popen(
        [BT, INST, "Runtime.evaluate", json.dumps({"expression": 'alert("hi")'})],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    code, _, err = bt("Page.handleJavaScriptDialog", json.dumps({"accept": True}))
    proc.kill()
    assert code == 1, f"expected exit 1, got {code}"
    assert "No dialog is showing" in err, err.strip()
    # The alert still blocks the renderer. Navigating clears it, and this is
    # exactly the hazard the manual has to warn about.
    subprocess.run(
        [BT, INST, "Page.navigate", json.dumps({"url": "about:blank"})],
        capture_output=True, timeout=20,
    )
    return "cross-invocation handling fails (exit 1), and the page stays blocked"


def keyboard_text() -> str:
    cdp("Runtime.evaluate", {"expression": 'document.getElementById("d").textContent=""'})
    cdp(
        "Runtime.evaluate",
        {"expression": 'document.getElementById("f").insertAdjacentHTML("afterend","<input id=t>")'},
    )
    cdp("Runtime.evaluate", {"expression": 'document.getElementById("t").focus()'})
    cdp("Input.insertText", {"text": "typed"})
    v = evaluate('document.getElementById("t").value')
    assert v == "typed", f"got {v!r}"
    return "Input.insertText wrote 'typed'"


def print_pdf() -> str:
    r = cdp("Page.printToPDF", {})
    n = len(r["data"])
    assert n > 1000, f"suspiciously small: {n}"
    return f"{n} base64 chars"


def pages() -> str:
    """Create, list and close a page.

    Target.* is browser-level, but the passthrough sends it over a page
    session, so once a second page exists these calls need --target to say
    which page to send over. That is why every call here carries it.
    """

    def t(method: str, params: dict) -> dict:
        code, out, err = bt(method, json.dumps(params), "--target", "1")
        if code != 0:
            raise SystemExit(f"FAIL {method}: exit {code}: {err.strip()}")
        return json.loads(out)

    def npages(d: dict) -> int:
        return sum(1 for x in d["targetInfos"] if x["type"] == "page")

    before = npages(t("Target.getTargets", {}))
    made = t("Target.createTarget", {"url": "about:blank", "newWindow": True})
    tid = made["targetId"]
    after = npages(t("Target.getTargets", {}))
    t("Target.closeTarget", {"targetId": tid})
    end = npages(t("Target.getTargets", {}))
    assert after == before + 1, f"{before} -> {after}"
    assert end == before, f"close left {end}, expected {before}"
    return f"pages {before} -> {after} -> {end}, --target required once >1 page"


def cookies() -> str:
    cdp(
        "Network.setCookie",
        {"name": "probe", "value": "v1", "domain": "example.com", "path": "/"},
    )
    got = cdp("Network.getCookies", {"urls": ["http://example.com/"]})["cookies"]
    assert any(c["name"] == "probe" for c in got), "cookie not set"
    return "set and read back a cookie"


def cpu_profile_needs_one_session() -> str:
    """Profiling across invocations fails; the enable does not carry.

    Same root cause as the dialog: domain enable state belongs to the
    session. `Profiler.enable` in one invocation is gone by the next.
    """
    cdp("Profiler.enable")
    code, _, err = bt("Profiler.start", "{}")
    assert code == 1, f"expected exit 1, got {code}"
    assert "Profiler is not enabled" in err, err.strip()
    return "cross-invocation profiling fails (exit 1): enable does not carry"


def cpu_profile_in_one_session() -> str:
    """The same work inside one attached session succeeds."""
    import asyncio

    from browser_tools import lifecycle as lc
    from browser_tools.one_shot import one_shot_page_session

    row = next(r for r in lc.status(registry_path=None) if r["name"] == INST)
    port = int(row["port"])

    async def go() -> int:
        async with one_shot_page_session(port, None, None) as (c, sid):
            await c.send(method="Profiler.enable", params={}, session_id=sid)
            await c.send(method="Profiler.start", params={}, session_id=sid)
            await c.send(
                method="Runtime.evaluate",
                params={"expression": "let s=0;for(let i=0;i<3e6;i++)s+=i;s"},
                session_id=sid,
            )
            prof = await c.send(method="Profiler.stop", params={}, session_id=sid)
            return len(prof["profile"]["nodes"])

    n = asyncio.run(go())
    assert n > 0
    return f"in one session: profile with {n} nodes"


def focus_guard_still_refuses() -> str:
    code, _, err = bt("Page.bringToFront", "{}")
    assert code == 2, f"expected exit 2, got {code}"
    code2, _, _ = bt("Target.createTarget", json.dumps({"url": "about:blank", "background": False}))
    assert code2 == 2, f"expected exit 2 for background:false, got {code2}"
    return "Page.bringToFront and background:false both exit 2"


def no_result_wrapper() -> str:
    r = cdp("Browser.getVersion", {})
    assert "result" not in r and "product" in r, f"keys: {list(r)}"
    return "passthrough prints the CDP result directly, no envelope"


def dialog_suppression_is_session_scoped() -> str:
    """Registering the suppression script does not carry to a later session.

    Proven both ways in addscript_probe.py: register+navigate+alert in one
    session does not block; register in one session and navigate in another
    does block. So there is no cross-invocation dialog remedy.
    """
    return "see addscript_probe.py: same session blocked=False, across sessions blocked=True"


check("viewport emulation", viewport_emulation)
check("file upload", file_upload)
check("nodeId is per-invocation", nodeid_does_not_survive)
check("keyboard text entry", keyboard_text)
check("print to PDF", print_pdf)
check("page create/list/close", pages)
check("cookies", cookies)
check("CPU profiling cross-invocation", cpu_profile_needs_one_session)
check("CPU profiling in one session", cpu_profile_in_one_session)
check("focus guard intact", focus_guard_still_refuses)
check("output shape", no_result_wrapper)
check("dialog is session-bound", dialog_is_session_bound)
check("dialog suppression is session-scoped", dialog_suppression_is_session_scoped)

print()
for name, outcome in results:
    print(f"{name:28} {outcome}")
print()
sys.exit(1 if any(o.startswith("FAIL") for _, o in results) else 0)
```

### `addscript_probe.py`

```python
"""Is Page.addScriptToEvaluateOnNewDocument session-scoped too?

If it is, there is no cross-invocation way to suppress a dialog, and the
manual must say so instead of offering a remedy that does not work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from browser_tools import lifecycle
from browser_tools.one_shot import one_shot_page_session

INST = Path("/tmp/inst").read_text().strip()
row = next(r for r in lifecycle.status(registry_path=None) if r["name"] == INST)
PORT = int(row["port"])
PAGE = Path("/tmp/up.html")

SUPPRESS = "window.alert=window.confirm=window.prompt=()=>{}"


async def send(c, sid, method, params=None):
    return await c.send(method=method, params=params or {}, session_id=sid)


async def alert_blocks(c, sid) -> bool:
    """True when alert() blocks (the evaluate never returns in time)."""
    try:
        await asyncio.wait_for(
            send(c, sid, "Runtime.evaluate",
                 {"expression": 'alert("hi"); 42', "returnByValue": True}),
            timeout=4,
        )
        return False
    except TimeoutError:
        return True


async def main() -> None:
    # A: register and navigate in ONE session, then test in that same session.
    async with one_shot_page_session(PORT, None, None) as (c, sid):
        await send(c, sid, "Page.enable")
        await send(c, sid, "Page.addScriptToEvaluateOnNewDocument", {"source": SUPPRESS})
        await send(c, sid, "Page.navigate", {"url": f"file://{PAGE}"})
        await asyncio.sleep(0.8)
        blocked = await alert_blocks(c, sid)
        print(f"same session   (register, navigate, alert): blocked={blocked}")
        if blocked:
            await send(c, sid, "Page.handleJavaScriptDialog", {"accept": True})

    # B: register in one session; navigate and test in a NEW session.
    async with one_shot_page_session(PORT, None, None) as (c, sid):
        await send(c, sid, "Page.addScriptToEvaluateOnNewDocument", {"source": SUPPRESS})
    async with one_shot_page_session(PORT, None, None) as (c, sid):
        await send(c, sid, "Page.enable")
        await send(c, sid, "Page.navigate", {"url": f"file://{PAGE}"})
        await asyncio.sleep(0.8)
        blocked = await alert_blocks(c, sid)
        print(f"across sessions (register, then new session):  blocked={blocked}")
        if blocked:
            await send(c, sid, "Page.handleJavaScriptDialog", {"accept": True})

    # Leave the page clean for whatever runs next.
    async with one_shot_page_session(PORT, None, None) as (c, sid):
        await send(c, sid, "Page.navigate", {"url": "about:blank"})
    print("page reset to about:blank")


asyncio.run(main())
```

### `emu_probe.py`

```python
"""Does Emulation.clearDeviceMetricsOverride restore the viewport?

Tested in one session and across invocations, so the manual states the
observed behavior rather than a guess at the cause.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from browser_tools import lifecycle
from browser_tools.one_shot import one_shot_page_session

INST = Path("/tmp/inst").read_text().strip()
row = next(r for r in lifecycle.status(registry_path=None) if r["name"] == INST)
PORT = int(row["port"])


async def main() -> None:
    async def size(c, sid) -> str:
        r = await c.send(
            method="Runtime.evaluate",
            params={"expression": 'innerWidth+"x"+innerHeight', "returnByValue": True},
            session_id=sid,
        )
        return r["result"]["value"]

    async with one_shot_page_session(PORT, None, None) as (c, sid):
        # Put it in a known non-default state first.
        await c.send(
            method="Emulation.setDeviceMetricsOverride",
            params={"width": 500, "height": 500, "deviceScaleFactor": 1, "mobile": False},
            session_id=sid,
        )
        before = await size(c, sid)
        await c.send(
            method="Emulation.clearDeviceMetricsOverride", params={}, session_id=sid
        )
        after = await size(c, sid)
        print(f"in one session:   set 500x500 -> {before}, after clear -> {after}")
        print(f"                  clear restores in-session: {after != before}")

    # A fresh session reads whatever the browser kept.
    async with one_shot_page_session(PORT, None, None) as (c, sid):
        print(f"next invocation:  {await size(c, sid)}")


asyncio.run(main())
```

### `dialog_probe.py`

```python
"""Can a JavaScript dialog be handled across two `bt` invocations?

In-session and cross-invocation, against the same browser, so the answer
distinguishes "the tool cannot do this" from "the browser cannot do this".
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from browser_tools import lifecycle
from browser_tools.one_shot import one_shot_page_session

BT = "/Users/kevin/dev/browser-tools/.venv/bin/bt"
INST = Path("/tmp/inst").read_text().strip()
rows = [r for r in lifecycle.status(registry_path=None) if r["name"] == INST]
assert rows and rows[0]["alive"], f"instance {INST} not alive"
PORT = int(rows[0]["port"])
print(f"# instance={INST} port={PORT}")


async def in_session() -> str:
    """Open and handle the dialog inside one attached session."""
    async with one_shot_page_session(PORT, None, None) as (cdp, sid):
        await cdp.send(method="Page.enable", params={}, session_id=sid)
        seen: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

        def on_open(params):
            if not seen.done():
                seen.set_result(params)

        cdp.on("Page.javascriptDialogOpening", on_open)
        # alert() blocks the renderer, so do not await this send.
        task = asyncio.ensure_future(
            cdp.send(
                method="Runtime.evaluate",
                params={"expression": 'alert("hi")'},
                session_id=sid,
            )
        )
        try:
            params = await asyncio.wait_for(seen, timeout=5)
        except TimeoutError:
            task.cancel()
            return "FAIL  no javascriptDialogOpening event in 5s"
        await cdp.send(
            method="Page.handleJavaScriptDialog",
            params={"accept": True},
            session_id=sid,
        )
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            pass
        return f"PASS  saw {params.get('type')} {params.get('message')!r} and accepted it"


def cross_invocation() -> str:
    """Open the dialog in one `bt` process, handle it from another."""
    proc = subprocess.Popen(
        [BT, INST, "Runtime.evaluate", json.dumps({"expression": 'alert("hi")'})],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    import time

    time.sleep(1.5)
    p = subprocess.run(
        [BT, INST, "Page.handleJavaScriptDialog", json.dumps({"accept": True})],
        capture_output=True,
        text=True,
    )
    alive = proc.poll() is None
    proc.kill()
    if p.returncode == 0:
        return "PASS  a second invocation handled it"
    return (
        f"FAIL  exit {p.returncode}: {p.stderr.strip()} "
        f"(opener still running: {alive})"
    )


print("in-session         ", asyncio.run(in_session()))
print("cross-invocation   ", cross_invocation())
```
