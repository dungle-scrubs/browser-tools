<!--
The report as the reviewer returned it, unedited. It was run against the
version 1 snapshot at `2239955` by `gpt-6-astra@codex`, cross-family with
`claude` excluded. Its sandbox refused sockets, so its work is source
inspection and controlled input rather than live browser measurement, which
it states itself.

One path in it has moved: the probes it inspects at `.scratch/rfc-04/` are
now committed at `docs/rfc/04_probes/`, partly because of its own finding
that this evidence was cited and not committed.
-->

I would block RFC-04 v1 pending revision. The feature fits `bt`, but the proposed routing and lifecycle rules are incomplete, and several measurements support narrower conclusions than the RFC claims.

I reviewed `2239955` against baseline `3baf228`. The shared checkout changed branches during the review; I left it untouched. No files were changed.

Live testing was unavailable:

```text
uv run bt launch --headless
error: Failed to initialize cache at `/Users/kevin/.cache/uv`
... Operation not permitted
```

A TCP-only connection attempt to `127.0.0.1:9222` also returned `PermissionError: [Errno 1] Operation not permitted`. No CDP commands went to that browser. Results below are source inspection or controlled-input tests, not live browser measurements.

**Blocking findings**

1. **The UID routing map breaks separate `snapshot` and `click` invocations.**

   RFC lines 256–262 populate the document-token-to-session map when a snapshot mints UIDs. Each invocation creates a new runtime. A subsequent `click` has neither that map nor those session IDs, even when the document remains unchanged.

   The current interaction code deliberately resolves a UID without a preceding snapshot in its process. This reproduction exercises that contract:

   ```bash
   .venv/bin/python -B -c '
   from browser_tools.native_snapshot import NativeSnapshotReader, make_uid, doc_token_from_loader_id
   from browser_tools.native_interaction import NativeInteractor

   loader = "A" * 32
   reader = NativeSnapshotReader()
   uid = make_uid(doc_token_from_loader_id(loader), 7)
   def send(method: str, params=None) -> dict:
       return {"frameTree": {"frame": {"loaderId": loader}}}

   print(reader.current)
   print(NativeInteractor(reader).resolve(send, uid))
   routes = {}
   print(routes.get(uid.split("-")[0]))
   '
   ```

   Output:

   ```text
   None
   7
   None
   ```

   The RFC would interpret the final missing route as a stale document. The existing contract requires the UID to remain usable.

   Routes must be reconstructed from live documents on each invocation, independently of snapshots. A missing route must distinguish a gone document from an undiscovered or unresponsive Frame Session. Within a Step Run, navigation must invalidate old routes even when the session survives.

2. **The existing snapshot reader cannot provide the claimed document identity by itself.**

   `doc_token_from_loader_id()` takes the first **12 hexadecimal characters**, not the complete loader ID. Two distinct loader IDs can therefore produce the same token:

   ```bash
   .venv/bin/python -B -c '
   from browser_tools.native_snapshot import doc_token_from_loader_id
   for loader in ("A"*12 + "0"*20, "A"*12 + "1"*20):
       print(doc_token_from_loader_id(loader))
   '
   ```

   Output:

   ```text
   AAAAAAAAAAAA
   AAAAAAAAAAAA
   ```

   This is a constructed collision, not a naturally occurring Chrome collision. Its probability is small; the categorical claim that tokens “differ by construction” is still false.

   More immediately, `NativeSnapshotReader.build()`:

   - Keys raw accessibility nodes by their AX `nodeId`.
   - Accepts one `doc_token` for the entire build.
   - Converts repeated backend IDs into unaddressable `x<ordinal>` UIDs.

   I passed parent and child trees with backend IDs `1` and `2` through the existing `stitch_ax_frames()` and `build()` functions. The result was:

   ```text
   [('RootWebArea', 'PARENTTOKEN-1'),
    ('Iframe', 'PARENTTOKEN-2'),
    ('RootWebArea', 'PARENTTOKEN-x3'),
    ('textbox', 'PARENTTOKEN-x4')]
   ```

   RFC lines 270–278 require the child textbox to receive its own document token and an addressable backend ID.

   Specify document identity per node, including same-process child documents. Namespace AX identifiers and owner-node lookups by their owning frame/session before merging. The outer `<docToken>-<backendNodeId>` shape can remain, but `docToken` needs an explicit identity and collision policy. A frame identifier plus full loader identifier is a defensible candidate; a session ID alone cannot survive invocations.

