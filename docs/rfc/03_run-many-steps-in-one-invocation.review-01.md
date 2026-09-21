# Review: RFC-03 Run many steps in one invocation

## What was reviewed

- **RFC:** `docs/rfc/03_run-many-steps-in-one-invocation.rfc.md`
- **Version:** 2. Snapshot SHA-256 `a0e187d18b9a5fcb95e84afae5eec49f9b015f0847e1618d50727662c7f110be`. **The RFC is now at version 3**, which
  was written in response to this review; see "What version 3 did with each
  finding" at the end of this file. Every finding below is against version 2, so
  line numbers in the findings refer to that snapshot, not to the current file.
- **Status:** Draft, before and after.
- **Repository:** `browser-tools` at `bf87ad8`, clean apart from the untracked RFC.
  The reviewer edited no file, and could not: it ran with `--disable-write` and
  `--disable-shell`.
- **Reviewers:** one cross-family pass plus one author pass.
  - `muse-spark-1.3-contributor@muse` (family meta), high effort, read-only, routed
    with `excludeFamilies: ["claude"]` because a Claude model drafted the RFC. The
    route was chosen from the model registry, which selected it independently as
    the cheapest route clearing the `plan` floors at high stakes. It ran on its
    intended route with no fallback walk; the `gpt-6-astra@codex` fallback was not
    used. Findings are unprefixed, as the reviewer wrote them.
  - One author pass by the drafting Claude session, run while the cross-family
    review was in flight. **Not independent.** It produced two findings recorded in
    the measurements file rather than here: the domain-enable replay measurement,
    and the count of frame-selection consumers that withdrew the RFC's lead
    motivation.
- **Requirement check:** the brief declared seven requirements.
  `delegate/scripts/verify-result.ts` reported all seven met, no gaps.
- **Brief:** `docs/rfc/03_run-many-steps-in-one-invocation.review-brief.md`,
  committed beside this file so the review is auditable against what it was asked.

**Editor's note on paths.** The reviewer ran against the drafting session's task
directory and refers to files under `.scratch/rfc-03-run/`, which is git-ignored.
Those references are left as written, because this section is verbatim. Every
number and harness they point at is in
`docs/rfc/03_run-many-steps-in-one-invocation.measurements.md`, including the raw
per-run data in its appendix.

## Verdict and findings, as the reviewer wrote them

Everything from here to "What version 3 did with each finding" is the reviewer's
output, unedited.

---

## 1. Verdict: accept with changes

The core shape stands. The restricted model, the step surface, and the provisional Run Document fit the codebase. But two blocking defects stop a build. The one-runtime requirement names no mechanism for five verbs and ignores behavior changes that follow. The exit-1 amendment breaks the letter of RFC-01 while claiming RFC-01 needs no change. Both need new normative text. The frame-navigation rule needs a no-match clause. Fix those and the RFC is buildable.

## 2. Blocking findings

**B1. One CDPRuntime for all steps is required but not specified. (spec defect)**

RFC-03:300-315 says every step MUST run over one CDPRuntime and admits five verbs need a new runtime-routed path, then states this "as a requirement on the contract" with no phasing and no mechanism.

Routing changes observable behavior in at least three ways the RFC never mentions:

- Domain enablement persists. The handler enables `Page` and `Runtime` at connect (`src/browser_tools/cdp_handler.py:412-413`). One-shot verbs get a fresh session with default domain state each time (`src/browser_tools/one_shot.py:52-116`). Over a shared runtime, a `network-list` step leaves `Network` enabled for every later step.
- Session scoping differs. One-shot verbs attach an isolated flattened `Target` session per verb (`src/browser_tools/one_shot.py:104-108`). The handler connects page-scoped (`src/browser_tools/cdp_handler.py:404-409`). Browser-level methods sent with different session scoping can answer differently.
- Subscription hygiene is unspecified. Current `wait` and `collect_on_session` unregister in `finally` (`src/browser_tools/events.py:367-368`, `src/browser_tools/list_verbs.py:114-116`). The RFC specifies no per-step subscribe and unsubscribe discipline for the shared session.

