---
number: 05
title: "Retire chrome-devtools-axi"
type: feature
status: Proposed
author: "Kevin Frilot"
date: 2026-09-22
version: 3
---

# RFC-05: Retire chrome-devtools-axi

## Abstract

Two browser CLIs are installed on the driving dev's machine. `chrome-devtools-axi`
wraps `chrome-devtools-mcp` behind a Node bridge; `bt` is this project. The
global agent instructions name axi the default and send `bt` the work axi
cannot do. Sampled across the Claude Code and Codex transcripts on that
machine, axi accounts for about 23,000 mentions of `chrome-devtools-axi <verb>`,
used as a proxy for invocations, against far fewer for `bt`. The ranking is
what the design rests on, not the absolute number.

This RFC specifies closing the gap and removing axi. It adds six curated
verbs (`eval`, `press`, `hover`, `type`, `wait-text`, `network-get`), two
capture verbs (`trace`, `heap`), one analysis verb (`insights`) behind a new
optional extra, and one flag (`--dialog`). It corrects seven documentation
defects the investigation found, and it lists the machine-level removals that
finish the retirement.

**It amends RFC-03 in two places, normatively.** RFC-03 declined `heap`,
`lighthouse` and the trace-insight verbs by name, and fixed the Step List
surface as a closed set. This RFC reverses the first and extends the second.
Both amendments are stated in their own section rather than left to be
inferred, because two accepted documents that disagree are worse than either
decision.

One structural fact shapes half of it. **A CDP payload that arrives as events
or as a stream handle cannot be reached through raw passthrough or a Step
Run**, because passthrough returns one command's return value and collects no
events, and no step reads another step's output. Tracing, heap snapshots and
response bodies all fall under it. Each is a curated verb or it does not
exist; there is no recipe option and no third choice.

The cost is a second runtime. `bt insights` needs Node, behind an extra that
the base install does not pull. RFC-01 deleted a Node subprocess and the
README advertises its absence. This RFC reopens that door deliberately, for
one optional capability, and says why.

## Introduction

The retirement was charted as a decision map, GitHub issue #147, with twelve
decision tickets. This document is normative: an implementer builds from it,
and the tickets are informative background holding the raw evidence. Where
this RFC and a ticket disagree, this RFC governs and the ticket is stale;
raise it as an erratum rather than following the ticket.

Seven of the twelve tickets were resolved with the driving dev present. Five
were resolved by the agent alone after the driving dev handed over the map,
and those five are marked machine-made in their resolution comments, in the
task's decision ledger, and in the Decisions table below. They are reversible
and each names its undo path.

Every ticket is linked by URL in References. Every runnable probe behind a
"Measured:" figure is committed with this RFC under `docs/research/probes/`,
so a reviewer can re-run the measurement rather than take it.

### What was measured

Verb usage came from the transcripts on the driving dev's machine, counting
mentions of `chrome-devtools-axi <verb>` as a proxy for invocations. Sampling
was a grep across both harnesses' transcript stores, unfiltered for echoes, so
a figure is an upper bound on real use. The ranking, not the absolute numbers,
is what the design rests on. The method is echo-prone in both directions and
is treated that way wherever a decision leans on it.

| Signal | Value |
|---|---|
| axi verb mentions, both harnesses | about 23,000 |
| Most mentioned verbs | `eval`, `snapshot`, `click`, `scroll`, `fill`, `open` |
| Performance verbs | `perf-insight` 8, `lighthouse` 6, all in one project's transcripts |
| No mentions in the sample | `heap`, `perf-start`, `perf-stop`, `drag`, `back` |
| Most mentioned environment variable | `CHROME_DEVTOOLS_AXI_SESSION`, about 3,200 |

## Terminology

**Bounded Capture.** `CONTEXT.md` defines this today as the whole of
`screencast`. This RFC generalises it to the family: one invocation starts the
capture, buffers, writes its output and exits, and nothing outlives the
invocation. `screencast`, `trace` and `heap` are all Bounded Captures.
`CONTEXT.md` MUST be updated to the generalised definition when this ships.

**Curated verb.** A `bt` subcommand that owns a CDP interaction the caller
would otherwise assemble by hand. **Capture verb.** A curated verb that is a
Bounded Capture. **Analysis verb.** A curated verb that reads a file a capture
verb wrote and computes over it, touching no browser except to read its
version. `insights` is the only one. All three MUST land in `CONTEXT.md`.

**Curation rule.** The test the map used to decide whether an axi verb becomes
a `bt` verb or a `bt guide` recipe. A gap becomes a curated verb when its raw
CDP form needs shell-hostile quoting, meaning JavaScript or prose inside JSON
inside a shell line, or when it needs more than one CDP call to do the obvious
thing. Otherwise it is a recipe.

**Payload reachability.** The structural fact in the Abstract. A capability
whose result arrives as CDP events, or as a stream handle that a later call
must redeem, cannot be expressed through `bt`'s raw passthrough or through a
Step List. It is a curated verb or it is nothing.

**Transfer mode.** The CDP `Tracing.start` parameter `transferMode`, whose two
values are `ReportEvents`, which streams the trace back as
`Tracing.dataCollected` events, and `ReturnAsStream`, which returns one
`IO`-domain stream handle in `Tracing.tracingComplete` for a later
`IO.read` to redeem. They are different mechanisms and both are unreachable
from a Step List, for different reasons. See the CDP `Tracing` domain
reference in References.

**Insight Set.** The trace engine's own term, adopted unchanged: the set of
insight models the engine produces for one navigation in a trace. This RFC
uses Insight Set for that set, and "insight" for one member.

**Dialog policy.** The answer `bt` gives to every JavaScript dialog raised
during an invocation, fixed before the invocation starts.

## Motivation

### The gap is not what it looked like

Three of the map's research tickets overturned their own premises, and the
design would be wrong without them.

`bt` was believed to refuse a second launch in one directory, which would have
made parallel agent sessions impossible. It does not: the registry appends a
`-NN` suffix, and this has been normative since RFC-01. The refusal that was
quoted belongs to named profiles, and it is correctly scoped where it is
written. Nothing states the positive case, which is the actual defect. The
real gap is that a caller cannot choose an instance name, and the derived name
is recycled after a stop.

Auto-connect to the user's own Chrome was believed to be reachable through
`--endpoint`. It is not. Chrome 144 and later expose an approval-based remote
debugging service that suppresses the HTTP discovery the existing flag depends
on, and that asks the user to approve every single connection. The second half
is the one that matters: it caps what this capability can ever be, and Design 6
says so rather than designing around it.

`type` was believed to be a short-string verb whose raw form is harmless. The
245 sampled mentions are bulk prose typed into chat inputs, which inside JSON
inside a shell line is exactly the hostile case the curation rule names.

