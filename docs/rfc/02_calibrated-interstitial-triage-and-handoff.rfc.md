---
number: 02
title: "Calibrated interstitial triage"
type: feature
status: Draft
author: "Kevin Frilot"
date: 2026-09-20
version: 3
---

# RFC-02: Calibrated interstitial triage

## Abstract

browser-tools decides how long to wait on an anti-bot challenge from the challenge's
*type*, a label assigned by regular expressions in a bundled JavaScript file. Two of
ten types are marked retryable, so the retry loop waits nine seconds on some pages
that will never clear and reports others at once that a short wait would have cleared.
This RFC specifies an optional triage step that replaces the type lookup with a
calibrated judgment from a System One model, gates the retry loop on that judgment,
and falls back to today's behavior whenever the model is unavailable. It partially
supersedes RFC-01's prohibition on inference, narrowing it to a prohibition on
solving, which this RFC restates as a normative requirement. It adds no captcha
solving.

This is version 3. Version 2 answered review `review-01`. Version 3 removes the
headless-to-headed promotion gate after finding that the code it would have changed
has no production caller. See "Changes in this revision".

## Introduction

### Problem statement

Interstitial Detection owns the post-navigation challenge-response policy
(`CONTEXT.md`, "Interstitial Detection"). Its retry decision has three defects, all
traceable to one cause: the policy branches on a type label instead of on the property
it cares about, which is whether waiting will help.

1. **The retry set is a two-element frozenset over ten types.**
   `INTERSTITIAL_AUTO_RETRY_TYPES = frozenset({"cloudflare_challenge", "access_denied"})`
   (`src/browser_tools/interstitial.py:38`). A challenge outside that set is reported
   immediately even when a short wait would clear it.

2. **One type spans both outcomes.** `detect_interstitial.js:30` assigns
   `cloudflare_challenge` on `/just a moment/i` or `/attention required/i` in the page
   title. The first is Cloudflare's self-clearing JS challenge. The second is its
   block page, which does not clear. Both route to the same retryable branch. A page
   whose detections include a retryable type costs up to
   `INTERSTITIAL_MAX_RETRIES * INTERSTITIAL_RETRY_DELAY_SECONDS`, nine seconds
   (`interstitial.py:36-37`), before the caller is told anything. A page whose
   detections are all non-retryable returns at once (`interstitial.py:146`), and a
   retryable page can clear after the first wait (`:153`), so nine seconds is the
   ceiling for one class of page, not the cost of every blocked navigation.

3. **`confidence` is an author-assigned string, not a measurement.** Each detection
   carries `'high'`, `'medium'`, or `'low'`, hand-written at the site that emits it.
   `_deduplicate` keeps the highest-confidence detection within each type
   (`interstitial.py:238`) and `format_interstitials` prints the string to the caller
   (`interstitial.py:102-106`). An agent reading `[low] cloudflare_challenge` has no
   basis on which to act differently from `[high]`.

### Where this applies

`run_post_navigation_detection` has two callers: the optional MCP front
(`mcp_daemon.py:497`), which runs it automatically after a navigation, and the CLI
`detect` verb (`curated.py:270`), which runs it on demand. There is no `navigate` verb
in `cli.py`, so the canonical CLI surface never triages automatically.

The saving this RFC buys therefore lands on the MCP front, and on an explicit `detect`.
That is smaller than it first appears and is stated here rather than in a footnote.

### Scope

**In scope:** the Challenge Gate that decides whether triage runs at all; the
Interstitial Evidence payload, its collection, and its bounds; the Interstitial
Verdict type; the decision table that maps a Verdict to a transition; the retry loop's
new gate; degradation when the model is unavailable; the packaging extra; the
amendment to RFC-01's inference prohibition; the two preconditions in existing code
this RFC depends on; the `CONTEXT.md` entries for the new domain nouns.

**Out of scope, with reasons:**

- **The headless-to-headed promotion gate.** Version 2 specified replacing
  `_response_signals_auth_wall`, a substring match that decides whether to tear down
  headless Chrome and seize the user's screen. That code has no production caller.
  `_maybe_promote_on_auth_wall` is reached only through `dispatch_session_tool`
  (`browser_session.py:855`), whose only external entry, `create_tool_proxy_handlers`,
  is called only from `tests/test_e2e_backend_seam.py:44`. The tool-proxy app that
  adapter served was retired in RFC-01 Phase 4 (`docs/tool-proxy-retirement.md`). The
  substring gate is a real defect in code nothing runs. Reviving promotion on the CLI
  is a product decision, not a triage question, and belongs in its own RFC.
- **Captcha solving of any kind.** RFC-01 forbids it. This RFC keeps the prohibition
  and restates it normatively in Design, "Amendment to RFC-01".
- **A vision model or third-party solving service behind this extra.** browser-tools
  publishes to PyPI, so such a capability would ship to every installer rather than
  being scoped to an authorized engagement. This reasoning is this RFC's, not RFC-01's.
  Design states it as a normative MUST NOT.
