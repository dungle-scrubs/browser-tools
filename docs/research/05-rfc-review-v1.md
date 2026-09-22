<!-- Committed as evidence with RFC-05. -->

> **What this is.** An adversarial review of RFC-05 **version 1**, produced by a
> model from a different family than the one that drafted it, running read-only
> with no shell. Version 2 of the RFC applies all sixteen findings. Line
> references of the form `05-draft:NN` point at version 1 and do not resolve
> against the committed document.
>
> Two of its findings were checked against the repository and changed the RFC
> materially: RFC-03 does normatively decline `heap`, `lighthouse` and the
> trace-insight verbs, and it does fix the step surface as a closed list. A
> third check, prompted by finding 13, found an error the review had not
> claimed: documentation defect 6 was an absence of a statement, not a wrong
> sentence, and version 2 restates it.

# Adversarial review: RFC-05 draft (retire chrome-devtools-axi)

Scope reviewed: `05-draft:1-600` in full **(observed)**. Compared against `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md`, `docs/rfc/03_run-many-steps-in-one-invocation.rfc.md`, `docs/rfc/04_reach-into-cross-origin-iframes.rfc.md`, `CONTEXT.md`, `src/browser_tools/GUIDE.txt`, `src/browser_tools/endpoint.py:1-42` **(observed)**. No shell available; every "Measured:" probe cited to `research/*` branches or `docs/research/` could not be opened from this session, so all such figures are treated as **unverified** below rather than accepted or denied.

## Blocking

### 1. Builds what RFC-03 normatively declined, without saying so
- Section: `05-draft:22-27` (Abstract scope), `05-draft:341-364` and `05-draft:178-260` (Design 2, 5), `05-draft:262-310` (Design 3); contradicted standard: `docs/rfc/03_run-many-steps-in-one-invocation.rfc.md:101-109`.
- What is wrong: RFC-03 declined `heap`, `lighthouse`, and the trace-insight verbs by name, on two grounds: the RFC-01:36 new-capability boundary and the packaging discipline that the default install depends on `websockets` only with `chrome-devtools-axi` keeping the job **(documented)**. This draft builds `heap`, `trace`, `insights`, and a Lighthouse recipe without one sentence stating it reverses or amends that decline.
- Why it matters: the prior reviews require exactly this sentence. The RFC-04 review states the pattern: "RFC-01's exclusion applies to that merge effort; RFC-04 should explicitly authorize this subsequent capability" **(documented)**. Silence leaves an implementer and a later reader with two accepted documents that flatly disagree about whether these verbs may exist.
- Fix: add one normative paragraph, e.g. under Decisions or Design 2/3/5: "This RFC reverses the RFC-03 Scope decline of heap, lighthouse, and trace-insight verbs for the reasons in ADR 0001," and cite `docs/adr/0001-trace-insights-in-bt.md`. The existing `05-draft:524-527` ("Recommended against building them... the driving dev decided to build them") is about a recommendation, not about the RFC-03 normative text, and does not satisfy this.

### 2. Extends RFC-03's closed step surface without amending it
- Section: `05-draft:258-260` ("`heap` is a valid Step List step. `trace` is not...").
- What is wrong: RFC-03's step surface is a closed list: "A step MUST be one of these, and nothing else" **(documented)** (`docs/rfc/03_run-many-steps-in-one-invocation.rfc.md:314-326`). Adding `heap` as a step changes that list, and the draft never states the amendment. It is also silent on whether the six new verbs in `05-draft:128-135` (`eval`, `press`, `hover`, `type`, `wait-text`, `network-get`) are steps at all.
- Why it matters: either answer breaks something. If they are steps, every RFC-03 validation, refusal, and domain-hygiene rule must be shown to cover them. If they are not steps, a run cannot use the most-used verbs in the record, which undercuts the retirement case. An implementer cannot choose silently.
- Fix: state the amended step surface explicitly (add `heap` and whichever of the six qualify, with reasons), or state that none of the new verbs is a step and justify how runs remain useful. Either way, label it "Amendment to RFC-03, The step surface."