RFC-01:194 requires attach subscriptions isolated per session. A Step Run multiplexes many steps over one session, so the isolation unit silently becomes the step. That violates the spirit of the rule. Unrelated events are dropped when no handler is registered (`src/browser_tools/core/cdp_client.py:155-161`), so leakage needs a registered handler, but a later step registers its handler into a session whose enabled domains earlier steps changed.

What must change: specify the session each runtime-routed step sends over, the per-step subscribe and unsubscribe rule, and whether domain enables persist or reset between steps. Or phase the five verbs out of version 1.

**B2. The exit-1 amendment violates RFC-01:204 in letter while claiming no amendment is needed. (spec defect)**

RFC-01:200-204 says a failed operation MUST NOT report a success field. RFC-03:478-480 claims compliance because `run.status` is never `"ok"` on failure. But a failed Run Document carries per-step `"status": "ok"` entries (RFC-03:457-466). Those are success fields inside a failed operation's stdout. The RFC redefines "operation" from verb to step without saying so, while RFC-03:30 and RFC-03:486-491 claim RFC-01 needs no amendment.

The hazard is concrete. Each successful step's `result` is byte-identical to what that step prints alone (RFC-03:474-476). A caller written against the old rule that parses stdout without gating on the exit code reads a partial success as a success. `run.status` guards only callers that know about `run`, which old callers do not.

What must change: amend RFC-01 explicitly to scope the success-field rule per step inside a run, or restructure the failed document so it carries no `"ok"` entries. State the caller contract: exit code first, then `run.status`, then step entries.

## 3. Major findings

**M1. The frame-navigation rule omits the no-match case, and the stale selection survives. (spec defect)**

RFC-03:328-334 says the selection re-points at whatever frame now matches. The code only re-points on a match (`src/browser_tools/frame_manager.py:349-353`). When nothing matches, `_selected_frame_id` keeps its old value. A later frame-scoped step then acts on a frame whose URL no longer matches the pattern instead of failing with "No frame selected". The detach claim (RFC-03:335-339, citing `src/browser_tools/frame_manager.py:313-314`) is half true: detach clears the frame id but keeps `_selected_url_pattern`, and a later navigated event can resurrect the selection through that retained pattern. Reads fail in between because `get_selected_frame` returns `None` on a cleared id without consulting the pattern (`src/browser_tools/frame_manager.py:223-224`).

What must change: specify no-match behavior (clear the id when re-resolution finds nothing) and state that the pattern half survives detach.

**M2. The 50% figure covers only handler-routed verbs. (evidence gap)**

`floor_probe.py:20-34` exercises `wait_idle`, `take_snapshot`, `list_frames`, `select_frame`, `get_frame_storage`, `reset_frame`, `wait_stable`. None of the five verbs that need new runtime paths (`passthrough`, `wait`, `console-list`, `network-list`, `screenshot`) appears in the probe or in `flow.sh`. The measured 1.70 s to 0.85 s saving is verified from the raw files (medians 1696.3 ms and 849.1 ms match `.scratch/rfc-03-run/raw-flow.tsv` and `.scratch/rfc-03-run/raw-floor.tsv`), but it validates only the verbs that already share the handler transport.

What must change: label the figure as a handler-verbs-only floor, or measure a flow that contains the five.

**M3. The RFC never says what a mid-run usage error does. (spec defect)**

Validation MUST catch every malformed step before step 1 (RFC-03:375-377). Step parsing is a new parser (`shlex` plus per-verb flag parsing, RFC-03:203-210) that must mirror argparse exactly, including every exit-2 versus exit-1 boundary. If it diverges and step 5 fails validation at runtime after steps 1 to 4 ran, no row of the Error Handling table (RFC-03:616-631) covers it: the exit-2 rows promise nothing ran, the exit-1 rows promise a Run Document.

What must change: require validation to reuse the exact argparse parsers, or add a table row for a usage error found mid-run (exit code, stdout, whether earlier steps stand).

**M4. Detecting an instance-carrying step needs a registry read the RFC never states. (spec defect)**

A step MUST NOT carry `[INSTANCE]` (RFC-03:293-294). But a bare leading token is an instance name only when the registry knows it (`src/browser_tools/GUIDE.txt:21-22`, `src/browser_tools/cli.py:848-854`). Validation therefore reads mutable external state, and the registry can change between validation and execution.