### What the payload rule costs

Tracing, heap snapshots and response bodies were each expected to be
expressible as raw steps. None is. The evidence is in Design, and the
consequence is that these three verbs are not conveniences: without them the
capability is absent from `bt` entirely.

## Amendments to RFC-03

Normative. RFC-03 is an accepted document, and this RFC changes two of its
decisions. An implementer reading RFC-03 alone would build the wrong thing.

### Amendment 1: the Scope decline of heap, lighthouse and insights is reversed

RFC-03, Scope, declined three things: `heap` capture, a `lighthouse` verb, and
the trace-insight verbs `perf-start`, `perf-stop` and `perf-insight`. It gave
two grounds. The first was the RFC-01 boundary excluding new browser
capabilities from that merge, with "no current user case" for heap. The second
was packaging: the default install depends on `websockets` only, and
`chrome-devtools-axi` keeps the Node-needing jobs.

**Both grounds have lapsed, and this RFC reverses the decline for `heap` and
for trace insights.** The RFC-01 boundary governed that merge and does not bind
later work, which is the same reading RFC-04 applied when it added a capability
after RFC-01. The packaging ground rested on axi keeping the job, and this RFC
retires axi, so the job has nowhere else to go. The user case for trace
insights is the driving dev's, recorded in ADR 0001 with its reasons and its
accepted upkeep.

**The decline of a `lighthouse` verb is not reversed.** It stands, and this RFC
strengthens it: Lighthouse remains outside `bt`, reached as a documented recipe
against a live instance's port. No `bt lighthouse` ships. The RFC-03 decline of
a shell-out form, where `bt` spawns a Node tool against the instance port, also
stands unchanged.

The packaging discipline itself is preserved, not waived. `pip install
browser-tools` still depends on `websockets` alone. Node enters only through
the `insights` extra, which the base install does not pull.

### Amendment 2: the step surface gains `heap`, and the six curated verbs

RFC-03 fixes the step surface as a closed list: "A step MUST be one of these,
and nothing else." This RFC amends that list. The amended surface is:

```
snapshot | click | fill
eval | press | hover | type | wait-text | network-get
wait-idle | wait-stable | wait
detect
console-list | network-list
frames list | frames select PATTERN | frames reset
storage get
screenshot
screencast
heap
Domain.method '{...json params...}'
```

The six curated verbs from Design 1 are steps. Each drives the attached browser
within its own step and returns a value, which is the property the list tests
for, and every RFC-03 rule applies to them unchanged: a step MUST NOT name an
instance, `--endpoint`, `--target` or a page-selecting `--url`, because the run resolves those
once; frame selection set by an earlier step governs them; the whole-run
deadline clamps each one's own wait. `network-get` as a step writes its body to
`--response-file` or reports it inline under the same size rule as the standalone
verb, since no later step can read its output. Its `--url` selects a response
and is allowed in a step. `--reload` is standalone-only. Network joins Page
and Runtime as a run-owned domain; a raw `Network.disable` step is refused.

`heap` is a step. It completes inside its own step and returns a summary.

`trace` is **not** a step, and joins `run`, `launch`, `attach`, `status`,
`stop`, `cleanup`, `profile`, `window-border`, `guide` and `help` in the
not-a-step list, because it owns the whole session window and wraps `run`
itself. `insights` is not a step either: it drives no browser session.

## Design

### 1. The curated verbs

Six verbs, each clearing a clause of the curation rule.

```
bt eval [INSTANCE] '<js>' [--await] [--target SPEC | --url SUB] [--endpoint URL]
bt press [INSTANCE] <key> [--modifiers NAME[,NAME...]] [--target ...]
bt hover [INSTANCE] --uid UID [--target ...]
bt type [INSTANCE] (<text> | --file FILE) [--target ...]
bt wait-text [INSTANCE] <substring> [--timeout-ms MS] [--target ...]
bt network-get [INSTANCE] (--url SUB | --request-id ID) [--response-file PATH]
               [--duration SECONDS] [--reload] [--target SPEC] [--endpoint URL]
```

Every one prints a single JSON object on stdout and exits 0 on success. The
per-verb shapes are below. All exit 2 on a usage error and 1 on a browser-side
failure, matching the house convention.

**`eval`** lands first, because it is the rule's own example and the most
mentioned verb in the record, and because its availability changes how the
other rows are judged. `returnByValue` defaults on, multi-statement JavaScript
is wrapped in an IIFE, and `--await` sets `awaitPromise`. `--await` against a
non-promise is not an error: CDP returns the value, and the verb reports it.
`--url SUB` selects the page whose URL contains `SUB`, exactly as it does on
every other verb; it does not scope evaluation to a frame, which is what
`frames select` is for.

It fixes a defect on the way: raw `Runtime.evaluate` exits 0 when the evaluated
JavaScript throws, reporting a thrown exception as success. `bt eval` exits 1
when `exceptionDetails` is present.

```json
{"value": <any>, "type": "string", "url": "https://...", "targetId": "..."}
```

On a throw, exit 1, nothing on stdout, and stderr carries the exception text
with `exceptionDetails.exception.description` when present.

**`press`** exists because a real key press is two CDP calls, and because the
keyDown alone does not produce a default action unless it carries `code`,
`windowsVirtualKeyCode`, `nativeVirtualKeyCode` and `text`. Measured: the
minimal form fires a page's `keydown` handler and does not submit a form; the
full form submits it. The per-key virtual key code table is the kind of
knowledge a verb should own rather than every caller.

`<key>` is either a single printable character or a name from the verb's own
table, which covers `Enter`, `Tab`, `Escape`, `Backspace`, `Delete`, the four
arrows, `Home`, `End`, `PageUp`, `PageDown`, and `F1` through `F12`. An
unrecognised name is exit 2 with the table printed. `--modifiers` takes a
comma-separated list from `Alt`, `Control`, `Meta`, `Shift`, mapped to CDP's
bitmask; an unrecognised name is exit 2.

```json
{"key": "Enter", "modifiers": ["Meta"], "dispatched": ["keyDown", "keyUp"]}
```

**`hover`** exists because it is `DOM.getBoxModel` followed by
`Input.dispatchMouseEvent` with arithmetic in between, and no step can do
arithmetic on another step's output. `eval` is not a substitute: a synthetic
`mouseover` event fires the page's handler but leaves CSS `:hover` unset, so
the two are observably different.

```json
{"uid": "e12", "x": 412.5, "y": 208.0}
```