- **Adding post-navigation detection to the CLI.** The canonical surface has no
  `navigate` verb, so triage cannot fire there automatically. Giving the CLI one is the
  change that would make this RFC matter on the surface RFC-01 made canonical, and it
  is a separate product decision.
- **Semantic element selection (`find "<description>"`), ranked `console-list` and
  `network-list`, and a natural-language front over `passthrough`.** All reachable with
  the same client. None is on the detection path. Deferred.
- **Detecting a vendor the script does not already name.** Version 1 claimed the
  model's `other` option would separate an unknown challenge from a clean page. It
  cannot: triage runs only when the Challenge Gate opens, and an unrecognized vendor
  produces no signal to open it. The claim is withdrawn.

## Terminology

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT,
RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted as described in
RFC 2119.

- **Interstitial** - an anti-bot challenge page. Defined in `CONTEXT.md`; unchanged
  by this RFC.
- **Interstitial Detection** - the existing module that owns the post-navigation
  challenge-response policy end to end (`src/browser_tools/interstitial.py`).
  This RFC narrows its responsibility: it keeps the policy, and delegates the judgment.
- **Presence Signal** - a detection whose `signal` field is `cookie` or `script_src`.
  It establishes that a site uses a vendor, not that this page is a challenge.
- **Challenge Signal** - a detection with any other `signal` value. It establishes
  that something on this page is blocking or asking.
- **Challenge Gate** - the rule that triage runs only when at least one Challenge
  Signal is present. New noun; MUST be added to `CONTEXT.md`.
- **Interstitial Evidence** - the bounded, structured record collected from a page
  when the Challenge Gate opens. It is the only thing that leaves the machine. New
  noun; MUST be added to `CONTEXT.md`.
- **Interstitial Verdict** - the typed result of triage: a vendor choice, two
  probabilities, a confidence, and the source that produced it. New noun; MUST be
  added to `CONTEXT.md`.
- **Interstitial Triage** - the step that turns Interstitial Evidence into an
  Interstitial Verdict. New noun; MUST be added to `CONTEXT.md`.
- **System One model** - a model that returns typed judgments and calibrated
  probabilities rather than generated text.
- **Jev** - TypeSafe's System One model, `jev-1.13.0`, served at `POST /v1/systemone`.
  It accepts text only: "State must be a string, JSON object, or array of text values.
  Images, audio, and video are not supported (yet)." Its request budget is 64k tokens
  total and 32k for state plus the longest question.
- **Degraded triage** - triage that produced a Verdict from the type-based policy
  rather than from a model answer. It covers the extra being absent, the key being
  absent, a timeout, and an API error, so it includes cases where a request was
  attempted. A Verdict from degraded triage carries `source = "heuristic"`.

## Motivation

The wait sits on the navigation path of the MCP front. A page whose detections include
a retryable type spends up to nine seconds before the caller learns anything, which
idles the agent driving the tool and the person waiting on the agent. Making the wait
conditional on a calibrated probability, rather than on a regular expression's guess
about the vendor, moves that cost onto the pages that justify it.

Version 1 claimed two further motivations. Unknown-vendor coverage is withdrawn, and
the promotion gate is out of scope; see Scope for both.

## Design

### The Challenge Gate

Detection firing is not evidence that a page is a challenge. Several detectors fire on
vendor presence alone: DataDome on `document.cookie.indexOf('datadome')`
(`detect_interstitial.js:132`), PerimeterX on `_px*` (`:184`), Imperva on
`incap_ses_` (`:211`), AWS WAF on `aws-waf-token` (`:238`), and four more on
`script[src*=...]`. Those cookies and scripts are present on every page of a protected
site, including ordinary pages inside an authenticated session.

Every detection already carries a `signal` field. The twelve values in use split
exactly:

| Class | `signal` values | Means |
|---|---|---|
| Presence Signal | `cookie`, `script_src` | This site uses this vendor |
| Challenge Signal | `title_pattern`, `meta_tag`, `css_selector`, `body_text`, `challenge_iframe`, `cookie_and_empty_page`, `reference_number`, `dom_element`, `title_and_form`, `form_heuristic` | This page is blocking or asking |

Triage MUST run only when the detection list contains at least one Challenge Signal.
A detection list of Presence Signals only MUST be treated as a clean page: no
Interstitial Evidence is collected, no request is made, and nothing leaves the machine.

This is the single most important constraint in this RFC. Without it, browsing an
authenticated session on any protected site sends every page to a hosted API.

### Evidence collection

The script gains collection, specified field by field, and it runs only when the
Challenge Gate opens.

