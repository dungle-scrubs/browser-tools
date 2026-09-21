---
number: 03
title: "Run many steps in one invocation"
type: feature
status: Accepted
author: "Kevin Frilot"
date: 2026-09-21
version: 6
---

# RFC-03: Run many steps in one invocation

## Abstract

Every browser-driving verb in browser-tools is its own process. It starts Python,
imports the package, reads the registry, opens a CDP connection, attaches a
**One-Shot Session**, does one thing, and exits. The fixed per-invocation tax is
67.7 ms before any browser work, measured, of which 52.3 ms is interpreter start
and import. A ten-step flow pays it ten times: 1.70 s against 0.85 s for the same
ten steps in one process, a measured 50%.

This RFC specifies one new verb, `run`, that executes an ordered list of verb
phrases against one CDPRuntime in one invocation. It adds no browser capability:
every step is a verb The Manual already documents, or the raw CDP passthrough. It
adds no dependency. It introduces no interpreter, no variables, and no data flow
between steps, so a step list can do exactly what a sequence of `bt` invocations
can do and nothing more. It amends two normative rules, each narrowly and each stated as
an amendment: The Manual's rule that exit 1 prints nothing on stdout, because a run
that fails at step 7 of 10 must tell the caller which six steps already changed the
page; and RFC-01's rule that a failed operation reports no success field, because
inside a run the operation that succeeded or failed is the step.

## Introduction

### Problem statement

One problem, measured in `docs/rfc/03_run-many-steps-in-one-invocation.measurements.md`, and one property
that follows from fixing it.

**1. The fixed per-invocation cost is paid per step.**

Measured on this machine at `bf87ad8`, headless Chrome 153, medians over 12 runs:

| Layer | Cost |
|---|---|
| Python interpreter start | 12.5 ms |
| `import browser_tools.cli` | 39.8 ms |
| Registry read plus liveness probe | 15.4 ms |
| CDP connect, attach, one round trip, detach | 5.6 ms |

The connection is not the expense. It is 5.6 ms. The expense is 52.3 ms of
interpreter start and import, spent before the process knows what it was asked to
do, and a further 15.4 ms finding the browser.

**2. Frame selection does not survive a process. This is a property, not a second
problem, and version 3 demotes it.** Selecting a frame writes two fields on the
frame manager, `_selected_frame_id` and `_selected_url_pattern`
(`frame_manager.py:89-90`). The frame manager belongs to CDPRuntime, which
`CONTEXT.md` defines as built and torn down once per CLI invocation. So a selection
made in one process is gone in the next:

| Step | Process | Command | Result |
|---|---|---|---|
| 1 | A | `bt I frames select child` | exit 0, frame selected |
| 2 | B | `bt I storage get` | exit 1, `error: Error: No frame selected. Use select_frame first.` |
| 3 | C | `bt I storage get --key child` | exit 0 |

`GUIDE.txt:128-131` documents step 3 as the way through, and `curated.py:427-428`
states the cause in its own docstring.

**Versions 1 and 2 called this the stronger motivation. That was wrong, and the
correction is the substance of version 3.** Frame selection has exactly one
consumer in the product. `get_selected_frame` and
`get_selected_execution_context_id` have three call sites: `cdp_handler.py:816`,
inside `select_frame` itself, printing the context id back in its own confirmation
text; and `cdp_handler.py:856` and `:863`, both inside `_handle_get_frame_storage`.
On the CLI that is `storage get`, which already has `--key`. Two further reads of
the selection exist and neither changes this. `frame_manager.py:240` is
`get_selected_execution_context_id` reading `get_selected_frame`, internal to the
pair. `cdp_handler.py:794` reads `selected_frame_id` off the frame manager
directly, to print a `[selected]` marker in `frames list`; that is display, not a
frame-scoped read. So `--key` serves every
consumer of frame selection that exists, not a fraction of them, and a Step Run
wins nothing here today.

The property still holds inside a run, and Design specifies it, because it is real
and because it costs nothing to keep. It is not a reason to build the verb. If
frame-scoped reads multiply later, this section is where the case would be
rebuilt.

### Scope

A fit check on 2026-09-21 settled what this RFC covers and what it does not. Those
outcomes are recorded here as settled input. This RFC does not reopen them.

**In scope, and the subject of this RFC:** a verb that runs many steps in one
invocation, holding one CDP connection and one attached session.

**Declined, with the reason each does not fit:**

- **`heap`, heap snapshot capture.** RFC-01:36, "New browser capabilities present
  in neither project today." No current user case.
- **`lighthouse`, and the trace insight verbs `perf-start`, `perf-stop`,
  `perf-insight`.** The same boundary at RFC-01:36. Each also needs Node, against
  the packaging discipline stated in `extras.py`: the default install depends on
  `websockets` only. `chrome-devtools-axi` keeps this job.
- **A shell-out form of the two above,** where `bt` spawns the Node tool against
  the live instance's port. The same boundary. One CLI surface is tidiness, not a
  user need.
- **An interpreter, variables, or data flow between steps.** Specified in Design,
  "The execution model". Declined by Kevin on 2026-09-21; see Decisions, item 1.

**Held, not declined.** Each is reachable through the raw CDP passthrough today, so
a verb would be ergonomics over capability the product already has. Add one when a
real case appears, one verb per case. This RFC builds none of them, and `run`
fronts no verb that does not exist: `emulate`, `dialog`, `upload`, `drag`,
`fillform`, `pages` / `newpage` / `selectpage`.

**Not a candidate.** Attaching to a Chrome the tool did not launch already shipped
as `--endpoint URL`.

**Not a motivation.** UID drift across invocations is solved. RFC-01 version 6 made
a UID `<docToken>-<backendNodeId>`, valid for the lifetime of the document that
produced it. The reproduction script that demonstrated the old drift no longer
runs, because `NativeSnapshotReader.build()` now requires `doc_token`
(`native_snapshot.py:304`). Nothing in this RFC rests
on UID drift, and Design, "UID validity across steps", states that one invocation
changes the UID rule in no way at all.

## Terminology

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT,
RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted as described
in RFC 2119.

- **Step Run**: one invocation of the `run` verb. One CDP connection, one attached
  session, one CDPRuntime, an ordered list of steps executed until the list ends or
  a step fails. Nothing outlives the invocation.
- **Step List**: the ordered input to a Step Run. One step per line, each line a
  verb phrase in the grammar The Manual already documents.
- **Step**: one line of a Step List. A verb phrase with no leading `[INSTANCE]`, no
  `--endpoint`, and no `--target` or `--url`.
- **Run Document**: the single JSON document a Step Run prints on stdout, on
  success and on failure alike.

Both **Step Run** and **Step List** are new domain nouns and MUST be added to
`CONTEXT.md` when this RFC is built.

## Motivation

Ten browser-driving steps, run two ways against the same page and the same browser.
The steps are identical in both:

```
wait-idle, snapshot, frames list, frames select child,
storage get --key child, frames reset, snapshot, wait-stable,
frames list, frames select child
```

| Shape | Median of 5 runs |
|---|---|
| Ten invocations | **1.70 s** |
| One process, one CDP session | **0.85 s** |
| Saving | **0.85 s, 50%** |

Most of the 0.85 s that remains is `wait-idle` and `wait-stable` sitting out their
quiet windows, 500 ms and 300 ms by default. Those are intrinsic to the work and
no verb removes them. What one invocation removes is
the other 0.85 s: nine repetitions of the per-invocation tax.

So the speed argument is real and bounded. It is 85 ms per step avoided. It matters
on a ten-step flow and not on a two-step one, and **it is the whole case for this
verb.** Version 2 claimed frame-selection continuity was the stronger motivation.
Problem statement, item 2, withdraws that: continuity has one consumer today and
`--key` already serves it.

**That flow contains only handler-routed verbs**, so a second flow was measured
over the One-Shot Session transport. Ten steps, none carrying an intrinsic wait,
20 runs each side:

| Shape | Median of 20 runs | Range |
|---|---|---|
| Ten invocations | **870.1 ms** | 843.1 to 989.4 |
| One browser-level connection, one attached session | **83.0 ms** | 57.6 to 84.4 |
| Saving | **787.1 ms, 90%** | |

Per step, 87.0 ms falls to 8.3 ms. Pairing the fastest ten-invocation run against
the slowest one-session run still gives 90%, so the figure does not depend on
picking favourable runs.

**What flow B covers, precisely.** Its ten steps are eight raw `Domain.method`
calls and two `screenshot`s. It exercises the passthrough and `screenshot`
transports. It does **not** contain `wait`, `console-list` or `network-list`,
whose cost is dominated by their own windows (2 seconds by default for the list
verbs, 30 for `wait`), which one invocation does not remove. Version 3 claimed
flow B covered "the five"; it covers two of them, and this is the correction.