**`type`** sends one `Input.insertText`. Measured: one call carried 123
characters of multi-line prose containing apostrophes, a quoted phrase and a
JSON-looking fragment, with an exact round trip and all three lines preserved.
The page saw `beforeinput` and `input` and no key events. `--file` exists
because the measured payloads are prose that does not belong on a command
line. No per-character key-event mode ships; see Alternatives Considered.

Exactly one of `<text>` and `--file` is required. Neither given, or both, is
exit 2. `--file -` reads stdin.

```json
{"chars": 123, "source": "file", "path": "/tmp/msg.txt"}
```

**`wait-text`** joins `wait-idle` and `wait-stable`. Its raw form is a
MutationObserver promise carrying its own timeout, about 400 characters of
JavaScript. `eval` removes the JSON layer but not the logic, and a race-free
wait repeated at every call site is a decision that belongs in one place.

`--timeout-ms` defaults to 5000, matching `wait-stable`. The substring test is
against the page's rendered text, not its HTML source. On the deadline: a
diagnostic on stderr, exit 1, nothing on stdout, matching `wait`.

```json
{"found": true, "substring": "Order placed", "waitedMs": 812}
```

**`network-get`** is the first verb here that exists because of payload
reachability rather than ergonomics. A response body needs
`Network.getResponseBody` with a `requestId`, the requestId dies with the
session, and inside a run no step can read the `network-list` step's output.
Measured: a real requestId taken from one invocation returns
`CDP error -32000: No resource with given identifier found` from the next. The
run owns Network. Before step 1, it subscribes and enables Network, retaining
response metadata and completion state on the handler's runtime until the run
ends. A later `network-get --url` reads traffic caused by an earlier step,
without reloading the page. Frame selection limits matching to that frame;
attached Frame Sessions have their own response buffers.

Standalone, `network-get` opens its own observation window. `--duration`
defaults to 2 seconds, matching `network-list`, and must be finite and
non-negative. `--reload` optionally triggers page-load traffic after subscribing;
a selected frame is navigated to its current URL. Reload is never implicit
and is refused in a Step List. In a run, duration bounds waiting for a missing
or incomplete match, subject to the run deadline. Zero checks buffered state.

Both forms return when a matching response finishes, without a fixed sleep.
The last matching response observed so far wins; this does not promise to
collect later matches for the full duration. Every run now pays for Network
events and retained metadata even if it never calls `network-get`. Bodies stay
in Chrome, whose retention limits and renderer lifetime still apply. A retained
metadata entry does not guarantee that Chrome can still supply its body.

Exactly one of `--url` and `--request-id` is required; neither or both is
exit 2. `--url` matching more than one observed response takes the last and reports
`matched` so the caller can see it was ambiguous.

With `--response-file`, the body is written there and the document reports the
path. Without it, a body that is UTF-8 text and at most 1 MiB is inlined as
`body`; anything larger, or base64-encoded, is omitted with `bodyOmitted`
naming the reason and the remedy. A body is never silently truncated.

```json
{"requestId": "1234.5", "url": "https://...", "status": 200,
 "mimeType": "application/json", "bytes": 4210, "matched": 1,
 "body": "...", "base64Encoded": false}
```

### 2. Capture verbs

```
bt trace [INSTANCE] --out FILE (--duration SECONDS | --steps FILE)
         [--categories LIST] [--timeout SECONDS] [--frames SCOPE]
         [--target ...] [--endpoint URL]
bt heap  [INSTANCE] --out FILE [--target ...] [--endpoint URL]
```

Both are Bounded Captures.

#### Why not a start/end pair

A `Tracing.start` step and a `Tracing.end` step in one Step Run was the
expected design. It cannot work, in either transfer mode, and the failure is
silent in the worse of the two.

With `transferMode: ReportEvents` the run succeeds and produces nothing. All
five steps completed, `Tracing.tracingComplete` reported
`dataLossOccurred: false`, and the trace itself arrived as
`Tracing.dataCollected` events that no step collects. A caller reading the Run
Document sees success.

With `transferMode: ReturnAsStream` the handle arrives in the
`tracingComplete` step's result and no later step can read it. Hardcoding the
handle does not rescue it: the same step list saw handle `"1"` in one run and
`"2"` in the next, because handles increment across the browser's lifetime
rather than the session's.

`heap` fails harder still. Its chunks are emitted synchronously during
`HeapProfiler.takeHeapSnapshot`, so a following
`wait --event HeapProfiler.addHeapSnapshotChunk` subscribes after they are
gone and times out, while `takeHeapSnapshot` itself returns `{}`.

Probes: `docs/research/probes/154/`.

#### What `trace` captures

Exactly one of `--duration` and `--steps` is required. Neither given is exit 2:
an unbounded trace is a hang, and no default window is safe for both a load
capture and an interaction capture. Both given is allowed and the capture ends
at whichever comes first, matching the existing Bounded Capture rule.
`--duration` takes seconds as a decimal.

`--categories` takes one comma-separated list and **replaces** the default set
rather than adding to it. It is not repeatable. This is the escape hatch, and a
caller reaching for it is taking responsibility for a trace the engine may not
fully read.

#### `--steps` and the `run` contract

`--steps FILE` runs a Step List inside the trace window, which inverts the
nesting that failed above: the run goes inside the trace rather than the trace
steps inside the run. It is **the same runner**, not a second one. `trace
--steps` calls `step_run.execute(steps, handler, registry_path, timeout)` at
`src/browser_tools/step_run.py:112`, behind the same validation `run` performs,
so there is one Step List grammar with one implementation.

Consequences, stated rather than left to drift:

- **Validation runs before the trace starts**, not before step 1. A step list
  that fails validation means no `Tracing.start`, no output file, exit 2. This
  is stricter than `run`'s validate-before-step-1 and is the only difference.
- **Every RFC-03 step refusal applies unchanged**, including the refusal of an
  instance name, `--endpoint`, `--target` or `--url` inside a step, and the
  not-a-step list from Amendment 2. `trace` inside a `--steps` file is a usage
  error for the same reason `run` inside a run is.
- **`--timeout` means what it means in `run`**: it bounds the whole step run,
  measured from step 1, and `handler.set_deadline` clamps every wait inside it.
  `--duration` bounds the trace window. With both, the trace ends at whichever
  fires first, and the document reports which. `--timeout 0` means no deadline
  in `run` and `wait`, and `--duration` has to bound the run as well, because
  one thread cannot close the trace on time otherwise. The two therefore
  contradict each other and the combination is refused, rather than letting
  `--duration` silently turn an explicit opt-out into a deadline.
- **`--frames` carries too**, because `--steps` is the same runner as `run`.
  Without it a step list that reaches an interaction inside a cross-origin
  iframe under `bt run --frames all` silently could not under
  `bt trace --steps`, and an OOPIF interaction is exactly the case `--steps`
  exists to capture.
