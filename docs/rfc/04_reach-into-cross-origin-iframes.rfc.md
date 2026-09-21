---
number: 04
title: "Reach into cross-origin iframes"
type: feature
status: Draft
author: "Kevin Frilot"
date: 2026-09-21
version: 2
---

# RFC-04: Reach into cross-origin iframes

## Abstract

A frame Chrome puts in its own renderer process gets its own CDP target.
Chrome's site isolation splits by **site**, not by origin, so a cross-site
iframe is the usual cause and a port difference alone is not one. `bt`
attaches to the page target and reads `Page.getFrameTree` there, so such a
frame is absent from `frames list`,
`frames select` cannot select it, and `snapshot` shows the `Iframe` node with
nothing under it. Logins, payment forms, consent banners and embedded editors
are usually cross-origin iframes, so an agent told "fill the card number"
fails on the first step.

This RFC specifies reaching them: `Target.setAutoAttach` with `flatten: true`,
a session per child target, one frame tree spliced from all of them, and UIDs
that carry enough to route a click to the session that minted them. No new
verb. Every existing frame-scoped verb starts working on frames it could not
see.

The cost is that a One-Shot Session becomes one session plus N, which is a
real change to the shape RFC-01 set and RFC-03 built on.

## Introduction

`FrameManager` is built from `Page.getFrameTree` on the attached page session,
kept current by `Page.frameAttached`, `frameDetached` and `frameNavigated`
from that same session. An out-of-process frame is in none of those. Nothing
in the codebase sends `Target.setAutoAttach`.

Issue #127 filed the evidence. `3baf228` landed the half that is right either
way: `frames select` now says when the frame it cannot find is a cross-origin
iframe, rather than sending the caller to a listing that will never contain
it, and `GUIDE.txt` says `frames` sees same-process frames only. That fix
describes the limit. This RFC removes it.

### What was measured first

Every claim about CDP behaviour in this document was measured against headless
Chrome before it was written down, not read from documentation. The figures,
the transcripts and the probes are in
`docs/rfc/04_reach-into-cross-origin-iframes.measurements.md`. The seven
findings that shape the design:

1. **The tool cannot cross the boundary today**, and `--target` is not a
   workaround: `core/attach.py:205-207` filters targets to `type == "page"`,
   so an `iframe` target is not addressable.
2. **`Target.setAutoAttach` costs 0.3 ms.** The child session arrival figure
   from version 1 is withdrawn: the probe measured it from a point before the
   explicit `Target.attachToTarget`, not from `setAutoAttach`. Version 1 also
   claimed `setAutoAttach` re-attaches the session's own target under a second
   session id. That is unproven, because the probe registered its handler
   before the explicit attach and counted that attach's own event.
3. **The child's frame id is its target id, and its `parentId` names a frame
   already in the parent's tree.** The splice is given, not guessed.
4. **A child session is an ordinary page session.** `Page.getFrameTree`,
   `DOM.getDocument` and `Runtime.evaluate` all answer on it, and the child's
   own same-origin subframes are in its tree.
5. **`backendNodeId` collides across renderers**: 9 of 10 parent ids also
   named a node in the child. The namespace is renderer-local, so a backend id
   is not an address once two renderers are in play. The 9-of-10 figure is
   specific to that page; the collision is not.
6. **Auto-attach does not cascade.** `setAutoAttach` on the page session
   reaches the child and stops; the grandchild needs it re-sent on the child's
   session.
7. **Three lifecycle edges**: a same-origin child navigation keeps the
   session, removing the iframe detaches it, and sending on a detached session
   is `-32001 Session with given id not found`. Version 1 added a fourth,
   "auto-attach survives a parent navigation". That is withdrawn: the probe
   removed the child before navigating the parent, so it never had an existing
   child session to keep. Cross-process transitions are untested.

## Terminology

`CONTEXT.md` is the naming authority. This RFC adds three nouns and asks for
them to land there when it is built.