**One asymmetry, in the direction that favours a critic.** The floor side issues
bare `cdp.send` calls, so it skips work the ten-invocation side pays: the
screenshot blank-frame retry (`curated.py:534-546`), the file write, and JSON
rendering of each result. The true floor for a built verb is therefore somewhat
above 83.0 ms.

The two flows disagree, 50% against 90%, and the whole difference is the intrinsic
waits. The per-step tax removed is about the same in both, roughly 70 to 85 ms.
Flow A's floor is mostly the browser waiting on purpose; flow B's is not. **So 90%
is the figure for a flow with no waits and 50% for a flow with two, and the honest
headline is neither percentage but the per-step number.**

Flow B also settles a question in this RFC's favour: the five verbs that need the
most rework show the larger saving, not a smaller one.

One bound in the direction that favours a critic: `floor_probe.py` runs
`select_frame` twice where a true single-session run needs it once, because the
earlier selection would persist. The overstatement is a few milliseconds.

Raw numbers, the harnesses that produced them, the environment, and the
reproduction of the frame-selection loss are in
`docs/rfc/03_run-many-steps-in-one-invocation.measurements.md`.

One finding from that run belongs here because it affects how the numbers are read.
The `bt` on the driving dev's `PATH` is a uv tool install of version 0.3.0 that
predates the version 6 CLI grammar and the version 6 UID scheme. Every figure above
was measured against the repository build at `.venv/bin/bt`. See the measurements
file, "Which `bt` produced these numbers".

## Design

### The verb

```
run [INSTANCE] FILE [--timeout SECONDS] [--endpoint URL]
run [INSTANCE] -    [--timeout SECONDS] [--endpoint URL]
```

`FILE` is a Step List on disk. `-` reads the Step List from stdin. Exactly one
source MUST be given; giving neither, or both, is a usage error (exit 2).

Both sources are REQUIRED. A file is reviewable, diffable, and re-runnable, which
is what a flow you keep and re-run needs. stdin is what a caller that generates a flow
and does not want a temporary file needs, and some agent harnesses cannot easily
write one.

The verb is named `run` and not `script`. "Script" tells a caller it may write code,
and Design, "The execution model", specifies that it may not. Kevin settled the name on 2026-09-21; see
Decisions, item 2.

### The Step List

One step per line, in the grammar The Manual already documents:

```
# select the checkout frame once, then read it twice
frames select checkout
storage get
Page.getNavigationHistory '{}'
snapshot
```

Rules:

- Each non-empty line is one step. Lines are executed in order.
- A line whose first non-whitespace character is `#` is a comment and MUST be
  ignored. A blank or whitespace-only line MUST be ignored.
- A line is split into argv with `shlex.split` in POSIX mode. That is stdlib, and
  it gives the quoting rules a caller already uses at the shell, so
  `Page.navigate '{"url": "https://example.com"}'` reads the same in a Step List as
  it does on the command line.
- A line that `shlex.split` cannot parse, for example one with an unbalanced quote,
  is a usage error (exit 2).
- An empty Step List, meaning one with no steps after comments and blank lines are
  removed, is a usage error (exit 2).

The Step List is not a new grammar. It is the CLI grammar, one phrase per line,
minus the parts that belong to the invocation rather than the step. That is
deliberate: The Manual documents every step already, and `tests/test_guide.py`
enumerates verbs from the parser, so the step surface cannot drift from the
documented surface without failing the build.

### The execution model

**A Step List is data, not code. A Step Run MUST NOT contain a general
interpreter.** There is no `if`, no loop, no variable, no expression, and no way for
one step to read another step's result.

Three models were available. Their safety properties differ, and only one of them
is a property at all.

| Model | Safety property | Cost |
|---|---|---|
| Arbitrary Python, in-process | **None.** The script runs with the full privileges of the `bt` process, which is the agent's identity. `import os` and `import subprocess` are reachable. | Anything that can write `bt`'s stdin, or a file `bt` reads, gets local code execution. |
| Arbitrary Python, in a subprocess | **None.** A subprocess has the same user and the same filesystem, and Python ships no portable sandbox. | The same exposure, plus either the loss of the one-session property, or a new IPC protocol between parent and child. Buys crash isolation only. |
| A restricted API, no interpreter | **A Step List can do exactly what a sequence of `bt` invocations can do, and nothing else.** | No control flow and no data flow between steps. |

The third is specified. The reason is a fit check, not a security preference: the
caller is an agent, and an agent already has a general interpreter in its own
harness. Giving `bt` a second one duplicates a capability the caller has and adds
local code execution to a tool whose job is driving a browser. What the caller
cannot do from outside is hold one CDP session across steps, and that is exactly
what a Step List provides.

**The consequence.** With no data flow, a Step Run cannot act on
something a step in the same run discovered. A flow that must snapshot, read the
UIDs, and then click one of them stays two invocations. That is safe, because the
version 6 UID rule makes a UID valid until the page navigates, so it crosses the
process boundary intact. The flows a Step Run serves are the ones with no decision
between the steps: frame-scoped reads, and fixed sequences.

This is the one decision in this RFC that changes what the verb is. Kevin settled
it on 2026-09-21; see Decisions, item 1.

### The step surface

A step MUST be one of these, and nothing else:

```
snapshot | click | fill
wait-idle | wait-stable | wait
detect
console-list | network-list
frames list | frames select PATTERN | frames reset
storage get
screenshot
screencast
Domain.method '{...json params...}'
```

That is the set of verbs that drive the attached browser, plus the raw passthrough.
Each keeps its own flags unchanged, except the three the invocation owns.

**Excluded, each a usage error (exit 2) when it appears in a Step List:**

- `attach`. It streams until stdin reaches EOF, so it has no bounded end and could
  never hand control to a following step. It also reads stdin for subscription
  changes, which a Step Run may itself be reading from.
- `launch`, `stop`, `cleanup`, `status`, `profile`, `window-border`, `guide`,
  `help`. None drives the attached browser. `launch` and `stop` would change the
  instance the run has already resolved and connected to.

A note for whoever builds this, because the derivation looks like it could have
been done by subtraction and MUST NOT be. RFC-01:182-184 lists `window-border`
among the verbs that never take `--endpoint`, but the shipped `REGISTRY_VERBS`
omits it (`cli.py:76`) and so does `GUIDE.txt:326`. The code still refuses the flag,
because `window-border`'s parser never adds it. The two lists disagree, and a step
surface derived by subtracting one of them would inherit the disagreement. The
enumeration above is explicit for that reason, and it excludes `window-border` on
the ground that it drives no browser, which is true under either list.

`screencast` is included. It is a **Bounded Capture**, so it ends at its
`--duration` or its `--max-frames` cap and returns control to the next step. That
property is what makes it a step at all.

### Instance resolution

The connection opens once, so the instance is resolved once.

- `run` takes a leading `[INSTANCE]` under the same rule as every other
  browser-driving verb. `INSTANCE` MAY be omitted when exactly one instance is
  registered. With several, `run` MUST name the candidates rather than guess
  (exit 1). Naming the instance twice is a usage error (exit 2).
- `--endpoint URL` goes on the invocation, under the loopback rule unchanged.
- `--target SPEC` and `--url SUBSTRING` go on the invocation. They select the page
  the whole run attaches to.
- **A step MUST NOT carry `[INSTANCE]`, `--endpoint`, `--target`, or `--url`.** Each
  is a usage error (exit 2), refused before any step runs.

The consequence: one Step Run drives one page. A flow that must act on two tabs
stays two invocations. That follows directly from one attached session, and this
RFC does not work around it.

### One CDPRuntime for the run

**A Step Run MUST hold exactly one CDPRuntime for its whole duration, and every
step MUST execute over that runtime's session.** A run that opened a fresh session
per step would carry almost none of the saving, which is to say it would be ten
invocations with one command line.

Version 2 stated that requirement and stopped there. It is not enough, because two
different session mechanisms exist today and they are not interchangeable. This
section specifies the one a run uses, and what happens to the session state a step
leaves behind.

#### The connection model is the One-Shot Session's, not the handler's

Today `snapshot`, `click`, `fill`, `wait-idle`, `wait-stable`, `detect`, `frames`,
`storage` and `screencast` run over a `CDPHandler`, which opens a **page-level**
WebSocket (`cdp_handler.py:404-409`, through `resolve_page_ws_url_async`) and
sends with no `sessionId` (`cdp_handler.py:411-413`). `passthrough`, `wait`,
`console-list`, `network-list` and `screenshot` open a One-Shot Session, which is a
**browser-level** connection plus `Target.attachToTarget` with `flatten: true`, and
every send and every event carries a `sessionId` (`one_shot.py:79-108`).