- **Exit code reports the run; the document reports the trace.** If a step
  fails, the run stops at that step, `Tracing.end` still runs, the trace is
  still written, and the verb exits 1. The exit code is never overridden by a
  successful capture, because a caller scripting `bt trace --steps ... && ...`
  is asking about the steps. The written trace is not lost: `trace.path` is in
  the document, which is printed on stdout on both paths.

`--steps` is not a convenience. It is the only shape that reaches an
interaction. Measured: a trace wrapping a driven click produced
`INPBreakdown: fail` with a 322 ms interaction broken into 0 ms input delay,
320 ms processing and 2 ms presentation, against a fixture whose handler burns
exactly 320 ms. The same page captured without the interaction returned
`INPBreakdown: pass` and nothing, which is the same blind spot Lighthouse's
navigation mode has. Probe: `docs/research/probes/154/interact.html`.

#### Default categories

The DevTools set that `chrome-devtools-mcp` uses:

```
-*, blink.console, blink.user_timing, devtools.timeline,
disabled-by-default-devtools.screenshot,
disabled-by-default-devtools.timeline,
disabled-by-default-devtools.timeline.frame,
disabled-by-default-devtools.timeline.stack,
disabled-by-default-v8.cpu_profiler,
disabled-by-default-v8.cpu_profiler.hires,
latencyInfo, loading, disabled-by-default-lighthouse, v8.execute, v8
```

This set is the only default because it is the set measured to produce a
complete Insight Set and a lighter one would silently produce traces the engine
cannot fully read. Transfer is `ReturnAsStream`, matching DevTools.
`dataLossOccurred` is reported and never swallowed: a truncated trace parses
and misleads.

#### Trace output

The file at `--out` is Chrome trace JSON, unmodified. stdout is the document,
in both modes:

```json
{"trace": {"path": "/tmp/t.json", "events": 4041, "bytes": 721344,
           "dataLossOccurred": false, "durationMs": 2980,
           "endedBy": "steps", "categories": ["-*", "..."]},
 "run": { ...RFC-03 Run Document, present only with --steps... }}
```

`endedBy` is one of `duration`, `steps`, `timeout`. With `--duration` alone the
`run` key is absent.

#### Where `heap` lives

On `bt`, not on `browser-tools-profiler`. The profiler is a separate command
because a CPU profile is an enable-start-stop sequence across time that cannot
be split across invocations. A heap snapshot completes inside one invocation,
so that reason does not apply. Measured: 3 chunks, 2.4 MB, 0.06 seconds, valid
node and edge counts.

```json
{"path": "/tmp/h.heapsnapshot", "nodes": 128442, "edges": 512203,
 "bytes": 2411008, "chunks": 3}
```

### 3. Insights

```
bt insights --trace FILE [--insight NAME] [--format json|text]
            [--ignore-engine-mismatch]
```

Behind a new `insights` optional extra. This is the one part of this RFC that
adds a runtime.

The engine is the DevTools Frontend trace model. `chrome-devtools-mcp` pins it
as a Git submodule and bundles it, so it is not an ordinary dependency. The
separately published `@paulirish/trace_engine` snapshot, which Lighthouse uses,
is the practical integration point. Its own README disclaims stability and
public consumption; see References for the package.

**Acceptance is proven, not assumed.** That engine, at 0.0.65, parsed a trace
captured by a `bt` prototype using the categories above and returned 19 of 19
insight models with zero model errors, correctly returning `RenderBlocking:
fail` against a deliberately render-blocking stylesheet. Probe:
`docs/research/probes/154/enginetest/insights.mjs`, which is runnable against
any trace file.

**Acceptance rests on one trace from one Chrome against one engine revision.**
That is enough to prove the integration works and not enough to predict drift.
Open Question 4 names the fixture matrix that would settle it. The decision to
build insights is the driving dev's under ADR 0001 and is not provisional. What
is provisional is the pinned engine revision and the validated Chrome range:
those two values are left unset in this document and are set by the ticket that
builds the extra, after the matrix exists or with an explicit note that it does
not.

**Cost is measured.** 120 to 130 ms of wall clock per invocation including Node
process start, for a 4041-event trace: import 26 to 48 ms, read 3 to 4 ms,
parse and generate 69 to 73 ms. 17 MB for the engine, 24 MB with dependencies,
three packages. Probe: `docs/research/probes/154/enginetest/coldstart.mjs`.

**A lockfile is mandatory.** Pinning the engine version does not pin its
behaviour. `@paulirish/trace_engine` declares both of its dependencies as
`"latest"`, and `third-party-web`, the dataset behind the `ThirdParties`
insight, shipped a release eight days before this RFC. Two machines installing
a month apart can get different verdicts from the same pinned engine.

**How the extra installs.** The adapter is a small Node package vendored in
this repository at `src/browser_tools/_insights/`, carrying its own
`package.json` and a committed `package-lock.json`. The Python extra
`browser-tools[insights]` declares no Python dependency; what it installs is a
marker plus the adapter sources. `npm ci --omit=dev` runs once, at the
adapter's directory, driven by `bt insights --setup` or by the first
invocation, and it is the only network access in the feature. `node_modules`
lands beside the adapter inside the installed package directory, never in the
user's working directory. A bare `npm install` is never run, because it would
resolve the two `latest` dependencies afresh.

**Network access.** The install step reaches the npm registry. Every later
invocation reads a local trace file and writes JSON, and the adapter is
invoked with an explicit path argument rather than a shell string. Offline
after install is fully supported; offline before it fails with the `npm ci`
command to run when network returns.

Output is JSON by default, because every other `bt` verb returns JSON. `bt`
owns only the stable outer fields per insight and carries the engine's own
formatter rendering as a `detail` string, so the volatile part of the model
stays upstream's to maintain.

```json
{"engineRevision": "0.0.65", "browserVersion": "HeadlessChrome/...",
 "navigations": [{"url": "https://...",
   "insights": [{"key": "RenderBlocking", "state": "fail",
                 "title": "Render blocking requests",
                 "savings": {"metric": "FCP", "ms": 430},
                 "detail": "..."}]}]}
```

`savings` is `null` when the engine reports none. `--insight NAME` returns the
same document carrying one insight; an unknown name is exit 2 with the valid
names listed. `--format text` prints the formatter output alone, unwrapped.

At run time the verb reads `Browser.getVersion`, stamps every result with the
engine revision and the browser version, and on a mismatch against the
validated range refuses with exit 1 naming the remedy, with
`--ignore-engine-mismatch` as an expert escape hatch. The stamp is present on
both paths.