### 3. `trace --steps` creates a second Step List runner outside the `run` contract
- Section: `05-draft:212-217`, `05-draft:246-248`, `05-draft:499`.
- What is wrong: RFC-03 pins validation-before-step-1, the `[INSTANCE]`/`--endpoint`/`--target`/`--url` exclusion, refusal handling, timeout semantics, and the Run Document shape with its ordered caller contract **(documented)** (`docs/rfc/03_run-many-steps-in-one-invocation.rfc.md:623-650`, `:681-722`, `:752-795`). `trace --steps` runs a Step List through `step_run.execute(steps, handler, ...)` **(observed)**, prints "the Run Document with a `trace` object beside `run`" **(observed)**, yet specifies none of: whether `run`-validation applies, which refusals are checked at validation vs at the step, what `--timeout` means when both a duration and a step list bound the capture, or which exit code wins when "the run stops at that step and the trace is still written" (`05-draft:499`) **(observed)**.
- Why it matters: a step failure plus a successful capture is simultaneously exit 1 (step failed) and exit 0 (trace written). The draft picks neither. Two runners for one Step List grammar will also drift exactly the way RFC-03's argparse-reuse rule was written to prevent **(documented)**.
- Fix: either route `--steps` through the `run` verb's validation/document/exit-code path by reference (one shared function, per the RFC-03 rule), or fully specify the trace-runner's own validation table, refusal table, timeout precedence, exit codes, and document schema.

### 4. The document declares itself non-normative against the tickets
- Section: `05-draft:43-47` ("Where this RFC and a ticket disagree, this RFC is wrong and the ticket is right, because the ticket holds the evidence").
- What is wrong: that precedence rule means the RFC cannot be built from alone; the implementer must read twelve issues, five of them agent-resolved with no human review **(observed)**. The evidence itself is not reachable from the repository as cited: References points at `research/*` branches "each at `docs/research/`" (`05-draft:594-597`) **(observed)**, which this review could not locate, and tickets #148/#154-#159 are given as bare numbers with no URLs (only #147 has one, `05-draft:592`) **(observed)**.
- Why it matters: every "Measured:" figure in the draft inherits this indirection. A reviewer cannot verify, and an implementer cannot build, from a document that defers to evidence outside itself while citing that evidence unreachably.
- Fix: either commit the probe transcripts/figures the design rests on (the RFC-04 review required exactly this: "Commit the runnable probes and fixtures" **(documented)**) and give full ticket URLs, or invert the precedence so the RFC is normative and the tickets are informative.

## Major

### 5. Abstract overstates the headcount evidence
- Section: `05-draft:18-20` ("axi carried roughly 23,000 verb invocations") vs `05-draft:56-58` ("counting mentions... as a proxy"; "The ranking, not the absolute numbers, is what the design rests on").
- What is wrong: the body admits a mentions-as-proxy method and rests only on ranking **(observed)**; the Abstract drops the qualifier and asserts invocations **(observed)**. "Never used: `heap`, `perf-start`, `perf-stop`, `drag`, `back`" (`05-draft:65`) **(observed)** is likewise categorical on proxy data, and "in one project" (`05-draft:64`) carries no source at all **(unverified)**.
- Why it matters: the retirement destination (Decisions row 1) leans on this count. A proxy is fine; presenting it as a census is not.
- Fix: carry the qualifier into the Abstract ("about 23,000 mentions, used as a proxy for invocations; ranking only"), soften line 65 to "no mentions found in the sampled transcripts," and source or drop "in one project."

### 6. Section 6's foundation (Chrome 144 approval-based debugging) is uncited
- Section: `05-draft:105-109`, `05-draft:398-402`.
- What is wrong: the claims that Chrome 144+ "returns 404 for `/json/version` by design and admits only WebSocket paths under `/devtools/browser`," with the port/path in `DevToolsActivePort`, have no URL or `file:line` anywhere in the draft **(observed)**. The whole attach design, the `--endpoint` reachability argument, and the `launch --auto-connect` rejection rest on it.
- Why it matters: if the behavior is version-, channel-, or flag-specific rather than "by design," the thin primitive may be wrong on arrival.
- Fix: cite the Chrome/Chromium source or official docs for the 404-by-design claim, the `/devtools/browser` path constraint, and the port-file format, or mark the section provisional on the verification in `05-draft:429-437`.

