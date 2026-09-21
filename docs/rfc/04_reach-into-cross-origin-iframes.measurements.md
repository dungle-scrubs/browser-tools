# RFC-04 measurements

Evidence for `docs/rfc/04_reach-into-cross-origin-iframes.rfc.md`. Every figure
here was measured on the machine and date below. Nothing in this file is
estimated. The three probes are inlined at the end, so the whole document is
reproducible without reaching outside the repository.

## Environment

| Fact | Value |
|---|---|
| Date | 2026-09-21 |
| Repository | `browser-tools` at `3baf228`, branch `main` |
| Python | 3.14.6 |
| Chrome | `Chrome/153.0.8010.53`, launched `--headless` |
| Machine | Apple M5 Max, 128 GB |
| Instance | `browser-tools-01`, no profile, port 9223, stopped after each probe |

The port matters. Port 9222 on this machine is a long-lived browser that is
not the one under test, so each probe resolves its port from the registry by
instance name and refuses to run when the name is absent. A probe that drove
the wrong browser would report the right shape for the wrong page.

## The page under test

Three loopback origins, because same address with a different host is enough
for Chrome's site isolation to split the processes.

| Origin | Serves |
|---|---|
| `http://127.0.0.1:8127/` | the parent, embedding the child |
| `http://localhost:8128/` | the child, with an `<input id="card">` |
| `http://[::1]:8133/` | the grandchild, for the nesting probe |

`127.0.0.2` is not bindable on macOS (`[Errno 49] Can't assign requested
address`), which is why the third origin is IPv6 loopback rather than a second
IPv4 address.

## Finding 1: the split is real, and the tool does not cross it

```
target types: ['background_page', 'browser_ui', 'iframe', 'page', 'service_worker']
  page     http://127.0.0.1:8127/
  iframe   http://localhost:8128/
```

Against that page, on `3baf228`:

| Command | Result |
|---|---|
| `frames list` | the parent only |
| `snapshot` | `Iframe` node with nothing under it |
| `frames select localhost` | exit 1 |
| `storage get --key localhost` | exit 1 |
| `snapshot --target <iframe target id>` | exit 1, `No target matching ID prefix` |

The last row is the one that rules out a workaround. `core/attach.py:205-207`
filters `Target.getTargets` to `type == "page"`, so an `iframe` target is not
addressable by `--target` at all.

## Finding 2: `Target.setAutoAttach` delivers the child, and costs nothing

Sent on the page session with
`{"autoAttach": true, "waitForDebuggerOnStart": false, "flatten": true}`:

```
6. setAutoAttach returned in 0.3 ms

2. every attach event:
       0.6ms Target.attachedToTarget  session=F9E176C3 type=page   url=http://127.0.0.1:8127/  attached=True
       1.2ms Target.attachedToTarget  session=501D55A3 type=iframe url=http://localhost:8128/  attached=True
```

The call itself is 0.3 ms and the child session arrives 1.2 ms after it. Across
the three probe runs the child arrived at 1.2 ms, 5.1 ms and 8.0 ms.

**The first event is our own page target under a second session id.** The
probe checks it: `isOurPage=True`. Filtering by `type == "iframe"` catches this,
and filtering by `targetInfo.targetId != <the session's own target>` catches it
precisely. Anything that attaches to it as if it were a child would drive the
page through a session the frame manager does not know about.

## Finding 3: the child splices into the parent's tree exactly

```
1. parent tree frame ids: ['EAB9B33A045D8CA800E46B5225025E60']
1. child parentId=EAB9B33A045D8CA800E46B5225025E60  in the parent's tree? True
```

And from the first probe:

```
child session: 34AFD2DBF91EC32F41533DA0459A832E  target: 67C2499FF03C05CCA8280C575AB568B9
child frame tree: { "frame": { "id": "67C2499FF03C05CCA8280C575AB568B9", ... } }
child frame id == child target id? True
```

Two facts, and together they make the splice mechanical rather than heuristic:

- the child's frame id **is** its target id, so a target maps to a frame
  without a lookup;
- the child's `parentId` names a frame that is already in the parent session's
  tree, so the attachment point is given rather than guessed.

## Finding 4: a child session is an ordinary page session

Once attached, the child answers `Page.getFrameTree`, `DOM.getDocument` and
`Runtime.evaluate` on its own session, and its own same-origin subframes are in
its tree:

```
child session's own frame tree:
   D7E98CB9 http://localhost:8132/
     A0453029 http://localhost:8132/same.html

child document nodes: 13
child has the card input? True
child origin: http://localhost:8128
```