The base install is unchanged: `pip install browser-tools` stays
`websockets`-only, with no Node. Capture is pure Python over CDP and ships in
the base. Only analysis needs the extra.

### 4. The dialog policy

```
--dialog dismiss|accept[:TEXT]
```

The default is `dismiss`.

Today an ordinary click on a button whose handler calls `alert()` never
returns. Measured: the click timed out at 6 seconds and `Runtime.evaluate`
against the same page timed out too. Worse than The Manual records, the state
is not recoverable: the next invocation's `Page.navigate` hung as well, and the
instance had to be stopped and relaunched. Probe:
`docs/research/probes/156/exp-dialog.py`.

**Which verbs carry it.** Any verb that can run page JavaScript can raise a
dialog, which is more than `click`. `eval` calling `alert()` raises one as
directly as a click handler does, and `press` and `type` reach handlers that
dialog. The flag therefore carries on `click`, `eval`, `press`, `type`,
`hover`, `wait-text` and `run`, which is every verb that drives the page.
`navigate` carries it too, for a `beforeunload` prompt.

**The subscription is unconditional.** `bt` subscribes to
`Page.javascriptDialogOpening` for the whole invocation on every one of those
verbs, whether or not `--dialog` was passed, because the default is a policy
and not an absence. A verb that does not carry the flag does not subscribe.
Inside a `run`, the run's policy governs every step; a step cannot set its own.

**More than one dialog in one invocation** is answered by the same policy, each
one recorded in order. There is no count limit and no escalation.

Measured, with an ordinary awaited click returning in 0.04 s every time: alert
accepted, confirm accepted as `true` and dismissed as `false`, prompt accepted
with `promptText` delivered and dismissed as `null`, and a plain button
producing zero dialog events and no interference.

`dismiss` is the default because today's default is a lost instance, because
declining is the conservative answer when a dialog was not expected, and
because Playwright's default is to dismiss, so the behaviour will not surprise
a caller who has used one. Every answered dialog is recorded in the verb's own
document under `dialogs`, so a policy that fires on an unexpected dialog is
visible rather than silent:

```json
{"dialogs": [{"type": "confirm", "message": "Delete this?",
              "answer": "dismiss", "result": false}]}
```

The key is absent when no dialog fired, never an empty array reported as a
successful capture of nothing.

### 5. Recipes, not verbs

These go in `bt guide` with a worked command line, because their raw form is
one CDP call and not hostile: `scroll` (`Input.dispatchMouseEvent` with
`mouseWheel`, or `End`/`Home` key events), `upload` (`DOM.setFileInputFiles`),
`newpage` (`Target.createTarget`), `resize` (`Emulation.setDeviceMetricsOverride`),
`fillform` (a Step List of `fill` lines), and `wait <ms>` (a shell `sleep`
outside a run).

The Lighthouse recipe also lives here, and Amendment 1 keeps the RFC-03 decline
of a `lighthouse` verb in force. Measured: Lighthouse runs against a `bt`
instance through `--port` and leaves it intact, and it audits inside a named
profile with that profile's cookies. Two hazards the recipe must carry. A
headed run takes the user's screen, because Lighthouse opens a foreground tab
through Puppeteer and `bt`'s own screen refusals do not apply to a tool
speaking CDP directly. And a wrong `--port` does not fail: chrome-launcher
launches its own Chrome, audits with it, and exits 0 with a clean-looking
report from a different browser. Probes:
`docs/research/probes/151/wrongport.sh` and `focus-sample.sh`.

**Five `emulate` overrides are run-step recipes only.** Measured: colour
scheme, network conditions, CPU throttling, geolocation and user agent all
revert when the invocation ends; only device metrics survives. A standalone
`bt emulate --cpu 4` would exit 0 and change nothing, which is the worst
possible shape. They are documented as steps that only mean anything inside the
run that uses them. Probes: `docs/research/probes/148/b10-emulate.sh`,
`b11-emulate2.sh`, `b12-geo.sh`.

Already covered with no gap: `pages` is `bt status`, `closepage` is
`bt stop --target`, `console` is `console-list`, `console-get` has nothing to
fetch, and `selectpage` is `--target` or `--url`.

### 6. Instance naming, and attaching to the user's own Chrome

Two capabilities axi had that `bt` does not match. Neither becomes a
first-class feature, and the reasons differ.

#### Instance naming: documentation now, revisited on a named trigger

axi's most mentioned environment variable, about 3,200 mentions, gave each
session its own named bridge. The sample that narrowed this was 20 mentions,
taken as the first 20 hits in transcript order across both harnesses, read in
full context. Zero were parallel workers and 17 were one caller choosing a name
and re-addressing it on every call, which `bt`'s positional instance argument
already is. A second `launch` in one directory already works, so concurrency
was never the gap.

**Twenty is a thin sample for a 3,200-mention population, and the method is the
echo-prone one described under What was measured.** The same transcript store
produced 328 apparent uses of a worker-name idiom that were all echoes of help
text. This close-out is therefore interim, not final.

No `--name` flag ships now. The argument for that: a chosen name would not be
more stable than a derived one, because a stop frees either, so the flag would
buy pre-choosing and cross-party agreement and not stability. Neither appears
in the sample. What ships is the documentation fix in the next section plus a
statement that `bt launch` prints the name it chose.

**The trigger that reopens `--name`**, stated so it is a measurement and not a
rediscovery: one confirmed incident of an agent driving the wrong instance
after a recycled name silently rebound. One is enough, because the failure is
silent and destructive. The other two known-hard cases, a caller that did not
perform the launch and cannot map `repo-01` through `repo-04` back to tasks,
and a registry loss that kills captured and chosen names alike, are recorded
here as context; neither is fixed by a `--name` flag, and neither is fixed by
the documentation either.

#### Attaching to the user's own Chrome: a thin primitive, gated on verification

Chrome 144 and later expose an approval-based remote debugging service that is
not a conventional HTTP CDP endpoint. Every claim below is cited to Chromium
source or to Chrome's own documentation; the citations are in References.

- **It returns 404 for `/json`, including `/json/version`, by design.** The
  check runs before every `/json` command, and it also 404s the discovery page
  and the frontend resources. `bt --endpoint` speaks HTTP and discovers through
  `/json/version`, so it cannot reach this service at all.
- **It admits only WebSocket paths beginning `/devtools/browser`** and rejects
  every other path.
- **The port and the path live in the profile's `DevToolsActivePort` file**,
  first line the listening port, second line the browser WebSocket path.
- **The port is not reliably 9222.** Chrome reads the previous
  `DevToolsActivePort` on startup and tries to reuse its first-line port; with
  no valid file it starts at 9222 and falls back to any available port when
  that one is taken. This is why discovery reads the file rather than assuming
  a port, and why a 404-then-try-`/devtools/browser` fallback on a hand-passed
  port is not a substitute for discovery.
