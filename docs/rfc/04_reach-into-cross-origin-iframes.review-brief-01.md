# Adversarial review: RFC-04, reaching into cross-origin iframes

Repository: `/Users/kevin/dev/browser-tools`. Branch: `rfc/04-cross-origin-iframes`.
Read `docs/rfc/04_reach-into-cross-origin-iframes.rfc.md` and its measurements
file beside it. Diff against main with `git diff main..HEAD`.

You have read and write access. Run things. Break things. Restore what you break.

**Your sandbox may refuse sockets.** Two earlier reviews of this project could
not reach `127.0.0.1:9222` and said so, which was the right call. If yours
cannot either, say so plainly and review what you can reach. Do not report a
controlled-transport result as a live one.

**If you can reach a browser**, launch your own with `uv run bt launch
--headless` and resolve its port from `uv run bt status` by instance name.
Port 9222 on this machine is a different, long-lived browser. Driving it would
give you the right shape for the wrong page.

## What the product is

`bt` is a Python CLI that drives Chrome over CDP. The CLI is the only surface.
`CONTEXT.md` is the naming authority. `src/browser_tools/GUIDE.txt` is the
shipped manual, and `tests/test_guide.py` fails the build when a verb has no
entry.

## The problem

A cross-origin iframe runs in its own renderer process under Chrome's site
isolation, with its own CDP target. `bt` attaches to the page target and reads
`Page.getFrameTree` there, so the frame is absent from `frames list`,
`frames select` cannot select it, and `snapshot` shows the `Iframe` node with
nothing under it. Issue #127.

Commit `3baf228` (merged) landed the half that is right either way: the error
message now says when the missing frame is a cross-origin iframe, and the
manual says `frames` sees same-process frames only. This RFC specifies
removing the limit.

## What I measured before writing it

All in `docs/rfc/04_reach-into-cross-origin-iframes.measurements.md`, with the
probes inlined. The seven findings, so you can attack them:

1. `--target` cannot name an iframe target; `core/attach.py:205-207` filters to
   `type == "page"`. So there is no workaround today.
2. `Target.setAutoAttach` costs 0.3 ms; the child session arrives 1.2 to 8.0 ms
   later.
3. The child's frame id **is** its target id, and its `parentId` names a frame
   already in the parent's tree.
4. A child session is an ordinary page session: `Page.getFrameTree`,
   `DOM.getDocument`, `Runtime.evaluate` all answer, and its same-origin
   subframes are in its tree.
5. `backendNodeId` collides across sessions: 9 of 10 parent ids also named a
   child node.
6. Auto-attach does not cascade. `setAutoAttach` must be re-sent on each child
   session.
7. Lifecycle: a child navigation keeps the session; removing the iframe sends
   `Target.detachedFromTarget`; a send on a detached session is
   `-32001 Session with given id not found`; auto-attach survives a parent
   navigation without a re-send.

## What I want from you

1. **Are the seven findings right?** They were measured on one probe page with
   one OOPIF. Which of them is an artifact of that page rather than a property
   of CDP? Finding 3 is the one the whole design rests on, and finding 5 is the
   one that decides the UID scheme. Check both hardest.
2. **The splice.** The design says: find the frame whose id equals the child's
   `parentId` and attach the child's tree there. What orderings break that?
   The RFC names one (the child attaches before the placeholder frame exists).
   Find the others. `3baf228` landed a frame-tree invariant - a frame the map
   holds is reachable from the root, exactly once, and a selection resolves in
   the order `frames list` prints - and I claim a spliced tree can keep it.
   Attack that claim. `tests/test_session_hygiene.py` has the assertion helper
   and an exhaustive search over short event sequences; extend it if that is
   the cheapest way to break me.
3. **The routing.** A UID is `<docToken>-<backendNodeId>`. I claim the document
   token can carry which session holds the node. Can two Frame Sessions mint
   the same document token? What does `NativeSnapshotReader` actually key on?
   If two documents in two renderers can collide, the scheme is wrong and the
   whole design needs a different address.
4. **The blast radius.** The RFC's hardest rule is "nothing about reaching into
   a child frame may fail an invocation that did not ask about one." Find a
   path where it breaks: a slow child, a child that never enables `Page`, a
   page with twenty ad iframes, a child that detaches mid-snapshot.
5. **Cost.** I measured one OOPIF. Open Question 4 admits an ad-heavy page is
   unmeasured. If you can reach a browser, measure it, and say whether the
   10-level depth bound should be a count instead.
6. **Open Question 1**, on by default or behind a flag. I recommend on by
   default. Argue it either way; it is the decision I am least sure of.
7. **Is anything in the RFC not true of this codebase?** I cite
   `core/attach.py:205-207`, `cdp_handler._connect_cdp`'s ordering,
   `FrameManager.get_selected_execution_context_id` and its two callers, and
   `step_list`'s refusal of `Page.disable` / `Runtime.disable`. Check each.
8. **Is the scope right?** RFC-01:36 draws a line at "new browser capabilities
   present in neither project today". Is auto-attach on the wrong side of it?
   Saying "do not build this" is a valid answer and I would rather hear it now.

## Rules

- Do not fix anything. Report.
- A finding needs a reproduction: the command, the output, and what the RFC
  says should have happened instead.
- Separate **blocking** from **non-blocking**.
- If a premise in this brief is itself wrong, say so.
- Voice: plain, no em dashes, no praise.

## Output

Return your report as your final message. An earlier reviewer's sandbox was
read-only and could not write a file, so do not depend on writing one.