## Finding 5: `backendNodeId` collides across sessions

```
parent ids: 10  child ids: 12  overlapping: 9
```

Nine of ten parent backend ids also name a node in the child. `backendNodeId`
is per-renderer, so it is not an address once more than one renderer is in
play. The UID that RFC-01 version 6 specified is
`<docToken>-<backendNodeId>`, and the document token is what keeps a UID
unique here. It is also what must carry the routing: a UID has to say which
session to send the click to, and the backend id alone cannot.

## Finding 6: auto-attach does not cascade

`setAutoAttach` on the page session reaches the child and stops:

```
after setAutoAttach on the page session: 2 attach events
   type=page     url=http://127.0.0.1:8131/   isOurPage=True
   type=iframe   url=http://localhost:8132/   isOurPage=False
```

Re-sent on the child session, the grandchild arrives:

```
setAutoAttach re-sent on the child session
  grandchild: 1 attach events
   type=iframe   url=http://[::1]:8133/   isOurPage=False
```

So reaching a nested cross-origin frame means re-sending `setAutoAttach` on
every child session as it attaches. The implementation recurses; it does not
send once.

## Finding 7: the lifecycle edges

| Event | What arrives | What it means |
|---|---|---|
| child navigates within its origin | `Target.targetInfoChanged` only | the session survives; re-read its tree |
| iframe removed from the parent | `Target.detachedFromTarget`, 0.9 ms | the session is gone |
| sending on a detached session | `CDP error -32001: Session with given id not found.` | one error class to suppress |
| parent navigates | a fresh `Target.attachedToTarget` for the new child, 9.4 ms | auto-attach survives; no re-send on the page session |

```
3. navigate the child within its own origin
       4.6ms Target.targetInfoChanged
   child session still answers: http://localhost:8128/index.html?v=2

4. remove the iframe from the parent
       0.9ms Target.detachedFromTarget session=501D55A3
   child session is gone: CDPError CDP error -32001: Session with given id not found.

5. navigate the parent back, then away
       9.4ms Target.attachedToTarget session=66A0E71E type=iframe url=
      10.9ms Target.targetInfoChanged type=iframe url=http://localhost:8128/
   auto-attach survived the parent navigation: True
```

The fourth row is the one that shapes teardown. A run holds child sessions
that can vanish between steps without anything the caller did, so every send
on a child session has to treat `-32001` as "this frame is gone" rather than
as a failure of the step.

## The probes

All five are committed at `docs/rfc/04_probes/`, so every figure above can be
reproduced from the repository. Version 1 of this file said they were
reproduced in full here and then paraphrased them, which the review caught.
They are described below and committed in full there.

| File | What it does | Findings |
|---|---|---|
| `probe.sh` | serves the two loopback origins, launches a headless instance, resolves its port from the registry by name, navigates, hands the browser WebSocket URL to a Python probe | setup |
| `probe_nested.sh` | the same for three origins, with the grandchild on IPv6 loopback | setup |
| `probe_autoattach.py` | attaches with `flatten: true`, sends `setAutoAttach`, then reads the child session's frame tree, DOM, backend ids and origin | 2 to 5 |
| `probe_edges.py` | four sequences in order: navigate the child, remove the iframe, send on the dead session, navigate the parent | 3 and 7 |
| `probe_nesting.py` | sends `setAutoAttach` on the page session, counts what arrives, re-sends on the child session, counts again | 6 |

Each shell probe refuses to run when it cannot resolve its instance's port:

```bash
[ -z "$PORT" ] && { echo 'no port; refusing to probe someone else's browser'; exit 1; }
```

## Two figures this file withdraws

The review of RFC-04 version 1 found two measurement errors here, and they are
corrected rather than quietly dropped.

**The child-arrival offsets are not offsets from `setAutoAttach`.**
`probe_edges.py` sets `start` before it sends `Target.attachToTarget`, and the
printed offsets are measured from `start`. The "1.2 ms after the call" reading
in version 1 is wrong. The 0.3 ms for the `setAutoAttach` command itself uses
its own `t0` and stands.

**The second `page` attach event was the probe's own explicit attach.** The
probe registers its `Target.attachedToTarget` handler before calling
`Target.attachToTarget`, so that call's event is in the list. Version 1 read
it as `setAutoAttach` re-attaching the session's own target and built a filter
rule on it. Replayed against a controlled client that emitted exactly one page
event during explicit attach and only an iframe event during auto-attach, the
probe still printed two events and `isOurPage=True`. Nothing here establishes
that auto-attach produces a second page session.

Finding 2 in the RFC is narrowed to what these probes actually measured.