- **Out-of-Process Frame** (OOPIF) - a frame Chrome put in its own renderer
  process, which is therefore its own CDP target. Cross-origin is the usual
  cause and not the only one.
- **Frame Session** - the CDP session attached to one target that carries
  frames. The page session is a Frame Session; so is each OOPIF's. A Frame
  Session owns its own domain-enable state and its own execution contexts.
- **Spliced Frame Tree** - the one tree the frame manager reports, assembled
  from every Frame Session. A frame knows which Frame Session answers for it.

Two existing nouns change meaning and the glossary MUST record it:

- **One-Shot Session** stops meaning one attached session. It becomes one
  browser-level connection, one page session, and a Frame Session per OOPIF
  below it, all opened and closed inside one invocation. The property that
  matters is unchanged: nothing outlives the invocation.
- **UID** keeps its `<docToken>-<backendNodeId>` form and gains a job. The
  document token stops being only a staleness check and becomes the routing
  key: it says which Frame Session holds the node.

## Motivation

### The frames an agent is asked about are the ones it cannot see

Measured against a parent on `127.0.0.1:8127` embedding a child on
`localhost:8128`:

```
$ bt I frames list
Frames in current page:
  F707F68D: http://127.0.0.1:8127/

$ bt I snapshot
[uid=5F11BE4F7DF8-1] RootWebArea "parent-origin"
  [uid=5F11BE4F7DF8-7] heading "parent"
  [uid=5F11BE4F7DF8-8] Iframe          <- nothing under it

$ bt I frames select localhost
exit 1
```

The child is a plain page with an `<input id="card">`. Nothing about it is
hard to drive; it is simply not reachable.

This is not an edge case. A checkout form, a Stripe or PayPal field, an OAuth
consent screen, a CMP banner and an embedded rich-text editor are all
cross-origin iframes by construction, because that isolation is the point of
using one. An agent working a real site meets one early.

### The workaround does not exist

`--target` cannot name an iframe target, measured:

```
$ bt I snapshot --target 9DC9577E1F22F878F9BB2F6A21456714
error: could not open a CDP session on the instance at port 9223:
No target matching ID prefix '9DC9577E1F22F878F9BB2F6A21456714'
Available targets:
  [1] F707F68D  http://127.0.0.1:8127/  "parent-origin"
```

Driving the iframe's URL as a top-level page is the advice `3baf228` gives,
and it is the best available today. It is often wrong: the frame's content
depends on the parent's cookies, its referrer, its `postMessage` handshake, or
a one-time URL the parent minted. A payment iframe opened directly usually
shows an error.

### The cost is small and measured

`Target.setAutoAttach` returns in 0.3 ms. The child session arrives 1.2 ms
later on the probe page. Each Frame Session then needs `Page.enable` and
`Runtime.enable`, which is the same pair the page session already pays.

Version 1 said "roughly one round trip each", which does not describe the
sequence. Per Frame Session the attachment costs `Page.enable`,
`Page.getFrameTree`, `Runtime.enable` and `Target.setAutoAttach`: four, before
any work. A merged snapshot adds an accessibility read and frame-owner
resolution per session on top.

**The RFC commits to no cost figure.** One OOPIF was measured; a news page has
a dozen and none of them was. An implementation phase MUST measure the real
total on a wide page, and the design is not called cheap until it has.

## Design

### The shape

```
browser connection
└── page session                      Frame Session, the root
    ├── frames: R, and every same-process frame below it
    └── OOPIF session (per cross-origin child)   Frame Session
        ├── frames: the child, and every same-process frame below it
        └── OOPIF session (per cross-origin grandchild)   Frame Session
```

`CDPRuntime` gains a map of Frame Sessions. `FrameManager` gains one field per
frame, the id of the Frame Session that answers for it, and reports one
Spliced Frame Tree.

### Attaching

On the page session, once, right after it is attached and **before**
`Page.enable`:

```json
Target.setAutoAttach {"autoAttach": true, "waitForDebuggerOnStart": false, "flatten": true}
```

