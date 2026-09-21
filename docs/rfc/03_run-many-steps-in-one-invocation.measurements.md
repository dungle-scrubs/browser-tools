# RFC-03 measurements

Evidence for `docs/rfc/03_run-many-steps-in-one-invocation.rfc.md`. Every figure
here was measured on the machine and date below. Nothing in this file is
estimated. The harnesses are inlined at the end, so the whole document is
reproducible without reaching outside the repository.

## Environment

| Fact | Value |
|---|---|
| Date | 2026-09-21 |
| Repository | `browser-tools` at `bf87ad8`, branch `main`, clean at measurement time |
| Python | 3.14.6 (`.venv/bin/python`) |
| Chrome | `Chrome/153.0.8010.50`, launched `--headless` |
| Machine | Apple M5 Max, 128 GB |
| Instance | `browser-tools-01`, no profile, stopped after each run. Port 9222 for every series except the flow B n=20 retake, which got 9223; the launch JSON is quoted where it matters. |

### Which `bt` produced these numbers

**The repository build at `.venv/bin/bt`, not the `bt` on `PATH`.**

`~/.local/bin/bt` is a uv tool install of version 0.3.0 dated 2026-09-20 09:37. It
predates the version 6 CLI grammar and the version 6 UID scheme:

- `bt browser-tools-01 snapshot` fails on it with
  `error: 'snapshot' does not look like Domain.method` (exit 2). That is the
  documented form at `GUIDE.txt:17`. The stale copy has no
  `_split_leading_instance`, which `cli.py:828` added.
- It mints UIDs of the form `1-1`. The repository build mints `B176A5CE9530-2`,
  the `<docToken>-<backendNodeId>` form RFC-01 version 6 specifies.

Every series below was run against `.venv/bin/bt`. The one exception is marked.

## Method

- Timing uses zsh's `EPOCHREALTIME`, read immediately before and after the
  command, so no subprocess runs inside the timed window.
- 12 runs per single-verb series, 5 runs per ten-step series. Median reported, with
  min and max so the spread is visible.
- Every run exited 0. Exit codes were recorded per run.
- The page under test is a `file://` page with one `<iframe>` whose URL contains
  `child`, so `frames select child` resolves.

## Single verbs

| Series | n | min ms | median ms | max ms |
|---|---|---|---|---|
| `python -c pass` (bare interpreter) | 12 | 12.3 | **12.5** | 13.0 |
| `python -c "import browser_tools.cli"` | 12 | 51.1 | **52.3** | 53.3 |
| `python -c "import browser_tools.curated"` | 12 | 49.5 | **51.5** | 57.1 |
| `bt guide` | 12 | 53.4 | **54.2** | 55.9 |
| `bt status`, empty registry *(stale `bt`)* | 12 | 51.9 | **52.9** | 54.8 |
| `bt status`, one instance registered | 12 | 65.7 | **67.7** | 70.9 |
| `bt I Runtime.evaluate` | 12 | 72.2 | **73.2** | 76.4 |
| `bt I frames list` | 12 | 82.3 | **85.5** | 89.5 |
| `bt I snapshot` | 12 | 82.8 | **86.5** | 90.4 |
| `bt I screenshot` | 12 | 81.8 | **94.3** | 102.2 |

## What each layer costs

Each line subtracts the medians of two adjacent series.

| Layer | Cost |
|---|---|
| Python interpreter start | 12.5 ms |
| `import browser_tools.cli` | 39.8 ms |
| Registry read plus liveness probe | 15.4 ms |
| **CDP connect, attach, one round trip, detach** | **5.6 ms** |
| A snapshot itself, above a bare One-Shot Session | 13.3 ms |

**The CDP work a One-Shot Session does is 5.6 ms.** The per-invocation cost is not
the connection. It is 52.3 ms of interpreter start and import, spent before the
process knows what it was asked to do, plus 15.4 ms finding the browser.

## Flow A: handler-routed verbs

Ten steps, identical in both shapes:

```
wait-idle, snapshot, frames list, frames select child,
storage get --key child, frames reset, snapshot, wait-stable,
frames list, frames select child
```

| Shape | n | min ms | median | max ms |
|---|---|---|---|---|
| Ten invocations | 5 | 1683.5 | **1.70 s** | 1737.5 |
| One process, one CDP session | 5 | 841.8 | **0.85 s** | 853.1 |
| Saving | | | **0.85 s, 50%** | |