3. **The OOPIF filter accepts workers, and the measurement used to justify it is contaminated.**

   RFC lines 196–200 call this an exact OOPIF filter:

   ```python
   targetInfo.targetId != own_target_id
   ```

   Reproduction:

   ```bash
   .venv/bin/python -B -c 'print({"targetId":"W", "type":"worker"}["targetId"] != "R")'
   ```

   Output:

   ```text
   True
   ```

   CDP explicitly includes workers among related targets reached by auto-attach. The proposed predicate sends them into `Page.enable` and `Page.getFrameTree`. It needs a supported target-type filter plus ancestry validation. [Official Target protocol](https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Target.pdl)

   The original probes remain available locally. Inspect them with:

   ```bash
   rg -n 'client.on|attachToTarget|setAutoAttach|attached.clear|start =|t0 =' .scratch/rfc-04/probe_*.py
   ```

   Relevant output:

   ```text
   probe_nesting.py:23: client.on("Target.attachedToTarget", ...)
   probe_nesting.py:28: method="Target.attachToTarget",
   probe_nesting.py:34: method="Target.setAutoAttach",
   probe_nesting.py:64: attached.clear()
   probe_edges.py:35: start = time.monotonic()
   probe_edges.py:46: method="Target.attachToTarget",
   probe_edges.py:55: t0 = time.monotonic()
   ```

   The initial event collection includes the explicit page attach. The probes do not establish that auto-attach produced a second page session.

   I replayed the original nesting probe against a controlled client that emitted exactly one page event during explicit attach and only an iframe event during auto-attach. It still printed:

   ```text
   after setAutoAttach on the page session: 2 attach events
      type=page   ... session=P isOurPage=True
      type=iframe ... session=B isOurPage=False
   ```

   Also, the reported child-arrival offsets use `start`, which precedes discovery and page setup. They are not delays measured from `setAutoAttach`. The separately measured command duration uses `t0` and does not have that error.

4. **The frame invariant is not established for the event sequences the splice introduces.**

   The invariant helper exists. The claimed exhaustive search does not exist in the tracked `tests/test_session_hygiene.py`.

   The following runs the existing helper against short controlled sequences:

   ```bash
   .venv/bin/python -B -c '
   import runpy, sys
   sys.path.insert(0, "tests")
   from browser_tools.frame_manager import FrameManager

   check = runpy.run_path("tests/test_session_hygiene.py")[
       "TestTheTwoViewsOfTheFrameTreeAgree"
   ]._assert_one_set_of_frames

   def fresh() -> FrameManager:
       fm = FrameManager()
       fm.update_from_frame_tree({"frame":{"id":"R","url":"https://root.test"}})
       return fm

   def show(label: str, fm: FrameManager) -> None:
       print(label, "map=", sorted(fm._frames),
             "listed=", [f["frameId"] for f in fm.get_flat_frames()])
       try:
           check(fm)
           print("PASS")
       except AssertionError:
           print("FAIL")

   fm = fresh()
   fm.handle_frame_attached({"frameId":"G","parentFrameId":"C"})
   show("missing parent", fm)

   fm = fresh()
   fm.handle_frame_attached({"frameId":"C","parentFrameId":"R"})
   fm.handle_frame_attached({"frameId":"G","parentFrameId":"C"})
   fm.handle_frame_detached({"frameId":"C","reason":"remove"})
   fm.handle_frame_navigated({"frame":{"id":"G","parentId":"C","url":"https://late.test"}})
   show("late navigation after ancestor removal", fm)

   fm = fresh()
   fm.handle_frame_navigated({"frame":{"id":"C","parentId":"R","url":"https://child.test"}})
   fm.handle_frame_detached({"frameId":"C","reason":"swap"})
   show("new child then old-session swap", fm)
   '
   ```

   Output:

   ```text
   missing parent map= ['G', 'R'] listed= ['R']
   FAIL
   late navigation after ancestor removal map= ['G', 'R'] listed= ['R']
   FAIL
   new child then old-session swap map= ['R'] listed= ['R']
   PASS
   ```

   The third result matters: the structural invariant passes even though the frame representing the newly attached child has disappeared.

   CDP distinguishes `frameDetached(reason="swap")` from DOM removal. The existing handler ignores that distinction. [Official Page protocol](https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Page.pdl)

   The RFC needs rules for:

   - Old-session events arriving after ownership transfers.
   - A tree response completing after its session or ancestor has detached.
   - A parent tree refresh replacing state after child trees have been spliced.
   - Duplicate attach/navigation notifications erasing descendants already discovered.
   - OOPIF-to-local and local-to-OOPIF transitions.
   - Pending children whose parent never becomes available.

   Track session ownership and document generation on asynchronous work. Reject stale completions. Separate a renderer swap from removal.

   The direct execution of all 11 existing invariant tests passed. An additional in-memory enumeration of 258 sequences of up to three events found 62 failing final invariants. Those sequences are synthetic; they do not establish Chrome’s actual event ordering.