### 7. The attach primitive is specified without a flag name and conflicts with current endpoint validation
- Section: `05-draft:404-416` (Design 6, attach).
- What is wrong: "Port-file discovery behind a verb-level flag" names no flag **(observed)**. "A raw `ws://...` form for `--endpoint`" conflicts with the shipped validator, which accepts only `http`/`https` URLs and returns a TCP port (`src/browser_tools/endpoint.py:70-114`) **(documented)**; the draft never states the amendment. "A bounded wait" gives no bound **(observed)**. `DevToolsActivePort` is named without its directory resolution per channel/OS, which is the entire discovery problem **(observed)**. The heading "a thin primitive, ungated by nothing" then says "Every existing External Endpoint refusal, unchanged: loopback only" (`05-draft:414`) **(observed)** - the heading and the body disagree about whether gating changed.
- Why it matters: this is machine-made row 12, and as written no implementer can build it: no flag name, no grammar for the `ws://` form, no validation amendment, no wait value.
- Fix: name the flag, give its exact grammar, state the `resolve_endpoint_port` amendment (or that `ws://` bypasses it and how loopback is then enforced), give the wait bound and the diagnostic text, and fix the heading.

### 8. None of the new verbs has a complete contract (machine-made rows 8-10 directly affected)
- Sections: `05-draft:128-176` (six verbs), `05-draft:180-260` (`trace`, `heap`), `05-draft:264-310` (insights), `05-draft:314-339` (`--dialog`).
- What is wrong: exact gaps, each **(observed)**:
  - `eval`: output document shape unspecified; `--url SUB` semantics for eval unspecified; whether `--await` without a promise is an error unspecified.
  - `press`: `--modifiers` value set unspecified; key-name vocabulary unspecified; output shape unspecified.
  - `hover`: output shape unspecified.
  - `type`: `<text>` vs `--file` exclusivity and neither-given behavior unspecified; output shape unspecified.
  - `wait-text`: default timeout, match-output document, and timeout exit code unspecified (contrast the fully specified `wait` contract in RFC-01 **(documented)**).
  - `network-get`: what is printed vs written to `--response-file` unspecified; requestId-vs-URL precedence when both given is parseable from the syntax line but its error when neither is usable unspecified beyond one Error Handling row.
  - `trace`: `--duration` units (line 181 shows `[--duration 5]`, unitless) unspecified; neither-flag default unspecified; `--categories` syntax (list form? separator? repeatable?) unspecified; no-`--steps` stdout shape unspecified; the `trace`-object field names/types for "events, bytes, `dataLossOccurred` and the path" unspecified.
  - `heap`: stdout shape unspecified (only `--out FILE` is given).
  - `insights`: the "stable outer fields" (key, state, title, metric savings, detail) lack field names, types, and the shape of "metric savings"; unknown `--insight NAME` behavior unspecified; the pinned engine revision and validated Chrome range values are promised (`05-draft:302-303`) but not given.
  - `--dialog`: applicable verb set is "the verbs that can trigger a dialog, which today is `click`" (`05-draft:318`) - but `eval` running `alert()` trivially triggers one, so the set is wrong or underspecified; multi-dialog-in-one-invocation behavior unspecified; the recording's field names unspecified.
- Why it matters: prior RFCs in this house specify verb contracts to the field-name level (cf. RFC-03 Run Document, RFC-04 snapshot merge shape) **(documented)**. These read as decided but leave the builder guessing on every verb.
- Fix: for each verb give the stdout JSON schema (field names/types), the full flag grammar with defaults, and the exit-code table. For insights, either pin the revision/range values now or mark the section a sketch pending Open Question 4.