Most of the 0.85 s floor is `wait-idle` and `wait-stable` sitting out their quiet
windows, 500 ms and 300 ms by default. Those are intrinsic and no verb removes
them. What one invocation removes is the other 0.85 s, which is nine repetitions
of the per-invocation tax.

Every verb in flow A already runs over a `CDPHandler`. That is the limit of what
flow A evidences, and it is why flow B exists.

## Flow B: the One-Shot Session transport

Added to answer the version 2 review's finding M2: flow A said nothing about the
verbs that open their own One-Shot Session today. Narrowed to answer the version 3
review's finding N-J2, which is right about what these ten steps cover.

Ten steps, identical in both shapes, none carrying an intrinsic wait:

```
Runtime.evaluate, Page.getNavigationHistory, Page.getLayoutMetrics,
DOM.getDocument, screenshot, Runtime.evaluate, Browser.getVersion,
Page.getFrameTree, screenshot, Network.getAllCookies
```

Eight raw `Domain.method` calls and two `screenshot`s. **That is two of the five
One-Shot Session verbs, `passthrough` and `screenshot`, not all five.** `wait`,
`console-list` and `network-list` are absent on purpose: their cost is dominated
by their own duration windows, 2 seconds by default for the list verbs and 30 for
`wait` (`GUIDE.txt:115-122`, `:159-166`), which one invocation does not remove.
Measuring them here would report their windows, not the transport.

| Shape | n | min ms | median | max ms |
|---|---|---|---|---|
| Ten invocations | 20 | 843.1 | **870.1 ms** | 989.4 |
| One browser-level connection, one attached session | 20 | 57.6 | **83.0 ms** | 84.4 |
| Saving | | | **787.1 ms, 90%** | |

Per step: **87.0 ms falls to 8.3 ms.**

The n=20 samples answer the version 3 review's finding N-J3, which objected to a
90% headline drawn from five runs over a 42% spread. The figure does not depend on
picking favourable runs: the fastest ten-invocation run (843.1 ms) against the
slowest one-session run (84.4 ms) is still a 90.0% saving.

The floor probe uses the connection model RFC-03 specifies: one browser-level CDP
connection plus one attached flattened `Target` session, through the existing
`one_shot_page_session`. It implements nothing the RFC specifies.

### One asymmetry, in the direction that favours a critic

The floor side issues bare `cdp.send` calls. It therefore skips work the
ten-invocation side pays for: the screenshot blank-frame retry loop
(`curated.py:534-546`), the file write, and JSON rendering of each result. **The
true floor for a built `run` verb is somewhat above 83.0 ms.** The measurement
bounds the transport saving, not the finished verb.

### A discarded run, and the defect it exposed

The n=20 series above is the second attempt. The first was discarded, because the
harness drove the wrong browser.

`oneshot_floor_probe.py` held `PORT = 9222` as a literal. That had been correct by
luck: 9222 is the first port the launcher hands out, and every earlier series got
it (`launch.json`, `launch2.json`, `launch3.json` all record port 9222). The n=20
retake launched a fresh instance, and by then another browser on this machine held
9222, so the launcher gave `browser-tools-01` port **9223**
(`launch-flowB.json`). The floor side ran its ten steps against a browser that was
not the one the ten-invocation side was timing, on a different page, with a
different profile. The two sides were not the same work, so that attempt's data
was discarded and is not in this file.

Ten reads reached the wrong browser, twenty times: two `Page.captureScreenshot`,
one `Network.getAllCookies`, and seven other read-only `Domain.method` calls.
Every one was a read; nothing was mutated, nothing was navigated, and the browser
was not stopped. Every response was discarded in the probe. Only elapsed
milliseconds were printed or written, so no page content, cookie value or
screenshot reached any file.

The harness now resolves its port from the registry by instance name, refuses to
run when the name is absent from the registry or the instance is not alive, and
re-reads the port after the run, voiding the measurement if it moved. The n=20
figures above were taken with that version against port 9223, confirmed before
and after.

**What this does and does not void.** It voids only the discarded attempt. The
version 3 n=5 flow B figures, 787.8 ms against 82.7 ms, were taken against
`browser-tools-01` on port 9222 when that instance did hold 9222 (`launch3.json`),
so they measured the right browser. They are superseded by the n=20 series because
five runs over a 42% spread cannot carry a 90% headline, which is finding N-J3,
not because they are wrong. Flow A and the enable probe were also on 9222 with the
registry agreeing (`launch.json`, `launch2.json`), and are unaffected.

## Reading flow A and flow B together