Subscribe-first, for the reason RFC-03's second review found the hard way: a
handler registered after the call misses what the call delivers. The
`Target.attachedToTarget` handler MUST be registered before `setAutoAttach` is
sent.

`waitForDebuggerOnStart` is `false`. `true` would pause each new target until
`Runtime.runIfWaitingForDebugger`, which buys determinism about catching a
frame's first bytes and costs a hang on every path that forgets to release it.
Nothing in `bt` needs a frame's first bytes; `wait-idle` is the barrier the
manual names.

On each `Target.attachedToTarget`:

1. **Ignore anything that is not an OOPIF.** Version 1 proposed
   `targetInfo.targetId != <this session's own target>` and called it exact.
   It is not: auto-attach reaches workers and other related targets, and that
   predicate sends a `worker` target into `Page.enable` and
   `Page.getFrameTree`. The filter is a positive list of target types this
   design supports, which today is `iframe` alone, **and** an ancestry check
   that the arriving target's frame resolves to a frame below this session's
   own. A type not on the list is ignored, and a new type does not become a
   Frame Session because nobody thought to exclude it.
2. Register that session's `Page` and `Runtime` handlers, then
   `Page.enable`, `Page.getFrameTree`, `Runtime.enable`, in that order. This
   is the ordering `cdp_handler._connect_cdp` already uses, and it exists
   because a first `Runtime.enable` replays the contexts that already exist
   and they must have somewhere to land.
3. Send `setAutoAttach` on the new session too. Measured: auto-attach does not
   cascade, so a grandchild needs it. The recursion terminates because the
   frame tree is finite; an implementation MUST still bound it, because a
   hostile page can nest deeply.

   **A depth bound alone is not a bound.** Twenty sibling OOPIFs all pass a
   depth limit of ten, which is the shape an ad-heavy page actually has. The
   bounds are: depth, active Frame Sessions, sessions initialising at once,
   and the aggregate budget for optional work. Their values come from
   measurement on a wide page, not from a guess. A frame past any bound is
   reported in `frames list` with its URL and marked unreachable rather than
   silently dropped.
4. Splice: find the frame whose id equals `targetInfo.targetId`'s `parentId`
   from the child's own `Page.getFrameTree`, and attach the child's tree
   there. Both halves were measured: the child's frame id is its target id,
   and its `parentId` is in the parent's tree.

### The invariant the splice must keep

`3baf228` landed the frame manager's invariant, and it is the reason this
design is tractable at all: **a frame the map holds is reachable from the
root, exactly once, and a selection resolves in the order `frames list`
prints.** A spliced tree MUST satisfy the same invariant, and the same
assertion helper MUST be run against it after every sequence.

Version 1 listed three new ways to break it. The review found the list short
and the claim behind it overstated: the search said to cover "every plausible
sequence" had been run with a filter, only sequences whose parent frame
already existed, and it lived in a scratch file rather than the suite. Two
sequences outside that filter stranded a frame, both in shipped code. They are
fixed and the search is now an unfiltered test (14,424 sequences of up to
three events, zero failures).

That does not settle the splice, because a splice adds concurrency the
existing event handlers have never seen. **Every asynchronous read MUST carry
the session and the document generation it was issued against, and a
completion whose generation has moved on MUST be discarded rather than
applied.** Without that rule, a `Page.getFrameTree` that returns after its
session detached overwrites state that replaced it.

The sequences the design MUST specify, beyond the three below:

- an old session's events arriving after ownership moved to a new one;
- a tree response completing after its session or an ancestor detached;
- a parent tree refresh landing after child trees were spliced under it;
- a duplicate attach or navigation erasing descendants already discovered;
- an OOPIF becoming same-process, and a same-process frame becoming an OOPIF;
- a pending child whose parent frame never arrives.

**A renderer swap is not a removal.** CDP distinguishes
`Page.frameDetached` with `reason: "swap"` from DOM removal, and the current
handler ignores the distinction, so a swap deletes a frame that is still
there. The review reproduced it: after a navigation and a swap-detach the
structural invariant still passes while the child frame has vanished. An
invariant that holds over a wrong tree is the reason this rule is explicit.