- **The enabling preference survives a normal restart**, because it is a
  persisted Local State preference. An administrator policy can disable the
  whole feature through `devtools.remote_debugging.allowed`, so a diagnostic
  must distinguish "not enabled" from "not permitted".
- **Approval is per connection, not per browser session.** Chrome asks the user
  to allow every incoming connection and shows an automation banner while
  connected.

**What is still unverified is local, not upstream.** None of this has been
exercised against the Chrome installed on the driving dev's machine: the
preference has never been set there and no port file exists. The implementing
ticket re-establishes the behaviour against that Chrome before building. If it
differs, the design here is wrong on arrival and the right answer is none of it.

**The per-connection approval is a design constraint, not a detail.** A fully
unattended agent cannot use `--chrome-profile`, because nobody is there to
click Allow, and this is the documented behaviour rather than a bug to work
around. The capability is for an agent working alongside someone at the
keyboard. That is what the bounded wait and its diagnostic are for, and it is
why the verb is not promoted or aliased.

What ships is deliberately thin, and it is gated, not ungated:

- **A `ws://` form for `--endpoint`.** Grammar:
  `ws://HOST:PORT/devtools/browser/UUID`, where `HOST` must be a loopback host
  and the path must begin `/devtools/browser/`. This **amends**
  `resolve_endpoint_port` at `src/browser_tools/endpoint.py:70`, which today
  accepts only `http` and `https` and returns a TCP port. The amended function
  returns a resolved endpoint carrying both the port and, for the `ws://` form,
  the full path, so the caller dials it directly instead of discovering it. The
  loopback check runs on the `ws://` form exactly as on the HTTP form, against
  the same `LOOPBACK_HOSTS`, and the same non-loopback refusal text and tunnel
  remedy apply.
- **Port-file discovery behind `--chrome-profile [CHANNEL]`**, a verb-level
  flag on every verb that takes `--endpoint`, mutually exclusive with it.
  `CHANNEL` is one of `stable`, `beta`, `dev`, `canary`, defaulting to
  `stable`. It resolves that channel's default user-data directory for the
  platform, reads `DevToolsActivePort`, and dials the resulting WebSocket. The
  per-channel, per-platform directory table is the verb's own knowledge and is
  the substance of the implementing ticket. A missing or malformed file is exit
  2 naming the path it looked at.
- **A bounded wait on Chrome's per-connection Allow prompt: 30 seconds**,
  matching `wait`'s default, then exit 1 with a diagnostic naming the remedy:
  `No approval received in 30s. Chrome asks permission for each debugging
  connection; click Allow in the browser window.` Chrome also shows an
  automation banner while connected. An unattended agent must never hang on a
  click that is not coming.
- **It writes nothing to the registry, on any path**, including on failure.
- **Every existing External Endpoint refusal, unchanged**: loopback only,
  `Browser.close` and `Browser.crash` refused. An auto-discovered port is as
  unauthenticated as a hand-passed one, so discovery changes what `bt` can find
  and changes nothing about what it will do.

**A `launch --auto-connect` is specifically rejected**, and this reverses what
the research recommended. It would register an instance whose user-data-dir is
the person's real Chrome profile directory. `src/browser_tools/endpoint.py:7-14`
states the consequence in the project's own words: two vendored paths `rmtree`
whatever that field holds, and a guard in the lifecycle layer would not hold
because the vendored `cleanup` iterates the registry itself. The previous map
already found that defect destroying a named profile on every normal close. The
absence of a registry entry is the safety property, and a launch-shaped attach
discards it.

Nothing here is promoted to first-class, advertised in the README, or aliased,
until one end-to-end attach is verified on the driving dev's machine. That
verification needs a human: the enabling preference has never been set there, no
port file exists, and each connection needs an Allow click. So the 458 sampled
mentions prove interest and not one verified attach, and the local evidence is
consistent with this never having worked on that machine. If the verification
fails, or a re-sample shows those mentions were configuration echoes, the right
answer becomes none of it.

### 7. Documentation defects

Seven, all found while investigating, all independent of the verbs above.

1. `WHAT DOES NOT CARRY BETWEEN INVOCATIONS` omits the five `emulate`
   overrides, and the one `Emulation` example nearby is the single case that
   does carry, which invites the wrong generalisation.
2. The `TYPE KEYSTROKES` recipe fires a page's `keydown` handler but produces
   no default action, so a form does not submit. Design 1's `press` replaces
   it; this entry is the documentation half.
3. Raw `Runtime.evaluate` exits 0 when the evaluated JavaScript throws.
   Design 1's `eval` fixes the behaviour; the guide must stop presenting the
   raw form as equivalent.
4. Curated verbs and `run` silently pick the first page when `--target` is
   absent, where the raw passthrough refuses with both targets listed. The
   behaviour is deliberate at `src/browser_tools/cdp_handler.py:627` and
   undocumented. It cost a wrong tab closure during the investigation.
5. `console-list` returns messages logged before it attached, because Chrome
   replays the buffer on enable. The `--duration` wording reads as "only what
   happens now".
6. Nothing states that a second unnamed launch in one directory succeeds with a
   `-NN` suffix. The nearest sentence, "rather than opening a second browser on
   the same directory", sits under `PROFILE EXCLUSIVITY` at
   `src/browser_tools/GUIDE.txt:591` and under the named-profile bullet at
   `README.md:176`, and is correct in both places: it describes the
   named-profile refusal, in a section about named profiles. The defect is the
   absence of the positive statement, which is why reading that sentence out of
   its section caused a whole ticket to be charted on a false premise. The fix
   is to state the succeeding case, not to change the existing sentence.
7. A dialog left open may make `Page.navigate` hang too, which The Manual
   currently presents as the escape. The only exit measured was stopping and
   relaunching the instance.

### 8. Retiring axi

Machine-level, outside this repository, ordered after the verbs ship.

| Target | Action |
|---|---|
| `~/.agents/GLOBAL.md`, tracked in `~/dev/dotfiles` | Replace the Browser Automation section with one that names `bt` as the only browser CLI. The replacement text is owned by the documentation ticket, which also owns the guide and README changes, so the three land in one voice. `~/.codex/AGENTS.md` symlinks to it, so one rewrite reaches both harnesses. |
| `~/.claude/settings.json` | Remove the `chrome-devtools-axi` SessionStart hook. |
| `~/dev/dotfiles/codex/.codex/hooks.json` | Remove the same hook. |
| `~/dev/skills/browser-tools` | Update to the new surface; it is the source of the deployed skill. |
| npm global | `npm uninstall -g chrome-devtools-axi`. |