What must change: state that validation resolves step-instance detection against one registry read, and that the read happens before step 1.

**M5. The no-data-flow verb may not earn its cost. (design disagreement, not decided)**

Without variables a run cannot click a UID it just discovered (RFC-03:240-245). What remains: frame select plus N reads, fixed read sequences, literal-UID click and fill from earlier snapshots, navigation plus re-read, and ordered bounded captures. Each of these runs today as N invocations at about 85 ms per step. The one capability N invocations cannot supply at any speed is multi-read frame selection. A narrower fix, such as `--key` equivalents on the other frame-scoped reads, would supply it with no new verb, no new `CONTEXT.md` nouns, and no output-contract amendment. The counterweight is real: selection continuity plus one process is a coherent tool. The verb's existence was not among the four decided questions, so this stays open.

## 4. Minor findings

- **References mislabels the detach path. (spec defect)** RFC-03:786 calls `src/browser_tools/frame_manager.py:313-314` "the navigation path that clears it". Those lines are the detach handler. The navigation path at `src/browser_tools/frame_manager.py:349-353` never clears.
- **`window-border` endpoint drift does not touch the derivation. (spec defect in the surroundings)** RFC-01:182-184 lists `window-border` among verbs that never take `--endpoint`, but shipped `REGISTRY_VERBS` omits it (`src/browser_tools/cli.py:76`) and `GUIDE.txt:326` omits it. The code still rejects the flag through argparse (`src/browser_tools/cli.py:152-163` adds no `--endpoint`). RFC-03 excludes `window-border` because it drives no browser, so the derivation holds anyway. Note the drift for the build ticket.
- **The floor probe carries one redundant select, in the conservative direction. (evidence gap)** `flow.sh` runs `storage get --key child` (select plus read). `floor_probe.py:27-29` runs select, select, read: one select more than a true single-session run needs, since step 4's selection would persist. The overstatement is milliseconds and favors the RFC's critics.
- **"Several instances, none named" exits 1 pre-step with no stdout (RFC-03:627).** This matches the existing instance rule (`src/browser_tools/GUIDE.txt:23-25`) and the amendment's carve-out for rows that never reach a step (RFC-03:632-635). Consistent. No change needed.
- **The RFC-01:200-207 citation holds in substance.** The exit rules sit at RFC-01:200-206 with a blank line at 207. No change needed.

## 5. The nine questions, in order

**1. Does the measurement support the Motivation?** Yes for the measured flow, with bounds. `flow.sh` and `floor_probe.py:20-34` perform the same 11 handler calls; the probe's extra select is conservative. The medians check out against the raw files. The 500 ms and 300 ms quiet windows match the shipped defaults (`src/browser_tools/GUIDE.txt:102-105`, `src/browser_tools/curated.py:95-97`). On a flow with no waits the absolute saving persists at about 85 ms per step avoided, and the percentage grows; the RFC's "real but bounded" framing holds. What the numbers do not support is any claim about flows containing the five one-shot verbs (see M2).

**2. Is one CDPRuntime achievable?** Achievable in principle, underspecified as written. `wait` and `console-list` register handlers synchronously before enabling domains (`src/browser_tools/events.py:342-350`, `src/browser_tools/list_verbs.py:92-108`), so that discipline ports to a shared session if the RFC requires unregistration per step. Two steps cannot collide concurrently because steps run sequentially, but a step sees session state earlier steps left behind (enabled domains, frame-manager selection). Against RFC-01:194, a Step Run shares one session across many subscriptions where attach isolates per session. That violates the spirit of the rule. The RFC needs the per-step hygiene contract from B1.

**3. Can validation run before the first step?** Mostly yes, with two gaps. The line is drawn at "needs the browser" (RFC-03:385-390) and the table applies it consistently: argv shape, verb membership, flag parsing, and method-name refusals (`Target.activateTarget`, `Browser.close` over endpoint) are all statically checkable (`src/browser_tools/passthrough.py:196-254`); UID resolution, frame-pattern match, and browser method support fail at runtime with exit 1. The gaps are M3 (a second parser must mirror argparse exactly or mid-run exit-2 is unspecified) and M4 (instance detection needs a registry read). Not an overclaim at the level of the table; an overclaim at the level of the absolute MUST.