| Field | Source | Cap |
|---|---|---|
| `title` | `document.title` | 200 chars |
| `cookie_names` | names parsed from `document.cookie`, values discarded before the array is built | 40 names, 64 chars each |
| `script_origins` | `new URL(s.src).origin` over `document.querySelectorAll('script[src]')`, deduplicated | 30 origins |
| `control_labels` | visible text of `button`, `a`, `input[type=submit]`, and `[role=button]`, trimmed, empties dropped | 25 labels, 80 chars each |
| `body_excerpt` | `document.body.innerText` | 2000 chars, matching the slice the script already uses (`detect_interstitial.js:95`) |
| `url` | `location.origin + location.pathname` | 300 chars |
| `elapsed_ms` | milliseconds since the first detection pass of this cycle, supplied by Python | integer |
| `attempts_remaining` | `INTERSTITIAL_MAX_RETRIES` minus attempts used, supplied by Python | integer |

`detections` travels with the evidence, with each detection's `details` field
**removed**. `details` embeds the full page title in free text
(`detect_interstitial.js:35`, `:126`), which defeats the `title` cap. `type`,
`confidence`, and `signal` are kept.

The serialized Interstitial Evidence MUST NOT exceed 8 KiB. A payload over that limit
MUST be truncated field by field in the order `body_excerpt`, `control_labels`,
`script_origins`, `cookie_names`, and the result MUST record that truncation occurred.
The caps above put a normal payload under 1000 tokens, which keeps the per-detection
cost under one hundredth of a cent at Jev's $42 per billion input tokens. The 8 KiB
ceiling, not the estimate, is what bounds it.

### Script return shape

`detect_interstitial.js` returns a JSON array today, and `parse_detection_result`
returns `[]` for anything that is not a list (`interstitial.py:75-76`). Evidence
cannot be added to a bare array, so the return shape MUST widen to an object:

```json
{ "detections": [ ... ], "evidence": { ... } | null, "document": { "url": "...", "nonce": "..." } }
```

`parse_detection_result` MUST accept both shapes. A bare array MUST continue to parse
as `detections` with no evidence, because the override path
(`~/.config/tool-proxy/browser-tools/detect-interstitial.js`) lets a user supply a
script this RFC has never seen. An override returning the old shape MUST keep working
and MUST take the degraded-triage path.

`tests/test_interstitial.py:75-78` (`test_non_array_returns_empty`) asserts that a
JSON object parses to `[]`. It MUST be amended, not deleted: the replacement asserts
that an object without a `detections` key still parses to `[]`.

### Evidence freshness

`detect_interstitials_async` runs two passes 500ms apart and merges their detections
(`interstitial.py:185`), so one `detect_once` already spans two documents.
`_deduplicate` keeps the higher-confidence detection per type and the earlier result
wins ties (`:243`), so a detection from the first pass can survive into a result
describing a page that has since changed.

`evidence` MUST come from the second pass. `document.nonce` MUST be generated per
evaluate and `document.url` recorded with it. When the two passes report different
`document.url`, the cycle MUST discard the merged detections, treat the navigation as
unresolved, and run one further pass rather than triaging a mixture of two documents.

### Data model

```python
@dataclass(frozen=True)
class InterstitialVerdict:
    vendor: str            # a detection type, "none", or "other"
    self_solving: float    # 0.0 - 1.0
    needs_human: float     # 0.0 - 1.0
    confidence: float      # 0.0 - 1.0, from the vendor Choice
    source: Literal["model", "heuristic"]
```

`source` is REQUIRED on every Verdict, and `format_interstitials` MUST say which it
got. A consumer cannot otherwise tell a calibrated number from a fallback.

The heuristic Verdict MUST be complete, not partial. The mapping, which reproduces
today's decisions exactly:

| Field | Heuristic value |
|---|---|
| `vendor` | the highest-confidence detection's `type`, or `"none"` when the list is empty |
| `self_solving` | `1.0` when any detection's `type` is in `INTERSTITIAL_AUTO_RETRY_TYPES`, else `0.0` |
| `needs_human` | `0.0` always. Today's code has no such concept, and inventing one would change behavior. |
| `confidence` | `0.0` always |

`INTERSTITIAL_AUTO_RETRY_TYPES` MUST remain, read only by this mapping, and its
comment MUST say that the fallback is now its only reader.

### The questions

Triage MUST issue exactly one request per triage call, carrying all three questions.
They are independent over the same state and evaluate in parallel.