That difference is not cosmetic. `cdp_client.py:158-161` dispatches an event to a
handler only when the handler's `filter_session` is `None` or equal to the event's
`sessionId`. On a page-level connection there is no `sessionId`, so an event's is
`None`, and a handler registered with one never fires. `wait` registers with one
(`events.py:344`). Routing `wait`, `console-list` and `network-list` through a
page-level handler would therefore break their event delivery outright, not subtly.

So the requirement is specific:

- **A Step Run MUST open one browser-level CDP connection and attach one flattened
  `Target` session, the One-Shot Session's model.** Every step sends over that
  session and every step's event handlers are registered against its `sessionId`.
- **The run's CDPRuntime, and therefore its frame manager, MUST be built over that
  attached session** rather than over a page-level WebSocket. This is a change to
  how `CDPHandler` connects today, and the RFC states it as a requirement on the
  contract. It specifies no phasing for meeting it.

#### Domain-enable state, and why it needs a rule

CDP domain enables are session state. Nothing in the tree ever sends
`Domain.disable`. Today that is harmless: the session is detached when the
invocation ends, so every enable dies with it. In a Step Run the session outlives
every step, so an enable from step 3 is still in force at step 9.

That is not merely untidy. It changes what a verb observes, and the difference was
measured rather than reasoned about:

| Case | `Runtime.enable` | `executionContextCreated` delivered |
|---|---|---|
| One session, first enable | first | **2** |
| One session, second enable | second | **0** |
| Fresh session, one enable | first | **2** |

A first enable replays the state that already exists. A second enable on the same
session replays nothing. `GUIDE.txt:121-122` promises that `console-list` and
`network-list` "subscribe before enabling their domain, so an event emitted during
enable is in the result". A second `console-list` in one run would not get that,
because its enable is a no-op. The probe is inlined in `docs/rfc/03_run-many-steps-in-one-invocation.measurements.md`, with the finding.

The rule, in three parts:

1. **`Page` and `Runtime` belong to the run's CDPRuntime.** They are enabled once
   when the session is attached and stay enabled for the whole run. **A step MUST
   NOT disable either.** The frame manager depends on `Page` events, so a step that
   disabled `Page` would break frame selection for every later step.
2. **Every other domain a step enables, that step MUST disable when it ends.** That
   restores first-enable semantics for the next step that wants the domain, so
   `console-list`, `network-list` and `wait` mean inside a run exactly what they
   mean outside it. The cost is one extra CDP round trip per domain per step, which
   is about 1 ms against the 85 ms a step already saves.

   Two cases sit under this rule and the build MUST handle both.

   A raw passthrough step naming an enable, for example `raw Network.enable '{}'`,
   is **not** covered. The rule governs the enables a verb issues on the caller's
   behalf, which the caller did not write and cannot see. An enable the caller
   typed is the step's whole output, and undoing it would make the step a no-op.
   A caller who turns a domain on by hand turns it off by hand, with a later
   passthrough step.

   **The two rules collide, and the caller's enable wins** (version 6). A raw
   `Network.enable` step followed by a `network-list` step satisfies both
   descriptions at once, and CDP cannot separate them: a second `Network.enable`
   succeeds exactly like a first, so the reply says nothing about who turned the
   domain on. The run therefore records the domains its raw steps named, and a
   later curated step MUST NOT disable one of them. The cost is that the curated
   step does not get a first enable on that domain, which is the same exception
   rule 1 already carries for `Page` and `Runtime`. The alternative costs the
   caller a step that silently did nothing.

   A disable that fails MUST NOT fail the step. Some domains have no `disable`,
   which is why the enable path already suppresses `CDPError` (`events.py:347-350`,
   `list_verbs.py:106-108`). The disable path gets the same guard: suppress the
   error, carry on to the next step. The cost of a missed disable is a later step
   losing its first enable, which rule 1 already names as the known exception;
   the cost of propagating it is a run that fails after its work succeeded.
3. **Every step MUST remove every event handler it registered before it ends.** The
   existing verbs already do this in a `finally` (`events.py:367-368`,
   `list_verbs.py:114-116`); a Step Run makes it normative rather than incidental,
   because a leaked handler now survives into later steps instead of dying with the
   process.

Together these give a caller most of the property they need, and the exception has
to be stated rather than glossed.

**After a navigation step, a run MUST wait before it reads** (version 6).
`Page.navigate` returns when the navigation commits, and the frame tree updates
from the event that follows. A step placed straight after a navigate can run
before that event arrives and read the frames of the page the run just left.

The manual MUST name `wait-idle` for this and MUST NOT name
`wait --event Page.loadEventFired`. `wait` reports events that arrive after it
subscribes, and it subscribes when its own step starts, so a load that fired in
the gap after the navigate step is gone and the wait times out on a page that has
finished loading. Keeping `Page` enabled across the run does not help: an enabled
domain delivers events, it does not replay them. `wait-idle` reads the page's
current state rather than waiting for an edge, so it cannot miss one.

**A step behaves the same inside a Step Run as it does as its own invocation, with
two exceptions.** The first is frame selection, which a run deliberately carries
forward. The second follows from rule 1 and is narrower but real: **a step that
reads from the `Page` or `Runtime` domain does not get a first enable.** Those two
stay enabled for the run, so a step re-enabling either gets a no-op.

That is not hypothetical. `console-list` subscribes to `Runtime.consoleAPICalled`
(`list_verbs.py:57`) and enables the domain its event names imply
(`list_verbs.py:100-108`), so `Runtime` is exactly a domain a step enables. The
practical damage is small, because `consoleAPICalled` is delivered live rather
than replayed, so a second `console-list` still collects what fires during its own
window. The visible case is a step that depends on replay: `wait --event
Runtime.executionContextCreated` after any `Runtime`-enabling step sees nothing,
where the same command alone sees the contexts that already exist.

The exemption is kept anyway, because the alternative is worse: disabling `Page`
or `Runtime` between steps breaks the frame manager, and frame-selection
continuity is a property this RFC specifies. The cost is this paragraph, and the
Manual entry carries it too.

The alternative considered and rejected was to let domain enables persist and
document the difference. It is cheaper to implement and it makes two documented
verbs mean something different inside a run than outside it, which is exactly the
kind of surprise The Manual exists to prevent.

#### Against RFC-01's attach isolation

RFC-01:194 requires `attach` subscriptions to be isolated per session, so two
observers never see each other's subscription set. A Step Run multiplexes many
steps over one session, which moves the isolation unit from the session to the
step. Part 3 above is what preserves the guarantee: because each step removes its
own handlers before the next begins, no two steps' subscriptions are ever live at
once, and sequential execution means they cannot race. `attach` itself is not a
step, so the verb the rule was written for is untouched.

### Frame selection across steps

**Frame selection persists between the steps of one Step Run.** Problem statement,
item 2, demotes this from a motivation: it has one consumer today and `--key`
already serves it. The property is specified anyway, because a run is one
CDPRuntime and the continuity follows whether or not anything needs it yet. What
follows is what a caller can rely on.

- `frames select PATTERN` in step N governs every frame-scoped step after it, until
  `frames reset`, another `frames select`, or the selected frame detaches. What a
  navigation does to it is the third bullet below, and it is not simply a clear.
- `storage get` with no `--key` MUST succeed after a `frames select` step in the
  same run. The `--key` workaround at `GUIDE.txt:128-131` becomes unnecessary
  inside a run. `--key` keeps working and keeps meaning a frame URL pattern.
- **`--key` MUST NOT change the run's selection** (version 6). The key names the
  frame for that one read. The read selects, reads, and puts the previous pattern
  back, so the sentence two bullets down stays true: only `frames reset` and a new
  `frames select` change the pattern. Outside a run the difference is invisible,
  because the selection dies with the process; inside one, a read that kept what
  it borrowed would silently re-point every later frame-scoped step.
- **Across a navigation the selection follows its pattern when the pattern still
  matches.** `frames select` stores the URL pattern as well as the frame id
  (`frame_manager.py:89-90`). On a frame-navigated event the manager re-resolves
  the pattern and re-points the selection at whatever frame now matches
  (`frame_manager.py:349-353`). So a Step List that selects `checkout`, navigates,
  and then reads storage gets the new `checkout` frame.
