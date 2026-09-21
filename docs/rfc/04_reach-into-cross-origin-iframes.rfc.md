---
number: 04
title: "Reach into cross-origin iframes"
type: feature
status: Draft
author: "Kevin Frilot"
date: 2026-09-21
version: 1
---

# RFC-04: Reach into cross-origin iframes

## Abstract

A cross-origin iframe runs in its own renderer process under Chrome's site
isolation, with its own CDP target. `bt` attaches to the page target and reads
`Page.getFrameTree` there, so such a frame is absent from `frames list`,
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
2. **`Target.setAutoAttach` costs 0.3 ms** and the child session arrives 1.2
   to 8.0 ms later.
3. **The child's frame id is its target id, and its `parentId` names a frame
   already in the parent's tree.** The splice is given, not guessed.
4. **A child session is an ordinary page session.** `Page.getFrameTree`,
   `DOM.getDocument` and `Runtime.evaluate` all answer on it, and the child's
   own same-origin subframes are in its tree.
5. **`backendNodeId` collides across sessions**: 9 of 10 parent ids also named
   a node in the child. A backend id is not an address once two renderers are
   in play.
6. **Auto-attach does not cascade.** `setAutoAttach` on the page session
   reaches the child and stops; the grandchild needs it re-sent on the child's
   session.
7. **The lifecycle edges**: a child navigation keeps the session, removing the
   iframe detaches it, sending on a detached session is
   `-32001 Session with given id not found`, and auto-attach survives a parent
   navigation without being re-sent.

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

Against the 85 ms a step already costs, a page with three OOPIFs adds roughly
one round trip each. The figure the RFC commits to is **one `setAutoAttach`
per Frame Session plus two enables per Frame Session**, and an implementation
phase MUST measure the real total on an ad-heavy page before the design is
called cheap.

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

1. **Ignore anything that is not an OOPIF.** Measured, `setAutoAttach` on a
   page session also re-attaches that session's own target under a second
   session id. The filter is `targetInfo.targetId != <this session's own
   target>`, which is exact, rather than `type == "iframe"`, which is merely
   usually right.
2. Register that session's `Page` and `Runtime` handlers, then
   `Page.enable`, `Page.getFrameTree`, `Runtime.enable`, in that order. This
   is the ordering `cdp_handler._connect_cdp` already uses, and it exists
   because a first `Runtime.enable` replays the contexts that already exist
   and they must have somewhere to land.
3. Send `setAutoAttach` on the new session too. Measured: auto-attach does not
   cascade, so a grandchild needs it. The recursion terminates because the
   frame tree is finite; an implementation MUST still bound it, because a
   hostile page can nest deeply. **The bound is 10 levels**, and a frame below
   it is reported in `frames list` with its URL and marked unreachable rather
   than silently dropped.
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

Three new ways to break it, each of which MUST be handled:

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
context id)`, and every caller updated. Returning a context id alone from a
world with several renderers is the `backendNodeId` mistake again.

**The UID carries the routing.** RFC-01 version 6 made a UID
`<docToken>-<backendNodeId>`, valid for the lifetime of the document that
produced it. Measured, backend ids collide across sessions: 9 of 10 did on the
probe page. The document token does not, so the runtime keeps a map from
document token to Frame Session, written when a snapshot mints UIDs, and
`click --uid` resolves through it. A UID whose token is not in the map is the
existing staleness error, unchanged: the document that produced it is gone.

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
- Auto-attach does not grant anything a caller could not already obtain by
  driving the iframe's URL directly, with enough effort. It removes the
  effort.

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
   session, bounded at 10 levels.
2. **One Spliced Frame Tree**, keeping the invariant `3baf228` landed, with
   the splice point taken from the child's own `parentId`.
3. **The document token routes.** `get_selected_execution_context_id` becomes
   `get_selected_context`, returning a `(frame session, context)` pair.
4. **`snapshot` merges child trees** at the `Iframe` node, and a child that
   does not answer is a note on that node rather than a failed snapshot.
5. **No new verb, no new flag** beyond whatever Open Question 1 settles.
6. **Nothing about a child frame may fail an invocation that did not ask about
   one.**

## Open Questions

1. **On by default, or behind a flag?** Kevin's call, because it widens what
   every invocation does. Recommendation: **on by default**, because the
   error-message fix exists precisely on the premise that agents do not know
   what they cannot see, and a flag they must know about has the same problem.
   The fallback is `--frames all`, named in the `3baf228` diagnosis.
2. **What does `frames list` print for an OOPIF?** The tree position is
   settled. Whether the listing marks the process boundary is not.
   Recommendation: mark it, because an agent that knows a frame is
   out-of-process can reason about why a `postMessage` will not work.
3. **Does `screencast` or `screenshot` change?** Neither is frame-scoped
   today; both capture the page. Recommendation: no change, and the manual
   says so.
4. **What is the real cost on an ad-heavy page?** The probe page has one
   OOPIF. A news site has a dozen. The implementation phase MUST measure
   before the design is called cheap, and Decision 1's bound may need to
   become a count rather than a depth.

## Changes in this revision

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