5. **Changing the selected-context getter does not isolate execution-context events.**

   RFC lines 250–254 address the returned context address. The stored context map and event handlers also need session identity.

   Reproduction:

   ```bash
   .venv/bin/python -B -c '
   from browser_tools.frame_manager import FrameManager
   fm = FrameManager()
   fm.update_from_frame_tree({
       "frame":{"id":"R"},
       "childFrames":[{"frame":{"id":"C"}}]
   })
   for fid in ("R", "C"):
       fm.handle_execution_context_created({
           "context":{"id":1,"auxData":{"frameId":fid,"isDefault":True}}
       })
   fm.handle_execution_context_destroyed({"executionContextId":1})
   print(fm._frames["R"].execution_context_id,
         fm._frames["C"].execution_context_id)
   '
   ```

   Output:

   ```text
   1 None
   ```

   Interpreting the destroy event as coming from the root Frame Session, the wrong context was cleared. Likewise, forwarding a child’s `executionContextsCleared` to the current handler clears every frame’s context.

   Key context state by session and context ID. Scope clear/destroy events to that session. Changing only the getter leaves a path where child navigation breaks an unrelated parent operation.

6. **Failure containment needs an execution policy, not only exception handling.**

   The current snapshot wrapper cancels the whole read when its deadline expires. The child-reading code catches ordinary exceptions, but that does not preserve the parent result when the outer operation is cancelled.

   Controlled reproduction using the existing snapshot path:

   ```bash
   .venv/bin/python -B -c '
   import asyncio, time
   from browser_tools.cdp_handler import CDPHandler, CDPRuntime
   from browser_tools.native_snapshot import NativeSnapshotReader

   class SlowChild:
       connected = True
       async def send(self, method: str, params=None) -> dict:
           if method == "Page.getFrameTree":
               return {"frameTree":{
                   "frame":{"id":"R","loaderId":"A"*32},
                   "childFrames":[{"frame":{"id":"C"}}]
               }}
           if method == "Accessibility.getFullAXTree":
               return {"nodes":[{
                   "nodeId":"1","backendDOMNodeId":1,
                   "role":{"value":"RootWebArea"},"name":{"value":"parent"}
               }]}
           if method == "DOM.getFrameOwner":
               await asyncio.Event().wait()
           return {}

   async def main() -> None:
       rt = CDPRuntime.__new__(CDPRuntime)
       rt._loop = asyncio.get_running_loop()
       rt._cdp_client = SlowChild()
       rt._deadline = time.monotonic() + 0.04
       rt._grace_until = None
       handler = CDPHandler.__new__(CDPHandler)
       handler._rt = rt
       handler._native_reader = NativeSnapshotReader()
       print(await asyncio.to_thread(handler.call_native, "take_snapshot", {}))

   asyncio.run(main())
   '
   ```

   Output includes:

   ```text
   "text": "Error: take_snapshot did not answer in time"
   "isError": True
   ```

   The parent AX tree had already answered. RFC lines 281–284 require that tree to be returned with a child-failure note. This tests existing machinery with a stalled child read, not an implemented OOPIF path.

   There is a second containment boundary: `core.cdp_client._recv_loop()` calls event callbacks synchronously. An exception escaping a child callback exits that shared loop. My controlled callback-failure test produced:

   ```text
   Unexpected error in receive loop: child setup failed
   connected= False
   parent pending= ConnectionError('WebSocket connection closed')
   ```

   Specify bounded child tasks, exception containment at callback boundaries, task cancellation on detach, and an aggregate optional-work budget that leaves time to return the parent result. Registering an `async` callback directly is insufficient because the receive loop does not await it.

   The error table also needs precedence. A detached child during optional snapshot expansion can be omitted. A detached child during an explicitly requested `click`, `fill`, or storage read must fail that operation. Suppressing every `-32001` as exit 0 would report actions that did not complete.

**Status of the seven findings**