**4. Is the exit-1 amendment safe?** No as written. See B2. The hazard is old callers parsing stdout without checking the exit code, met with result blobs identical to standalone successes. `run.status` guards new callers only. It also conflicts with RFC-01:204 in letter.

**5. Is no-data-flow defensible?** It is coherent and narrow. The remaining flows are frame select plus reads, fixed sequences, literal-UID interaction, navigation plus re-read, and ordered captures. The strongest abolition case: all of these run today as N invocations, and the single impossible-today piece (multi-read frame selection) could ship as flag extensions without a new verb or a contract amendment. Against that: one process plus selection continuity is genuinely new. Design disagreement; the verb itself is undecided.

**6. Is the frame-selection claim correct?** Accurate on match, silent on no-match. Re-resolution on navigation works as described when a frame matches (`src/browser_tools/frame_manager.py:349-353`), and detach clears the selected id (`src/browser_tools/frame_manager.py:312-314`). But no-match keeps the stale id, and detach keeps the pattern. See M1. The state fields citation (`src/browser_tools/frame_manager.py:89-90`) holds.

**7. Does `screencast` work as a step?** Yes. The recorder resets on both paths: start clears frames and acks when idle (`src/browser_tools/screencast.py:152-164`), stop clears active state, unsubscribes, drains acks, and empties the buffer (`src/browser_tools/screencast.py:203-213`). The verb runs start, capture, and stop atomically over one handler (`src/browser_tools/curated.py:491-497`), so sequential reuse cannot hit "already recording". The cost is time: each screencast step blocks the run for its duration. No conflict with a following step.

**8. Do the citations hold?** See the audit table below. All load-bearing code citations hold except the References mislabel of `:313-314` (minor finding 1). The `curated.py:550 onward` pointer lands loosely (`screenshot` is defined at `src/browser_tools/curated.py:549`, its session helper at `src/browser_tools/curated.py:520-546`). The RFC-01:200-207 pointer covers lines 200-206 plus a blank line.

**9. Is the step surface derived correctly?** Yes. `_KNOWN_VERBS` holds 22 verbs (`src/browser_tools/cli.py:46-69`). RFC-03 includes 13 plus passthrough and excludes exactly the other 9 (`attach`, `launch`, `stop`, `cleanup`, `status`, `profile`, `window-border`, `guide`, `help`) with a reason for each (RFC-03:269-276). `frames` and `storage` have no other sub-actions in the parser (`src/browser_tools/cli.py:676-679`, `src/browser_tools/cli.py:765`). The `window-border` endpoint drift (minor finding 2) does not affect the derivation because the exclusion rests on "drives no browser".

## 6. Citation audit

| RFC-03 citation | Claims | Holds? |
|---|---|---|
| `frame_manager.py:89-90` | selection state fields | Yes |
| `GUIDE.txt:128-131` | `--key` workaround | Yes |
| `curated.py:427-428` | docstring states cause | Yes (`src/browser_tools/curated.py:427-430`) |
| RFC-01:36 | new-capability boundary | Yes |
| RFC-01:43 | declined recorder | Yes |
| RFC-01:148-210 | CLI surface | Yes |
| RFC-01:200-207 | exit codes | Yes in substance (rules at 200-206) |
| RFC-01:193 | wait design | Yes |
| RFC-01:411 | tab-close defect test | Yes |
| RFC-01 Packaging:339 | packaging section | Yes |
| `GUIDE.txt:342-344` | exit-1 stdout rule | Yes |
| `GUIDE.txt:333-346` | output block | Yes |
| `GUIDE.txt:197-209` | UID rule | Yes |
| `frame_manager.py:349-353` | navigation re-resolution | Yes, on match only |
| `frame_manager.py:313-314` | detach clears | Yes for the id; pattern retained; References mislabels it as navigation path |
| `curated.py:550 onward` | screenshot one-shot path | Loosely (def at 549, helper at 520-546) |
| `one_shot.py` (no line) | five verbs share the seam | Yes (`src/browser_tools/passthrough.py:322`, `src/browser_tools/events.py:402`, `src/browser_tools/list_verbs.py:140`, `src/browser_tools/curated.py:531`) |
| `extras.py` packaging discipline | default `websockets` only | Yes (`src/browser_tools/extras.py:1-14`) |
| `tests/test_guide.py` parser enumeration | drift guard | Yes (`tests/test_guide.py:26-42`) |
| `.scratch/wayfinder-82/repro-uid-drift.py` + `build()` requires `doc_token` | UID drift solved | Yes, file exists and `src/browser_tools/native_snapshot.py:304` requires keyword `doc_token` |
| `cli.py:828` in measurements.md | `_split_leading_instance` | Yes (`src/browser_tools/cli.py:828`) |
| `GUIDE.txt:17` in measurements.md | leading-instance form | Yes (line 17) |