- **When the pattern no longer matches, today's code keeps the stale frame id, and
  a Step Run MUST NOT.** `frame_manager.py:349-353` assigns `_selected_frame_id`
  only inside `if resolved:`; there is no `else`, so a failed re-resolution leaves
  the previous id in place. Across one invocation that is nearly unreachable. Across
  a run it means a later step reads a frame whose URL no longer matches the pattern
  the caller selected, and reports success. **The frame manager MUST clear the
  selected frame id when re-resolution after a navigation finds no match**, so the
  next frame-scoped step fails with "No frame selected" instead of reading the
  wrong frame. The direct path already behaves this way:
  `select_frame_by_url` clears the id on a no-match (`frame_manager.py:207-208`).
  It is the navigation path alone that does not.

  **The fix belongs in the frame manager, not in the run.** Re-resolution happens
  inside `handle_frame_navigated` (`frame_manager.py:349-353`), which has no
  concept of a Step Run and should not acquire one. So this is the single
  behavior change this RFC makes outside a run, and the "nothing changes outside"
  claim below is worded to admit it. It is a correctness fix that is right in
  either context: a stale id after a failed re-resolution is wrong in a bare
  invocation too, merely almost unreachable there because the process ends first.

  **The URL pattern is kept, not dropped.** After the clear,
  `_selected_url_pattern` stays set, matching what a detach already does
  (`frame_manager.py:313-314`) rather than what `reset_frame` does
  (`frame_manager.py:212-215`). The caller selected by pattern, so a later
  navigation that brings a matching frame back re-points the selection at it. Only
  `frames reset` and a new `frames select` clear the pattern.
- **A detach clears the frame id but keeps the pattern.**
  `frame_manager.py:313-314` sets `_selected_frame_id` to `None` and leaves
  `_selected_url_pattern` set. `get_selected_frame` returns `None` as soon as the id
  is `None`, without consulting the pattern (`frame_manager.py:223-224`); the
  pattern fallback at `:229-230` is reached only when the id is set but stale. So a
  frame-scoped step straight after a detach fails with "No frame selected", and a
  later navigated event can re-point the selection through the retained pattern. The
  RFC states this rather than smoothing it over: a caller reading the sequence needs
  to know the selection can come back.
- **One behavior changes, and it changes everywhere: the no-match clear.**
  Everything else above is the frame manager's behavior today, reachable across
  steps because a run is one CDPRuntime.

Outside a Step Run one thing changes and one does not. The no-match clear above
applies to every invocation, because it lives in the frame manager. Everything
else is untouched: a `frames select` in a bare invocation still dies with that
process, and The Manual still documents `--key` as the way to read a frame's
storage in a single command.

### UID validity across steps

**One invocation changes the UID rule in no way.** The rule is a property of the
document, not of the process:

> A UID is valid until the page navigates. After a navigation, take a new snapshot.
> Nothing else invalidates it.

So inside a Step Run:

- A UID minted by a snapshot in step 2 still resolves at step 9, provided no step
  between them navigated.
- A step that navigates invalidates every UID minted before it. The next step that
  uses one MUST fail with exit 1 and the existing "take a new snapshot" diagnostic,
  exactly as it does across two invocations.
- Clicking, filling, scrolling and DOM changes invalidate nothing, inside a run as
  outside it.

This section exists because "one process" invites the assumption that UIDs are more
durable inside a run. They are not. The document token is the browser's, not the
process's. Being in one process makes a UID neither longer-lived nor shorter-lived.

Note that a Step Run cannot use a UID a step in the same run discovered, because
there is no data flow between steps. Every UID in a Step List was written there by
the caller, from a snapshot the caller already read.

### Validation, before any step runs

**A Step Run MUST validate every step before it executes the first one.** If any
step is malformed, the run MUST exit 2, print nothing on stdout, and execute
nothing.

This preserves the exit-2 contract exactly as The Manual states it: "the invocation
was malformed or refused before anything happened. Nothing was sent, written or
deleted." A run that discovered a typo at step 8 after clicking through steps 1 to 7
would break that contract, and a caller would have no way to know it had been
broken.

Validation covers: the line parses as argv; the verb is on the step surface; the
verb's own flags parse; no step carries `[INSTANCE]`, `--endpoint`, `--target`, or
`--url`; the Step List is not empty. Validation does not cover anything that needs
the browser. A UID that no longer resolves, a frame pattern that matches nothing,
and a CDP method the browser does not support are runtime failures, not usage
errors, and they are found at their own step.

**The step parser MUST be the existing parser, not a second one that matches it.**
A Step Run parses a step by handing its argv to the same `argparse` parsers
`build_parser` already builds, and by reusing the same passthrough head test. It
MUST NOT reimplement flag parsing. The reason is drift: a second parser that
disagrees with the first about one flag turns a usage error into a runtime failure
or the reverse, and the disagreement would surface as a step that behaves
differently inside a run than outside it. This repository already carries one live
example of two lists disagreeing about one verb; see Design, "The step surface",
on `window-border`. A reused parser cannot drift from itself.

**Reusing argparse is not sufficient on its own, and the requirement extends past
it.** Required per-verb inputs are deliberately left optional at the argparse layer
and validated afterwards in `_run`: `--uid` for `click` and `fill`
(`cli.py:607-608`, `:621-624`), `--text` for `fill` (`:623-624`), the `storage`
sub-action (`:678-679`), `--dir` and the removed sub-action for `screencast`
(`:704-717`), and the `--target`/`--url` exclusion (`:505-506`, `:520-521`,
`:536-537`, `:550-551`). `cli.py:246-249` states that split is on purpose, so the
parser accepts a bare verb. A step `click` with no `--uid` therefore parses
cleanly and fails only later.

So validation MUST run **both** layers for every step: the reused `argparse`
parser, and the same per-verb precondition checks `_run` applies. Those checks MUST
be reached through one shared function that `_run` also calls, never copied, for
the same drift reason.

**Two mechanics the implementer has to handle, named here rather than left
implicit.** `argparse` reports a parse failure by writing to stderr and raising
`SystemExit`, which would end the run rather than report a step. Validation MUST
intercept both: capture the parser's stderr and convert `SystemExit` into the
step's usage error, so the diagnostic names the step's line number instead of
appearing as a bare parser message about a program the caller did not invoke.

**Instance detection reads the registry once, before step 1.** A bare leading token
is an instance name only when the registry knows it (`GUIDE.txt:21-22`,
`cli.py:848-854`), so checking that no step carries `[INSTANCE]` needs a registry
read. **That read happens once, during validation, and the whole run uses its
result.** The registry is mutable and another process can change it mid-run; a run
that re-read it per step could classify the same token two ways in one invocation.

### Failure semantics

A step fails when it would have exited 1 as a bare invocation.

- **The run stops at the first failing step.** Steps after it MUST NOT run.
  Continuing would drive the browser through steps written for a state the flow
  never reached.
- **Nothing rolls back.** Browser actions are not transactional. A step that has
  run has already reached the browser, and a Step Run MUST NOT attempt to undo an
  earlier step. The Run
  Document says exactly which steps completed, so the caller knows the state it is
  in and can decide what to do about it.
- **Exit 1**, the same code the failing step would have returned alone.
- **The diagnostic goes to stderr**, naming the step index, the step as written, and
  the failure. Diagnostics from completed steps go to stderr as they are produced,
  unchanged.
- **The Run Document goes to stdout**, carrying every completed step's result and
  the failed step's error. This is the amendment below.

**Refusals are not bypassed by being inside a run.** The focus guard, the
`--endpoint` loopback rule, the `Browser.close` and `Browser.crash` refusals, and
profile exclusivity apply to a step exactly as they apply to a bare invocation. A
Step Run MUST NOT provide a path around any of them.

**They do not all land in the same place, and version 3 wrongly said they did.**
Three classes:

1. **Statically checkable, exit 2, caught at validation.** The refused method
   names, `Target.activateTarget`, `Page.bringToFront`, and
   `Target.createTarget` with `background:false` (`passthrough.py:240-253`). A
   Step List containing one of these is refused before step 1 and the run
   executes nothing.
2. **Needs the live browser, exit 1, caught at its own step.** Input sent to a
   background tab. `passthrough.py:256-264` sends `Runtime.evaluate` and reads
   `document.visibilityState` before deciding, so it cannot be known up front, and
   `GUIDE.txt:226-228` gives it exit 1, not exit 2. It is an ordinary runtime step
   failure and takes the exit-1 row of the Error Handling table.
3. **Unreachable inside a run.** The `Browser.close` and `Browser.crash` refusals
   fire only over `--endpoint` (`passthrough.py:297-298`), and a step cannot carry
   `--endpoint`. Profile exclusivity belongs to `launch`, which is not a step.
   There is nothing to validate in either case.

### Timeouts

`--timeout SECONDS` is OPTIONAL and bounds the whole run. **It defaults to no
whole-run deadline.**

The default is no deadline because every step already bounds itself: `wait` defaults
to 30 seconds, `wait-idle` and `wait-stable` to 5000 ms, `screencast` to its
duration and frame cap. A default whole-run deadline would have to be larger than
any legitimate flow, which makes it useless, or smaller, which makes it kill
legitimate flows. `--timeout 0` means no deadline, matching `wait`.