| Finding | Assessment |
|---|---|
| 1. `--target` cannot select an iframe | Correct for the shipped selector. `core/attach.py:205–207` filters page targets; the One-Shot Session path also selects pages. |
| 2. Auto-attach costs 0.3 ms; child arrives 1.2–8.0 ms later | The command duration is a page-specific measurement. The arrival offsets use the wrong reference point. The claimed extra page session is unproven because explicit-attach events were included. |
| 3. Child frame ID equals target ID; parent is already known | The identity equality is supported by Chromium’s frame-target implementation. Parent identity is meaningful; “already in the map” is not guaranteed across asynchronous reads and events. [Frame target implementation](https://raw.githubusercontent.com/chromium/chromium/main/content/browser/devtools/render_frame_devtools_agent_host.cc), [frame identifiers](https://raw.githubusercontent.com/chromium/chromium/main/third_party/blink/renderer/core/inspector/identifiers_factory.cc) |
| 4. Child sessions answer Page, DOM and Runtime methods | Supported for the measured iframe target. This does not apply to every target accepted by the proposed filter. Local child frames appear in that renderer’s frame tree. [Page agent implementation](https://raw.githubusercontent.com/chromium/chromium/main/third_party/blink/renderer/core/inspector/inspector_page_agent.cc) |
| 5. Backend IDs collide | Correct conclusion. The namespace is renderer-local, not inherently session-local. The exact 9-of-10 overlap is fixture-specific. Chromium uses a process-local node-ID counter. [DOM node IDs](https://raw.githubusercontent.com/chromium/chromium/main/third_party/blink/renderer/core/dom/dom_node_ids.cc) |
| 6. Auto-attach must recurse | Supported by the protocol, which explicitly describes recursive attachment. [Target protocol](https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Target.pdl) |
| 7. Lifecycle behavior | Narrow the claim to the tested same-origin child navigation and iframe removal. The probe removes the child before navigating the parent, so it does not demonstrate parent navigation detaching an existing child. Cross-process transitions remain untested. |

Finding 3 supports a stable-tree splice. It does not supply a concurrency model. Finding 5 requires renderer-aware addressing, but it does not force abandonment of the UID’s two-part textual shape.

**Non-blocking corrections**

- The measurements file says all probes are reproduced in full. They are not. `git show 2239955:docs/rfc/04_reach-into-cross-origin-iframes.measurements.md` contains a shell fragment and descriptions of the three Python scripts. Commit the runnable probes and fixtures.
- The opening premise equates cross-origin with out-of-process. Chrome’s site-isolation boundary is based on sites; a port difference alone does not establish a separate renderer. [Chromium Site Isolation](https://www.chromium.org/Home/chromium-security/site-isolation/)
- Open Question 2’s `postMessage` explanation is wrong. Chrome supports it across renderer processes. Marking the process boundary may help diagnostics, but it does not explain a `postMessage` failure. [Site Isolation design](https://www.chromium.org/developers/design-documents/site-isolation/)
- The cost accounting omits `Page.getFrameTree`, which the attachment sequence explicitly requires. Snapshot work also requires AX reads and frame-owner resolution. “Roughly one round trip each” does not describe that sequence.
- The Page-enable failure row says the subtree is both absent and marked unreachable. Define which frame remains listed and how its parent and URL are obtained when tree discovery fails.
- The security section’s direct-navigation equivalence contradicts the motivation’s parent-dependent state example. Access to the embedded document’s current state is not equivalent to opening its URL.
- `wait-idle` currently polls resource-entry counts in the page session. It is not an OOPIF discovery or readiness barrier.

The cited codebase details mostly check out. `_connect_cdp` enables Page, obtains the frame tree, then enables Runtime with the relevant subscriptions installed first. The selected-context getter has exactly two production callers: frame selection’s diagnostic and storage reading. Direct `Page.disable` and `Runtime.disable` steps are rejected.

The new runtime-owned auto-attach state needs a policy too. Running `_validate_passthrough` produced:

```text
Page.disable StepListError
Runtime.disable StepListError
Target.setAutoAttach accepted
```

A raw step can currently turn auto-attach off. The existing domain guards do not cover that new state.

I could not measure an ad-heavy page. A depth bound alone is nevertheless inadequate: twenty sibling OOPIFs all pass a depth limit of ten. Keep a depth limit if useful, and add bounds on active sessions, pending initialization, concurrency, and aggregate optional work. Choose their values from live measurements. Do not substitute a guessed latency figure.

I recommend **off by default for the first release**, with the existing diagnosis naming the opt-in. Default-on becomes defensible after separate-invocation UID routing, process swaps, slow children, detach-during-snapshot, and wide-frame pages pass. A flag does not excuse correctness defects; it limits the initial behavior change.

The scope fits. The users are agents operating embedded forms, editors, and consent interfaces through the existing CLI. Reaching those documents serves the product’s stated purpose. It widens browser reach and adds permanent session-lifecycle maintenance, but no new verb or persistent daemon is necessary. RFC-01’s exclusion applies to that merge effort; RFC-04 should explicitly authorize this subsequent capability.

The normal pytest run was blocked before test bodies executed because its autouse profile-isolation fixture could not create temporary directories. I did not bypass that fixture for the suite. The direct invariant tests and controlled probes above performed no browser or filesystem mutations.