## 7. What is sound

- The per-invocation tax breakdown (52.3 ms interpreter plus import, 15.4 ms registry plus liveness, 5.6 ms CDP) follows the measured medians.
- The Scope fit-check record and the declined list match RFC-01:36 and `extras.py`.
- Terminology additions (`Step Run`, `Step List`) follow `CONTEXT.md` naming practice.
- The stale-`bt` finding in measurements.md is verified and correctly scoped to the figures.
- Instance resolution for the run (resolve once, steps carry no selector) follows from one session.
- UID validity across steps restates the document rule with no process-based extension.
- The `--timeout` default, the verb name, the restricted model, and the provisional document shape are decided; this review does not reopen them.
- Packaging adds no dependency; `shlex` and `json` are stdlib.
- Security Considerations states the restricted-model property correctly and keeps existing refusals intact.
- The Manual entry text matches the Design sections it summarizes.

---

## What version 3 did with each finding

Written by the drafting session, not by the reviewer.

| Finding | Class | Disposition in version 3 |
|---|---|---|
| **B1** one CDPRuntime unspecified | blocking, spec defect | **Fixed.** Design, "One CDPRuntime for the run", now fixes the connection model as the One-Shot Session's browser-level attach, adds the domain enable and disable rule, and makes per-step handler removal normative. The claim that the handler's page-level connection carries no `sessionId` was verified at `cdp_handler.py:404-413` and `cdp_client.py:158-161`. |
| **B2** exit-1 amendment violates RFC-01:204 | blocking, spec defect | **Accepted and fixed.** Version 2 claimed RFC-01 needed no amendment. That was wrong: a failed Run Document carries per-step `"status": "ok"` entries. Design now carries two amendments, and the Run Document section states the ordered caller contract. |
| **M1** frame no-match case omitted | major, spec defect | **Fixed.** Verified at `frame_manager.py:349-353` (assigns only inside `if resolved:`), `:207-208` (the direct path does clear), and `:223-224` (a cleared id short-circuits before the pattern fallback). A run MUST clear the id on a no-match. |
| **M2** 50% figure covers handler-routed verbs only | major, evidence gap | **Accepted.** Motivation now labels the figure for what it covers and states that a flow containing the five one-shot verbs is unmeasured. |
| **M3** mid-run usage error unspecified | major, spec defect | **Designed out.** The step parser MUST reuse the existing `argparse` parsers rather than mirror them, so the divergence that would produce a mid-run usage error cannot arise. |
| **M4** instance detection needs a registry read | major, spec defect | **Fixed.** The read happens once, during validation, before step 1. |
| **M5** the verb may not earn its cost | major, design disagreement | **Escalated and decided.** The author pass found that frame selection has one consumer, which strengthened this finding beyond what the reviewer established. Put to the repository owner, who kept the verb, remotivated it on the measured saving, and attached the argparse-reuse condition. Recorded as Decisions, item 5. |
| References mislabels `:313-314` | minor, spec defect | **Fixed.** It is the detach handler, not a navigation path. |
| `window-border` endpoint drift | minor, surrounding defect | **Recorded** in Design, "The step surface", for the build ticket. The derivation was already explicit rather than by subtraction, so it holds. |
| Floor probe's redundant `select_frame` | minor, evidence gap | **Disclosed** in Motivation. The overstatement is a few milliseconds and favours a critic. |
| "Several instances, none named" consistent | minor, no change needed | No change. |
| RFC-01:200-207 citation holds | minor, no change needed | No change. |