`--timeout` exists because one step can opt out of its own bound. `wait --timeout 0`
inside a run blocks forever, and unlike a bare `bt wait`, a run gives the caller no
signal about which step it is stuck on. `--timeout` is how a caller bounds a run
whose steps it does not fully control.

On the deadline:

- The run stops. The step in flight is reported with `"status": "timeout"`.
- Steps after it do not run.
- Nothing rolls back, and the in-flight step is not undone. Whatever it had already
  sent to the browser was sent.
- Exit 1, diagnostic on stderr, Run Document on stdout, the same shape as a step
  failure.

The run deadline is wall time measured from the start of the first step. A step's
own timeout flags are honored unchanged; the run deadline is an outer bound, never
a replacement for them.

### The Run Document

A Step Run prints exactly one JSON document on stdout, on success and on failure
alike.

```json
{
  "run": {
    "steps": 10,
    "completed": 6,
    "status": "failed"
  },
  "steps": [
    {"index": 1, "step": "frames select checkout", "status": "ok",
     "result": {"selected": "Selected frame: B5C33E7A...\nURL: ..."}},
    {"index": 2, "step": "storage get", "status": "ok",
     "result": {"storage": "Cookies (0):"}},
    {"index": 7, "step": "click --uid B176A5CE9530-12", "status": "failed",
     "error": "UID 'B176A5CE9530-12' was minted against a previous document. Take a new snapshot."}
  ]
}
```

- `run.steps` is the number of steps in the Step List. `run.completed` is the number
  that succeeded.
- `run.status` is `"ok"`, `"failed"`, or `"timeout"`.
- The `steps` array holds one entry per step **attempted**, in order. Steps never
  reached MUST NOT appear. On a failure or a timeout the last entry is the step that
  failed or timed out.
- A successful step's `result` is the JSON document that step would have printed
  alone, unchanged. This is what makes a Step Run composable with what a caller
  already parses.
- A failed step carries `error` and no `result`.
- `run.status` MUST NOT be `"ok"` unless every step succeeded.
- **The caller contract is ordered, and The Manual MUST state the order: read the
  exit code first, then `run.status`, then the step entries.** A failed run's
  document does contain per-step `"status": "ok"` entries, and each successful
  step's `result` is byte-identical to what that step prints alone. A caller that
  parses stdout without looking at the exit code would read a partial run as a
  whole one. That is the hazard the amendment below takes on deliberately, and the
  ordering is what contains it.
- The document is deterministic. It carries no timing field; a caller that wants the
  wall time measures the invocation.

### Two amendments

Version 2 claimed this RFC amended The Manual only and left RFC-01 untouched. That
was wrong on the second rule below. Both amendments are stated here, and both are
narrow.

#### Amendment 1, to The Manual: stdout on exit 1