```python
questions = {
    "vendor": Choice(
        instructions=(
            "Which anti-bot or access-control system is serving this page, "
            "judging from the collected signals?"
        ),
        criteria={
            "cloudflare_challenge": "Cloudflare challenge or block page",
            "datadome": "DataDome",
            "akamai_bot_manager": "Akamai Bot Manager",
            "perimeterx": "PerimeterX or HUMAN",
            "imperva": "Imperva or Incapsula",
            "aws_waf": "AWS WAF",
            "ngrok_warning": "ngrok free-tier interstitial",
            "auth_wall": "A sign-in or account authentication page",
            "captcha": "A standalone CAPTCHA with no identified vendor",
            "access_denied": "A block or 403 page with no identified vendor",
            "none": "Ordinary page content; no challenge is present",
            "other": "A challenge from a system not listed here",
        },
    ),
    "self_solving": Noul(
        instructions=(
            "Will this page clear on its own, with no user interaction, within the "
            "next 3 seconds? `evidence.elapsed_ms` gives how long it has already "
            "been waiting and `evidence.attempts_remaining` how many further 3-second "
            "waits are available."
        ),
    ),
    "needs_human": Noul(
        instructions=(
            "Does clearing this page need a person: solving a visual puzzle, "
            "pressing and holding a control, or entering credentials?"
        ),
    ),
}
```

The `vendor` criteria keys MUST be the detector's own type strings, so no
normalization layer is needed between the model's answer and the heuristic mapping.

`self_solving` names one horizon, the next 3 seconds, which is the wait the loop
actually performs. Version 1 said "a few more seconds", which does not identify
whether the prediction covers the next wait or the whole cycle, and left the model
unable to condition on either because neither elapsed time nor attempts remaining was
in the evidence. Both are now fields.

Version 2 asked a fourth question, `is_auth_wall`, to drive the promotion gate. With
promotion out of scope it has no consumer and is removed. A sign-in wall is still
reachable through `vendor = "auth_wall"` and through `needs_human`, whose wording
already covers entering credentials.

### The decision table

The three questions are independent, so every combination is reachable. Policy is a
table, evaluated top to bottom, first match wins:

| # | Condition | Transition | Code |
|---|---|---|---|
| 1 | `vendor == "none"` and `confidence >= VENDOR_NONE_CONFIDENCE` | `CLEAR` | - |
| 2 | `self_solving >= SELF_SOLVING_WAIT_THRESHOLD` and attempts remain | `WAITING` | - |
| 3 | `needs_human >= NEEDS_HUMAN_THRESHOLD` | `BLOCKED` | E010 |
| 4 | otherwise | `BLOCKED` | E003 |

Two precedence decisions are settled here rather than deferred, because no measurement
settles them:

- **A confident `none` clears.** A false-positive detection that the model calls
  ordinary content MUST NOT be waited on or reported as blocked.
- **`WAITING` outranks `needs_human`.** A challenge that both needs a person and is
  about to clear itself should be allowed to clear. Only when it will not clear does
  needing a person become the answer.

`E003` keeps its current meaning, a challenge needing manual resolution with no more
specific cause. `E010` is the narrower case and takes precedence where both apply.

### Preconditions in existing code

Two defects in current code sit under this design. Each MUST be fixed before the phase
that depends on it, and each is a behavior change in its own right.

1. **The retry loop double-counts.** The exhausted-retries path returns
   `detections + non_retryable`, where `non_retryable` was computed from the first pass
   and never reassigned (`interstitial.py:160-165`), so a persistent non-retryable
   detection is reported twice. Version 1 promised the fallback "reproduces today's
   behavior exactly", which would have specified reproducing this. The fallback MUST
   reproduce today's *decisions*, not this defect, and the defect MUST be fixed in
   Phase 0.
2. **The detection future is never cancelled.** `run_post_navigation_detection` calls
   `future.result(timeout=...)` and swallows the timeout (`cdp_handler.py:515-520`)
   without cancelling the coroutine, which keeps running on the CDP event loop. Today
   that leaks a detection pass. Under this RFC it would leave a hosted API request in
   flight after the caller gave up. The future MUST be cancelled on timeout, in Phase 1.

### Execution context

`detect_with_retry` currently takes one parameter (`interstitial.py:118-120`). Its new
signature:

```python
async def detect_with_retry(
    detect_once: Callable[[], Awaitable[DetectionResult]],
    triage: Callable[[InterstitialEvidence], Awaitable[InterstitialVerdict]],
) -> dict[str, Any]:
```

The loop runs marshalled onto the CDP event loop (`cdp_handler.py:509-517`). Triage
MUST therefore use `AsyncTypeSafeClient` and MUST be awaited, never called through a
synchronous client, which would block the event loop that owns the browser connection
for the duration of every request.

Module dependencies: `challenge_evidence.py` MUST be free of network, CDP, and
TypeSafe dependencies; its input is the parsed script return and its output is an
`InterstitialEvidence`. `challenge_triage.py` is the only module that MAY import
`typesafe_sdk`. `interstitial.py` imports `challenge_triage`, which satisfies both.

### Thresholds

`SELF_SOLVING_WAIT_THRESHOLD`, `NEEDS_HUMAN_THRESHOLD`, `VENDOR_NONE_CONFIDENCE`, and
`TRIAGE_TIMEOUT_SECONDS` are all read by the retry loop and MUST live in
`interstitial.py` beside it, following the convention that module already states
(`interstitial.py:32-38`). Their values are Open Question 1.