The two disagree, 50% against 90%, and the reason is entirely the intrinsic waits.

| | Flow A | Flow B |
|---|---|---|
| n | 5 | 20 |
| Ten invocations | 1696.3 ms | 870.1 ms |
| One session | 849.1 ms | 83.0 ms |
| Of which intrinsic wait | about 800 ms | 0 ms |
| Saving | 50% | 90% |

The per-step tax removed is about the same in both: roughly 70 to 85 ms. The
percentage differs because flow A's floor is mostly the browser waiting on purpose.
**90% is the honest figure for a flow with no waits in it, and 50% for a flow with
two.** Neither is the headline on its own; the per-step figure is.

Two bounds in the direction that favours a critic. Flow B's floor side skips
verb-level work the invocation side pays, so its floor is understated; the section
above states which work. And flow A's floor probe runs
`select_frame` twice where a true single-session run needs it once, because the
earlier selection would persist. The overstatement is a few milliseconds.

## Frame selection does not survive a process

Reproduced against the repository build, one instance, the iframe page:

| Step | Process | Command | Result |
|---|---|---|---|
| 1 | A | `bt I frames select child` | exit 0, selects `B5C33E7A...` |
| 2 | B | `bt I storage get` | **exit 1**, stdout empty, stderr `error: Error: No frame selected. Use select_frame first.` |
| 3 | C | `bt I storage get --key child` | exit 0, reads the child frame |
| 4 | D | `bt I frames list` | lists both frames, reports no selection |

Step 2's exit code was captured directly, not through a pipe. This matches
`GUIDE.txt:131` and the exit-code contract.

The cause is structural. Selection is two fields on the frame manager,
`_selected_frame_id` and `_selected_url_pattern` (`frame_manager.py:89-90`). The
frame manager belongs to **CDPRuntime**, which `CONTEXT.md` defines as built and
torn down once per invocation. `curated.py:427-428` says so in its own docstring.

### How many consumers frame selection has: one

This count withdrew RFC-03's original lead motivation, so it is recorded here.

`get_selected_frame` and `get_selected_execution_context_id` have three call sites
in the whole tree:

| Call site | What it does |
|---|---|
| `cdp_handler.py:816` | Inside `_handle_select_frame`, printing the execution context id back in its own confirmation text |
| `cdp_handler.py:856` | Inside `_handle_get_frame_storage` |
| `cdp_handler.py:863` | Inside `_handle_get_frame_storage` |

So `get_frame_storage` is the only tool that consumes frame selection. On the CLI
that is `storage get`, which already has `--key`. `--key` serves every consumer
that exists, not a fraction of them.

Versions 1 and 2 of the RFC called frame-selection continuity the stronger
motivation and said "the workaround covers `storage get` and nothing else. Every
other frame-scoped read has no equivalent flag." There is no other frame-scoped
read. Version 3 withdraws the claim.

## Domain-enable state persists across steps, and that changes behavior

Found by the drafting session while the cross-family review was in flight. It
corroborates the review's blocking finding B1 and measures its consequence.

**1. An event with no registered callback is dropped.** `cdp_client.py:158-161`
dispatches only to registered callbacks. There is no client-side buffer.

**2. `wait` registers its handler before the enable, deliberately.**
`events.py:344` registers, `events.py:350` enables. The docstring at
`events.py:323-330` states the ordering "is the whole point (RFC-01 'wait
design')". RFC-01:193 makes it normative.

**3. Nothing ever disables a domain.** Neither `events.py` nor `list_verbs.py`
sends `Domain.disable`. Today that is harmless, because the One-Shot Session is
detached when the invocation ends, so the enable dies with it.

**4. A reused session does not replay on a second enable.** Measured with
`enable_probe.py`:

| Case | `Runtime.enable` | `executionContextCreated` delivered |
|---|---|---|
| One session, first enable | first | **2** |
| One session, second enable | second | **0** |
| Fresh session A, one enable | first | **2** |
| Fresh session B, one enable | first | **2** |

A fresh session replays existing state. A reused session's second enable replays
nothing.

**What it breaks.** `GUIDE.txt:121-122` documents `console-list` and
`network-list`: "Both subscribe before enabling their domain, so an event emitted
during enable is in the result." Inside a Step Run the second `console-list` gets
no such guarantee, because its enable is a no-op on an already-enabled domain.

RFC-03 version 3, Design, "One CDPRuntime for the run", answers this: a step
disables every domain it enabled, except `Page` and `Runtime`, which belong to the
run's CDPRuntime.