**Two capabilities are given up on purpose** and must be named in the removal
record: Lighthouse's audit list, which the recipe does not reproduce because
the audits are that tool's own scoring, and anything ticket #159's verification
turns up.

firstmate is uninstalled alongside, because it reinstalls axi from its
bootstrap. Its measured footprint: `~/.local/bin/treehouse` and its
`~/.treehouse` state, `~/.local/bin/no-mistakes` symlinking into a 26 MB
`~/.no-mistakes`, an unmanaged LaunchAgent
`com.kunchenguid.no-mistakes.daemon.b6c2ac14.plist` with `RunAtLoad` and
`KeepAlive` true and two live daemon processes, and the npm globals `tasks-axi`
and `quota-axi`. Nothing in any shell, tmux, agent hook or config file
references it. `gh-axi` and `lavish-axi` stay; they are used elsewhere.

One check before removing the global `quota-axi`: NotchBar's quota plugin reads
a `quota-axi`, and its README says it uses its own pinned copy at 0.1.49 while
the global is 0.1.42. Confirm the plugin resolves its own copy first, by
running the plugin with the global removed from `PATH` for that invocation and
confirming it still reports a quota. If it fails, the global stays.

Removal changes state outside any repository, so each uninstall's reinstall
line is recorded and exercised once before anything is removed.

The `~/dev/firstmate` clone stays. Deleting it is out of scope.

## Error Handling

| Condition | Behaviour |
|---|---|
| `bt eval` and the JavaScript throws | exit 1, `exceptionDetails` on stderr, nothing on stdout. Corrects the raw passthrough's exit 0. |
| `bt press` and the key or modifier is unknown | exit 2 with the valid names listed. |
| `bt type` with neither `<text>` nor `--file`, or both | exit 2. |
| `bt wait-text` and the deadline passes | diagnostic on stderr, exit 1, nothing on stdout, matching `wait`. |
| `bt network-get` with neither selector, or both | exit 2. |
| `bt network-get` with negative or non-finite `--duration` | exit 2 before connecting or running step 1. |
| `network-get --reload` in a Step List | exit 2 before step 1; reload is standalone-only. |
| `bt network-get` with no match or an unfinished response at the deadline | exit 1 with a diagnostic; the run deadline may end the wait sooner. |
| `bt network-get` with a failed matching request | exit 1 naming the loading error. |
| `bt network-get` and the response body is gone | exit 1 with the CDP error. Bodies cannot cross invocations and Chrome may evict them within a run. |
| `bt network-get` and the body is over 1 MiB or base64 | written to `--response-file` when given; otherwise omitted with `bodyOmitted` naming the remedy. Never truncated. |
| `bt trace` with neither `--duration` nor `--steps` | exit 2. An unbounded trace is a hang. |
| `bt trace --steps` and the step list fails validation | exit 2 before `Tracing.start`; no output file is created. |
| `bt trace --steps` and a step fails at run time | the run stops at that step, the trace is still written and reported, and the verb exits 1. The run's status wins. |
| `bt trace` and `dataLossOccurred` is true | reported in the trace document, never swallowed. |
| `bt insights` and the extra is absent | exit 2 naming the exact install line, matching the existing rule for a command whose extra is missing. |
| `bt insights` and `npm ci` has not run | exit 2 naming the setup command, and naming the network requirement when offline. |
| `bt insights --insight` with an unknown name | exit 2 with the valid names listed. |
| `bt insights` and the engine revision does not match the browser | exit 1 naming the remedy, unless `--ignore-engine-mismatch`. The stamp is present either way. |
| `--endpoint ws://` and the host is not loopback | exit 2, the existing non-loopback refusal with the tunnel remedy. |
| `--chrome-profile` and the port file is missing or malformed | exit 2 naming the path it read. |
| `--chrome-profile` and no approval arrives in 30 s | exit 1 naming the Allow click. |
| A dialog is answered by the policy | recorded under `dialogs` with type, message, answer and result; never silent. |

## Security Considerations

`bt insights` introduces a Node process that reads a trace file and writes
JSON. The trace is a local file the caller named, and the adapter is invoked
with an explicit path argument rather than a shell string. The install step
reaches the npm registry; no later invocation touches the network.

The `insights` extra pulls a package whose upstream disclaims stability, along
with two transitive dependencies that upstream declares as `latest`. The
lockfile requirement above is a supply-chain control as much as a
reproducibility one: without it, an install resolves whatever those two
packages publish that day. `npm ci --omit=dev` from a committed lockfile is the
only supported install path.

The dialog policy answers dialogs the caller did not necessarily anticipate.
`dismiss` as the default means the answer is always the declining one, and
every answer is recorded.

Attaching to the user's own Chrome is the highest-risk capability here, which
is why it ships gated. A CDP endpoint is unauthenticated full control of a
logged-in browser. Discovery through `--chrome-profile` makes such an endpoint
easier to find and changes nothing about the refusals: loopback only, browser
lifetime methods refused, and no registry entry, so no vendored cleanup path
can reach the real profile directory.

## Alternatives Considered

**A trace start/end pair as Step List steps.** Impossible in both transfer
modes; see Design. Not a preference.

**Trace insights dropped, with Lighthouse covering the need.** The agent
recommended against building them on upkeep grounds, and the driving dev
decided to build them. ADR 0001 records that decision and its reasons.
Lighthouse covers the load case and does not reach an interaction from a
one-line command.

**A Python port of the insight definitions.** Each changed insight ports with
its handler, normalization, navigation, entity, Lantern and synthetic-event
dependencies, which is far larger than 19 formulas, and it recurs on every
Chrome release with drift detectable only by a fixture suite this project would
have to build. Bounded, explicit instability beats unbounded, silent drift.

**Vendoring a built DevTools Frontend bundle.** RFC-01's vendoring rules assume
reviewable source and a reading of the vendored code before merge. A
multi-megabyte built JavaScript artifact cannot satisfy that intent, so
vendoring here would contradict the existing regime without saying so.

**Shelling out to `chrome-devtools-mcp` or Lighthouse for insights.**
`performance_analyze_insight` reads its own daemon's stored trace by index and
does not accept an arbitrary file, and Lighthouse's CLI owns capture. Either
way the `--steps` interaction capture would be handed to another tool. RFC-03
declined a shell-out form on its own grounds, and Amendment 1 leaves that
decline standing.

**A separate step-list runner inside `trace`.** Rejected: two runners for one
grammar drift, which is the reason RFC-03 requires argparse reuse. `trace
--steps` calls the same `step_run.execute`.

**A non-blocking `click --no-wait` instead of the dialog policy.** It works,
but it needs three steps and prior knowledge that the button raises a dialog.
Its only theoretical advantage is branching on the dialog's message, and no
step can read another step's output, so that advantage is nil.