### Packaging

Triage MUST ship as an optional extra named `triage`. The default install MUST keep
its current dependency set of `websockets>=16.0` and MUST NOT acquire a transitive
dependency on `typesafe-sdk`. The `all` extra (`pyproject.toml:61-63`) MUST gain
`triage`. The `typesafe-sdk` pin MUST be recorded with the same treatment
`pyproject.toml:44-59` gives `camoufox`: a stated floor and a note on any
known-vulnerable transitive dependency it carries.

### Amendment to RFC-01

RFC-01 is Accepted. Two of its statements are policy governing browser-tools:

- `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md:31` (Scope)
- `:248` (Security Considerations)

A third, `:81`, sits under the heading `### chrome-agent (~/dev/chrome-agent, v0.5.7)`
and describes the upstream project's state at merge time. It is a historical
description, not policy, and MUST NOT be amended.

On acceptance of this RFC, the two policy statements are partially superseded by:

> browser-tools MUST NOT solve a challenge. It MUST NOT solve a captcha, and MUST NOT
> use inference to produce a token, an answer, or an interaction whose purpose is to
> satisfy an anti-bot check. It MUST NOT bundle or invoke a vision model, an
> image-to-text step, or a third-party solving service for that purpose. It MAY use
> inference to classify a challenge and to route control, subject to RFC-02's
> Security Considerations.

RFC-01's `status` MUST remain `Accepted`. `VALID_STATUSES` in
`draft-rfc/scripts/validate-structure.ts:66` is
`["Draft", "Review", "Accepted", "Implemented", "Superseded", "Withdrawn"]`, so an
`Accepted (amended by RFC-02)` string would fail RFC-01's own validator. The amendment
MUST instead be recorded in RFC-01's body, next to each amended statement, as a
pointer to this RFC, and RFC-01's `version` MUST increment.

## State Machine

States of one detection cycle. `MAX` is `INTERSTITIAL_MAX_RETRIES`. Transitions out of
`TRIAGING` are the decision table in Design, evaluated in order.

```
DETECTING        -> CLEAR             (on: no detections)
DETECTING        -> CLEAR             (on: Presence Signals only; Challenge Gate shut)
DETECTING        -> DETECTING         (on: the two passes report different document.url)
DETECTING        -> TRIAGING          (on: at least one Challenge Signal)

TRIAGING         -> DEGRADED          (on: extra absent, no API key, timeout,
                                           API error, or malformed Verdict)
TRIAGING         -> CLEAR             (table row 1)
TRIAGING         -> WAITING           (table row 2)
TRIAGING         -> BLOCKED           (table rows 3 and 4)

DEGRADED         -> WAITING           (guard: heuristic self_solving == 1.0
                                              and attempts < MAX)
DEGRADED         -> BLOCKED           (otherwise)

WAITING          -> DETECTING         (after: INTERSTITIAL_RETRY_DELAY_SECONDS)
WAITING          -> BLOCKED           (on: attempts == MAX)
```

Terminal states: `CLEAR`, `BLOCKED`.

`CLEAR` reports nothing, which is today's behavior for a clean page. `BLOCKED` reports
the detections, the Verdict's `source`, and its error code.

A cycle MUST re-enter `TRIAGING` from `DETECTING` on every pass rather than reusing an
earlier Verdict, and each re-entry MUST carry fresh evidence with updated
`elapsed_ms` and `attempts_remaining`.

`DETECT_TOTAL_TIMEOUT_SECONDS` (`interstitial.py:43-45`) MUST be recomputed to include
`MAX + 1` triage calls at `TRIAGE_TIMEOUT_SECONDS` each. Every state in this machine
runs inside that marshalled coroutine, so the constant now bounds the whole cycle.

## Error Handling

E001 through E009 are in use (`browser_session.py:153`, `cdp_handler.py:685`, and
others). E003 is the existing interstitial code and keeps its meaning: a challenge
needing manual resolution, with no more specific cause. New codes:

```
E010 - Challenge needs a person and cannot be cleared by waiting
       (severity: warning)
       Raised when: decision table row 3 matches.
       Recovery: stop retrying, report the vendor and what the page asks for.
       Escalation: the caller decides. The tool takes no action of its own.
       Precedence: E010 is narrower than E003 and takes precedence where both
                   would apply. A cycle emits one or the other, never both.

E011 - Interstitial triage unavailable; fell back to type-based policy
       (severity: info)
       Raised when: the triage extra is not installed, TYPESAFE_API_KEY is
                    absent, the triage call timed out, the API returned an
                    error, or the Verdict failed validation.
       Recovery: automatic. Degraded triage produces a heuristic Verdict and
                 the cycle continues under today's decisions.
       Escalation: none. This MUST NOT fail a navigation.
       Emission: at most once per detection cycle. detect_with_retry owns the
                 flag and sets it on the first degraded pass; the per-pass
                 triage call never emits it directly.
```