## A defect found along the way, not fixed

Step 2 above fails with `error: Error: No frame selected. Use select_frame first.`
There is no `select_frame` verb on the CLI. The verb is `frames select`.
`select_frame` is the internal tool name, reaching the user unmapped. RFC-01
requires every refusal to give a remedy that works. Recorded, not fixed: the run
that found it was drafting a specification and changed no code.

## Harnesses

Shown as run. Paths are this machine's checkout at `/Users/kevin/dev/browser-tools`;
a reader reproducing these substitutes their own.

### `measure.sh`, single verbs

```bash
#!/bin/zsh
# RFC-03 measurement harness. Times "$BT" verbs over N runs, writes raw ms to raw.tsv.
# Timing uses zsh's EPOCHREALTIME so no subprocess is spawned inside the timed window.
set -u
zmodload zsh/datetime
INST="$1"
BT=/Users/kevin/dev/browser-tools/.venv/bin/bt   # the repo build, not the stale ~/.local/bin/bt (0.3.0)
N=12
OUT=/Users/kevin/dev/browser-tools/.scratch/rfc-03-run/raw.tsv
: > "$OUT"

run_series() {
  local label="$1"; shift
  local i t0 t1 rc
  for i in {1..$N}; do
    t0=$EPOCHREALTIME
    "$@" >/dev/null 2>&1
    rc=$?
    t1=$EPOCHREALTIME
    printf '%s\t%d\t%.1f\t%d\n' "$label" "$i" $(( (t1 - t0) * 1000 )) "$rc" >> "$OUT"
  done
}

run_series status            "$BT" status
run_series guide             "$BT" guide
run_series runtime-evaluate  "$BT" "$INST" Runtime.evaluate '{"expression":"1+1"}'
run_series snapshot          "$BT" "$INST" snapshot
run_series frames-list       "$BT" "$INST" frames list
run_series screenshot        "$BT" "$INST" screenshot

echo "--- raw written to $OUT ---"
wc -l < "$OUT"
```

### `flow.sh`, flow A as ten invocations

```bash
#!/bin/zsh
# Ten browser-driving steps, each its own CLI invocation. Timed end to end.
# The same ten steps run in one process in floor_probe.py.
set -u
zmodload zsh/datetime
BT=/Users/kevin/dev/browser-tools/.venv/bin/bt
I=browser-tools-01
t0=$EPOCHREALTIME
$BT $I wait-idle               >/dev/null 2>&1
$BT $I snapshot                >/dev/null 2>&1
$BT $I frames list             >/dev/null 2>&1
$BT $I frames select child     >/dev/null 2>&1
$BT $I storage get --key child >/dev/null 2>&1
$BT $I frames reset            >/dev/null 2>&1
$BT $I snapshot                >/dev/null 2>&1
$BT $I wait-stable             >/dev/null 2>&1
$BT $I frames list             >/dev/null 2>&1
$BT $I frames select child     >/dev/null 2>&1
t1=$EPOCHREALTIME
printf '%.1f\n' $(( (t1 - t0) * 1000 ))
```

### `floor_probe.py`, flow A in one process