The three from version 1, each of which MUST still be handled:

- A child session attaches before the parent's frame tree contains the
  placeholder frame. The splice point does not exist yet. The child's frames
  are held aside and spliced when the placeholder arrives, rather than
  attached to the root, which would put them in the wrong place in the
  listing.
- A child detaches while its own children are still attached. The existing
  `_forget_descendants` handles the frames; the Frame Sessions below it MUST
  be dropped from the map in the same step, or they leak for the rest of the
  run.
- A parent navigates. Measured: the old child's session detaches and a new one
  attaches. The existing "a navigated frame forgets its descendants" rule
  covers the frames; the sessions MUST follow them.

### Routing

Three reads currently assume one session, and each needs a routing key.

| Read | Today | With Frame Sessions |
|---|---|---|
| `Runtime.evaluate` on the selected frame | the page session, with `contextId` | the selected frame's Frame Session, with that session's `contextId` |
| `click` / `fill --uid` | the page session, resolving `backendNodeId` | the Frame Session the UID's document token names |
| `snapshot` | one accessibility tree | one tree per Frame Session, merged at the `Iframe` node |

`FrameManager.get_selected_execution_context_id()` returns a context id and
nothing else, which is no longer an address. It MUST become
`get_selected_context()`, returning the pair `(frame session id, execution
context id)`, and its two production callers updated. Returning a context id
alone from a world with several renderers is the `backendNodeId` mistake
again.

**Changing the getter is not enough.** Execution context ids are also
renderer-local, so the stored map and the event handlers need session identity
too. Two frames in two sessions can both own context `1`, and today the second
`Runtime.executionContextCreated` overwrites the first:

```
handle_execution_context_created(id=1, frame=R)
handle_execution_context_created(id=1, frame=C)
handle_execution_context_destroyed(id=1)
-> R keeps context 1, C has none
```

The destroy cleared the wrong frame. `Runtime.executionContextsCleared` is
worse: forwarded from a child session to the current handler, it clears every
frame's context in the whole tree, so a child navigation breaks an unrelated
parent read. **Context state MUST be keyed by `(frame session, context id)`,
and a clear or destroy MUST be scoped to the session it arrived on.**

**The UID carries the routing, and version 1 specified it wrongly.** Two
defects, both reproduced by the review.

*The map cannot be built by the snapshot.* Version 1 wrote the document token
to Frame Session map when a snapshot minted UIDs. Each invocation is a new
runtime, so a later `bt click --uid` has neither that map nor those session
ids. The existing contract is the opposite, and it is deliberate: a UID
resolves in a process that never took a snapshot.

```
reader.current: None
resolved backendNodeId: 7
```

Version 1 would read the missing route as a stale document and refuse a UID
the product currently honours. So **routes are reconstructed from the live
frame tree on every invocation**, independently of any snapshot, at the point
a UID is resolved. A route that is missing after reconstruction is one of
three things and the design MUST tell them apart: the document is gone
(the existing staleness error, exit 1), the Frame Session has not been
discovered yet (wait for discovery within the deadline), or the Frame Session
did not answer (exit 1, and say which frame). Inside a Step Run a navigation
invalidates the routes for that frame even when its Frame Session survives.

*The document token is not an identity.* `doc_token_from_loader_id` takes the
first 12 hex characters of the loader id, so two loader ids can produce one
token:

```
doc_token_from_loader_id("A"*12 + "0"*20)  ->  AAAAAAAAAAAA
doc_token_from_loader_id("A"*12 + "1"*20)  ->  AAAAAAAAAAAA
```

Version 1 said tokens "differ by construction". They do not. This is a
constructed collision rather than one Chrome is likely to produce, and the
categorical claim was still false.

Worse for this design, `NativeSnapshotReader.build()` takes **one**
`doc_token` for a whole build and keys raw nodes by AX `nodeId`. Run over a
parent and a child tree it produces:

```
[('RootWebArea', 'PARENTTOKEN-1'),
 ('Iframe',      'PARENTTOKEN-2'),
 ('RootWebArea', 'PARENTTOKEN-x3'),
 ('textbox',     'PARENTTOKEN-x4')]
```

The child's nodes take the parent's token, and their repeated backend ids
become unaddressable `x<ordinal>` UIDs. The merged snapshot this RFC specifies
cannot be produced by the current reader at all.

So the build MUST take **document identity per node**, not per build,
including for same-process child documents, and AX node ids and owner-node
lookups MUST be namespaced by their owning frame before any merge. The
`<docToken>-<backendNodeId>` text shape survives; what changes is what
`docToken` means. It becomes a frame id plus the **full** loader id, hashed to
a fixed width, with the collision policy stated: a token is an identity, and
two live documents MUST NOT share one.

### `snapshot` across the boundary

A snapshot is one accessibility tree read from one session. With Frame
Sessions it becomes one read per session, merged at the `Iframe` node whose
frame id names the child.

The merged tree MUST be indistinguishable from a same-process one in
everything except the UIDs' document tokens, which differ by construction:

```
[uid=5F11BE4F-1] RootWebArea "parent-origin"
  [uid=5F11BE4F-7] heading "parent"
  [uid=5F11BE4F-8] Iframe
    [uid=A91C0E22-1] RootWebArea "child-origin"
      [uid=A91C0E22-4] textbox "card number"
```

A child session that fails to answer MUST NOT fail the snapshot. The `Iframe`
node is rendered with a one-line note saying the frame did not answer, and the
rest of the page is returned, because a snapshot that returns nothing because
one ad frame timed out is worse than one that returns the page.

**Catching exceptions does not achieve that**, and version 1 assumed it would.
The snapshot call is bounded by the run's deadline, and when that expires the
whole read is cancelled, parent tree included. The review reproduced it with a
stalled child read against the existing machinery:

```
"text": "Error: take_snapshot did not answer in time"
"isError": true
```

The parent tree had already answered and was thrown away. So containment is an
execution policy, not a `try`:

- each child read is its own bounded task, with its own budget;
- the child budget in aggregate is capped below the operation's deadline, so
  there is always time left to return the parent;
- a task is cancelled when its session detaches;
- a child failure or timeout resolves to the note on the `Iframe` node.

**There is a second containment boundary, and it is sharper.**
`core.cdp_client._recv_loop()` calls event handlers synchronously, so an
exception escaping a child's handler exits the receive loop for the whole
connection:

```
Unexpected error in receive loop: child setup failed
connected= False
parent pending= ConnectionError('WebSocket connection closed')
```

One child's setup failure takes down the page session. Every handler this
design adds MUST contain its own exceptions at that boundary. Registering an
`async` handler does not help, because the receive loop does not await it.

### Domain enables, per Frame Session

RFC-03's rule is per-session state and reads unchanged once "session" means
"Frame Session":

- `Page` and `Runtime` belong to the run on **every** Frame Session, and no
  step may disable them on any of them. `step_list`'s refusal of `Page.disable`
  and `Runtime.disable` already covers this, because a raw step names no
  session.
- Every other domain a step enables on a Frame Session, that step gives back
  on that Frame Session.
- A domain the caller enabled by hand in a raw step is not undone. A raw step
  sends on the page session, so this is unchanged.

Auto-attach is new run-owned state and the existing guards do not cover it.
Measured against `_validate_passthrough`:

```
Page.disable            StepListError
Runtime.disable         StepListError
Target.setAutoAttach    accepted
```

A raw step can turn auto-attach off, which silently removes every Frame
Session from the rest of the run: the same shape as the `Page.disable` step
RFC-03 refused, with the same silence. **`Target.setAutoAttach` MUST be
refused as a step** for the same reason, exit 2, nothing sent.

### Step Run