Version 2 defined E012 for a failed promotion. Promotion is out of scope, so E012 is
withdrawn and the code is not reserved.

Further normative requirements:

- Triage failure MUST NOT propagate to the caller as a navigation failure. The
  existing best-effort contract in `_run_detection` (`interstitial.py:222-226`) is the
  precedent and MUST be preserved.
- The triage call MUST carry a timeout, `TRIAGE_TIMEOUT_SECONDS`. On timeout the cycle
  MUST take the `DEGRADED` transition and MUST cancel the in-flight request.
  `RetryPolicy` on the TypeSafe client MUST be configured so SDK-level retries cannot
  exceed that timeout.
- A Verdict whose probabilities fall outside `0.0` to `1.0`, or whose `vendor` is not
  in the criteria set, MUST be rejected and treated as a triage failure. Typed output
  guarantees the interface, not the values.
- `format_interstitials` MUST continue to return `None` for an empty detection list.

## Security Considerations

**Trust boundary: the challenge page is hostile input.** The page is served by the
system trying to block the tool, and its text becomes model input. A page can carry
text written to steer the judgment. Jev returns typed answers only: a choice and two
probabilities, no generated text, no tool calls. That bounds the output channel.

With promotion out of scope, the reachable consequences of a steered Verdict are:

- waiting up to `MAX * INTERSTITIAL_RETRY_DELAY_SECONDS` longer than necessary;
- reporting a challenge earlier than necessary;
- reporting a genuine challenge as `CLEAR`, so the caller sees a blocked page with no
  warning.

None is an action the tool takes against the user's machine. Version 2 had a fourth,
promotion, which tore down Chrome and seized the screen; removing the gate removes the
only attacker-triggerable action in this design. This narrowing is the main security
benefit of version 3.

Evidence MUST be passed as a named JSON field distinct from the instructions and MUST
NOT be interpolated into an instruction string. The Phase 2 gate MUST include an
adversarial-evidence case: a synthetic page whose body text attempts to steer the
Verdict toward `none`, asserting the resulting transition, not the returned
probability.

**Data sensitivity: the profile is logged in.** browser-tools drives Named Profiles
carrying live sessions. Two rules bound egress:

- The Challenge Gate. Triage runs only on a Challenge Signal, so vendor presence on an
  ordinary authenticated page sends nothing. This is the primary control.
- The Evidence caps in Design, "Evidence collection", including the 8 KiB ceiling, the
  removal of each detection's `details` field, cookie names without values, and `url`
  reduced to origin plus path with no query string.

Request headers, storage contents, and form field values MUST NOT be collected.
Evidence MUST NOT be logged at `INFO` or above.

**Network egress is a new property of this tool.** Today browser-tools makes no
outbound connection except to the browser it drives. Under this RFC it can contact a
third party during a browsing session, which is observable to anyone watching the
host's traffic. It MUST be opt-in twice over: the `triage` extra installed, and
`TYPESAFE_API_KEY` present. With either missing, no request is made and behavior is
today's.

**Credentials.** `TYPESAFE_API_KEY` MUST be read from the environment. It MUST NOT be
written to the registry, which RFC-01 already forbids from carrying credentials, nor
to `BrowserState`, nor to any session file on disk.

**Blast radius.** With triage unavailable, behavior is today's. With triage wrong, the
worst case is a wait of at most nine seconds too long, or a challenge reported as a
clean page. Alpha status and a single maintainer, as RFC-01 records.

**The solving prohibition.** This RFC adds no capability to satisfy an anti-bot check
and normatively forbids adding one, including a vision model or a solving service; see
Design, "Amendment to RFC-01". Version 1 argued instead that solving was impossible
because Jev accepts text only. That argument does not hold: `models.md` instructs
callers to "pre-process non-text inputs (images, audio, video, binaries) into text or
structured fields before sending them", and a text CAPTCHA or a hold-instruction is
expressible in `control_labels` and `body_excerpt`. What prevents solving is the typed
output channel combined with the prohibition, not the input modality.

## Alternatives Considered

**A separate `browser-tools-triage` package.** Jev would sit outside browser-tools,
consuming its JSON output, and RFC-01 would need no amendment. Attractive because it
keeps the default tool provably free of outbound inference. Rejected because the retry
loop is the only consumer and it runs inside the package, reached from
`mcp_daemon.py:497` and `curated.py:270`. An external consumer sees the challenge only
after the wait is already spent, which is the entire saving. Version 1 rejected this on
a claim about promotion that was wrong; the rejection now rests only on the retry loop,
and on that ground it is sound.

**Triage in the agent layer only.** The browser-tools agent skill and the
`chrome-devtools-axi` wrapper would make the calls, with no package change and no RFC.
Attractive for its near-zero cost. Rejected for the same reason: it cannot reach the
loop that spends the time.