```python
"""Measure the floor a single-invocation script verb could reach.

Runs the SAME ten steps as flow.sh, but inside one process holding one
CDPHandler session for all ten, and reports wall time. This is evidence for
RFC-03's Motivation. It is not the specified verb and implements nothing: it
is the cost of the same work once the per-invocation tax is paid once.
"""

from __future__ import annotations

import statistics
import sys
import time

from browser_tools import curated

PORT = 9222


def ten_steps(handler) -> None:
    t = curated._tool_or_raise
    n = curated._native_or_raise
    t(handler, "wait_idle", {"timeout_ms": 5000, "idle_ms": 500})
    n(handler, "take_snapshot", {})
    t(handler, "list_frames", {})
    t(handler, "select_frame", {"url_pattern": "child"})
    # storage get --key child is select_frame then get_frame_storage
    t(handler, "select_frame", {"url_pattern": "child"})
    t(handler, "get_frame_storage", {})
    t(handler, "reset_frame", {})
    n(handler, "take_snapshot", {})
    t(handler, "wait_stable", {"timeout_ms": 5000, "stable_ms": 300})
    t(handler, "list_frames", {})
    t(handler, "select_frame", {"url_pattern": "child"})


def main() -> int:
    runs: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        with curated._cdp_handler_session(PORT) as handler:
            ten_steps(handler)
        runs.append((time.perf_counter() - t0) * 1000)
    for i, ms in enumerate(runs, 1):
        print(f"floor-10-step-one-process\t{i}\t{ms:.1f}")
    print(f"# median {statistics.median(runs):.1f} ms over {len(runs)} runs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### `flow-oneshot.sh`, flow B as ten invocations

```bash
#!/bin/zsh
# Ten steps over the One-Shot Session transport, as ten invocations.
# Eight raw Domain.method calls and two screenshots: passthrough and screenshot,
# not all five One-Shot Session verbs. The same ten run over one session in
# oneshot_floor_probe.py.
set -u
zmodload zsh/datetime
BT=/Users/kevin/dev/browser-tools/.venv/bin/bt
I=browser-tools-01
t0=$EPOCHREALTIME
$BT $I Runtime.evaluate '{"expression":"1+1"}'   >/dev/null 2>&1
$BT $I Page.getNavigationHistory '{}'            >/dev/null 2>&1
$BT $I Page.getLayoutMetrics '{}'                >/dev/null 2>&1
$BT $I DOM.getDocument '{}'                      >/dev/null 2>&1
$BT $I screenshot                                >/dev/null 2>&1
$BT $I Runtime.evaluate '{"expression":"document.title"}' >/dev/null 2>&1
$BT $I Browser.getVersion '{}'                   >/dev/null 2>&1
$BT $I Page.getFrameTree '{}'                    >/dev/null 2>&1
$BT $I screenshot                                >/dev/null 2>&1
$BT $I Network.getAllCookies '{}'                >/dev/null 2>&1
t1=$EPOCHREALTIME
printf '%.1f\n' $(( (t1 - t0) * 1000 ))
```

### `oneshot_floor_probe.py`, flow B over one session

The version shown is the corrected one, which resolves its port from the registry.
Earlier runs used a literal `PORT = 9222`; see "A discarded run, and the defect it
exposed" above for which data that affects.

```python
"""Floor for the five verbs that route through a One-Shot Session today.

RFC-03 finding M2: the original harnesses exercised only handler-routed verbs, so
the flow A figure said nothing about `passthrough`, `wait`, `console-list`,
`network-list` and `screenshot`. This runs the same ten steps as flow-oneshot.sh
over the connection model RFC-03 specifies: one browser-level CDP connection plus
one attached flattened Target session.

It implements nothing RFC-03 specifies. It measures what the same work costs once
the per-invocation tax is paid once.

WHY THIS TAKES AN INSTANCE NAME
-------------------------------
An earlier version hardcoded PORT = 9222. Another session launched a browser on
9222 with a real logged-in profile, this probe's own browser was given a different
port, and the probe drove the wrong browser for twenty runs. The numbers were void
and it sent a cookie-read to a browser it did not own.

So the port is never assumed. It is resolved from the registry for the named
instance, and the probe refuses to run if that instance is missing or not alive.
Pass the instance name; there is no default.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

from browser_tools import lifecycle
from browser_tools.one_shot import one_shot_page_session


def resolve_port(instance: str) -> int:
    """Port for this instance, from the registry. Never a literal."""
    rows = [r for r in lifecycle.status(registry_path=None) if r["name"] == instance]
    if not rows:
        raise SystemExit(
            f"refusing to run: no instance named {instance!r} in the registry. "
            f"Launch one and pass its name."
        )
    row = rows[0]
    if not row.get("alive"):
        raise SystemExit(f"refusing to run: instance {instance!r} is not alive.")
    return int(row["port"])


async def ten_steps(cdp, sid: str) -> None:
    async def s(method: str, params: dict | None = None):
        return await cdp.send(method=method, params=params or {}, session_id=sid)

    await s("Runtime.evaluate", {"expression": "1+1"})
    await s("Page.getNavigationHistory")
    await s("Page.getLayoutMetrics")
    await s("DOM.getDocument")
    await s("Page.captureScreenshot")
    await s("Runtime.evaluate", {"expression": "document.title"})
    await s("Browser.getVersion")
    await s("Page.getFrameTree")
    await s("Page.captureScreenshot")
    await s("Network.getAllCookies")


async def one_run(port: int) -> float:
    t0 = time.perf_counter()
    async with one_shot_page_session(port, None, None) as (cdp, sid):
        await ten_steps(cdp, sid)
    return (time.perf_counter() - t0) * 1000


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("instance", help="registry instance name; the port comes from it")
    ap.add_argument("--runs", type=int, default=20)
    args = ap.parse_args()

    port = resolve_port(args.instance)
    print(f"# instance={args.instance} port={port} runs={args.runs}", file=sys.stderr)

    runs = [await one_run(port) for _ in range(args.runs)]

    # The instance must still be the same one on the same port afterwards. A port
    # that changed hands mid-run invalidates every number above.
    if resolve_port(args.instance) != port:
        raise SystemExit("port changed during the run; the measurement is void")

    for i, ms in enumerate(runs, 1):
        print(f"oneshot-floor-10-step-one-session\t{i}\t{ms:.1f}")
    print(f"# median {statistics.median(runs):.1f} ms", file=sys.stderr)
    return 0


raise SystemExit(asyncio.run(main()))
```

### `enable_probe.py`, the domain-enable replay test

```python
"""Does a second Domain.enable on an ALREADY-ENABLED session replay buffered state?