A Step Run opens its session once and every step runs on it. With Frame
Sessions it opens the page session once, and Frame Sessions come and go
underneath as the page navigates. That is a change to what "one attached
session" means and `CONTEXT.md` MUST record it, but nothing in the Step Run
contract depends on the count:

- Frame selection still lasts across steps, and now a selected OOPIF's session
  is what a later frame-scoped step routes to.
- The deadline still bounds every wait, including waits on child sessions,
  because `bounded_timeout` is on the runtime rather than the session.
- The run document is unchanged.

## Error Handling

| Case | Exit | Where | Message |
|---|---|---|---|
| `setAutoAttach` is refused by the browser | 0 | stderr, one line | the page is driven without OOPIF support, and `frames select` keeps the `3baf228` diagnosis |
| a child session fails to enable `Page` | 0 | stderr, one line | that subtree is absent from the listing and marked unreachable |
| a send on a detached child session (`-32001`) | 0 | none | the frame is gone; treat as a detach, not a failure |
| a frame-scoped read whose Frame Session is gone | 1 | stderr | "the frame you selected is gone"; the pattern is kept, per the existing rule |
| `click --uid` whose document token names no session | 1 | stderr | the existing staleness message, unchanged |
| nesting beyond 10 levels | 0 | stderr, one line | the frame is listed and marked unreachable |

The shape of that table is deliberate: **nothing about reaching into a child
frame may fail an invocation that did not ask about one.** A page with a
broken ad iframe must still snapshot, still list frames, and still exit 0.
That is the rule the implementation is most likely to break and the one its
tests should attack hardest.

**It has a precedence rule, which version 1 missed.** Suppressing every
`-32001` as exit 0 would report a `click` that never reached a page. The rule
is which work the caller asked for:

| The child frame was | A detach mid-operation |
|---|---|
| optional, expanding a snapshot or a listing | omit it, note it, exit 0 |
| named by the caller, through `--uid`, `--key` or a selection | fail that operation, exit 1 |

A `click --uid` whose frame went away did not happen, and the caller MUST be
told.

## Security Considerations

Reaching into a cross-origin iframe reads content the parent page's own
JavaScript cannot read. That is not new: CDP already grants this, and `bt`
already drives any page the caller points it at. What changes is that a
command aimed at a page now touches origins the caller did not name.

Three consequences, and the first two are the ones that matter:

- **`snapshot` output now carries other origins' content.** A card number
  typed into a payment iframe is in the accessibility tree. It is already in
  the screenshot, so this is a widening of an existing exposure rather than a
  new one, but the manual MUST say that a snapshot of a page with embedded
  third-party frames includes those frames' content.
- **`storage get` on a selected OOPIF reads that origin's storage**, including
  its cookies for that origin. `storage get` already refuses without a
  selection or a `--key`, so this needs no new gate, and the manual MUST say
  what the selection now reaches.
- Version 1 said auto-attach grants nothing a caller could not get by driving
  the iframe's URL directly. That contradicts this RFC's own motivation, which
  is that the frame's content depends on the parent. Reading an embedded
  document's **current state**, after its handshake with the parent and with
  the parent's session behind it, is not the same as opening its URL. This is
  a real widening, and it is the right one for a tool whose purpose is driving
  pages an agent was pointed at, but it should not be described as free.

No new network exposure: every session rides the existing browser-level
WebSocket, bound to loopback.

## Alternatives Considered

**Leave it, and keep the diagnosis.** `3baf228` already makes the failure
legible. The argument for stopping there is that auto-attach changes the
One-Shot Session shape for every invocation, to serve frames many pages do not
have. The argument against is the one the Motivation makes: the frames an
agent is asked about are disproportionately the ones behind this boundary.
Rejected, but it is the honest fallback if the implementation proves to have a
blast radius the error handling above cannot contain.

**A flag, `--frames all`, off by default.** Zero blast radius, and every agent
hits the broken behaviour first and must know a flag exists to get past it.
The `3baf228` message could name the flag, which removes most of that
objection. Recorded as the fallback for Open Question 1.

