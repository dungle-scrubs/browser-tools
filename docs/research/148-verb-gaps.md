# Findings: which axi verb gaps become curated bt verbs, and which become guide recipes

Ticket: [dungle-scrubs/browser-tools#148](https://github.com/dungle-scrubs/browser-tools/issues/148).
Map: [#147](https://github.com/dungle-scrubs/browser-tools/issues/147).
Researcher: opus-5@claude, 2026-09-22.

**The rule applied here, from #148, unchanged:** a gap becomes a curated verb
when its raw CDP form needs shell-hostile quoting (JavaScript inside JSON
inside a shell line) or more than one CDP call to do the obvious thing.
Otherwise it becomes a guide recipe. Every verdict below is machine-made under
that rule and marked so.

**What was run.** `bt` 0.8.0, Chrome 153.0.8010.53, one headless instance named
`148-01` launched from
`/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148`, driving a local
fixture page on `http://127.0.0.1:17490/fixture.html` (forms, a file input, a
tall scroll region, alert/confirm/prompt buttons, a hover target, a 1.5s
delayed text node, a console line and a fetch). The instance was stopped and
the fixture server killed at the end. `chrome-devtools-axi` was read with
`--help` only and never run against a browser, as the brief requires.

**Standing.** *observed* means this session ran it and saw the result recorded
here. *documented* means the owning source says it. *unverified* means neither.

---

## Reframing findings

Lead with these. They change what #148's answer should be, not just its
contents.

### R1. The rule does not decide `emulate`, because five of its six overrides die with the invocation

Every `emulate` sub-capability is one CDP call with no JavaScript in it, so
both clauses of the rule say "recipe" for all of them. That verdict is empty
for five of the six, because a recipe run as its own invocation is a no-op.
Measured, one override at a time, each set in one invocation and read in a
separate one, then set and read again inside one `bt run`:

| override | CDP call | survives its invocation | evidence |
|---|---|---|---|
| device metrics (`resize`, `emulate --viewport`) | `Emulation.setDeviceMetricsOverride` | **yes** | set 390x844, a separate invocation read `[390, 844, 1]` |
| colour scheme | `Emulation.setEmulatedMedia` | no | set `light`; separate invocation `matchMedia("(prefers-color-scheme: light)").matches` = `false`; inside one run = `true` |
| network conditions | `Network.emulateNetworkConditions` | no | set `offline: true`; separate invocation fetch = `OK 200`; inside one run = `FAILED Failed to fetch` |
| CPU throttling | `Emulation.setCPUThrottlingRate` | no | rate 20; spin-loop iterations in 200 ms: baseline 3,096,271, separate invocation 3,064,220, inside one run 147,273 |
| geolocation | `Browser.grantPermissions` + `Emulation.setGeolocationOverride` | no | separate invocation `ERR 1 User denied Geolocation`, even with the grant already sent; grant + override + read inside one run = `[48.8566, 2.3522]` |
| user agent | `Emulation.setUserAgentOverride` | no | separate invocation reports the real `HeadlessChrome/153.0.0.0` UA; inside one run reports `bt-verb-gap-probe/1.0` |

Standing: observed, all six rows.

So `emulate` is not one verb-shaped decision. It is one page-state recipe
(viewport) plus five run-step recipes that only mean anything when they sit in
the same Step Run as the work they shape. A curated `bt emulate --cpu 4` run on
its own would exit 0 and change nothing, which is the worst possible shape: a
silent no-op.

Consequence for The Manual: "WHAT DOES NOT CARRY BETWEEN INVOCATIONS" lists
four things (a DOM nodeId, an enabled domain, a JavaScript dialog, a selected
frame). It is short by five more: the colour-scheme, network-conditions, CPU,
geolocation and user-agent overrides. The section next to it, "SET THE VIEWPORT,
or emulate a phone", says the device-metrics override outlives the invocation,
which is true, and an agent generalises from it to the rest of `Emulation.*`,
which is false.

### R2. `dialog` inside a Step Run is settled - yes - and the shape that matters still deadlocks

#147 lists "whether bt can handle a JavaScript dialog inside a Step Run" as not
yet specified. It can. Observed, twice, exit 0 both times: a dialog opened in
step 1 is caught by `wait --event Page.javascriptDialogOpening` in step 2 and
answered by `Page.handleJavaScriptDialog` in step 3, with `promptText` reaching
the page (`window.__dialogResult` = `"typed-answer"`), and `accept: false`
reaching it as `"false"`.

The condition is that the step which raises the dialog has to return first. The
common shape does not. A `click --uid` on a button whose `onclick` calls
`alert()` never returns: the click blocks on the dialog, the dialog waits for a
later step, and the run sits there until its deadline.

    $ printf ... | bt 148-01 run - --timeout 20
    call_native(click) error:
    error: step 1 timed out: click --uid AB5DFB3B758F-200
    the run's 20.0s deadline passed: click did not answer in time
    {"run": {"steps": 4, "completed": 0, "status": "timeout"}, ...}
    [exit 1]

The alert was left open. A new invocation could not clear it
(`Page.handleJavaScriptDialog` = `CDP error -32602: No dialog is showing`,
exit 1), which re-confirms what The Manual already documents. Navigating away
cleared it, which The Manual also documents.

The workaround is to make the trigger return: `Runtime.evaluate` with
`setTimeout(() => el.click(), 300)` as step 1, then wait, then handle. Observed
working for both a prompt and a confirm.

So a curated `bt dialog accept|dismiss [text]` verb is not the decision here.
Whatever the verb is called, it can only run in a Step Run, and only after a
trigger step that does not block. The decision worth taking is whether `click`
gets a non-blocking mode, or whether bt answers
`Page.javascriptDialogOpening` itself with a policy set on the run.

### R3. `network-get`'s response body has no raw form at all

It is the only verb on #148's list that is unreachable today rather than merely
awkward. A response body needs `Network.getResponseBody` with a `requestId`,
and the requestId is session state:

- Across invocations: a real requestId taken from one invocation's
  `network-list`, sent from the next, returns
  `CDP error -32000: No resource with given identifier found` (exit 1).
  Observed.
- Inside one Step Run: a step cannot read another step's output, which The
  Manual states as a property of the Step List. So the `requestId` the
  `network-list` step prints cannot reach the `Network.getResponseBody` step.
  Observed, as a deliberately unfillable placeholder that failed the run at
  step 4.

Neither clause of the rule is what decides this one. There is no raw form to
judge. It is a curated verb or it is a drop.

### R4. A curated verb and a Step Run silently pick a page where a raw call refuses

With two tabs open and no `--target`:

- `bt 148-01 snapshot` read the *other* tab and said nothing about it.
- `bt 148-01 run -` drove the first page in target-ID order.
- `bt 148-01 Runtime.evaluate '{...}'` exited 1: `Multiple page targets found.
  Specify one:` with both listed.

Standing: observed for all three. Documented in the source: `_target_selector`
in `src/browser_tools/cdp_handler.py` returns `("1", "index")` for a missing
spec, with a comment saying why ("Passing the absence straight through would
break every handler-routed verb the moment a second tab or a popup is open").
The Manual does not say it. It says a run "resolves the instance, the endpoint
and the page once, before step one", which a reader takes as "resolves", not
"picks the first".

This is what `selectpage` actually maps onto. The selectpage row below is not a
verb decision; it is a documentation defect, and it bit this session: after
`bt snapshot` silently retargeted, `bt stop --target 2` closed the fixture tab
rather than the new one, because target-ID order had put the new tab at
index 1.

### R5. `eval` lands first, and it moves other rows

`eval` is the rule's own example of clause 1, and it is the most-used axi verb
in the measured record (6642 Codex invocations). Once `bt eval '<js>'` exists,
the raw form of several other gaps stops being JSON-wrapped, and two rows below
change shape: `wait <text>` (today a 400-character MutationObserver promise
inside JSON inside a shell line) becomes one readable `bt eval` line, and the
axi-`run` substitute (one `Runtime.evaluate` carrying the whole decision)
becomes something an agent would actually type.

The table below judges every row against today's raw CDP, as #148 asks. The
implementation set should be decided in one pass with `eval` first, because
verb-by-verb judgement double-counts: it reads each JavaScript-shaped gap as
its own curated verb when `eval` plus a guide line would cover it.

---

## The table

One row per verb on #148's list. "Hostile" is the rule's clause 1
(shell-hostile quoting) or clause 2 (more than one CDP call to do the obvious
thing).

| verb | raw form tried (exact) | result | hostile under the rule | outcome | standing |
|---|---|---|---|---|---|
| `eval` | `bt 148-01 Runtime.evaluate '{"expression": "document.querySelectorAll('"'"'a'"'"').length", "returnByValue": true}'` | works, `{"result": {"type": "number", "value": 1}}` | **yes, clause 1** - JavaScript inside JSON inside a shell line, the rule's own example. Every quote in the JS has to survive two layers | **curated verb**: `bt eval '<js>' [--await] [--target SPEC \| --url SUB]`. `returnByValue` defaults on, an IIFE wrapper for multi-statement JS, and exit 1 on `exceptionDetails` | observed |
| `eval` (defect) | `bt 148-01 Runtime.evaluate '{"expression": "nope.nope", "returnByValue": true}'` | `exceptionDetails` with `ReferenceError`, **exit 0** | n/a | the curated verb fixes this; the raw form reports a thrown exception as success | observed |
| `scroll` | `bt 148-01 Input.dispatchMouseEvent '{"type": "mouseWheel", "x": 100, "y": 300, "deltaX": 0, "deltaY": 600}'` | works, `scrollY` 0 to 600 | no - one CDP call, no JavaScript | **recipe** | observed |
| `scroll bottom` / `top` | `bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "End", "code": "End", "windowsVirtualKeyCode": 35, "nativeVirtualKeyCode": 35}'` | works, `scrollY` 3894 of 4363 | no - one CDP call | **recipe**. `window.scrollTo(0, 0)` through `Runtime.evaluate` also works and is clause 1; the key form is the non-hostile one | observed |
| `press` | `bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "Enter"}'` then `... '{"type": "keyUp", ...}'` | the minimal form fires the page's `keydown` handler but produces **no default action** (no form submit). Adding `code`, `windowsVirtualKeyCode`, `nativeVirtualKeyCode` and `text` to the keyDown submits the form. A `keyup` listener still needs the second call | **yes, clause 2** - a complete key press is keyDown plus keyUp. Also a per-key virtual-key-code table the agent has to know | **curated verb**: `bt press <key> [--modifiers ...]` | observed |
| `press` (defect) | the GUIDE's TYPE KEYSTROKES recipe, `'{"type": "keyDown", "key": "Enter"}'` | fires the handler, does not submit | n/a | The Manual's recipe is incomplete whichever way the verb decision goes | observed |
| `type` (text into the focused field) | `bt 148-01 Input.insertText '{"text": "TYPED"}'` | works, appends at the caret (`seed` to `seedTYPED`), fires **no** key events (the fixture's `keydown` recorder stayed at `NO-KEYDOWN`) | no - one CDP call, no JavaScript | **recipe**, next to `fill --uid` | observed |
| `type` (real key events, per character) | `Input.dispatchKeyEvent` keyDown plus keyUp per character | works (`q` reached both the handler and the value) | **yes, clause 2** - two CDP calls per character | **curated verb if wanted**, otherwise a named drop. Which of axi's 231 `type` uses needed real key events is unverified | observed (mechanics), unverified (which shape past use needed) |
| `upload` | `bt 148-01 DOM.setFileInputFiles '{"files": ["/abs/path"], "backendNodeId": 4}'` | works, `files.length` 1, name `upload.txt` | no - one CDP call, the backendNodeId is the number after the dash in the snapshot UID | **recipe**, already in The Manual under UPLOAD A FILE | observed |
| `pages` | `bt status 148-01` | works, lists every page target with index, 8-char id, full id, URL and title | no - not a CDP call at all | **no gap**: the curated verb exists. `Target.getTargets '{}' --target 1` also works but returns `browser_ui` targets as noise | observed |
| `newpage` | `bt 148-01 Target.createTarget '{"url": "http://127.0.0.1:17490/second.html", "newWindow": true}'` | works, returns the targetId, tab appears in `status` | no - one CDP call | **recipe**, already in The Manual | observed |
| `selectpage` | none exists, and none is needed: `--target SPEC` / `--url SUBSTRING` per invocation, or once per run | `bt 148-01 Runtime.evaluate '{...}' --url second.html` and `--target 2` both reached the second page | no | **no verb**: there is no mode to change. But see R4 - the *default* when the flag is absent is a silent pick in curated verbs and in `run`, and a refusal in the raw passthrough. Manual defect | observed |
| `closepage` | `bt 148-01 stop --target 2` | works, `Closed tab FC0471B9 in 148-01` | no | **no gap**: the curated verb exists | observed |
| `console` | `bt 148-01 console-list --duration 2` | works, and returns the three messages the page logged **before** the command attached; Chrome replays buffered console messages on enable | no | **no gap**. The retroactive buffer axi's long-lived bridge kept is matched here. The Manual should say the replay happens, because the `--duration` wording reads as "only what happens now" | observed |
| `console-get` | no raw form needed | `console-list` returns each message whole (`type`, `text`, `timestamp`); there is no per-message id to fetch one by | no | **no verb, no recipe**: there is nothing to fetch. axi's `--type` filter maps to a pipe | observed |
| `network` | `bt 148-01 network-list --duration 2` | with the page loaded and quiet: `[]`. With `Network.enable` and `Page.reload` as earlier steps of one run: the document row and the `/data.json` fetch row | no, as a single capture | **recipe** for the in-run shape, with a warning: `network-list` is a window, not a buffer, and the traffic has to happen inside it. A Step Run cannot navigate *during* a step that is already running, so "load this page and show me every request" has no single-invocation form | observed |
| `network-get` | `bt 148-01 Network.getResponseBody '{"requestId": "F0CAB589ACBDF60CFA428FF679111DD2"}'` (a real id from the previous invocation) | `error: CDP error -32000: No resource with given identifier found` (exit 1). Inside one run the requestId cannot travel from the list step to the body step | **yes, clause 2**, and past it: see R3, there is no raw form at all | **curated verb**: response-body capture that enables, collects and fetches in one invocation, for example `bt network-list --bodies` or `bt network-get --url SUB --response-file PATH` | observed |
| `resize` | `bt 148-01 Emulation.setDeviceMetricsOverride '{"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": true}'` | works, and a **separate** invocation reads `[390, 844, 1]`, so the override outlives its session | no - one CDP call | **recipe**, already in The Manual | observed |
| `emulate --viewport` | same call as `resize` | same | no | **recipe** | observed |
| `emulate --network` | `bt 148-01 Network.emulateNetworkConditions '{"offline": true, ...}'` | the call succeeds, and the next invocation is online again. Only inside one run does the page go offline | no by the rule's letter, **but the rule does not decide it**: see R1 | **run-step recipe**, documented as dying with the invocation | observed |
| `emulate --cpu` | `bt 148-01 Emulation.setCPUThrottlingRate '{"rate": 20}'` | no measurable effect in the next invocation (3,064,220 vs 3,096,271 baseline spin iterations); 147,273 inside one run | as above | **run-step recipe** | observed |
| `emulate --geolocation` | `bt 148-01 Emulation.setGeolocationOverride '{"latitude": 37.7749, "longitude": -122.4194, "accuracy": 10}'`, with `Browser.grantPermissions` sent first | `ERR 1 User denied Geolocation` in a separate invocation even after the grant; `[48.8566, 2.3522]` when grant, override and read are three steps of one run | **clause 2 in practice** - the permission grant and the override have to be in the same invocation as the read | **run-step recipe** (three steps), documented | observed |
| `emulate --color-scheme` | `bt 148-01 Emulation.setEmulatedMedia '{"features": [{"name": "prefers-color-scheme", "value": "light"}]}'` | `false` in a separate invocation, `true` inside one run | as R1 | **run-step recipe** | observed |
| `emulate --user-agent` | `bt 148-01 Emulation.setUserAgentOverride '{"userAgent": "bt-verb-gap-probe/1.0"}'` | the real UA in a separate invocation, the probe UA inside one run | as R1 | **run-step recipe** | observed |
| `hover` | `bt 148-01 DOM.getBoxModel '{"backendNodeId": 48}'` then `bt 148-01 Input.dispatchMouseEvent '{"type": "mouseMoved", "x": 62, "y": 188, "button": "none"}'` | works: the handler fired (`HOVERED`) **and** the CSS `:hover` rule applied (`rgb(0, 170, 0)`). The state survives into later invocations | **yes, clause 2** - two CDP calls with arithmetic between them, and the arithmetic cannot happen inside a run because no step reads another step's output | **curated verb**: `bt hover --uid UID` | observed |
| `hover` (why eval is not a substitute) | `bt 148-01 Runtime.evaluate '{"expression": "...dispatchEvent(new MouseEvent(\"mouseover\", {bubbles: true}))..."}'` | fires the JS handler (`HOVERED`) but leaves the CSS `:hover` state alone (`rgb(221, 221, 221)`) | n/a | a synthetic event is not a pointer; `eval` does not close this gap | observed |
| `fillform` | `printf 'fill --uid ...-2 --text alpha\nfill --uid ...-3 --text beta\n' \| bt 148-01 run -` | works, one invocation, both fields set, each step's result in the Run Document. A value with a space quotes as at the prompt: `fill --uid ...-3 --text "two words"` | no - `bt run` already is this verb | **recipe** | observed |
| `dialog` | see R2 | handled inside one run; deadlocks when the raising step blocks | **yes, clause 2** - open, wait, handle are three calls, and they must share one session | **curated verb is not sufficient**: the decision is whether `click` gets a non-blocking mode or bt answers the dialog event itself. Until then: run-step recipe with a deferred trigger | observed |
| `wait <ms>` | `sleep 2` in the shell | works, zero CDP calls | no | **recipe** outside a run. **Inside a run there is no sleep step**: `wait --event Nothing.happensHere --timeout 2` fails the whole run (exit 1, `completed: 0`), and `console-list --duration 3` is the fixed-duration wait that exists (measured: the run took 3s) | observed |
| `wait <text>` | `bt 148-01 Runtime.evaluate '{"expression": "new Promise((res, rej) => { const t = \"LATE TEXT APPEARED\"; ... new MutationObserver(...) ... setTimeout(() => rej(...), 5000); })", "returnByValue": true, "awaitPromise": true}'` | works, returns `"appeared"`. One CDP call - and roughly 400 characters of JavaScript inside JSON inside a shell line | **yes, clause 1**, at its worst | **curated verb**: `bt wait-text "SUBSTRING" [--timeout-ms MS]`, beside `wait-idle` and `wait-stable`. The alternative is a shell polling loop of separate invocations (observed: found after 4 polls) | observed |
| axi `run` vs bt `run` | `printf 'Runtime.evaluate ...\nclick --uid THERE-IS-NO-WAY-TO-PUT-THE-ANSWER-HERE\n' \| bt 148-01 run -` | step 1 returns `3`; step 2 cannot use it and fails | n/a - not a verb gap | **guide section, no verb.** See below | observed |

### axi `run` against bt `run`, in full

They are different execution models, not two versions of one verb.

| | axi `run` | bt `run` |
|---|---|---|
| what it takes | a JavaScript program on stdin, with a global `page` object | a Step List: verb phrases, one per line |
| variables, conditions, loops | yes, it is JavaScript | no, stated in CONTEXT.md and The Manual |
| one step reading another's output | yes | no |
| what comes back | only the script's stdout | the Run Document: one entry per step attempted, each carrying what that step prints alone |
| failure | the script throws | stops at the first failing step, prints the document anyway, exit 1 |
| page API | `open`, `eval`, `snapshot`, `wait(ms\|selector)`, `click(@uid\|selector)`, `fill`, `type`, `press`, `back` | every curated verb plus any raw `Domain.method` |

Standing: documented for both columns (axi `run --help`;
`src/browser_tools/GUIDE.txt` "RUNNING MANY STEPS IN ONE INVOCATION" and
`CONTEXT.md` "Step List"), observed for the bt column.

The only capability axi `run` has that bt `run` does not is carrying a value
from one step into the next. The substitute is to put the whole decision into
one `Runtime.evaluate`:

    bt 148-01 Runtime.evaluate '{"expression": "(() => { const n = document.querySelectorAll(\"input\").length; if (n > 2) { document.getElementById(\"go\").click(); return \"clicked, inputs=\" + n; } return \"skipped, inputs=\" + n; })()", "returnByValue": true}'
    -> {"result": {"type": "string", "value": "clicked, inputs=3"}}

Observed. Which is exactly the clause-1 shape that the curated `eval` fixes, so
axi `run` needs no verb of its own: it needs `eval` plus a guide section saying
"a decision goes inside one eval, or you read the output and run again".

---

## The dialog result, in full

The brief asks for this one separately. Four runs, all against `148-01`.

**1. Opened in step 1, handled in step 3, same run. Works.**
Step 1 `Runtime.evaluate` with `setTimeout(() => { window.__dlg =
String(confirm("CONFIRM-FIXTURE")); }, 1500)`. Step 2 `wait --event
Page.javascriptDialogOpening --timeout 10` returned the event with
`"message": "CONFIRM-FIXTURE", "type": "confirm"`. Step 3
`Page.handleJavaScriptDialog '{"accept": true}'` returned `{}`. Step 4 read
`window.__dlg` = `"true"`. `run.status` `ok`, exit 0.

**2. Raised by a real click, same run. Deadlocks.**
Step 1 `click --uid AB5DFB3B758F-200` (the "Raise alert" button, whose
`onclick` calls `alert()` synchronously). The step never returned. The run hit
its 20s deadline with `completed: 0`, `status: timeout`, exit 1, and stderr
`call_native(click) error:` followed by `click did not answer in time`. Steps
2 to 4 never ran. The alert was left open.

**3. The left-open dialog, from a new invocation. Refused, as documented.**
`bt 148-01 Page.handleJavaScriptDialog '{"accept": true}'` gave
`error: CDP error -32602: No dialog is showing`, exit 1. `Page.navigate` back
to the fixture cleared it and the page answered again. Both behaviours are
already in The Manual under WHAT DOES NOT CARRY BETWEEN INVOCATIONS; this
re-confirms them on Chrome 153.

**4. The deferred-trigger workaround. Works, including prompt text.**
Step 1 `Runtime.evaluate` with `setTimeout(() =>
document.getElementById("promptbtn").click(), 300)`. Step 3
`Page.handleJavaScriptDialog '{"accept": true, "promptText": "typed-answer"}'`.
Step 4 read `window.__dialogResult` = `"typed-answer"`. The same shape with
`{"accept": false}` on the confirm button gave `"false"`. Both exit 0.

**The answer to #147's open question:** bt can handle a JavaScript dialog
inside a Step Run, provided the step that raises it returns before the dialog
has to be answered. `click --uid` does not, which is the shape agents reach
for.

---

## Left open

1. **Whether `type` needed real key events.** Input.insertText covers "put this
   text in the focused field" in one call. axi's `type` used puppeteer's
   keyboard, which fires real key events per character and so drives search
   autocompletes and key handlers. Which of the 231 Codex `type` invocations
   needed that is unverified here; deciding it needs the transcripts, not a
   browser.
2. **What to do about the blocking click.** R2 names two candidate fixes (a
   non-blocking click mode, or bt answering `Page.javascriptDialogOpening`
   itself with a run-level policy). Both are product decisions outside a
   research ticket, and neither was prototyped.
3. **Whether the curated set should be decided with `eval` in hand.** R5 says
   the verb-by-verb judgement double-counts. Re-running the rule against a bt
   that already has `eval` would change at least the `wait <text>` row. Not
   done here, because #148 asks for the judgement against today's raw CDP.
4. **`--frames all` was not exercised.** Every command here ran against the
   page's own frames. Whether `hover`, `press`, `type` and the emulation
   overrides behave the same inside an out-of-process frame is unverified.
5. **Camoufox was not exercised.** Chrome only. Whether `Input.*` and
   `Emulation.*` behave the same on the Camoufox engine is unverified.
6. **A registry row that is not mine disappeared.** `bt status` at the start
   listed `graybox-01` (port 9225, profile `graybox-proof-2`, alive). By the
   time this session stopped its own first instance, that row read
   `alive: false` with no targets; this session had sent nothing to port 9225
   at that point. A later `bt launch` here was assigned port 9225 by
   `allocate_port`, which skips only ports held by live entries, and after that
   instance was stopped the `graybox-01` row was gone from the registry. This
   session never ran `bt cleanup` and never stopped an instance other than its
   own `148-01`. What removed the row is not established: another session was
   active in this repo throughout (`153-01`, `153-02`, `153-03` and
   `worker-a-01` appeared during the run). No live browser was stopped by this
   session.

## Files

- Findings (this file):
  `/Users/kevin/dev/browser-tools/.scratch/retire-axi/findings/148-verb-gaps.md`
- Raw transcript, same content as the appendix below:
  `/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148/transcript.log`
- Fixture page and the batch scripts that produced the transcript:
  `/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148/`

---

## Appendix: full transcript

Every command this session ran against the browser, in order, with its output
and exit code. `148-01` is this session's own headless instance, stopped at the
end. The fixture server on `127.0.0.1:17490` was a `python3 -m http.server`
started and killed in the same work directory; it is transient, so no row was
added to `~/.agents/PORTS.md`.

```text

### setup: navigate to fixture
$ bt 148-01 Page.navigate '{"url": "http://127.0.0.1:17490/fixture.html"}'
{
  "frameId": "E0195465CF309F500271FDB23C5BA8B3",
  "loaderId": "D2FA83F9E0F6232AD7A21235757A8F6E",
  "isDownload": false
}
[exit 0]

### snapshot: UIDs for the fixture
$ bt 148-01 snapshot
{
  "snapshot": "[uid=A16B214C022B-5] RootWebArea \"bt verb-gap fixture\"\n  [uid=A16B214C022B-8] heading \"bt verb-gap fixture\"\n    [uid=A16B214C022B-9] StaticText \"bt verb-gap fixture\"\n      [uid=A16B214C022B-x6] InlineTextBox \"bt verb-gap fixture\"\n  [uid=A16B214C022B-1] form\n    [uid=A16B214C022B-44] LabelText\n      [uid=A16B214C022B-10] StaticText \"One \"\n        [uid=A16B214C022B-x10] InlineTextBox \"One \"\n      [uid=A16B214C022B-2] textbox \"One \"\n        [uid=A16B214C022B-11] generic\n    [uid=A16B214C022B-45] LabelText\n      [uid=A16B214C022B-12] StaticText \"Two \"\n        [uid=A16B214C022B-x15] InlineTextBox \"Two \"\n      [uid=A16B214C022B-3] textbox \"Two \"\n        [uid=A16B214C022B-13] generic\n    [uid=A16B214C022B-46] LabelText\n      [uid=A16B214C022B-14] StaticText \"File \"\n        [uid=A16B214C022B-x20] InlineTextBox \"File \"\n      [uid=A16B214C022B-4] button \"File \" = 'No file chosen'\n    [uid=A16B214C022B-18] button \"Go\"\n      [uid=A16B214C022B-19] StaticText \"Go\"\n        [uid=A16B214C022B-x24] InlineTextBox \"Go\"\n  [uid=A16B214C022B-20] paragraph\n  [uid=A16B214C022B-21] paragraph\n    [uid=A16B214C022B-48] generic\n      [uid=A16B214C022B-22] StaticText \"Hover me\"\n        [uid=A16B214C022B-x29] InlineTextBox \"Hover me\"\n    [uid=A16B214C022B-49] generic\n      [uid=A16B214C022B-23] StaticText \"no-hover\"\n        [uid=A16B214C022B-x32] InlineTextBox \"no-hover\"\n  [uid=A16B214C022B-24] paragraph\n    [uid=A16B214C022B-25] button \"Raise alert\"\n      [uid=A16B214C022B-26] StaticText \"Raise alert\"\n        [uid=A16B214C022B-x36] InlineTextBox \"Raise alert\"\n    [uid=A16B214C022B-27] button \"Raise confirm\"\n      [uid=A16B214C022B-28] StaticText \"Raise confirm\"\n        [uid=A16B214C022B-x39] InlineTextBox \"Raise confirm\"\n    [uid=A16B214C022B-29] button \"Raise prompt\"\n      [uid=A16B214C022B-30] StaticText \"Raise prompt\"\n        [uid=A16B214C022B-x42] InlineTextBox \"Raise prompt\"\n  [uid=A16B214C022B-7] paragraph\n    [uid=A16B214C022B-31] link \"Open second page in a tab\"\n      [uid=A16B214C022B-32] StaticText \"Open second page in a tab\"\n        [uid=A16B214C022B-x46] InlineTextBox \"Open second page in a tab\"\n  [uid=A16B214C022B-33] paragraph\n    [uid=A16B214C022B-34] StaticText \"LATE TEXT APPEARED\"\n      [uid=A16B214C022B-x49] InlineTextBox \"LATE TEXT APPEARED\"\n  [uid=A16B214C022B-6] generic\n    [uid=A16B214C022B-35] StaticText \"scroll region\"\n      [uid=A16B214C022B-x52] InlineTextBox \"scroll region\"\n  [uid=A16B214C022B-36] paragraph\n    [uid=A16B214C022B-37] StaticText \"BOTTOM MARKER\"\n      [uid=A16B214C022B-x55] InlineTextBox \"BOTTOM MARKER\""
}
[exit 0]

### eval: simplest raw form, no quotes inside the JS
$ bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "bt verb-gap fixture"
  }
}
[exit 0]

### eval: JS carrying a single quote (axi: eval "document.querySelectorAll(\047a\047).length")
$ bt 148-01 Runtime.evaluate '{"expression": "document.querySelectorAll('"'"'a'"'"').length", "returnByValue": true}'
{
  "result": {
    "type": "number",
    "value": 1,
    "description": "1"
  }
}
[exit 0]

### eval: same, double quotes inside the JS instead (needs JSON backslash escaping)
$ bt 148-01 Runtime.evaluate '{"expression": "document.querySelectorAll(\"a\").length", "returnByValue": true}'
{
  "result": {
    "type": "number",
    "value": 1,
    "description": "1"
  }
}
[exit 0]

### eval: without returnByValue, an object comes back as a remote handle
$ bt 148-01 Runtime.evaluate '{"expression": "({a: 1, b: 2})"}'
{
  "result": {
    "type": "object",
    "className": "Object",
    "description": "Object",
    "objectId": "7421604669216475113.1.1"
  }
}
[exit 0]

### eval: with returnByValue, the object comes back as a value
$ bt 148-01 Runtime.evaluate '{"expression": "({a: 1, b: 2})", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": {
      "a": 1,
      "b": 2
    }
  }
}
[exit 0]

### eval: multi-statement IIFE, the axi "() => {...}" shape
$ bt 148-01 Runtime.evaluate '{"expression": "(() => { const rows = [...document.querySelectorAll(\"p\")]; return rows.map(r => r.textContent); })()", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      "",
      "Hover me no-hover",
      "\n  Raise alert\n  Raise confirm\n  Raise prompt\n",
      "Open second page in a tab",
      "LATE TEXT APPEARED",
      "BOTTOM MARKER"
    ]
  }
}
[exit 0]

### eval: a JS exception is reported in exceptionDetails, exit 0
$ bt 148-01 Runtime.evaluate '{"expression": "nope.nope", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "subtype": "error",
    "className": "ReferenceError",
    "description": "ReferenceError: nope is not defined\n    at <anonymous>:1:1",
    "objectId": "7421604669216475113.1.1"
  },
  "exceptionDetails": {
    "exceptionId": 1,
    "text": "Uncaught",
    "lineNumber": 0,
    "columnNumber": 0,
    "scriptId": "9",
    "exception": {
      "type": "object",
      "subtype": "error",
      "className": "ReferenceError",
      "description": "ReferenceError: nope is not defined\n    at <anonymous>:1:1",
      "objectId": "7421604669216475113.1.2"
    }
  }
}
[exit 0]

### eval: awaitPromise for async JS
$ bt 148-01 Runtime.evaluate '{"expression": "fetch(\"/data.json\").then(r => r.json())", "returnByValue": true, "awaitPromise": true}'
{
  "result": {
    "type": "object",
    "value": {
      "ok": true,
      "n": 42
    }
  }
}
[exit 0]

### scroll: read the starting scrollY
$ bt 148-01 Runtime.evaluate '{"expression": "window.scrollY", "returnByValue": true}'
{
  "result": {
    "type": "number",
    "value": 0,
    "description": "0"
  }
}
[exit 0]

### scroll down: one mouseWheel dispatch, no JS
$ bt 148-01 Input.dispatchMouseEvent '{"type": "mouseWheel", "x": 100, "y": 300, "deltaX": 0, "deltaY": 600}'
{}
[exit 0]

### scroll: scrollY after the wheel
$ bt 148-01 Runtime.evaluate '{"expression": "window.scrollY", "returnByValue": true}'
{
  "result": {
    "type": "number",
    "value": 600,
    "description": "600"
  }
}
[exit 0]

### scroll bottom: one Input.dispatchKeyEvent End press (keyDown only)
$ bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "End", "code": "End", "windowsVirtualKeyCode": 35, "nativeVirtualKeyCode": 35}'
{}
[exit 0]

### scroll: scrollY after End
$ bt 148-01 Runtime.evaluate '{"expression": "[window.scrollY, document.body.scrollHeight]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      3894,
      4363
    ]
  }
}
[exit 0]

### scroll top: the JS form (JavaScript inside JSON inside a shell line)
$ bt 148-01 Runtime.evaluate '{"expression": "window.scrollTo(0, 0)", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": {}
  }
}
[exit 0]

### scroll: scrollY after scrollTo(0,0)
$ bt 148-01 Runtime.evaluate '{"expression": "window.scrollY", "returnByValue": true}'
{
  "result": {
    "type": "number",
    "value": 0,
    "description": "0"
  }
}
[exit 0]

### type: focus the field first with the curated fill, then insertText
$ bt 148-01 fill --uid A16B214C022B-2 --text seed
{
  "uid": "A16B214C022B-2",
  "text": "seed",
  "result": "Filled uid=A16B214C022B-2 (value now 'seed')."
}
[exit 0]

### type: Input.insertText at the focused element, one call
$ bt 148-01 Input.insertText '{"text": "TYPED"}'
{}
[exit 0]

### type: read the field value back
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"one\").value", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "seedTYPED"
  }
}
[exit 0]

### press Enter: keyDown alone (the GUIDE recipe), does it reach the handler?
$ bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "Enter"}'
{}
[exit 0]

### press: what the keydown handler recorded
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "KEYDOWN:Enter"
  }
}
[exit 0]

### press Enter: the full keyDown/keyUp pair with the virtual key code, step 1
$ bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r"}'
{}
[exit 0]

### press Enter: step 2, keyUp
$ bt 148-01 Input.dispatchKeyEvent '{"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}'
{}
[exit 0]

### press: did the form submit (submit handler writes SUBMITTED:...)
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "SUBMITTED:seedTYPED|"
  }
}
[exit 0]

### press: reset the page state and refocus the field
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent = \"\"; document.getElementById(\"one\").value = \"x\"; document.getElementById(\"one\").focus(); \"reset\"", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "reset"
  }
}
[exit 0]

### press: keyDown alone WITH the virtual key code and text, no keyUp
$ bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r"}'
{}
[exit 0]

### press: did that one call alone submit the form?
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "SUBMITTED:x|"
  }
}
[exit 0]

### type: does Input.insertText fire key events? reset the recorder first
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"submitted\").textContent = \"NO-KEYDOWN\"; document.getElementById(\"one\").focus(); \"reset\"", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "reset"
  }
}
[exit 0]

### type: insertText into the focused field
$ bt 148-01 Input.insertText '{"text": "abc"}'
{}
[exit 0]

### type: did the keydown handler fire during insertText?
$ bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"submitted\").textContent, document.getElementById(\"one\").value]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      "NO-KEYDOWN",
      "xabc"
    ]
  }
}
[exit 0]

### type: char-by-char real keys, the puppeteer shape, is 2 CDP calls per character
$ bt 148-01 Input.dispatchKeyEvent '{"type": "keyDown", "key": "q", "code": "KeyQ", "windowsVirtualKeyCode": 81, "nativeVirtualKeyCode": 81, "text": "q"}'
{}
[exit 0]

### type: and its keyUp
$ bt 148-01 Input.dispatchKeyEvent '{"type": "keyUp", "key": "q", "code": "KeyQ", "windowsVirtualKeyCode": 81, "nativeVirtualKeyCode": 81}'
{}
[exit 0]

### type: value and keydown record after the real key
$ bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"submitted\").textContent, document.getElementById(\"one\").value]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      "KEYDOWN:q",
      "xabcq"
    ]
  }
}
[exit 0]

### hover: scroll back to the top so the target is in the viewport
$ bt 148-01 Runtime.evaluate '{"expression": "window.scrollTo(0,0); document.getElementById(\"hoverstate\").textContent", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "no-hover"
  }
}
[exit 0]

### hover: step 1 of 2, box model for the UID backendNodeId 48 (span#hovertarget)
$ bt 148-01 DOM.getBoxModel '{"backendNodeId": 48}'
{
  "model": {
    "content": [
      28,
      179.875,
      97.671875,
      179.875,
      97.671875,
      197.875,
      28,
      197.875
    ],
    "padding": [
      16,
      167.875,
      109.671875,
      167.875,
      109.671875,
      209.875,
      16,
      209.875
    ],
    "border": [
      16,
      167.875,
      109.671875,
      167.875,
      109.671875,
      209.875,
      16,
      209.875
    ],
    "margin": [
      16,
      167.875,
      109.671875,
      167.875,
      109.671875,
      209.875,
      16,
      209.875
    ],
    "width": 94,
    "height": 42
  }
}
[exit 0]

### hover: step 2 of 2, mouseMoved to the box centre computed from step 1
$ bt 148-01 Input.dispatchMouseEvent '{"type": "mouseMoved", "x": 62, "y": 188, "button": "none"}'
{}
[exit 0]

### hover: did the mouseover handler and the :hover rule take effect?
$ bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      "HOVERED",
      "rgb(0, 170, 0)"
    ]
  }
}
[exit 0]

### hover: does the :hover state survive into a separate invocation?
$ bt 148-01 Runtime.evaluate '{"expression": "getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "rgb(0, 170, 0)"
  }
}
[exit 0]

### hover: the whole thing as one bt run (2 steps, one invocation) - still needs the coordinates from step 1
$ printf 'DOM.getBoxModel {"backendNodeId": 48}\nInput.dispatchMouseEvent {"type": "mouseMoved", "x": 62, "y": 188, "button": "none"}\n' | bt 148-01 run -
error: line 1: 'DOM.getBoxModel' takes at most one JSON params argument
[exit 2]

### upload: the GUIDE recipe, one CDP call with the backendNodeId out of the UID
$ bt 148-01 DOM.setFileInputFiles '{"files": ["/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148/upload.txt"], "backendNodeId": 4}'
{}
[exit 0]

### upload: read the file input back
$ bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"file\").files.length, document.getElementById(\"file\").files[0] && document.getElementById(\"file\").files[0].name]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      1,
      "upload.txt"
    ]
  }
}
[exit 0]

### pages: bt status already lists the page targets
$ bt status 148-01
[
  {
    "name": "148-01",
    "port": 9228,
    "alive": true,
    "engine": "chrome",
    "profile": null,
    "supervisor": null,
    "targets": [
      {
        "id": "E0195465",
        "full_id": "E0195465CF309F500271FDB23C5BA8B3",
        "index": 1,
        "url": "http://127.0.0.1:17490/fixture.html",
        "title": "bt verb-gap fixture"
      }
    ]
  }
]
[exit 0]

### newpage: one CDP call (GUIDE recipe)
$ bt 148-01 Target.createTarget '{"url": "http://127.0.0.1:17490/second.html", "newWindow": true}'
{
  "targetId": "FC0471B950D8CACFCDB6E3359C281753"
}
[exit 0]

### pages: status after the new tab
$ bt status 148-01
[
  {
    "name": "148-01",
    "port": 9228,
    "alive": true,
    "engine": "chrome",
    "profile": null,
    "supervisor": null,
    "targets": [
      {
        "id": "E0195465",
        "full_id": "E0195465CF309F500271FDB23C5BA8B3",
        "index": 1,
        "url": "http://127.0.0.1:17490/fixture.html",
        "title": "bt verb-gap fixture"
      },
      {
        "id": "FC0471B9",
        "full_id": "FC0471B950D8CACFCDB6E3359C281753",
        "index": 2,
        "url": "http://127.0.0.1:17490/second.html",
        "title": "second page"
      }
    ]
  }
]
[exit 0]

### pages: the raw CDP form, browser-level over a page session
$ bt 148-01 Target.getTargets '{}' --target 1
{
  "targetInfos": [
    {
      "targetId": "E0195465CF309F500271FDB23C5BA8B3",
      "type": "page",
      "title": "bt verb-gap fixture",
      "url": "http://127.0.0.1:17490/fixture.html",
      "attached": true,
      "canAccessOpener": false,
      "browserContextId": "120149EEF71B8E59F5E9C533A4C99EC7"
    },
    {
      "targetId": "A96FB956BFC7B669D669182B3F347ABA",
      "type": "browser_ui",
      "title": "Omnibox Popup",
      "url": "chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html",
      "attached": false,
      "canAccessOpener": false,
      "browserContextId": "120149EEF71B8E59F5E9C533A4C99EC7"
    },
    {
      "targetId": "E4798BC432B2AC7483F8212A954639D3",
      "type": "browser_ui",
      "title": "Omnibox Popup",
      "url": "chrome://omnibox-popup.top-chrome/",
      "attached": false,
      "canAccessOpener": false,
      "browserContextId": "120149EEF71B8E59F5E9C533A4C99EC7"
    },
    {
      "targetId": "CD9A4F87DEC9FA17DBCB4EB3A5C44B58",
      "type": "browser_ui",
      "title": "Omnibox Popup",
      "url": "chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html",
      "attached": false,
      "canAccessOpener": false,
      "browserContextId": "120149EEF71B8E59F5E9C533A4C99EC7"
    },
    {
      "targetId": "7010D37392FD8C5E23C3F0EF278D5C53",
      "type": "browser_ui",
      "title": "Omnibox Popup",
      "url": "chrome://omnibox-popup.top-chrome/",
      "attached": false,
      "canAccessOpener": false,
      "browserContextId": "120149EEF71B8E59F5E9C533A4C99EC7"
    },
    {
      "targetId": "FC0471B950D8CACFCDB6E3359C281753",
      "type": "page",
      "title": "second page",
      "url": "http://127.0.0.1:17490/second.html",
      "attached": false,
      "canAccessOpener": false,
      "browserContextId": "120149EEF71B8E59F5E9C533A4C99EC7"
    }
  ]
}
[exit 0]

### selectpage: there is no mode to change - the page is a per-invocation flag
$ bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}' --url second.html
{
  "result": {
    "type": "string",
    "value": "second page"
  }
}
[exit 0]

### selectpage: same flag by index
$ bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}' --target 2
{
  "result": {
    "type": "string",
    "value": "second page"
  }
}
[exit 0]

### run: a raw step needs its JSON quoted, as at the prompt (my previous unquoted line was my error, not bt refusing the method)
$ printf '%s\n' 'DOM.getBoxModel '"'"'{"backendNodeId": 48}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 1,
    "completed": 1,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "DOM.getBoxModel '{\"backendNodeId\": 48}'",
      "result": {
        "model": {
          "content": [
            28,
            179.875,
            97.671875,
            179.875,
            97.671875,
            197.875,
            28,
            197.875
          ],
          "padding": [
            16,
            167.875,
            109.671875,
            167.875,
            109.671875,
            209.875,
            16,
            209.875
          ],
          "border": [
            16,
            167.875,
            109.671875,
            167.875,
            109.671875,
            209.875,
            16,
            209.875
          ],
          "margin": [
            16,
            167.875,
            109.671875,
            167.875,
            109.671875,
            209.875,
            16,
            209.875
          ],
          "width": 94,
          "height": 42
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### hover: move the pointer away first, so the next test starts un-hovered
$ bt 148-01 Input.dispatchMouseEvent '{"type": "mouseMoved", "x": 600, "y": 600, "button": "none"}'
error: Multiple page targets found. Specify one:
  [1] E0195465  http://127.0.0.1:17490/fixture.html  "bt verb-gap fixture"
  [2] FC0471B9  http://127.0.0.1:17490/second.html  "second page"
[exit 1]

### hover: confirm the :hover rule has dropped
$ bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
error: Multiple page targets found. Specify one:
  [1] E0195465  http://127.0.0.1:17490/fixture.html  "bt verb-gap fixture"
  [2] FC0471B9  http://127.0.0.1:17490/second.html  "second page"
[exit 1]

### hover: the JS substitute - dispatch a synthetic MouseEvent instead of a real pointer move
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"hoverstate\").textContent = \"reset\"; document.getElementById(\"hovertarget\").dispatchEvent(new MouseEvent(\"mouseover\", {bubbles: true})); [document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
error: Multiple page targets found. Specify one:
  [1] E0195465  http://127.0.0.1:17490/fixture.html  "bt verb-gap fixture"
  [2] FC0471B9  http://127.0.0.1:17490/second.html  "second page"
[exit 1]

### fillform: N fields in one invocation is a bt run with N fill steps
$ printf 'fill --uid A16B214C022B-2 --text alpha\nfill --uid A16B214C022B-3 --text beta\n' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "fill --uid A16B214C022B-2 --text alpha",
      "result": {
        "uid": "A16B214C022B-2",
        "text": "alpha",
        "result": "Filled uid=A16B214C022B-2 (value now 'alpha')."
      },
      "status": "ok"
    },
    {
      "index": 2,
      "step": "fill --uid A16B214C022B-3 --text beta",
      "result": {
        "uid": "A16B214C022B-3",
        "text": "beta",
        "result": "Filled uid=A16B214C022B-3 (value now 'beta')."
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### fillform: read both fields back
$ bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"one\").value, document.getElementById(\"two\").value]", "returnByValue": true}'
error: Multiple page targets found. Specify one:
  [1] E0195465  http://127.0.0.1:17490/fixture.html  "bt verb-gap fixture"
  [2] FC0471B9  http://127.0.0.1:17490/second.html  "second page"
[exit 1]

### fillform: a value with a space, quoted in the step list the way a shell quotes it
$ printf '%s\n' 'fill --uid A16B214C022B-3 --text "two words"' | bt 148-01 run -
{
  "run": {
    "steps": 1,
    "completed": 1,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "fill --uid A16B214C022B-3 --text \"two words\"",
      "result": {
        "uid": "A16B214C022B-3",
        "text": "two words",
        "result": "Filled uid=A16B214C022B-3 (value now 'two words')."
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### fillform: read the spaced value back
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"two\").value", "returnByValue": true}'
error: Multiple page targets found. Specify one:
  [1] E0195465  http://127.0.0.1:17490/fixture.html  "bt verb-gap fixture"
  [2] FC0471B9  http://127.0.0.1:17490/second.html  "second page"
[exit 1]

### run vs bare verb with two pages open: which page does a run with no --target drive?
$ printf '%s\n' 'Runtime.evaluate '"'"'{"expression": "document.title", "returnByValue": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 1,
    "completed": 1,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Runtime.evaluate '{\"expression\": \"document.title\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "bt verb-gap fixture"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### run vs bare verb: the same call as a bare verb refuses to guess
$ bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
error: Multiple page targets found. Specify one:
  [1] E0195465  http://127.0.0.1:17490/fixture.html  "bt verb-gap fixture"
  [2] FC0471B9  http://127.0.0.1:17490/second.html  "second page"
[exit 1]

### closepage: the curated form is stop --target
$ bt 148-01 stop --target 2
{
  "stopped": true,
  "message": "Closed tab FC0471B9 in 148-01"
}
[exit 0]

### closepage: status after
$ bt status 148-01
[
  {
    "name": "148-01",
    "port": 9228,
    "alive": true,
    "engine": "chrome",
    "profile": null,
    "supervisor": null,
    "targets": [
      {
        "id": "E0195465",
        "full_id": "E0195465CF309F500271FDB23C5BA8B3",
        "index": 1,
        "url": "http://127.0.0.1:17490/fixture.html",
        "title": "bt verb-gap fixture"
      }
    ]
  }
]
[exit 0]

### console: the page logged two lines at load. Does console-list see them afterwards?
$ bt 148-01 console-list --duration 2
[
  {
    "type": "log",
    "text": "fixture console log line",
    "timestamp": 1790071797547.116
  },
  {
    "type": "error",
    "text": "fixture console error line",
    "timestamp": 1790071797547.135
  },
  {
    "type": "log",
    "text": "fetched true",
    "timestamp": 1790071797556.419
  }
]
[exit 0]

### console: the same window, but with the page reloading inside it (bt run, one invocation)
$ printf 'Page.reload '"'"'{}'"'"'\nconsole-list --duration 2\n' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Page.reload '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "console-list --duration 2",
      "result": [
        {
          "type": "log",
          "text": "fixture console log line",
          "timestamp": 1790072041461.74
        },
        {
          "type": "error",
          "text": "fixture console error line",
          "timestamp": 1790072041461.766
        },
        {
          "type": "log",
          "text": "fetched true",
          "timestamp": 1790072041463.39
        }
      ],
      "status": "ok"
    }
  ]
}
[exit 0]

### console-get: there is no per-message id to fetch; console-list carries the whole message
$ bt 148-01 console-list --duration 1 | head -40
[
  {
    "type": "log",
    "text": "fixture console log line",
    "timestamp": 1790072041461.74
  },
  {
    "type": "error",
    "text": "fixture console error line",
    "timestamp": 1790072041461.766
  },
  {
    "type": "log",
    "text": "fetched true",
    "timestamp": 1790072041463.39
  }
]
[exit 0]

### network: network-list over a window with a reload inside it
$ printf 'Page.reload '"'"'{}'"'"'\nnetwork-list --duration 3\n' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Page.reload '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "network-list --duration 3",
      "result": [
        {
          "requestId": "F0CAB589ACBDF60CFA428FF679111DD2",
          "method": null,
          "url": "http://127.0.0.1:17490/fixture.html",
          "resourceType": "Document",
          "status": 200,
          "statusText": "OK"
        }
      ],
      "status": "ok"
    }
  ]
}
[exit 0]

### network-get: a response body needs the requestId AND the Network domain on in the same session. Try it alone, with a requestId from the list above.
$ bt 148-01 Network.getResponseBody '{"requestId": "PLACEHOLDER"}'
error: CDP error -32000: No resource with given identifier found
[exit 1]

### network-get: a REAL requestId from the previous invocation, fetched in a new one
$ bt 148-01 Network.getResponseBody '{"requestId": "F0CAB589ACBDF60CFA428FF679111DD2"}'
error: CDP error -32000: No resource with given identifier found
[exit 1]

### network: with the page already loaded and quiet, network-list sees nothing - it is a window, not a buffer
$ bt 148-01 network-list --duration 2
[]
[exit 0]

### console: with the page already loaded and quiet, console-list DOES see the old messages - Chrome replays them on enable
$ bt 148-01 console-list --duration 1
[
  {
    "type": "log",
    "text": "fixture console log line",
    "timestamp": 1790072044765.31
  },
  {
    "type": "error",
    "text": "fixture console error line",
    "timestamp": 1790072044765.342
  },
  {
    "type": "log",
    "text": "fetched true",
    "timestamp": 1790072044766.354
  }
]
[exit 0]

### network-get inside one run: enable, reload, list, then body - the requestId cannot be carried from the list step to the body step
$ printf '%s\n' 'Network.enable '"'"'{}'"'"'' 'Page.reload '"'"'{}'"'"'' 'network-list --duration 3' 'Network.getResponseBody '"'"'{"requestId": "CANNOT-BE-FILLED-IN-FROM-A-PREVIOUS-STEP"}'"'"'' | bt 148-01 run -
error: step 4 failed: Network.getResponseBody '{"requestId": "CANNOT-BE-FILLED-IN-FROM-A-PREVIOUS-STEP"}'
CDP error -32000: No resource with given identifier found
{
  "run": {
    "steps": 4,
    "completed": 3,
    "status": "failed"
  },
  "steps": [
    {
      "index": 1,
      "step": "Network.enable '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Page.reload '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 3,
      "step": "network-list --duration 3",
      "result": [
        {
          "requestId": "8542D97E6966EECAD79795536101DAB2",
          "method": null,
          "url": "http://127.0.0.1:17490/fixture.html",
          "resourceType": "Document",
          "status": 200,
          "statusText": "OK"
        },
        {
          "requestId": "15552.10",
          "method": "GET",
          "url": "http://127.0.0.1:17490/data.json",
          "resourceType": "Fetch",
          "status": 200,
          "statusText": "OK"
        }
      ],
      "status": "ok"
    },
    {
      "index": 4,
      "step": "Network.getResponseBody '{\"requestId\": \"CANNOT-BE-FILLED-IN-FROM-A-PREVIOUS-STEP\"}'",
      "status": "failed",
      "error": "CDP error -32000: No resource with given identifier found"
    }
  ]
}
[exit 1]

### resize: baseline innerWidth/innerHeight/devicePixelRatio
$ bt 148-01 Runtime.evaluate '{"expression": "[innerWidth, innerHeight, devicePixelRatio]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      756,
      469,
      1
    ]
  }
}
[exit 0]

### resize: one CDP call (GUIDE recipe)
$ bt 148-01 Emulation.setDeviceMetricsOverride '{"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": true}'
{}
[exit 0]

### resize: read it back in a SEPARATE invocation - does the override outlive its session?
$ bt 148-01 Runtime.evaluate '{"expression": "[innerWidth, innerHeight, devicePixelRatio]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      390,
      844,
      1
    ]
  }
}
[exit 0]

### resize: put it back (clearDeviceMetricsOverride cannot, per the GUIDE)
$ bt 148-01 Emulation.setDeviceMetricsOverride '{"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": false}'
{}
[exit 0]

### emulate color-scheme: baseline
$ bt 148-01 Runtime.evaluate '{"expression": "matchMedia(\"(prefers-color-scheme: dark)\").matches", "returnByValue": true}'
{
  "result": {
    "type": "boolean",
    "value": true
  }
}
[exit 0]

### emulate color-scheme: one CDP call
$ bt 148-01 Emulation.setEmulatedMedia '{"features": [{"name": "prefers-color-scheme", "value": "dark"}]}'
{}
[exit 0]

### emulate color-scheme: read back in a SEPARATE invocation
$ bt 148-01 Runtime.evaluate '{"expression": "matchMedia(\"(prefers-color-scheme: dark)\").matches", "returnByValue": true}'
{
  "result": {
    "type": "boolean",
    "value": true
  }
}
[exit 0]

### emulate color-scheme: set and read inside ONE invocation (bt run)
$ printf '%s\n' 'Emulation.setEmulatedMedia '"'"'{"features": [{"name": "prefers-color-scheme", "value": "dark"}]}'"'"'' 'Runtime.evaluate '"'"'{"expression": "matchMedia(\"(prefers-color-scheme: dark)\").matches", "returnByValue": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Emulation.setEmulatedMedia '{\"features\": [{\"name\": \"prefers-color-scheme\", \"value\": \"dark\"}]}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Runtime.evaluate '{\"expression\": \"matchMedia(\\\"(prefers-color-scheme: dark)\\\").matches\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "boolean",
          "value": true
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate cpu: baseline spin-loop iterations in 200ms
$ bt 148-01 Runtime.evaluate '{"expression": "(() => { const t = performance.now(); let n = 0; while (performance.now() - t < 200) n++; return n; })()", "returnByValue": true}'
{
  "result": {
    "type": "number",
    "value": 3096271,
    "description": "3096271"
  }
}
[exit 0]

### emulate cpu: one CDP call, rate 20
$ bt 148-01 Emulation.setCPUThrottlingRate '{"rate": 20}'
{}
[exit 0]

### emulate cpu: same spin loop in a SEPARATE invocation
$ bt 148-01 Runtime.evaluate '{"expression": "(() => { const t = performance.now(); let n = 0; while (performance.now() - t < 200) n++; return n; })()", "returnByValue": true}'
{
  "result": {
    "type": "number",
    "value": 3064220,
    "description": "3064220"
  }
}
[exit 0]

### emulate cpu: throttle and measure inside ONE invocation (bt run)
$ printf '%s\n' 'Emulation.setCPUThrottlingRate '"'"'{"rate": 20}'"'"'' 'Runtime.evaluate '"'"'{"expression": "(() => { const t = performance.now(); let n = 0; while (performance.now() - t < 200) n++; return n; })()", "returnByValue": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Emulation.setCPUThrottlingRate '{\"rate\": 20}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Runtime.evaluate '{\"expression\": \"(() => { const t = performance.now(); let n = 0; while (performance.now() - t < 200) n++; return n; })()\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "number",
          "value": 147273,
          "description": "147273"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate cpu: back to rate 1
$ bt 148-01 Emulation.setCPUThrottlingRate '{"rate": 1}'
{}
[exit 0]

### emulate color-scheme: set LIGHT, then read it back in a SEPARATE invocation
$ bt 148-01 Emulation.setEmulatedMedia '{"features": [{"name": "prefers-color-scheme", "value": "light"}]}'
{}
[exit 0]

### emulate color-scheme: separate invocation - is it still light?
$ bt 148-01 Runtime.evaluate '{"expression": "matchMedia(\"(prefers-color-scheme: light)\").matches", "returnByValue": true}'
{
  "result": {
    "type": "boolean",
    "value": false
  }
}
[exit 0]

### emulate color-scheme: set LIGHT and read it inside ONE invocation
$ printf '%s\n' 'Emulation.setEmulatedMedia '"'"'{"features": [{"name": "prefers-color-scheme", "value": "light"}]}'"'"'' 'Runtime.evaluate '"'"'{"expression": "matchMedia(\"(prefers-color-scheme: light)\").matches", "returnByValue": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Emulation.setEmulatedMedia '{\"features\": [{\"name\": \"prefers-color-scheme\", \"value\": \"light\"}]}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Runtime.evaluate '{\"expression\": \"matchMedia(\\\"(prefers-color-scheme: light)\\\").matches\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "boolean",
          "value": true
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate network: Network.emulateNetworkConditions without Network.enable first
$ bt 148-01 Network.emulateNetworkConditions '{"offline": true, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0}'
{}
[exit 0]

### emulate network: is the page offline in a SEPARATE invocation?
$ bt 148-01 Runtime.evaluate '{"expression": "fetch(\"/data.json\", {cache: \"no-store\"}).then(r => \"OK \" + r.status).catch(e => \"FAILED \" + e.message)", "returnByValue": true, "awaitPromise": true}'
{
  "result": {
    "type": "string",
    "value": "OK 200"
  }
}
[exit 0]

### emulate network: offline + the fetch inside ONE invocation
$ printf '%s\n' 'Network.enable '"'"'{}'"'"'' 'Network.emulateNetworkConditions '"'"'{"offline": true, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0}'"'"'' 'Runtime.evaluate '"'"'{"expression": "fetch(\"/data.json\", {cache: \"no-store\"}).then(r => \"OK \" + r.status).catch(e => \"FAILED \" + e.message)", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 3,
    "completed": 3,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Network.enable '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Network.emulateNetworkConditions '{\"offline\": true, \"latency\": 0, \"downloadThroughput\": 0, \"uploadThroughput\": 0}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 3,
      "step": "Runtime.evaluate '{\"expression\": \"fetch(\\\"/data.json\\\", {cache: \\\"no-store\\\"}).then(r => \\\"OK \\\" + r.status).catch(e => \\\"FAILED \\\" + e.message)\", \"returnByValue\": true, \"awaitPromise\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "FAILED Failed to fetch"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate network: back online, then confirm the page fetches again
$ printf '%s\n' 'Network.enable '"'"'{}'"'"'' 'Network.emulateNetworkConditions '"'"'{"offline": false, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1}'"'"'' 'Runtime.evaluate '"'"'{"expression": "fetch(\"/data.json\", {cache: \"no-store\"}).then(r => \"OK \" + r.status).catch(e => \"FAILED \" + e.message)", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 3,
    "completed": 3,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Network.enable '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Network.emulateNetworkConditions '{\"offline\": false, \"latency\": 0, \"downloadThroughput\": -1, \"uploadThroughput\": -1}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 3,
      "step": "Runtime.evaluate '{\"expression\": \"fetch(\\\"/data.json\\\", {cache: \\\"no-store\\\"}).then(r => \\\"OK \\\" + r.status).catch(e => \\\"FAILED \\\" + e.message)\", \"returnByValue\": true, \"awaitPromise\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "OK 200"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate user-agent: one CDP call
$ bt 148-01 Emulation.setUserAgentOverride '{"userAgent": "bt-verb-gap-probe/1.0"}'
{}
[exit 0]

### emulate user-agent: read navigator.userAgent in a SEPARATE invocation
$ bt 148-01 Runtime.evaluate '{"expression": "navigator.userAgent", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36"
  }
}
[exit 0]

### emulate user-agent: set and read inside ONE invocation
$ printf '%s\n' 'Emulation.setUserAgentOverride '"'"'{"userAgent": "bt-verb-gap-probe/1.0"}'"'"'' 'Runtime.evaluate '"'"'{"expression": "navigator.userAgent", "returnByValue": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Emulation.setUserAgentOverride '{\"userAgent\": \"bt-verb-gap-probe/1.0\"}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Runtime.evaluate '{\"expression\": \"navigator.userAgent\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "bt-verb-gap-probe/1.0"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate geolocation: grant the permission and set the override, one invocation each
$ bt 148-01 Browser.grantPermissions '{"origin": "http://127.0.0.1:17490", "permissions": ["geolocation"]}'
{}
[exit 0]

### emulate geolocation: set the override
$ bt 148-01 Emulation.setGeolocationOverride '{"latitude": 37.7749, "longitude": -122.4194, "accuracy": 10}'
{}
[exit 0]

### emulate geolocation: read the position in a SEPARATE invocation
$ bt 148-01 Runtime.evaluate '{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'
{
  "result": {
    "type": "string",
    "value": "ERR 1 User denied Geolocation"
  }
}
[exit 0]

### emulate geolocation: set and read inside ONE invocation
$ printf '%s\n' 'Emulation.setGeolocationOverride '"'"'{"latitude": 48.8566, "longitude": 2.3522, "accuracy": 10}'"'"'' 'Runtime.evaluate '"'"'{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Emulation.setGeolocationOverride '{\"latitude\": 48.8566, \"longitude\": 2.3522, \"accuracy\": 10}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Runtime.evaluate '{\"expression\": \"new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\\\"ERR \\\" + e.code + \\\" \\\" + e.message), {timeout: 4000}))\", \"returnByValue\": true, \"awaitPromise\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "ERR 1 User denied Geolocation"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate geolocation: grant, override and read all inside ONE invocation
$ printf '%s\n' 'Browser.grantPermissions '"'"'{"origin": "http://127.0.0.1:17490", "permissions": ["geolocation"]}'"'"'' 'Emulation.setGeolocationOverride '"'"'{"latitude": 48.8566, "longitude": 2.3522, "accuracy": 10}'"'"'' 'Runtime.evaluate '"'"'{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 3,
    "completed": 3,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Browser.grantPermissions '{\"origin\": \"http://127.0.0.1:17490\", \"permissions\": [\"geolocation\"]}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Emulation.setGeolocationOverride '{\"latitude\": 48.8566, \"longitude\": 2.3522, \"accuracy\": 10}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 3,
      "step": "Runtime.evaluate '{\"expression\": \"new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\\\"ERR \\\" + e.code + \\\" \\\" + e.message), {timeout: 4000}))\", \"returnByValue\": true, \"awaitPromise\": true}'",
      "result": {
        "result": {
          "type": "object",
          "value": [
            48.8566,
            2.3522
          ]
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### emulate geolocation: after that run, read the position again in a SEPARATE invocation
$ bt 148-01 Runtime.evaluate '{"expression": "new Promise(res => navigator.geolocation.getCurrentPosition(p => res([p.coords.latitude, p.coords.longitude]), e => res(\"ERR \" + e.code + \" \" + e.message), {timeout: 4000}))", "returnByValue": true, "awaitPromise": true}'
{
  "result": {
    "type": "string",
    "value": "ERR 1 User denied Geolocation"
  }
}
[exit 0]

### wait <ms>: outside a run it is the shell, no bt verb and no CDP call
$ s=$(date +%s); sleep 2; e=$(date +%s); echo "slept $((e-s))s"
slept 2s
[exit 0]

### wait <ms>: inside a run there is no sleep step. wait --event with a timeout FAILS the run.
$ printf '%s\n' 'wait --event Nothing.happensHere --timeout 2' 'snapshot' | bt 148-01 run -
error: step 1 failed: wait --event Nothing.happensHere --timeout 2
timeout: no Nothing.happensHere event within 2.0s
{
  "run": {
    "steps": 2,
    "completed": 0,
    "status": "failed"
  },
  "steps": [
    {
      "index": 1,
      "step": "wait --event Nothing.happensHere --timeout 2",
      "status": "failed",
      "error": "timeout: no Nothing.happensHere event within 2.0s"
    }
  ]
}
[exit 1]

### wait <ms>: inside a run, console-list --duration N is the fixed-duration wait that exists
$ s=$(date +%s); printf '%s\n' 'console-list --duration 3' 'Runtime.evaluate '"'"'{"expression": "1+1", "returnByValue": true}'"'"'' | bt 148-01 run - > /dev/null; e=$(date +%s); echo "run took $((e-s))s"
run took 3s
[exit 0]

### wait <text>: reload so the LATE TEXT is not there yet, then check immediately
$ printf '%s\n' 'Page.reload '"'"'{}'"'"'' 'Runtime.evaluate '"'"'{"expression": "document.body.innerText.includes(\"LATE TEXT APPEARED\")", "returnByValue": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Page.reload '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Runtime.evaluate '{\"expression\": \"document.body.innerText.includes(\\\"LATE TEXT APPEARED\\\")\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "boolean",
          "value": false
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### wait <text>: is there a single CDP call for it? A MutationObserver promise with awaitPromise is one call - and this is what it costs to write
$ printf '%s\n' 'Page.reload '"'"'{}'"'"'' 'Runtime.evaluate '"'"'{"expression": "new Promise((res, rej) => { const t = \"LATE TEXT APPEARED\"; const hit = () => document.body && document.body.innerText.includes(t); if (hit()) return res(\"already there\"); const obs = new MutationObserver(() => { if (hit()) { obs.disconnect(); res(\"appeared\"); } }); obs.observe(document.documentElement, {childList: true, subtree: true, characterData: true}); setTimeout(() => { obs.disconnect(); rej(new Error(\"timeout waiting for text\")); }, 5000); })", "returnByValue": true, "awaitPromise": true}'"'"'' | bt 148-01 run -
{
  "run": {
    "steps": 2,
    "completed": 2,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Page.reload '{}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 2,
      "step": "Runtime.evaluate '{\"expression\": \"new Promise((res, rej) => { const t = \\\"LATE TEXT APPEARED\\\"; const hit = () => document.body && document.body.innerText.includes(t); if (hit()) return res(\\\"already there\\\"); const obs = new MutationObserver(() => { if (hit()) { obs.disconnect(); res(\\\"appeared\\\"); } }); obs.observe(document.documentElement, {childList: true, subtree: true, characterData: true}); setTimeout(() => { obs.disconnect(); rej(new Error(\\\"timeout waiting for text\\\")); }, 5000); })\", \"returnByValue\": true, \"awaitPromise\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "appeared"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### wait <text>: the polling alternative, a shell loop of separate invocations
$ bt 148-01 Page.reload '{}' > /dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do
  v=$(bt 148-01 Runtime.evaluate '{"expression": "document.body.innerText.includes(\"LATE TEXT APPEARED\")", "returnByValue": true}' | grep -c 'true')
  if [ "$v" -gt 0 ]; then echo "found after $i polls"; break; fi
  sleep 0.5
done
found after 4 polls
[exit 0]

### dialog: open a confirm in step 1 of a run, wait for the event in step 2, handle it in step 3, read the answer in step 4
$ printf '%s\n' \
 'Runtime.evaluate '"'"'{"expression": "setTimeout(() => { window.__dlg = String(confirm(\"CONFIRM-FIXTURE\")); }, 1500)"}'"'"'' \
 'wait --event Page.javascriptDialogOpening --timeout 10' \
 'Page.handleJavaScriptDialog '"'"'{"accept": true}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "window.__dlg", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 25
{
  "run": {
    "steps": 4,
    "completed": 4,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Runtime.evaluate '{\"expression\": \"setTimeout(() => { window.__dlg = String(confirm(\\\"CONFIRM-FIXTURE\\\")); }, 1500)\"}'",
      "result": {
        "result": {
          "type": "number",
          "value": 2,
          "description": "2"
        }
      },
      "status": "ok"
    },
    {
      "index": 2,
      "step": "wait --event Page.javascriptDialogOpening --timeout 10",
      "result": {
        "method": "Page.javascriptDialogOpening",
        "params": {
          "url": "http://127.0.0.1:17490/fixture.html",
          "frameId": "E0195465CF309F500271FDB23C5BA8B3",
          "message": "CONFIRM-FIXTURE",
          "type": "confirm",
          "hasBrowserHandler": true,
          "defaultPrompt": ""
        }
      },
      "status": "ok"
    },
    {
      "index": 3,
      "step": "Page.handleJavaScriptDialog '{\"accept\": true}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 4,
      "step": "Runtime.evaluate '{\"expression\": \"window.__dlg\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "true"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### dialog: the realistic shape - a CLICK raises the dialog synchronously, then a later step handles it
$ printf '%s\n' \
 'click --uid AB5DFB3B758F-200' \
 'wait --event Page.javascriptDialogOpening --timeout 8' \
 'Page.handleJavaScriptDialog '"'"'{"accept": true}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "document.title", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 20
call_native(click) error: 
error: step 1 timed out: click --uid AB5DFB3B758F-200
the run's 20.0s deadline passed: click did not answer in time
{
  "run": {
    "steps": 4,
    "completed": 0,
    "status": "timeout"
  },
  "steps": [
    {
      "index": 1,
      "step": "click --uid AB5DFB3B758F-200",
      "status": "timeout",
      "error": "the run's 20.0s deadline passed: click did not answer in time"
    }
  ]
}
[exit 1]

### dialog: the alert the timed-out click left open - can a NEW invocation handle it? (the GUIDE says no)
$ bt 148-01 Page.handleJavaScriptDialog '{"accept": true}'
error: CDP error -32602: No dialog is showing
[exit 1]

### dialog: recover - navigate away from the wedged page
$ bt 148-01 Page.navigate '{"url": "http://127.0.0.1:17490/fixture.html"}'
{
  "frameId": "E0195465CF309F500271FDB23C5BA8B3",
  "loaderId": "95780532CDEC545BF8AF989BA1C416EB",
  "isDownload": false
}
[exit 0]

### dialog: is the page answering again?
$ bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "bt verb-gap fixture"
  }
}
[exit 0]

### dialog: the workaround - defer the click so the step returns, then handle the prompt with text
$ printf '%s\n' \
 'Runtime.evaluate '"'"'{"expression": "setTimeout(() => document.getElementById(\"promptbtn\").click(), 300)"}'"'"'' \
 'wait --event Page.javascriptDialogOpening --timeout 8' \
 'Page.handleJavaScriptDialog '"'"'{"accept": true, "promptText": "typed-answer"}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "window.__dialogResult", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 20
{
  "run": {
    "steps": 4,
    "completed": 4,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Runtime.evaluate '{\"expression\": \"setTimeout(() => document.getElementById(\\\"promptbtn\\\").click(), 300)\"}'",
      "result": {
        "result": {
          "type": "number",
          "value": 2,
          "description": "2"
        }
      },
      "status": "ok"
    },
    {
      "index": 2,
      "step": "wait --event Page.javascriptDialogOpening --timeout 8",
      "result": {
        "method": "Page.javascriptDialogOpening",
        "params": {
          "url": "http://127.0.0.1:17490/fixture.html",
          "frameId": "E0195465CF309F500271FDB23C5BA8B3",
          "message": "PROMPT-FIXTURE",
          "type": "prompt",
          "hasBrowserHandler": true,
          "defaultPrompt": "default"
        }
      },
      "status": "ok"
    },
    {
      "index": 3,
      "step": "Page.handleJavaScriptDialog '{\"accept\": true, \"promptText\": \"typed-answer\"}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 4,
      "step": "Runtime.evaluate '{\"expression\": \"window.__dialogResult\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "typed-answer"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### dialog: dismiss a confirm the same way
$ printf '%s\n' \
 'Runtime.evaluate '"'"'{"expression": "setTimeout(() => document.getElementById(\"confirmbtn\").click(), 300)"}'"'"'' \
 'wait --event Page.javascriptDialogOpening --timeout 8' \
 'Page.handleJavaScriptDialog '"'"'{"accept": false}'"'"'' \
 'Runtime.evaluate '"'"'{"expression": "window.__dialogResult", "returnByValue": true}'"'"'' \
 | bt 148-01 run - --timeout 20
{
  "run": {
    "steps": 4,
    "completed": 4,
    "status": "ok"
  },
  "steps": [
    {
      "index": 1,
      "step": "Runtime.evaluate '{\"expression\": \"setTimeout(() => document.getElementById(\\\"confirmbtn\\\").click(), 300)\"}'",
      "result": {
        "result": {
          "type": "number",
          "value": 3,
          "description": "3"
        }
      },
      "status": "ok"
    },
    {
      "index": 2,
      "step": "wait --event Page.javascriptDialogOpening --timeout 8",
      "result": {
        "method": "Page.javascriptDialogOpening",
        "params": {
          "url": "http://127.0.0.1:17490/fixture.html",
          "frameId": "E0195465CF309F500271FDB23C5BA8B3",
          "message": "CONFIRM-FIXTURE",
          "type": "confirm",
          "hasBrowserHandler": true,
          "defaultPrompt": ""
        }
      },
      "status": "ok"
    },
    {
      "index": 3,
      "step": "Page.handleJavaScriptDialog '{\"accept\": false}'",
      "result": {},
      "status": "ok"
    },
    {
      "index": 4,
      "step": "Runtime.evaluate '{\"expression\": \"window.__dialogResult\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "string",
          "value": "false"
        }
      },
      "status": "ok"
    }
  ]
}
[exit 0]

### pages: open a second tab again, to test how a CURATED verb resolves the page
$ bt 148-01 Target.createTarget '{"url": "http://127.0.0.1:17490/second.html", "newWindow": true}'
{
  "targetId": "2827A81510581B2A4047F144C0017646"
}
[exit 0]

### pages: a curated verb with two tabs open and no --target
$ bt 148-01 snapshot | head -5
{
  "snapshot": "[uid=AAD5FD98FA86-1] RootWebArea \"second page\"\n  [uid=AAD5FD98FA86-7] heading \"SECOND PAGE\"\n    [uid=AAD5FD98FA86-8] StaticText \"SECOND PAGE\"\n      [uid=AAD5FD98FA86-x6] InlineTextBox \"SECOND PAGE\""
}
[exit 0]

### pages: the same shape as a raw passthrough
$ bt 148-01 Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
error: Multiple page targets found. Specify one:
  [1] 2827A815  http://127.0.0.1:17490/second.html  "second page"
  [2] E0195465  http://127.0.0.1:17490/fixture.html  "bt verb-gap fixture"
[exit 1]

### pages: close the second tab again
$ bt 148-01 stop --target 2
{
  "stopped": true,
  "message": "Closed tab E0195465 in 148-01"
}
[exit 0]

### run semantics: bt run cannot branch on a step result - the whole decision goes into one eval instead
$ bt 148-01 Runtime.evaluate '{"expression": "(() => { const n = document.querySelectorAll(\"input\").length; if (n > 2) { document.getElementById(\"go\").click(); return \"clicked, inputs=\" + n; } return \"skipped, inputs=\" + n; })()", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "skipped, inputs=0"
  }
}
[exit 0]

### help: the live protocol schema for one method
$ bt 148-01 help Page.handleJavaScriptDialog
Page.handleJavaScriptDialog
Accepts or dismisses a JavaScript initiated dialog (alert, confirm, prompt, or onbeforeunload).

Parameters:
  accept: boolean (required)
    Whether to accept or dismiss the dialog.
  promptText: string
    The text to enter into the dialog prompt before accepting. Used only if this is a prompt
dialog.
[exit 0]

### help: Input.dispatchKeyEvent signature, the parameter table a press verb would hide
$ bt 148-01 help Input.dispatchKeyEvent
Input.dispatchKeyEvent
Dispatches a key event to the page.

Parameters:
  type: string (required)
    Type of the key event.
  modifiers: integer
    Bit field representing pressed modifier keys. Alt=1, Ctrl=2, Meta/Command=4, Shift=8
(default: 0).
  timestamp: TimeSinceEpoch
    Time at which the event occurred.
  text: string
    Text as generated by processing a virtual key code with a keyboard layout. Not needed for
for `keyUp` and `rawKeyDown` events (default: "")
  unmodifiedText: string
    Text that would have been generated by the keyboard if no modifiers were pressed (except for
shift). Useful for shortcut (accelerator) key handling (default: "").
  keyIdentifier: string
    Unique key identifier (e.g., 'U+0041') (default: "").
  code: string
    Unique DOM defined string value for each physical key (e.g., 'KeyA') (default: "").
  key: string
    Unique DOM defined string value describing the meaning of the key in the context of active
modifiers, keyboard layout, etc (e.g., 'AltGr') (default: "").
  windowsVirtualKeyCode: integer
    Windows virtual key code (default: 0).
  nativeVirtualKeyCode: integer
    Native virtual key code (default: 0).
  autoRepeat: boolean
    Whether the event was generated from auto repeat (default: false).
  isKeypad: boolean
    Whether the event was generated from the keypad (default: false).
  isSystemKey: boolean
    Whether the event was a system key event (default: false).
  location: integer
    Whether the event was from the left or right side of the keyboard. 1=Left, 2=Right (default:
0).
  commands: array
    Editing commands to send with the key event (e.g., 'selectAll') (default: []).
These are related to but not equal the command names used in `document.execCommand` and NSStandardKeyBindingResponding.
See https://source.chromium.org/chromium/chromium/src/+/main:third_party/blink/renderer/core/editing/commands/editor_command_names.h for valid command names.
[exit 0]

### pages: note what the previous two commands did - snapshot drove second.html (target-id order), and stop --target 2 closed the fixture tab. Back to the fixture.
$ bt 148-01 Page.navigate '{"url": "http://127.0.0.1:17490/fixture.html"}'
{
  "frameId": "2827A81510581B2A4047F144C0017646",
  "loaderId": "E1E63C91040365AA1BFFE253D4736F02",
  "isDownload": false
}
[exit 0]

### run semantics: the branch demo, this time on the fixture page
$ bt 148-01 Runtime.evaluate '{"expression": "(() => { const n = document.querySelectorAll(\"input\").length; if (n > 2) { document.getElementById(\"go\").click(); return \"clicked, inputs=\" + n; } return \"skipped, inputs=\" + n; })()", "returnByValue": true}'
{
  "result": {
    "type": "string",
    "value": "clicked, inputs=3"
  }
}
[exit 0]

### run semantics: the same decision as a bt run cannot be written - a step cannot read step 1 output
$ printf '%s\n' 'Runtime.evaluate '"'"'{"expression": "document.querySelectorAll(\"input\").length", "returnByValue": true}'"'"'' 'click --uid THERE-IS-NO-WAY-TO-PUT-THE-ANSWER-HERE' | bt 148-01 run -
error: step 2 failed: click --uid THERE-IS-NO-WAY-TO-PUT-THE-ANSWER-HERE
cannot interact with uid 'THERE-IS-NO-WAY-TO-PUT-THE-ANSWER-HERE': minted against a previous document (the page navigated since); take a new snapshot
{
  "run": {
    "steps": 2,
    "completed": 1,
    "status": "failed"
  },
  "steps": [
    {
      "index": 1,
      "step": "Runtime.evaluate '{\"expression\": \"document.querySelectorAll(\\\"input\\\").length\", \"returnByValue\": true}'",
      "result": {
        "result": {
          "type": "number",
          "value": 3,
          "description": "3"
        }
      },
      "status": "ok"
    },
    {
      "index": 2,
      "step": "click --uid THERE-IS-NO-WAY-TO-PUT-THE-ANSWER-HERE",
      "status": "failed",
      "error": "cannot interact with uid 'THERE-IS-NO-WAY-TO-PUT-THE-ANSWER-HERE': minted against a previous document (the page navigated since); take a new snapshot"
    }
  ]
}
[exit 1]

### hover (re-run, the first attempt died on an ambiguous target): fresh instance and fixture
$ (nohup python3 -m http.server 17490 --bind 127.0.0.1 > server.log 2>&1 &) ; sleep 1; bt launch --headless
{
  "name": "148-01",
  "port": 9225,
  "pid": 75003,
  "engine": "chrome",
  "profile": null,
  "browser_version": "Chrome/153.0.8010.53",
  "user_data_dir": "/tmp/chrome-agent/session-4clbd8mt"
}
[exit 0]

### hover: navigate
$ bt 148-01 Page.navigate '{"url": "http://127.0.0.1:17490/fixture.html"}'
{
  "frameId": "4A66DCA742D1DCF60CCBE441596ADAD3",
  "loaderId": "AA0FAFE863CC649CF0A756D273225D53",
  "isDownload": false
}
[exit 0]

### hover: baseline, nothing hovered
$ bt 148-01 Runtime.evaluate '{"expression": "[document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      "no-hover",
      "rgb(221, 221, 221)"
    ]
  }
}
[exit 0]

### hover: the JS substitute - a synthetic MouseEvent fires the handler but does NOT set the CSS :hover state
$ bt 148-01 Runtime.evaluate '{"expression": "document.getElementById(\"hovertarget\").dispatchEvent(new MouseEvent(\"mouseover\", {bubbles: true})); [document.getElementById(\"hoverstate\").textContent, getComputedStyle(document.getElementById(\"hovertarget\")).backgroundColor]", "returnByValue": true}'
{
  "result": {
    "type": "object",
    "value": [
      "HOVERED",
      "rgb(221, 221, 221)"
    ]
  }
}
[exit 0]

### hover: the real pointer move, for comparison (box centre from DOM.getBoxModel)
$ bt 148-01 DOM.getBoxModel '{"backendNodeId": 48}'
error: CDP error -32000: No node found for given backend id
[exit 1]
```