Decides whether a Step Run changes observable behavior for `wait`, `console-list`
and `network-list`, which today always run on a fresh One-Shot Session.

Runtime.enable is the clean test: on a fresh session it emits
Runtime.executionContextCreated for every context that already exists. That is a
replay of existing state, which is exactly what a reused session would miss.
"""

from __future__ import annotations

import asyncio

from browser_tools.core.cdp_client import CDPClient, get_ws_url_async

PORT = 9222


async def attach(cdp: CDPClient) -> str:
    targets = await cdp.send(method="Target.getTargets")
    pages = sorted(
        (t for t in targets.get("targetInfos", []) if t.get("type") == "page"),
        key=lambda t: t.get("targetId", ""),
    )
    att = await cdp.send(
        method="Target.attachToTarget",
        params={"targetId": pages[0]["targetId"], "flatten": True},
    )
    return att["sessionId"]


async def main() -> None:
    ws = await get_ws_url_async(port=PORT, target_type="browser")

    # --- Case A: one session, two enables. This is a Step Run. ---
    async with CDPClient(ws_url=ws) as cdp:
        sid = await attach(cdp)
        got: list[str] = []
        cdp.on(
            event="Runtime.executionContextCreated",
            callback=lambda p: got.append(p.get("context", {}).get("id", "?")),
            session_id=sid,
        )
        await cdp.send(method="Runtime.enable", session_id=sid)
        await asyncio.sleep(0.5)
        first = len(got)
        got.clear()
        # A later step in the same run re-enables the same domain.
        await cdp.send(method="Runtime.enable", session_id=sid)
        await asyncio.sleep(0.5)
        second = len(got)
        await cdp.send(method="Target.detachFromTarget", params={"sessionId": sid})

    # --- Case B: two separate sessions, one enable each. This is today. ---
    async with CDPClient(ws_url=ws) as cdp:
        sid = await attach(cdp)
        got_b: list[str] = []
        cdp.on(
            event="Runtime.executionContextCreated",
            callback=lambda p: got_b.append(p.get("context", {}).get("id", "?")),
            session_id=sid,
        )
        await cdp.send(method="Runtime.enable", session_id=sid)
        await asyncio.sleep(0.5)
        fresh_1 = len(got_b)
        await cdp.send(method="Target.detachFromTarget", params={"sessionId": sid})

    async with CDPClient(ws_url=ws) as cdp:
        sid = await attach(cdp)
        got_c: list[str] = []
        cdp.on(
            event="Runtime.executionContextCreated",
            callback=lambda p: got_c.append(p.get("context", {}).get("id", "?")),
            session_id=sid,
        )
        await cdp.send(method="Runtime.enable", session_id=sid)
        await asyncio.sleep(0.5)
        fresh_2 = len(got_c)
        await cdp.send(method="Target.detachFromTarget", params={"sessionId": sid})

    print("STEP RUN  (one session, two enables)")
    print(f"  1st Runtime.enable on the session : {first} executionContextCreated")
    print(f"  2nd Runtime.enable on the session : {second} executionContextCreated")
    print()
    print("TODAY     (two sessions, one enable each)")
    print(f"  fresh session A                   : {fresh_1} executionContextCreated")
    print(f"  fresh session B                   : {fresh_2} executionContextCreated")
    print()
    if fresh_2 > 0 and second == 0:
        print("RESULT: CONFIRMED behavioral difference.")
        print("  A fresh session replays existing state on enable. A reused session")
        print("  does not. So a step that re-enables a domain an earlier step already")
        print("  enabled sees LESS than the same verb run as its own invocation.")
    elif second > 0:
        print("RESULT: no difference; the second enable replays too.")
    else:
        print("RESULT: inconclusive.")


asyncio.run(main())
```

## Appendix: raw per-run data

Every run behind every median above.

### `raw.tsv`

Single verbs. Columns: series, run, milliseconds, exit code.

```
status	1	67.0	0
status	2	68.1	0
status	3	67.5	0
status	4	66.2	0
status	5	68.2	0
status	6	66.3	0
status	7	68.1	0
status	8	68.4	0
status	9	70.9	0
status	10	66.7	0
status	11	67.8	0
status	12	65.7	0
guide	1	54.0	0
guide	2	55.5	0
guide	3	54.2	0
guide	4	55.0	0
guide	5	53.4	0
guide	6	55.2	0
guide	7	53.8	0
guide	8	54.4	0
guide	9	55.9	0
guide	10	54.0	0
guide	11	54.1	0
guide	12	53.7	0
runtime-evaluate	1	76.1	0
runtime-evaluate	2	73.5	0
runtime-evaluate	3	72.2	0
runtime-evaluate	4	76.1	0
runtime-evaluate	5	76.4	0
runtime-evaluate	6	73.4	0
runtime-evaluate	7	72.8	0
runtime-evaluate	8	73.1	0
runtime-evaluate	9	73.8	0
runtime-evaluate	10	72.2	0
runtime-evaluate	11	73.1	0
runtime-evaluate	12	72.7	0
snapshot	1	88.7	0
snapshot	2	89.8	0
snapshot	3	85.0	0
snapshot	4	90.4	0
snapshot	5	85.5	0
snapshot	6	84.9	0
snapshot	7	87.6	0
snapshot	8	82.8	0
snapshot	9	90.4	0
snapshot	10	88.1	0
snapshot	11	84.9	0
snapshot	12	84.7	0
frames-list	1	83.5	0
frames-list	2	86.2	0
frames-list	3	83.3	0
frames-list	4	86.6	0
frames-list	5	89.5	0
frames-list	6	85.5	0
frames-list	7	85.6	0
frames-list	8	82.3	0
frames-list	9	86.1	0
frames-list	10	84.7	0
frames-list	11	85.3	0
frames-list	12	85.9	0
screenshot	1	100.2	0
screenshot	2	90.7	0
screenshot	3	83.0	0
screenshot	4	83.4	0
screenshot	5	84.1	0
screenshot	6	100.4	0
screenshot	7	99.5	0
screenshot	8	99.8	0
screenshot	9	81.8	0
screenshot	10	84.8	0
screenshot	11	102.2	0
screenshot	12	97.9	0
```

### `raw-startup.tsv`

Interpreter and import. Columns: series, run, milliseconds.

```
python-bare	1	12.3
python-bare	2	12.5
python-bare	3	12.6
python-bare	4	12.6
python-bare	5	13.0
python-bare	6	12.3
python-bare	7	12.7
python-bare	8	12.8
python-bare	9	12.4
python-bare	10	12.5
python-bare	11	12.4
python-bare	12	12.4
python-import-cli	1	53.0
python-import-cli	2	52.5
python-import-cli	3	53.3
python-import-cli	4	53.2
python-import-cli	5	52.3
python-import-cli	6	53.1
python-import-cli	7	52.2
python-import-cli	8	51.5
python-import-cli	9	51.1
python-import-cli	10	52.3
python-import-cli	11	51.8
python-import-cli	12	51.8
python-import-curated	1	51.3
python-import-curated	2	50.1
python-import-curated	3	51.3
python-import-curated	4	53.0
python-import-curated	5	53.5
python-import-curated	6	55.6
python-import-curated	7	57.1
python-import-curated	8	52.3
python-import-curated	9	49.5
python-import-curated	10	50.1
python-import-curated	11	50.4
python-import-curated	12	51.7
```

### `raw-prelaunch.tsv`

`bt status` against an empty registry, measured with the stale `bt`.

```
status-empty-registry	1	52.2
status-empty-registry	2	52.9
status-empty-registry	3	52.9
status-empty-registry	4	52.4
status-empty-registry	5	52.9
status-empty-registry	6	54.8
status-empty-registry	7	53.9
status-empty-registry	8	52.6
status-empty-registry	9	53.5
status-empty-registry	10	53.1
status-empty-registry	11	52.5
status-empty-registry	12	51.9
```

### `raw-flow.tsv`

Flow A, ten invocations.

```
cli-10-step-ten-processes	1	1737.5
cli-10-step-ten-processes	2	1732.1
cli-10-step-ten-processes	3	1694.0
cli-10-step-ten-processes	4	1683.5
cli-10-step-ten-processes	5	1696.3
```

### `raw-floor.tsv`

Flow A, one process.

```
# median 849.1 ms over 5 runs
floor-10-step-one-process	1	849.1
floor-10-step-one-process	2	853.1
floor-10-step-one-process	3	841.8
floor-10-step-one-process	4	846.9
floor-10-step-one-process	5	852.0
```

### `raw-oneshot20.tsv`

Flow B, ten invocations, n=20.

```
oneshot-cli-10-step-ten-processes	1	876.5
oneshot-cli-10-step-ten-processes	2	868.7
oneshot-cli-10-step-ten-processes	3	888.8
oneshot-cli-10-step-ten-processes	4	876.8
oneshot-cli-10-step-ten-processes	5	882.8
oneshot-cli-10-step-ten-processes	6	872.9
oneshot-cli-10-step-ten-processes	7	861.7
oneshot-cli-10-step-ten-processes	8	862.9
oneshot-cli-10-step-ten-processes	9	866.0
oneshot-cli-10-step-ten-processes	10	879.5
oneshot-cli-10-step-ten-processes	11	887.4
oneshot-cli-10-step-ten-processes	12	914.3
oneshot-cli-10-step-ten-processes	13	871.5
oneshot-cli-10-step-ten-processes	14	847.8
oneshot-cli-10-step-ten-processes	15	843.1
oneshot-cli-10-step-ten-processes	16	862.3
oneshot-cli-10-step-ten-processes	17	848.7
oneshot-cli-10-step-ten-processes	18	860.9
oneshot-cli-10-step-ten-processes	19	989.4
oneshot-cli-10-step-ten-processes	20	867.5
# median 870.1 ms over 20 runs
```

### `raw-oneshot-floor20.tsv`

Flow B, one session, n=20, against port 9223 confirmed before and after.

```
oneshot-floor-10-step-one-session	1	57.6
oneshot-floor-10-step-one-session	2	83.7
oneshot-floor-10-step-one-session	3	82.5
oneshot-floor-10-step-one-session	4	66.6
oneshot-floor-10-step-one-session	5	84.4
oneshot-floor-10-step-one-session	6	82.9
oneshot-floor-10-step-one-session	7	66.7
oneshot-floor-10-step-one-session	8	83.1
oneshot-floor-10-step-one-session	9	83.0
oneshot-floor-10-step-one-session	10	67.0
oneshot-floor-10-step-one-session	11	83.4
oneshot-floor-10-step-one-session	12	84.0
oneshot-floor-10-step-one-session	13	66.1
oneshot-floor-10-step-one-session	14	83.3
oneshot-floor-10-step-one-session	15	83.3
oneshot-floor-10-step-one-session	16	66.8
oneshot-floor-10-step-one-session	17	83.1
oneshot-floor-10-step-one-session	18	82.7
oneshot-floor-10-step-one-session	19	66.8
oneshot-floor-10-step-one-session	20	83.9
# median 83.0 ms over 20 runs
```

### `raw-oneshot.tsv` (superseded, n=5)

Flow B, ten invocations.

```
oneshot-cli-10-step-ten-processes	1	787.8
oneshot-cli-10-step-ten-processes	2	778.5
oneshot-cli-10-step-ten-processes	3	776.4
oneshot-cli-10-step-ten-processes	4	831.9
oneshot-cli-10-step-ten-processes	5	791.9
```

### `raw-oneshot-floor.tsv` (superseded, n=5)

Flow B, one session.

```
# median 82.7 ms over 5 runs
oneshot-floor-10-step-one-session	1	59.1
oneshot-floor-10-step-one-session	2	83.8
oneshot-floor-10-step-one-session	3	82.7
oneshot-floor-10-step-one-session	4	69.2
oneshot-floor-10-step-one-session	5	83.2
```

### `enable-probe.out`

The domain-enable replay probe output.

```
STEP RUN  (one session, two enables)
  1st Runtime.enable on the session : 2 executionContextCreated
  2nd Runtime.enable on the session : 0 executionContextCreated

TODAY     (two sessions, one enable each)
  fresh session A                   : 2 executionContextCreated
  fresh session B                   : 2 executionContextCreated

RESULT: CONFIRMED behavioral difference.
  A fresh session replays existing state on enable. A reused session
  does not. So a step that re-enables a domain an earlier step already
  enabled sees LESS than the same verb run as its own invocation.
```