**Drive the iframe's URL as a top-level page.** Available today and sometimes
correct. It fails whenever the frame's content depends on the parent, which is
the common case for exactly the frames that matter.

**`Page.createIsolatedWorld` on the parent.** Does not cross a process
boundary; an isolated world is still within one renderer. Not applicable.

**`waitForDebuggerOnStart: true`.** Deterministic about a frame's first
bytes, at the cost of a hang on every path that forgets to release. Nothing in
`bt` needs a frame's first bytes.

## Decisions

1. **`Target.setAutoAttach` with `flatten: true`**, re-sent on every child
   session, filtered to a positive list of supported target types plus an
   ancestry check, and bounded on depth, session count, concurrent
   initialisation and aggregate optional work.
2. **One Spliced Frame Tree**, keeping the invariant `3baf228` and `#138`
   landed, with the splice point taken from the child's own `parentId`, and
   every asynchronous read carrying the session and document generation it was
   issued against.
3. **Routes are reconstructed from the live frame tree on every invocation**,
   never carried from a snapshot, because a UID resolves in a process that
   never took one. `get_selected_execution_context_id` becomes
   `get_selected_context`, returning a `(frame session, context)` pair, and
   execution-context state is keyed by `(frame session, context id)`.
4. **Document identity is per node, not per build.** `docToken` becomes a
   frame id plus the full loader id, hashed to a fixed width, and two live
   documents MUST NOT share a token. The `<docToken>-<backendNodeId>` text
   shape is unchanged.
5. **`snapshot` merges child trees** at the `Iframe` node. Each child read is
   a bounded task with a budget capped below the operation's deadline, so the
   parent tree is returned even when a child stalls, and a child that does not
   answer is a note on that node.
6. **Every handler contains its own exceptions at the receive-loop boundary**,
   because that loop is shared and calls handlers synchronously.
7. **`Target.setAutoAttach` is refused as a step**, exit 2, alongside
   `Page.disable` and `Runtime.disable`.
8. **Off by default for the first release**, behind `--frames all`, which the
   `3baf228` diagnosis names. Settled, see Open Question 1.
9. **Nothing about an *optional* child frame may fail an invocation.** A child
   the caller named through `--uid`, `--key` or a selection fails that
   operation loudly instead.

## Open Questions

1. ~~**On by default, or behind a flag?**~~ **Settled: behind a flag for the
   first release**, and Decision 8 records it. Version 1 recommended on by
   default, on the grounds that an agent must not need to know a flag exists.
   That reasoning is sound and it is outweighed: six blocking findings in
   version 1 mean the correctness work is real, and default-on would put every
   one of those paths into every invocation, including the ones that never
   asked about a child frame. A flag does not excuse a defect; it limits how
   far the first release's defects reach. The `3baf228` message names the
   flag, so an agent that hits the boundary is still told what to do.
   Default-on becomes a separate, later change once separate-invocation UID
   routing, renderer swaps, slow children, detach-during-snapshot and wide
   pages all pass.
2. **What does `frames list` print for an OOPIF?** The tree position is
   settled. Whether the listing marks the process boundary is not.
   Recommendation: mark it, because knowing a frame is out-of-process explains
   why a UID from one snapshot addresses a different document. Version 1 gave
   `postMessage` as the reason, which is wrong: `postMessage` works across
   renderer processes.
3. **Does `screencast` or `screenshot` change?** Neither is frame-scoped
   today; both capture the page. Recommendation: no change, and the manual
   says so.
4. **What is the real cost on a wide page?** Unmeasured, and Decision 1's
   bounds cannot be given values until it is. This is the first thing the
   implementation phase does, before any of the design is called cheap.
5. **What is the OOPIF readiness barrier?** `wait-idle` polls resource-entry
   counts in the page session, so it says nothing about whether a child
   session has attached and answered. A run that navigates and then reads a
   child frame needs a barrier that waits for discovery, and this RFC does not
   specify one.

## Changes in this revision