**A per-character key-event `type`.** Zero of 245 sampled mentions needed one.
`Input.insertText` fires the `input` events a chat interface's send control
listens for. The flag ships when a page that gates on `keydown` actually
appears.

## Decisions

Seven were taken with the driving dev present, counting the two where the dev
fixed the rule and the agent applied it in that session. Five were taken by the
agent alone, after the driving dev handed over the map, and are marked. Every
machine-made decision names its undo path in its ticket.

| # | Decision | Ticket | Decider |
|---|---|---|---|
| 1 | Destination: retire axi, `bt` covers what was used | #147 | dev |
| 2 | Perf scope: trace, heap, Lighthouse recipe, and insights | #147 | dev |
| 3 | Curation rule as stated in Terminology | #147 | dev |
| 4 | Machine-level removals and firstmate are in scope | #147 | dev |
| 5 | `bt trace` and `bt heap` shapes | #154 | dev |
| 6 | Verb gap classification under rule 3 | #148 | agent applying rule 3, dev present |
| 7 | Lighthouse is a recipe, headless, port read in-script | #151 | agent applying rule 3, dev present |
| 8 | Insights route, packaging and surface | #155 | machine-made |
| 9 | Dialog policy, defaulting to dismiss | #156 | machine-made |
| 10 | Final curated set, and `type` without key events | #157 | machine-made |
| 11 | Instance naming: documentation now, `--name` on a named trigger | #158 | machine-made, interim |
| 12 | Attach: thin primitive, no `launch --auto-connect`, gated on verification | #159 | machine-made |
| 13 | Amendments to RFC-03, scope and step surface | this RFC | machine-made |
| 14 | Settle the network-get window question: the run owns Network and buffers responses before step 1; standalone observes for at most --duration (default 2 seconds), with reload opt-in only | this RFC | driving dev |

Row 13 is new in version 2 and is the most consequential machine-made row: it
changes an accepted document. Its undo is to withdraw `heap` and `insights`,
which leaves the retirement short of two capabilities and reopens decision 2.

## Open Questions

1. Whether `bt trace` captures activity in other tabs of the same instance,
   which decides whether it documents itself as page-scoped or
   instance-scoped. Not measured.
2. Trace size on a busy real page. The fixture produced 721 KB for under three
   seconds.
3. Whether `--steps` should accept a step list on stdin, as `run -` does.
4. Cross-version insight drift. The fixture matrix of traces from supported
   Chrome milestones against candidate engine revisions has not been built. It
   is what sets the pinned revision and the validated Chrome range, both left
   unset in Design 3.
5. Whether a headed Lighthouse run can be made safe. There is no flag for a
   background tab.
6. Whether Chrome's documented per-connection approval behaves as documented
   on the driving dev's installed Chrome. The documentation is unambiguous and
   is cited; what is open is only whether that Chrome matches it. It is the
   first thing the attach verification checks, because a per-connection prompt
   is what makes this unusable unattended.

## References

- Decision map: https://github.com/dungle-scrubs/browser-tools/issues/147
- Decision tickets, all in the same repository:
  [#148](https://github.com/dungle-scrubs/browser-tools/issues/148),
  [#149](https://github.com/dungle-scrubs/browser-tools/issues/149),
  [#150](https://github.com/dungle-scrubs/browser-tools/issues/150),
  [#151](https://github.com/dungle-scrubs/browser-tools/issues/151),
  [#152](https://github.com/dungle-scrubs/browser-tools/issues/152),
  [#153](https://github.com/dungle-scrubs/browser-tools/issues/153),
  [#154](https://github.com/dungle-scrubs/browser-tools/issues/154),
  [#155](https://github.com/dungle-scrubs/browser-tools/issues/155),
  [#156](https://github.com/dungle-scrubs/browser-tools/issues/156),
  [#157](https://github.com/dungle-scrubs/browser-tools/issues/157),
  [#158](https://github.com/dungle-scrubs/browser-tools/issues/158),
  [#159](https://github.com/dungle-scrubs/browser-tools/issues/159)
- ADR 0001, insights: `docs/adr/0001-trace-insights-in-bt.md`
- Research findings, one branch per ticket, each at `docs/research/`:
  `research/verb-gaps` (#148), `research/auto-connect` (#149),
  `research/trace-insights` (#150), `research/lighthouse-recipe` (#151),
  `research/firstmate-footprint` (#152), `research/parallel-instances` (#153)
- Runnable probes behind every "Measured:" figure: `docs/research/probes/`,
  committed with this RFC.
- CDP `Tracing` domain, for `transferMode` and `dataLossOccurred`:
  https://chromedevtools.github.io/devtools-protocol/tot/Tracing/
- Approval-mode 404 for `/json`, and the `/devtools/browser` path constraint:
  Chromium `content/browser/devtools/devtools_http_handler.cc` at blob
  `45ff52e`, lines 598-605, 774-805 and 827-867.
  https://chromium.googlesource.com/chromium/src/+/45ff52e032500bea7703cd4f22723b519fa81fa2/content/browser/devtools/devtools_http_handler.cc#598
- `DevToolsActivePort` format, port line then path line: the same file, lines
  305-322.
- Port reuse and fallback away from 9222: Chromium commit
  `cec7af242a1ec8b33becd04582467cddb2902a26`, lines 33-40, 101-120 and 162-186.
- The preference persists in Local State, and the
  `devtools.remote_debugging.allowed` policy: Chromium commit
  `27485803c25baad7e0aeb4c4368403d10003dc53`, lines 251-286.
- Per-connection approval and the automation banner: Chrome documentation,
  "Connect to an existing browser session".
  https://developer.chrome.com/docs/devtools/agents/get-started/configuration#connect-to-an-existing-browser-session
- Why normal CDP discovery does not apply here: CDP, "How do I access the
  browser target?" https://chromedevtools.github.io/devtools-protocol/
- Prior art for the same discovery, read as source: chrome-devtools-mcp
  `src/browser.ts` at tag `chrome-devtools-mcp-v1.9.0`, lines 28-109, and
  Puppeteer 25.11.0 `ConnectOptions.channel`.
- `@paulirish/trace_engine`: https://www.npmjs.com/package/@paulirish/trace_engine
- RFC-01, the merge that set the vendoring rules and removed the Node
  subprocess: `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md`
- RFC-03, the Step Run and Step List contract this RFC builds on and amends:
  `docs/rfc/03_run-many-steps-in-one-invocation.rfc.md`
- Adversarial review of version 1, whose findings produced version 2:
  `docs/research/05-rfc-review-v1.md`, committed with this RFC.