**Better regular expressions.** Splitting `cloudflare_challenge` into a challenge type
and a block type, and growing the retry set, would fix defect 2 specifically.
Attractive because it costs nothing and adds no dependency. Rejected as a complete
answer because the label still has to be maintained per vendor. It remains the
fallback if Open Question 4 resolves against the model, and Phase 0 serves it either
way.

**A general-purpose language model.** Attractive for vision input, which would reach
visual challenges. Rejected because it would put the tool in the business of solving,
which this RFC normatively forbids, and because it returns generated text to parse with
latency in seconds rather than the sub-second budget the detection path allows.

**Keeping the string confidence and thresholding on it.** Attractive as the smallest
change. Rejected because the strings are author-assigned per emit site and carry no
calibration, so a threshold over them encodes the script author's guess with a decimal
point added.

## Implementation Plan

The RFC-01 amendment is not a phase. It takes effect when this RFC is accepted, and
the edit to RFC-01 lands with that acceptance.

**Phase 0: collection and the heuristic Verdict, no model.** Fix precondition 1, the
retry-loop double-count. Add `challenge_evidence.py`, the dataclasses, the Challenge
Gate, and the complete heuristic mapping. Widen the script return shape and
`parse_detection_result`. Route `detect_with_retry` through the heuristic Verdict.
Gate: the existing suite green with `test_non_array_returns_empty` amended; a new test
proving a bare-list override script still works; a characterization test asserting that
for each of the ten detection types, and for mixed retryable-plus-non-retryable lists,
the new path produces the same transition and the same reported detections as the
pre-Phase-0 code, with the double-count corrected and that correction asserted
explicitly. Phase 0 changes behavior in exactly one way, and the test names it.

**Phase 1: the adapter, observation only.** Fix precondition 2, future cancellation.
Add `challenge_triage.py`, the `triage` extra, the async client, the timeout, and the
fallback. The model Verdict is computed and recorded; the heuristic Verdict controls
every decision. No threshold can make a live gate decision-neutral, so this phase
routes around thresholds rather than setting them to impossible values. Gate: install
matrix covering default install, `[triage]` with a key, `[triage]` without a key, and
an API that times out, each completing a detection; plus a recorded log of paired
heuristic and model Verdicts, which is the corpus Phase 2 needs.

**Phase 2: the gate.** Turn on the decision table with thresholds set from the Phase 1
corpus. Gate: Open Question 4 answered from the corpus; the adversarial-evidence case
in Security Considerations; and no regression against the Phase 0 characterization
baseline.

**Phase 3: documentation.** `CONTEXT.md` gains Challenge Gate, Interstitial Evidence,
Interstitial Verdict, and Interstitial Triage. The agent skill and README describe the
extra and what it changes. The README's architecture diagram and its
`browser-tools navigate` example are stale independently of this RFC and SHOULD be
corrected in the same change.

## Open Questions

The decision table, the precedence rules, the complete heuristic mapping, the Challenge
Gate, and the prediction horizon are settled in Design. What remains is measurement.

1. **What are the four threshold values?** `SELF_SOLVING_WAIT_THRESHOLD`,
   `NEEDS_HUMAN_THRESHOLD`, `VENDOR_NONE_CONFIDENCE`, and `TRIAGE_TIMEOUT_SECONDS`.
   Needed to decide: the Phase 1 corpus, scored through the three questions with
   observed outcomes attached. **Recommendation:** set each from the corpus, with
   `VENDOR_NONE_CONFIDENCE` highest, because a wrong `CLEAR` hides a real challenge
   from the caller while a wrong wait costs three seconds. **Decider:** Kevin, on the
   Phase 1 corpus.

2. **What counts as a pass in Phase 2?** Aggregate accuracy can improve while the more
   damaging error worsens. Needed to decide: which of three rates the gate reads, namely
   unnecessary wait, missed wait, and challenge wrongly reported clear.
   **Recommendation:** wrongly-reported-clear is the blocking metric and MUST NOT
   regress against the heuristic, which never emits `CLEAR` on a detection; the other
   two are reported and traded off. **Decider:** Kevin, before the corpus is collected,
   because it changes the labels.

3. **Does triage meet the latency budget?** The vendor documents about 100ms; this RFC
   treats that as a claim to measure. The detection path can absorb 100ms per pass and
   cannot absorb 500ms. Needed to decide: measured p50 and p99 from this machine against
   the real endpoint with the real payload. **Recommendation:** measure in Phase 1 and
   set `TRIAGE_TIMEOUT_SECONDS` at the measured p99. If p99 exceeds 500ms, this RFC's
   latency argument fails and Phase 2 should not proceed. **Decider:** Kevin.