**Version 2** (2026-09-21) is the revision after a cross-family adversarial
review by `gpt-6-astra@codex` against the version 1 snapshot at `2239955`.
The full report is in
`docs/rfc/04_reach-into-cross-origin-iframes.review-01.md`, and the brief it
answered is beside it. The verdict was
block pending revision: six blocking findings, seven non-blocking, and a
correction to three of version 1's seven measured findings. Every one was
reproduced locally before it was applied. The reviewer's sandbox refused
sockets, so its work was source inspection and controlled input, which it
said plainly rather than reporting as live.

Blocking, and what changed:

1. **UID routing could not work across invocations.** Version 1 built the
   document-token-to-session map when a snapshot minted UIDs. A later `bt
   click --uid` is a new process with no map, and the existing contract
   deliberately resolves a UID without a snapshot in that process. Routes are
   now reconstructed from the live frame tree on every invocation, and a
   missing route distinguishes a gone document from an undiscovered or
   unresponsive Frame Session.
2. **The document token is not an identity.** `doc_token_from_loader_id` takes
   12 hex characters, so version 1's "differ by construction" was false. Worse,
   `NativeSnapshotReader.build()` takes one token per build and turns repeated
   backend ids into unaddressable `x<ordinal>` UIDs, so the merged snapshot
   could not be produced at all. Identity is now per node, from a frame id plus
   the full loader id.
3. **The OOPIF filter accepted workers.** `targetId != own_target_id` is true
   for a worker target, which version 1 would have sent into `Page.enable`.
   The filter is now a positive type list plus an ancestry check.
4. **The invariant was not established for the sequences a splice adds.** The
   search behind the claim was filtered and uncommitted. It is now an
   unfiltered test, and the two sequences it had excluded were real defects,
   fixed in #138. The RFC now requires session and document generation on
   every asynchronous read, and separates a renderer swap from a removal.
5. **Changing the context getter was not enough.** Context ids are
   renderer-local, so the stored map and the events need session identity too.
   Reproduced: two frames owning context `1`, and a destroy clearing the wrong
   one.
6. **Containment needs an execution policy, not exception handling.** The
   deadline cancels the whole snapshot, parent tree included, so catching a
   child's exception does not save the page. Child reads are now bounded tasks
   with an aggregate budget below the deadline. A second boundary was found:
   `_recv_loop` calls handlers synchronously, so one child's exception closes
   the whole connection.

Three of version 1's measured findings were overstated and are corrected in
place: the child-arrival timings used the wrong reference point, the claimed
second page session was the probe counting its own explicit attach, and
"auto-attach survives a parent navigation" was never tested because the probe
removed the child first.

Non-blocking, applied: cross-origin is not the same as out-of-process, since
site isolation splits by site; `postMessage` works across renderer processes,
so Open Question 2's reason was wrong; the cost accounting omitted
`Page.getFrameTree` and the snapshot reads; the security section's
direct-navigation equivalence contradicted the motivation; a depth bound
alone does not bound twenty siblings; and `wait-idle` is not an OOPIF
readiness barrier, which is now Open Question 5.

**Open Question 1 is settled**: off by default for the first release, behind
`--frames all`. This reverses version 1's recommendation, on Kevin's decision
and with the reviewer concurring.

The reviewer confirmed the scope fits, and noted that RFC-01's exclusion
applies to that merge effort rather than to this capability, so RFC-04 should
authorize it explicitly. It does, in Decisions.

**Version 1** (2026-09-21) is the first draft. Every CDP claim was measured
against headless Chrome before it was written; the transcripts and probes are
in `docs/rfc/04_reach-into-cross-origin-iframes.measurements.md`.

## References

- Issue #127, which filed the evidence.
- `3baf228`, the diagnosis and manual entry that landed first.
- RFC-01 version 6, for the UID scheme this design gives a second job.
- RFC-03 version 6, for the domain-enable rule and the frame-tree invariant
  that a spliced tree must keep.
- `docs/rfc/04_reach-into-cross-origin-iframes.measurements.md`.