### 9. Terminology drift, both directions
- Section: `05-draft:68-90` vs `CONTEXT.md:10-142`.
- Defined and never used: **Insight Set** (`05-draft:86-87`) never appears again as that noun; later text says "insight models" (`05-draft:277-280`) and "per insight" (`05-draft:296`) **(observed)**.
- Used and never defined (and absent from `CONTEXT.md`): **curated verb / capture verb / analysis verb** as nouns (Abstract's three categories, `05-draft:22-25`) - Terminology defines only the *rule*, and `CONTEXT.md` has no Curated entry **(observed)**; **`transferMode: ReportEvents / ReturnAsStream`** (`05-draft:193-199`) with no CDP citation **(observed)**; **AUTO_CONNECT / `launch --auto-connect`** (`05-draft:105`, `05-draft:418`) as a capability noun **(observed)**; **the `trace`-object / Run Document extension** (`05-draft:246-248`) without defining its fields **(observed)**.
- Redefinition without a landing note: **Bounded Capture** is "Defined in `CONTEXT.md` today as the whole of `screencast`" and generalised to a family (`05-draft:70-73`) **(observed)**. `CONTEXT.md:45-49` confirms the current narrow definition **(documented)**. RFC-04's Terminology explicitly asked for its nouns "to land there when it is built" **(documented)**; this draft never says `CONTEXT.md` must record the generalised meaning.
- Why it matters: the house rule from the prior reviews is both-directions checking (defined-used, used-defined) **(documented)**. The `transferMode` and Insight-Set gaps are the kind that later cause a builder to implement the wrong thing (event stream vs stream handle are different CDP mechanisms).
- Fix: use Insight Set or delete it; define the three verb-category nouns or stop using two of them; cite the CDP `Tracing` domain for the transfer modes; add "CONTEXT.md MUST record the generalised Bounded Capture" to the build list.

### 10. Dialog scope understates the trigger set
- Section: `05-draft:318` ("the verbs that can trigger a dialog, which today is `click`").
- What is wrong: `eval` evaluating page JavaScript can raise `alert`/`confirm`/`prompt` as directly as a click handler **(unverified against the cited probes, but follows from the verb's own definition at `05-draft:129`)**. `press`/`type` can also submit into handlers that dialog. "Today is click" is either false or means "only click is instrumented," which contradicts the `--dialog` flag being offered "on `run`" where any step could dialog **(observed)**.
- Why it matters: a dialog raised by an uninstrumented verb reproduces the exact unrecoverable hang Design 4 is built to eliminate (`05-draft:321-325`) **(observed)**.
- Fix: specify the instrumented set exactly (per verb, including `run` steps), or specify that the policy subscribes for every verb carrying `--dialog` and name which verbs carry it.

### 11. Insights acceptance rests on one trace; the draft admits the flip test is unbuilt (machine-made row 8)
- Section: `05-draft:277-293`, `05-draft:584-586` (Open Question 4).
- What is wrong: acceptance is "19 of 19 insight models... correctly returning `RenderBlocking: fail`" on one 4041-event capture plus cost figures **(observed)** (`05-draft:277-285`), all **unverified** here. Open Question 4 then states the cross-version fixture matrix "has not been built, and it is the test that would flip decision 8" **(observed)**. The packaging mechanism is also underspecified: "ships a `package-lock.json` and installs with `npm ci`" (`05-draft:291-293`) never says who runs `npm ci` (pip extra? first-run download? vendored script?) or where the 17-24 MB lands **(observed)**. "Takes no network access" (`05-draft:505-507`) states no enforcement mechanism **(observed)**. The upstream-instability quote (README "not for public consumption") and the `third-party-web` 0.30.0 timing claim (`05-draft:274-275`, `:287-290`) carry no URLs **(unverified)**.
- Why it matters: a single-trace, single-version acceptance plus an unbuilt drift matrix does not defensibly carry a new Node runtime and a supply-chain surface. The draft is honest about this in Open Questions, which is why this is Major rather than Blocking - but row 8 should be marked provisional, not taken.
- Fix: mark Decision 8 provisional pending the OQ4 matrix; specify the `npm ci` trigger, install location, offline behavior, and network-access enforcement (or drop the "takes no network access" claim); add the two missing citations or soften to "the adapter author reports."

### 12. Instance-naming close-out rests on n=20 for a 3,200-mention gap (machine-made row 11)
- Section: `05-draft:375-394`, esp. `05-draft:378-382`.
- What is wrong: "Of 20 sampled uses, zero were parallel workers and 17 were..." **(observed)** generalises from 20 to 3,200 without a sampling method **(observed)**, in a document that elsewhere shows transcript counts are echo-prone ("328 apparent uses... were all echoes of help text," `05-draft:437`) **(observed)**. The "argument that settles it" (a chosen name is no more stable than a derived one, `05-draft:384-386`) is reasoning, not evidence **(observed)**. The genuinely hard cases listed (unmappable `repo-01`-`repo-04`, silent rebind after relaunch, registry loss, `05-draft:390-394`) are recorded as revisit triggers but left unaddressed by the shipped documentation fix **(observed)**.
- Why it matters: if silent rebind is real, documentation does not fix it; the next agent still drives the wrong instance. That is a safety-relevant close-out on thin evidence with no human reviewer.
- Fix: name the sampling method and its limits, state the metric that reopens `--name` (e.g. count of rebind incidents), or downgrade row 11 to "interim: documentation now, `--name` revisited after N weeks of rebind telemetry."

## Minor

### 13. Unverifiable source and measurement citations
- `05-draft:451` cites `cdp_handler._target_selector`; `05-draft:216` cites `step_run.execute(steps, handler, ...)`; defect 6's `GUIDE.txt`/`README.md` sentence is confirmed in `GUIDE.txt:591` ("rather than opening a second browser on the same directory") **(documented)** but the `README.md` half was not checked here **(unverified)**. Target-selector and step-run symbols were not located with the available tools **(unverified)** - they may exist under names this review could not resolve without a shell, so treat as citation gaps, not falsehoods.
- "Playwright auto-dismisses by default" (`05-draft:337`), "the DevTools set that `chrome-devtools-mcp` uses" (`05-draft:227`), and the `press`/`hover`/`wait-text` probe figures (`05-draft:147-160`, `:165-168`) all lack a URL or probe path **(unverified)**.
- Fix: add `file:line` or URL to each, as the prior reviews' citation audits required **(documented)**.

### 14. Six defects promised, seven listed
- `05-draft:25-26` promises six documentation defects; `05-draft:443-462` lists six numbered plus an unnumbered "Add to these" dialog-hang note **(observed)**. Fix: number it as 7 or fold it into item 3's entry.

### 15. Decisions rows 6-7 decider is "rule," not a party
- `05-draft:568-569` lists Decider "rule" for verb-gap classification and the Lighthouse recipe **(observed)**. A rule does not decide; someone applied it. Given five machine-made rows are called out and these two are not, the reader infers human application, but it is never stated **(observed)**. Fix: name the decider (dev or machine) for rows 6-7.

### 16. Retirement checks lack methods
- `05-draft:485-487` ("Confirm the plugin resolves its own copy first") gives no command or config path **(observed)**; `05-draft:470` ("Rewrite the Browser Automation section") ships no replacement text or content contract **(observed)**. Fix: give the check command and either the replacement section or a pointer to the writing ticket that owns it.

## What was checked and found sound

- The `launch --auto-connect` rejection is correctly grounded: `src/browser_tools/endpoint.py:7-14` states the no-registry-entry safety property and the two `rmtree` paths **(documented)**, and the draft cites it at `05-draft:420-422` **(observed)**. The line-number citation holds.
- The Node reopening is explicit, not silent: Abstract (`05-draft:36-39`) and Alternatives (`05-draft:535-538`) both state that `bt insights` reopens the door RFC-01 closed and why, and the base install stays `websockets`-only (`05-draft:308-309`) **(observed)**, consistent with RFC-01 Packaging **(documented)**.
- The payload-reachability argument is consistent with RFC-03's no-data-flow rule ("no way for one step to use another step's output," `GUIDE.txt:269-272` and RFC-03 Design) **(documented)**; tracing/heap/response-body as curated-or-nothing follows from that premise if the transfer-mode facts hold (those facts are **unverified** here, the inference from them is sound).
- The extra-absent exit-2 rule (`05-draft:497`) matches RFC-01's "fail with the exact `pip install` line" packaging rule **(documented)**.
- The `-NN` suffix claim (`05-draft:100-101`) matches RFC-01's adopted derivation/collision rules **(documented)**.
- The dialog-recording requirement ("recorded in the output... never silent," `05-draft:337-339`, `:500`) and the `dataLossOccurred`-never-swallowed rule (`05-draft:243-244`, `:496`) are the right shape for fail-loud rather than fail-silent behavior.
- Open Questions 1-5 are honestly scoped unknowns (page- vs instance-scoped trace, trace size, `--steps` stdin, drift matrix, headed-Lighthouse safety) **(observed)**; OQ4's admission strengthens rather than weakens the document, and is the basis for finding 11.
- The Decisions table arithmetic is consistent: 5 dev + 2 rule + 5 machine-made = 12, matching "seven... with the driving dev present" only if the two rule-applied rows count as dev-present - which finding 15 asks to be stated.

## Coverage note

Checked all twelve decisions against the evidence the draft itself provides, per the brief's item 6: rows 1-7 are taken as input (prior human/rule provenance asserted but not re-audited here); rows 8-12 are judged above (8 provisional, 9 defensible-but-underscoped, 10 defensible, 11 thin, 12 direction-sound but unbuildable). All cited measurements are **unverified** by this review because the cited `research/*` / `docs/research/` evidence was not reachable in this session and the draft's own precedence rule points past itself to the tickets. The single most valuable fix is finding 4 (commit the probes, make the RFC normative); the single most blocking substantive fix is finding 1 (explicitly amend RFC-03 scope).