4. **Does the model beat the regular expressions on the corpus?** This RFC assumes it
   does and that assumption is untested on this domain. Needed to decide: the Phase 1
   corpus of paired Verdicts against labeled outcomes. **Recommendation:** if the model
   does not beat the heuristic on the Question 2 metrics, take the "better regular
   expressions" alternative and withdraw Phase 2. Phase 0 is useful either way.
   **Decider:** Kevin, on the Phase 1 corpus.

5. **Is the MCP front worth optimizing?** This RFC's saving lands on the optional MCP
   front and on an explicit `detect`. RFC-01 made the CLI canonical and demoted the
   daemon. Needed to decide: whether the MCP front is still used enough to justify the
   dependency, or whether this RFC should wait for a CLI navigation path.
   **Recommendation:** land Phase 0, which needs no dependency and fixes a real defect,
   then decide Phases 1 and 2 on that answer. **Decider:** Kevin.

## Changes in this revision

**Version 3** (2026-09-20) narrows the RFC after a finding that came out of Open
Question 5 in version 2:

1. **Promotion removed from scope.** `_maybe_promote_on_auth_wall` has no production
   caller. It is reached only through `dispatch_session_tool` (`browser_session.py:855`),
   whose only external entry is called from `tests/test_e2e_backend_seam.py:44`, and the
   tool-proxy app it served was retired in RFC-01 Phase 4. Removed: the promotion gate,
   the `is_auth_wall` question, `AUTH_WALL_PROMOTION_THRESHOLD`, the Verdict transport
   section, the `HANDOFF` and `PROMOTED` states, error code E012, precondition 3 on
   teardown ordering, and old Phase 2. The title drops "and handoff".
2. **The applicable surface is now stated up front.** New Introduction section names the
   two callers of `run_post_navigation_detection` and records that the canonical CLI has
   no `navigate` verb, so triage never fires there automatically.
3. **Open Question 5 replaced.** The placement question is resolved: with promotion gone,
   the retry loop is the only consumer and it lives in-package, so the separate-package
   alternative fails on its own merits rather than on the wrong claim version 1 made. The
   new Question 5 asks whether the MCP front is worth optimizing at all.
4. **The security position improves.** Removing promotion removes the only
   attacker-triggerable action in the design. The blast radius is now a wait of at most
   nine seconds or a challenge reported as clean.
5. **Three questions, four table rows, six states, two error codes, two preconditions.**
   Down from four, five, eight, three and three.

**Version 2** (2026-09-20) answered review
[`review-01`](02_calibrated-interstitial-triage-and-handoff.review-01.md), nineteen
findings from `gpt-6-astra@codex` (openai) and `muse-spark-1.3-contributor@muse`
(meta): 17 applied, finding 8 conceded, finding 16 applied with one sub-claim rejected.
Version 3 removes the parts of that response that addressed promotion, namely findings
3 and 6 and the `HANDOFF` half of finding 4, by removing what they were about. The
full per-finding record is in review-01 and in the version 2 block above. The version 2
document itself was not retained; version 1 is at
`~/.agents/tasks/browser-tools-rfc-02-jev/rfc-v1.md`.

The filename keeps the original `-and-handoff` slug so review-01's links and RFC-01's
numbering convention stay intact, although the title no longer carries "and handoff".

## References

### Normative

- [RFC-01: Merge chrome-agent core into browser-tools](01_merge-chrome-agent-core-into-browser-tools.rfc.md) -
  Accepted; this RFC amends its inference prohibition at `:31` and `:248` and inherits
  its packaging, testing, and security conventions. Its Phase 4 tool-proxy retirement
  is why promotion is out of scope.
- [Review-01](02_calibrated-interstitial-triage-and-handoff.review-01.md) - the
  two-family review of version 1.
- [State](https://docs.typesafe.ai/concepts/state.md) - the input contract, including
  the instruction to pre-process non-text input.
- [Primitives](https://docs.typesafe.ai/primitives.md) - Choice, Noul, and Score, and
  which to use for which kind of judgment.
- [Models](https://docs.typesafe.ai/models.md) - `jev-1.13.0`, the 64k and 32k token
  budgets, pricing, and rate limits.
- [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) - the keyword definitions.

### Informative

- [How to build with System One](https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md) -
  the code-owns-the-workflow model this design follows, and the source of the ~100ms
  latency claim in Open Question 3. Neither reviewer could retrieve this page.
- [Confidence](https://docs.typesafe.ai/confidence.md) - how confidence differs from
  probability, which is why `confidence` and the two probabilities are separate fields
  on the Verdict, and why decision table row 1 reads `confidence`.
- [Confidence-gated routing](https://docs.typesafe.ai/patterns/confidence-routing.md) -
  the pattern the decision table implements.
- [Speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md) - why the three
  questions go in one request.
- [Python SDK](https://docs.typesafe.ai/sdk/python.md) - `AsyncTypeSafeClient`,
  `RetryPolicy`, and the response shape the adapter parses.
- [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md) - known
  limitations of the pinned model version, relevant to Open Questions 2 and 4.