**Where the rule lives.** The general rule that exit 1 prints nothing on stdout is
in The Manual, at `GUIDE.txt:342-344`. RFC-01 does not state it generally. RFC-01
states it for `wait` alone, in the `wait` design bullet at `:193` ("On deadline: a
timeout error on stderr, exit 1, no partial output on stdout"), and restates it for
the `stop --target` defect at `:411`. Neither is affected by this RFC.

The rule exists so a caller can never parse a partial result as a success, and it
is right for every verb that does one thing.

**This RFC amends it for `run` alone.** A Step Run that fails at step 7 of 10 has
already changed the page six times. Suppressing stdout would leave the caller
unable to see the state of a browser the run has just mutated, and that is the
larger risk here. The partial-result risk is handled by the document instead:
`run.status` is never `"ok"` for a failed run, and the exit code is still 1.

The amendment is narrow and MUST be written as a narrow one:

> `run` is an exception to the rule that exit 1 prints nothing on stdout. On exit 1
> it prints its Run Document, which reports `"status": "failed"` or `"timeout"` and
> names the step that ended the run. Every other verb is unchanged.

This is the same kind of stated exception The Manual already carries for the
one-JSON-document rule, where `guide` and `help` print plain text and `attach`
prints JSON Lines. It MUST appear in `GUIDE.txt` under OUTPUT AND EXIT CODES when
this RFC is built. The entry in Design, "The Manual entry (normative)", carries the
per-verb half of it; the OUTPUT AND EXIT CODES block needs the exception named
there too, so a caller reading only that block is not misled.

Exit 2 is **not** amended. A Step Run that exits 2 has executed nothing and prints
nothing on stdout, which is why validation runs before the first step.

#### Amendment 2, to RFC-01: what "operation" means inside a run

RFC-01, "Refusals and exit codes", at `:204`: "A failed operation MUST NOT exit 0,
and MUST NOT report a success field."

A Step Run that fails at step 7 exits 1, so the first half holds. The second half
does not hold on a plain reading. The Run Document of a failed run carries
`"status": "ok"` on each of steps 1 through 6. Those are success fields, printed on
the stdout of an invocation that failed. The rule was written when one invocation
was one operation, and a run breaks that assumption rather than satisfying it.

**So RFC-01 is amended, narrowly:**

> Inside a Step Run, the unit the success-field rule governs is the **step**, not
> the invocation. A step that succeeded MUST report that it succeeded. The
> invocation reports its own outcome in `run.status` and in its exit code, and
> `run.status` MUST NOT be `"ok"` unless every step succeeded. Outside a Step Run
> the rule is unchanged: one invocation is one operation.

This is the honest version of what the design already does. The alternative was to
strip `"status": "ok"` from a failed run's step entries, which would satisfy the
letter of `:204` and destroy the information the document exists to carry: which
steps ran, and what they returned. The amendment is preferred because the rule's
purpose, stopping a caller reading failure as success, is served by the exit code
and by `run.status`, both of which still report the failure.

This amendment MUST be written into RFC-01's "Refusals and exit codes" section when
this RFC is built.

### Packaging

`run` adds no dependency and goes behind no extra. The default install continues to
depend on `websockets` only, per RFC-01, "Packaging", and `extras.py`. A Step List
is parsed with `shlex` and reported with `json`, both stdlib.

This holds under any of the three execution models in "The execution model": none
of them needs a package. The packaging discipline is therefore not an argument for
or against the restricted API, and this RFC does not use it as one.

### The Manual entry (normative)

`tests/test_guide.py` enumerates verbs from the parser and fails the build when one
has no entry, so `run` cannot ship without this. The following is the normative
text. It belongs in `GUIDE.txt` after the CURATED VERBS block, as its own section.

```
RUNNING MANY STEPS IN ONE INVOCATION

  run FILE [--timeout SECONDS]
  run -    [--timeout SECONDS]
      Run an ordered list of steps against one browser, in one invocation, over
      one CDP connection. FILE is the list; `-` reads it from stdin. Takes
      [INSTANCE] and --endpoint URL as above, and --target SPEC or --url
      SUBSTRING to pick the page the whole run drives.

      One step per line, each line a verb phrase exactly as you would type it
      after the instance name. Blank lines and lines starting with # are
      ignored. Lines are split the way a shell splits them, so quoting works as
      it does at the prompt.

          # select the checkout frame once, then read it twice
          frames select checkout
          storage get
          Page.getNavigationHistory '{}'
          snapshot

      This is not a scripting language. There are no variables, no conditions,
      no loops, and no way for one step to use another step's output. A run does
      what the same commands would do one after another; it cannot decide
      anything. Where you need a decision, read the output and run again.

      A STEP IS A VERB, NOT AN INVOCATION. Steps may be: snapshot, click, fill,
      wait-idle, wait-stable, wait, detect, console-list, network-list, frames,
      storage get, screenshot, screencast, and any raw Domain.method. A step may
      NOT name an instance, --endpoint, --target or --url: those belong to the
      run, which resolves them once. attach is not a step (it runs until stdin
      ends, so nothing could follow it), and neither are launch, status, stop,
      cleanup, profile, window-border, guide or help. Any of these in a step
      list is a usage error (exit 2).

      FRAME SELECTION LASTS. `frames select` in one step governs the steps after
      it, so `storage get` with no --key works inside a run. A selection
      remembers the pattern you gave it, so after a navigation it re-points at
      whatever frame now matches. If nothing matches any more, the selection is
      cleared and the next frame-scoped step fails rather than reading the wrong
      frame. The selected frame going away clears it too, but the pattern is
      kept either way, so a later navigation that brings a matching frame back
      selects it again. Only `frames reset` and a new `frames select` change the
      pattern.

      EVERYTHING ELSE BEHAVES AS IT WOULD ALONE, WITH ONE EXCEPTION. A step
      inside a run does what the same command does on its own. Domains a step
      turns on are turned off when that step ends, so a second `network-list`
      in one run sees what the first one saw.

      The exception is the Page and Runtime domains. A run needs them on
      throughout to track frames, so they are never turned off between steps.
      A step that relies on being the first to turn one of them on does not
      get that. `wait --event Runtime.executionContextCreated` after any step
      that touched Runtime reports nothing, where the same command on its own
      reports the contexts that already exist; `console-list` is in the same
      position, because it turns Runtime on too. Run either one on its own.

      A raw step you write yourself is yours to undo. `raw Network.enable {}`
      stays on until a later step turns it off; the rule above covers only the
      domains a verb turns on for you.

      UIDS ARE UNCHANGED. A UID is still valid until the page navigates, no
      longer and no shorter. One process does not extend it. Because no step can
      read another step's output, every UID in a step list is one you put there
      from a snapshot you already read.

      IT STOPS AT THE FIRST FAILURE, and nothing rolls back. A step that has run
      has already reached the browser. Steps after the failure do not run.

      --timeout SECONDS bounds the whole run; without it there is no whole-run
      deadline, because every step already bounds itself. Use it when a step
      opts out of its own bound, as `wait --timeout 0` does.

      OUTPUT. One JSON document, on success and on failure alike: a `run` object
      with the step count, how many completed, and the status; and a `steps`
      array with one entry per step attempted, each carrying the JSON that step
      would have printed alone. Steps never reached are absent.

      READ IT IN THIS ORDER: the exit code, then `run.status`, then the steps.
      A failed run still contains successful step entries, and each of those
      carries exactly what that step prints on its own. If you parse the steps
      without checking the exit code first, a run that died at step 7 reads
      like a run that finished.

      Exit 0   every step succeeded.
      Exit 1   a step failed, or the run timed out. The document is still
               printed, so you can see what already ran. This is the one verb
               that prints on stdout when it exits 1.
      Exit 2   the step list was malformed, or a step named an instance,
               --endpoint, --target or --url. The whole list is checked before
               the first step runs, so nothing ran.
```

The OUTPUT AND EXIT CODES block at `GUIDE.txt:333-346` also changes, so a caller
reading only that block learns about the exception. Its Exit 1 line becomes:

```
  Exit 1   operational failure: the browser is gone, a CDP call failed, a
           deadline passed, a profile is held, a UID no longer resolves.
           Nothing is printed on stdout, except by `run`, which prints its
           run document so you can see which steps already ran.
```

## Error Handling

Every failure this verb can produce, and where it lands.

| Failure | Exit | stdout | stderr |
|---|---|---|---|
| Every step succeeded | 0 | Run Document, `status: "ok"` | step diagnostics, if any |
| Neither `FILE` nor `-` given, or both | 2 | nothing | usage |
| `FILE` does not exist or cannot be read | 2 | nothing | the path and the reason |
| Step List empty after comments and blanks | 2 | nothing | usage |
| A line does not parse as argv | 2 | nothing | the line number and the parse error |
| A step uses a verb off the step surface | 2 | nothing | the line number, the verb, and the step surface |
| A step carries `[INSTANCE]`, `--endpoint`, `--target` or `--url` | 2 | nothing | the line number and which flag belongs to the run |
| A step's own flags do not parse | 2 | nothing | the line number and the verb's usage |
| Instance named twice | 2 | nothing | both names |
| Several instances registered, none named | 1 | nothing | the candidate list |
| The browser is gone, or the connection fails | 1 | nothing | the existing connection diagnostic |
| A step fails at runtime | 1 | Run Document, `status: "failed"` | the step index, the step, the failure |
| The run deadline passes | 1 | Run Document, `status: "timeout"` | the step index and the deadline |

Two rules hold across the table. Every exit-2 row prints nothing on stdout and
executes nothing, because validation runs before the first step. Every exit-1 row
that reached a step prints the Run Document, under the amendment above; the exit-1
rows that never reached a step print nothing, exactly as every other verb does.

## Security Considerations

**The execution model is the whole of it.** Under the restricted API specified here,
a Step List is data. It can do what a sequence of `bt` invocations can do, and
nothing else. Reading an untrusted Step List is no more dangerous than accepting
untrusted `bt` arguments, which is already this tool's threat model.

Under either arbitrary-Python model, that changes completely. A Step List becomes
executable code running with the agent's identity, and any path that can write
`bt`'s stdin or a file `bt` reads becomes a local code-execution path. `bt run -`
makes this concrete: a pipeline that feeds `bt` from a network fetch would be
piping remote input into an interpreter. This is stated here so the trade-off in
Decisions, item 1, was made with its consequence visible.

**Existing refusals are not weakened.** The focus guard, the `--endpoint` loopback
rule, the `Browser.close` and `Browser.crash` refusals, and profile exclusivity
apply to every step. A Step Run MUST NOT provide a path around any of them, and
because each is a usage error, a Step List containing one is refused before the run
begins.

**Nothing new is written.** A Step Run writes what its steps write: `screenshot
--path`, `screencast --dir`. It creates no state of its own, and per `CONTEXT.md`
nothing outlives the invocation.

## Alternatives Considered

**A general interpreter with a `bt` object.** The most capable option, and the one a
caller would ask for first. Declined in Design, "The execution model": it duplicates
an interpreter the calling agent already has, and it turns a browser-driving tool
into a local code-execution surface. Declined by Kevin on 2026-09-21; see
Decisions, item 1.

**A binding mechanism between steps,** so step 5 could use a UID step 3 discovered,
with some restricted path syntax. Declined. A selector syntax over step results is a
query language, and a query language grows. The case it would serve, snapshot then
click, already works across two invocations because the version 6 UID rule makes a
UID valid until the page navigates. Add a mechanism when a case appears that the
two-invocation form genuinely cannot serve, and add it for that case.

**JSON or JSON Lines as the Step List format.** Easier for an agent to emit without
quoting mistakes. Declined: it is a second grammar, needing its own schema, its own
documentation in The Manual, and its own drift risk against the CLI parser. Verb
phrases have none of those costs, because The Manual documents them already and
`tests/test_guide.py` keeps the surface honest. This trade-off held only while the
interpreter option was live; Decisions, item 1, closed it.

**YAML.** Declined on packaging: it needs a dependency, and the default install
depends on `websockets` only.

**A fail-closed output envelope**, where a failed run's document is shaped so an
old caller parsing it cannot mistake it for a whole run, rather than relying on
the caller reading the exit code first. Not chosen. The ordered caller contract in
"The Run Document" describes the hazard and does not mechanically prevent it: a
caller that reads the `steps` array and ignores both the exit code and
`run.status` still reads a partial run as a complete one. The trade accepted here
is that the document stays directly composable, because each step's `result` is
byte-identical to what that step prints alone, which is the property that lets a
caller parse a run at all. An envelope that broke that composability to protect a
caller doing two wrong things at once was judged the worse trade. Recorded because
the option was real and the protection is genuinely weaker without it.

**Keeping the frame selection in the registry** so a bare `frames select` survives
between invocations, with no new verb. Declined. It would make a registry entry
carry live CDP state that the browser can invalidate at any moment, with no process
holding it and no way to learn it has gone stale. The registry tracks processes, not
sessions. It would also not address the per-invocation cost at all.

**A long-lived session daemon.** Declined by RFC-01:43 for the screencast recorder,
and the reasoning is unchanged: `CONTEXT.md` states that nothing outlives the
invocation and no verb pair spans two processes. A Step Run is the shape that gets
multi-step continuity while holding to that property, because the run is one
process and the continuity ends with it.

## Decisions

All five questions this RFC raised were put to Kevin on 2026-09-21 and answered.
Each took the recommendation, so the Design sections above stand as written and
nothing in them changed as a result. None of these is machine-made.

1. **The execution model is the restricted API, with no interpreter.**
   **Decided by Kevin, 2026-09-21.** The alternatives were arbitrary Python
   in-process and arbitrary Python in a subprocess. Neither carries a safety
   property: both run with the full privileges of the `bt` process, which is the
   calling agent's identity, and Python ships no portable sandbox. The reason for
   the restricted API is a fit check rather than a security preference: the caller
   is an agent that already has an interpreter, so a second one inside `bt` adds no
   capability the caller lacks, while it does turn any path that can write `bt`'s
   stdin into a local code-execution path. The one capability it would add, acting
   on what a step discovered, is served today by two invocations and the version 6
   UID rule. Reversibility drove the order: adding an interpreter later is additive,
   and removing one later breaks every Step List written against it. See Design,
   "The execution model", and Security Considerations.

2. **The verb is named `run`.** **Decided by Kevin, 2026-09-21.** The brief called
   it "a script verb", and `script` is the name a caller reaches for first, but
   after decision 1 there is no scripting language behind it. A caller told it has a
   script writes `if` on the first attempt and gets exit 2. `run` promises nothing
   the verb does not deliver. `steps` was the third option: accurate about the
   file's content, awkward as a verb. See Design, "The verb".

3. **`--timeout` has no default. There is no whole-run deadline unless the caller
   sets one.** **Decided by Kevin, 2026-09-21.** Every step already bounds itself,
   so a whole-run default is either larger than every legitimate flow, which makes
   it useless, or smaller, which kills legitimate flows. Two defaults were offered
   and rejected. A 300-second default fails on a long screencast plus several waits,
   which can legitimately approach it. A 60-second default fails on a concrete case:
   a single `screencast --duration 60` exhausts it, so callers would pass
   `--timeout 0` reflexively and the protection would be gone. `--timeout` remains available for the real gap, which is a step
   that opts out of its own bound, as `wait --timeout 0` does. This is a default and
   not a contract, so it can change later without breaking a Step List. See Design,
   "Timeouts".

4. **The Run Document ships in the shape specified, and it is provisional.**
   **Decided by Kevin, 2026-09-21.** One JSON document: a `run` object carrying the
   step count, the completed count and the status, plus a `steps` array holding one
   entry per step attempted. Steps never reached stay absent from the array, because
   `run.steps` and `run.completed` already say how many were not reached. Two
   alternatives were rejected: emitting `"status": "not-reached"` entries, which pads
   the document with entries carrying no new information; and JSON Lines with one
   line per step, which would break the one-JSON-document rule for a second verb and
   leave the run-level summary nowhere natural to go. **Provisional means one thing
   concretely: no test pins a field this RFC has not justified.** The shape was
   derived from the failure cases rather than from use, so the first real flows may
   move it. See Design, "The Run Document".

5. **The verb is kept, and the motivation is speed.** **Decided by Kevin,
   2026-09-21**, after the adversarial review. The finding that forced the question:
   frame-selection continuity, which versions 1 and 2 called the stronger
   motivation, has exactly one consumer in the product, and `--key` already serves
   it. Three alternatives were rejected. Withdrawing the RFC, on the RFC-02
   precedent, was rejected because the measured saving is real and independent of
   the frame claim. Narrowing to flag extensions on the frame-scoped reads was
   rejected because there is only one such read and it already has its flag, so the
   alternative fixes nothing. Pausing for a fresh fit check was rejected because the
   corrected evidence is already in hand. The decision carries one condition, now
   normative in Design, "Validation, before any step runs": the step parser reuses
   the existing `argparse` parsers rather than mirroring them, which removes the
   drift hazard that was the strongest maintainability argument against the verb.

## Open Questions

None. The five questions this RFC raised are recorded in Decisions above.

## Changes in this revision

**Version 6** (2026-09-21) records what the build found. The verb is implemented
and merged; these are corrections to the specification, each one already in
`GUIDE.txt` and in the suite. All four came from the second adversarial review of
the implementation, in `.scratch/rfc-03-phase4/review-report.md`.

1. **The caller's enable wins** (Design, rule 2). Rule 1 and rule 3 both described
   a raw `Network.enable` step followed by a `network-list` step, and the
   implementation resolved the collision by disabling the caller's domain. The
   precedence is now stated rather than left to whichever rule the code read
   first.
2. **`--key` borrows the selection** (Design, frame selection). `storage get
   --key K` called `select_frame`, which changed the persistent pattern. In a
   one-shot invocation that is invisible. In a run it re-pointed every later
   frame-scoped step, contradicting the bullet that says only `frames reset` and
   `frames select` change the pattern.
3. **The navigation wait is `wait-idle`** (Design). The manual's post-navigation
   advice offered `wait --event Page.loadEventFired` as an equal. It is not: the
   wait subscribes when its own step starts, so a load delivered in the gap is
   already gone and the wait fails a run whose page had finished loading.
4. **`#` starts a comment only at the start of a line.** `CONTEXT.md` said "`#`
   starts a comment", which reads as trailing comments too. The parser drops
   whole comment lines only, and it is right to: `shlex`'s comment mode also
   truncates `--text abc#def`, which no shell does.

Further corrections changed code and not this specification. The initial
execution-context replay was lost because `Runtime.enable` preceded its handler
registration. `Page.navigatedWithinDocument` was unhandled, so a single-page app
kept the URL it first loaded. `Runtime.executionContextsCleared` was unhandled,
so every frame kept a destroyed context id.

The rest are all one defect wearing five faces: `_frames` and the `children`
lists are two representations of one tree, and nothing held them together. A
frame could be in the map and not in the tree, and `_resolve_frame_by_url`
walked the map while `frames list` walked the tree, so `storage get` would read
a frame the caller had been told did not exist. The rule is now stated and
tested as one invariant - **a frame the map holds is reachable from the root,
exactly once, and a selection resolves in the order `frames list` prints** -
and five paths that broke it are fixed: a parent detach stranded its children, a
re-attach stranded the old frame's descendants, a re-attach under a new parent
left the old one still holding the frame, a frame whose `frameNavigated`
preceded its `frameAttached` was never linked in, and an attach that would make
a frame its own ancestor built an island. The invariant is asserted after every
sequence in `TestTheTwoViewsOfTheFrameTreeAgree`, and an exhaustive search over
every plausible three-, four- and five-event sequence finds no violation.

Each of these is a pre-existing defect that the landed contract turned from
invisible into observable: before a run, the frame manager died with the
process a moment later.

**Version 5** (2026-09-21) is the acceptance. Kevin accepted the RFC as it
stood at version 4. Two things follow.

The status is `Accepted`. No normative text changed with it; version 4 is what
was accepted, and the specification is what gets built.

`N-J1` is settled by the same decision. The version 3 reviewer noted that the
ordered caller contract describes the partial-run hazard without containing it,
and that a fail-closed envelope was available and not chosen. Version 4
recorded the rejected alternative in Alternatives Considered rather than
deciding it, because the trade was Kevin's. Accepting version 4 accepts the
composable envelope: a step's `result` stays byte-identical to what that step
prints alone, and a caller reads the exit code and `run.status` before the step
entries. The rejected alternative stays recorded, so a later reader can see the
trade rather than assume nobody weighed it.

**Step Run** and **Step List** are still not in `CONTEXT.md`. Abstract requires
them when the RFC is built, not when it is accepted, and the glossary should
not name domain nouns that no code yet uses. They land with the first
implementation phase.


**Version 4** (2026-09-21) is the revision after a second adversarial review by
`muse-spark-1.3-contributor@muse`, again cross-family, against the version 3
snapshot at commit `3662093`. Its verdict and every finding are in
`docs/rfc/03_run-many-steps-in-one-invocation.review-02.md`. The verdict was
"accept with changes": four blocking findings, four major, seven minor, all
verified against source before being applied. What changed:

1. **Flow B is retaken at n=20** (N-J3). Five runs over a 42% spread could not
   carry a 90% headline. Retaken at 20 runs a side: **870.1 ms** across ten
   invocations against **83.0 ms** over one session, still 90%, and 90% again when
   the fastest ten-invocation run is paired against the slowest one-session run.
   The retake also exposed a latent defect in the harness, which held a literal
   port that had been correct by luck until the launcher handed the fresh instance
   a different one. The first n=20 attempt drove another browser and was
   discarded. The harness now resolves its port from the registry by instance
   name, refuses to run when the name is absent or the instance is not alive, and
   re-checks the port after the run. The measurements file records the discarded
   attempt and what it does and does not void: the version 3 n=5 figures measured
   the right browser and are superseded, not wrong.
2. **The flow B coverage claim is narrowed** (N-J2). Its ten steps are eight raw
   `Domain.method` calls and two `screenshot`s. It covers two One-Shot Session
   verbs, not five. `wait`, `console-list` and `network-list` are dominated by
   their own windows, which one invocation does not remove, and Motivation now
   says so rather than implying the saving extends to them.
3. **The floor-side asymmetry is disclosed** (N-J2, second half). The one-session harness issues
   bare `cdp.send` calls, skipping the screenshot blank-frame retry
   (`curated.py:534-546`), the file write, and JSON rendering. The true floor for
   a built verb sits above 83.0 ms, and Motivation states this in the direction
   that favours a critic.
4. **The References parenthetical no longer contradicts Design** (N-B1). It said
   RFC-01 was not amended; Design amends it twice. The parenthetical now names
   both amendments.
5. **Refusals are split into three classes** (N-B2). Version 3 treated every
   refusal as a statically-checkable usage error and exited 2 before connecting.
   One class is not static: the hidden-tab refusal needs a live browser read
   (`passthrough.py:256-264`, `GUIDE.txt:226-228`), so it cannot be pre-checked
   and exits 1 as an operational failure at the step that hits it. A third class,
   verbs unreachable inside a run at all, is named separately.
6. **The frame no-match clear moves into the frame manager** (N-B3, N-m4). A run
   is not the right owner: the stale-id hazard is the frame manager's, and a
   caller-side clear leaves every other caller exposed. The URL pattern is kept
   on a no-match, matching what detach already does, so a later navigation that
   brings a matching frame back re-selects it.
7. **Parser reuse extends to the semantic checks** (N-B4). Reusing the `argparse`
   parsers is not enough: `_run` carries checks `argparse` cannot express
   (`cli.py:607-608`, `:621-624`, `:678-679`, `:704-717`, `:505-506`), and a step
   validator that skips them accepts steps the same command rejects on its own.
   The section also states the mechanic, since the parsers raise `SystemExit` and
   write to stderr, which a run MUST intercept rather than let terminate it.
8. **The `Runtime` exemption reopens the replay hole for `console-list`** (N-J4).
   `console-list` subscribes to `Runtime.consoleAPICalled` (`list_verbs.py:57`),
   so leaving `Runtime` enabled across steps costs it the same replay that
   `wait --event` loses. Both are now named in the same place, with the same
   remedy: run that step on its own.
9. **The rejected fail-closed envelope is recorded** (N-J1) in Alternatives
   Considered, rather than being absent from a document that specifies the
   envelope it beat.
10. **An explicit enable and a failing disable are specified** (N-m5, N-m6). The
    per-step disable rule governs the enables a verb issues for the caller, not an
    enable the caller typed as a passthrough step, and a disable that fails is
    suppressed rather than failing the step, matching what the enable path already
    does (`events.py:347-350`, `list_verbs.py:106-108`).
11. Minor: the question count reads "five", not "four" (N-m1). The normative
    `GUIDE.txt` entry now carries the Page and Runtime exemption, which Design
    requires and the entry omitted (N-m2), and the resurrection case for a
    selection whose frame goes away, which the entry flattened to "goes away"
    (N-m3). The frame-selection call-site list adds the two further reads it
    omitted, neither of which changes the one-consumer conclusion (N-m7).

**Version 3** (2026-09-21) is the revision after an adversarial review by
`muse-spark-1.3-contributor@muse`, a cross-family reviewer chosen with the Claude
family excluded because a Claude model drafted this RFC. The review returned
"accept with changes" with two blocking findings. Its verdict and every finding are
in `docs/rfc/03_run-many-steps-in-one-invocation.review-01.md`. What changed:

1. **The lead motivation is withdrawn and replaced.** Frame-selection continuity
   has one consumer, `get_frame_storage`, and `--key` already serves it
   (`cdp_handler.py:816`, `:856`, `:863`). Versions 1 and 2 called it the stronger
   motivation. Problem statement and Motivation now lead on the measured saving,
   and the frame property is kept without being leaned on. Decisions, item 5.
2. **Session and domain state across steps are specified** (blocking finding B1).
   Design, "One CDPRuntime for the run", now fixes the connection model as the
   One-Shot Session's browser-level attach, because the handler's page-level
   connection carries no `sessionId` and would break event delivery for `wait`,
   `console-list` and `network-list` (`cdp_client.py:158-161`, `events.py:344`). It
   adds the domain enable and disable rule, backed by a measurement, and the
   per-step handler hygiene rule.
3. **RFC-01 is amended after all** (blocking finding B2). A failed Run Document
   carries per-step `"status": "ok"` entries, which are success fields inside a
   failed invocation. Version 2's claim that RFC-01 needed no amendment was wrong.
   Design now carries two amendments, and the Run Document section states the
   ordered caller contract.
4. **The frame no-match case is specified** (M1). `frame_manager.py:349-353`
   assigns only on a match, so a failed re-resolution keeps a stale frame id. A run
   MUST clear it. Detach keeping the pattern is stated too.
5. **The evidence gap in the saving is closed by measurement** (M2). Version 2's
   harnesses exercised only handler-routed verbs. A second flow was measured
   over the One-Shot Session transport: 787.8 ms across ten invocations against
   82.7 ms over one session, a 90% saving. Motivation now carries both flows and
   says why the percentages differ. **Version 4 supersedes those two figures at
   n=20** and narrows the coverage claim, which was too broad.
6. **The step parser MUST reuse the existing `argparse` parsers** (M3), which is
   the condition attached to decision 5. **Instance detection reads the registry
   once, before step 1** (M4).
7. Minor: the References mislabel of `frame_manager.py:313-314` as a navigation
   path is corrected, the `window-border` list drift is recorded for the build
   ticket, and the floor probe's one redundant `select_frame` is disclosed.
8. **The evidence moved into the repository.** Versions 1 and 2 cited five
   git-ignored paths under `.scratch/`, which resolve on one machine and nowhere
   else. The measurements and the review are now tracked beside this RFC, following
   the naming RFC-01 and RFC-02 already use, and the harnesses are inlined in the
   measurements file so it reproduces without reaching outside the repository.

**Version 2** (2026-09-21) records Kevin's answers to all four questions version 1
raised. Every answer took the recommendation, so no Design section changed. Open
Questions is replaced by Decisions, which carries the four answers with the
alternatives that were rejected and why. The forward references in Scope, Design
and Alternatives Considered that described these as open now point at Decisions.
No normative text moved.

**Version 1** (2026-09-21) is the first draft. It specifies `run`, the Step List,
the step surface, instance resolution, the one-CDPRuntime requirement, frame
selection and UID validity across steps, validation, failure semantics, timeouts,
the Run Document, the amendment to The Manual's exit-1 stdout rule, packaging, and
the normative `GUIDE.txt` entry. Every figure in Motivation was measured for this
draft; see the measurements file.

Status is **Accepted**, by Kevin on 2026-09-21.

## References

### Normative

- RFC-01, "Merge chrome-agent core into browser-tools", `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md`. Scope boundary at `:36`; the long-lived recorder declined at `:43`; "CLI surface (normative)"; "Refusals and exit codes", **amended by this RFC**: Design, "Two amendments", scopes the `:204` success-field rule per step inside a run; "Packaging"; "The agent manual".
- `CONTEXT.md`. **One-Shot Session**, **CDPRuntime**, **UID**, **Bounded Capture**, **The Manual**. **Step Run** and **Step List** are added by this RFC.
- `src/browser_tools/GUIDE.txt`. The verb surface the step surface is derived from. The UID rule at `:197-209`; output and exit codes at `:333-346`; the `storage get --key` workaround at `:128-131`.
- `src/browser_tools/extras.py`. The packaging discipline and the `MissingExtraError` wording. Neither is invoked by this RFC, which adds no dependency.
- `tests/test_guide.py`. Enumerates verbs from the parser and fails the build when one has no entry. It is why the Manual entry above is normative text and not a suggestion.

### Informative

- `docs/rfc/03_run-many-steps-in-one-invocation.measurements.md`. Every figure in Motivation, the harnesses, the environment, the reproduction of the frame-selection loss, and the stale-`bt` finding.
- `docs/rfc/03_run-many-steps-in-one-invocation.review-01.md`. The adversarial review that produced version 3, by `muse-spark-1.3-contributor@muse`, a cross-family reviewer chosen with the Claude family excluded because a Claude model drafted the RFC. It carries the verdict, every finding, and what version 3 did with each one. Its brief is `.review-brief-01.md` beside it.
- `docs/rfc/03_run-many-steps-in-one-invocation.review-02.md`. The adversarial review that produced version 4, by the same reviewer on the same route, against the version 3 snapshot at commit `3662093`. It carries the verdict, every finding, and what version 4 did with each one. Its brief is `.review-brief-02.md` beside it.
- `src/browser_tools/frame_manager.py:89-90` (selection state), `:207-208` (the direct path clears on a no-match), `:223-224` and `:229-230` (a cleared id short-circuits before the pattern fallback), `:313-314` (the **detach** handler, which clears the id and keeps the pattern), `:349-353` (the **navigation** re-resolution, which assigns only on a match and today clears nothing).
- `src/browser_tools/cdp_handler.py:404-413` (the handler's page-level, sessionless connection), `:816`, `:856`, `:863` (every call site that consumes frame selection: one inside `select_frame`, two inside `get_frame_storage`).
- `src/browser_tools/core/cdp_client.py:158-161`. Event dispatch and the `sessionId` filter that makes the two connection models incompatible.
- `src/browser_tools/curated.py:427-428`. The docstring that already states frame selection does not survive between one-shot CLI processes.
- `src/browser_tools/one_shot.py`. The One-Shot Session seam and the five verbs that open their own.
- RFC-02, "Calibrated interstitial triage", `docs/rfc/02_calibrated-interstitial-triage-and-handoff.rfc.md`. Withdrawn. Cited for its document structure only.
