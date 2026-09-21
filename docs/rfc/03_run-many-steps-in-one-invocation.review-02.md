# Review: RFC-03 version 3

## What was reviewed

- **RFC:** `docs/rfc/03_run-many-steps-in-one-invocation.rfc.md`
- **Version:** 3, at commit `3662093`. Snapshot SHA-256
  `fa4914bd36c0a7a485d386df476842513cec1e7e2c5d76e232183d5525254c71`.
  **The RFC is now at version 4**, which was written in response to this review;
  see "What version 4 did with each finding" at the end of this file. Every
  finding below is against version 3, so the `rfc.md:NNN` line numbers in the
  findings refer to that snapshot, not to the current file.
- **Status:** Draft, before and after.
- **Repository:** `browser-tools` at `3662093`, in a worktree created from that
  commit. The reviewer edited no file, and could not: it ran with `--access read`.
- **Reviewer:** `muse-spark-1.3-contributor@muse` (family meta), high effort,
  read-only, routed with `excludeFamilies: ["claude"]` because a Claude model
  drafted the RFC. The route was chosen from the model registry with
  `task: plan`, `stakes: high`, `prefer: cost`. It ran on its intended route with
  no fallback walk; the `gpt-6-astra@codex` fallback was not used.
- **Brief:** `docs/rfc/03_run-many-steps-in-one-invocation.review-brief-02.md`.
- **Verdict:** accept with changes.

Finding ids below are the reviewer's own, unprefixed in its text. The RFC's
changelog prefixes them `N-` to keep them apart from the version 2 review's `B`,
`M` and `m` ids.

---

# Review: RFC-03 version 3 (the fixes)

## 1. Verdict: accept with changes

Version 3 answers both blocking findings with real text, not gestures. The session model is now specified, the RFC-01 amendment is now written, the no-match rule exists, and a second flow was measured. But 440 new lines introduced four defects that stop a build as written: the References section denies the RFC-01 amendment the Design section adds, the refusals paragraph misstates exit codes the tree fixes at `GUIDE.txt:226-228` and `passthrough.py:256-264`, the no-match clear has no implementable locus, and parser reuse leaves the `_run`-level semantic checks mirrored. None of these is structural. Each has a narrow correction.

## 2. Did version 3 fix the earlier findings?

| Finding | Judgment | Citation behind it |
|---|---|---|
| **B1** one CDPRuntime unspecified | **Partly fixed.** The session model, per-step handler removal, and the domain enable/disable rule are now specified at `rfc.md:352-447`. But the RFC admits it "specifies no phasing" at `rfc.md:386-389`, the handler's client has no session concept (`cdp_client.py:111-158`, `send` takes no `session_id`, `on` takes no filter), and the Runtime exemption reopens the replay hole for Runtime-domain steps (see Q1). | `rfc.md:383-389`; `cdp_client.py:111-113`; `list_verbs.py:57` |
| **B2** exit-1 amendment violates RFC-01:204 | **Fixed in letter.** Amendment 2 at `rfc.md:700-727` scopes the success-field rule per step, and the ordered caller contract is stated at `rfc.md:651-657` and in the Manual entry at `rfc.md:810-814`. Containment remains convention, not mechanism (new major finding 5). | `rfc.md:711-727`; `RFC-01:204` |
| **M1** frame no-match omitted | **Partly fixed.** The no-match clear is specified at `rfc.md:469-479` and every code description around it is accurate. But the clearing locus contradicts "Outside a Step Run nothing changes" at `rfc.md:495`, and pattern retention after the clear is unstated (new blocking finding 3). | `rfc.md:474-479`; `rfc.md:489-496`; `frame_manager.py:349-353` |
| **M2** 50% covers handler verbs only | **Partly fixed.** Flow B is measured, labeled, and its limits are partly disclosed at `rfc.md:172-196`. But its ten steps contain no `console-list`, `network-list`, or `wait` invocation despite the claim at `rfc.md:172-175`, and n=5 with a 59.1 to 83.8 ms spread cannot carry a 90% headline (new major finding 6). | `measurements.md:386-442`; `measurements.md:737-743` |
| **M3** mid-run usage error | **Partly fixed.** Parser reuse is normatively required at `rfc.md:543-555`. But semantic usage checks live in `_run`, not in argparse (`cli.py:505-506`, `cli.py:607-608`, `cli.py:621-624`, `cli.py:678-679`, `cli.py:704-717`), so a second validation layer still needs mirroring and the "no case" claim at `rfc.md:553-555` is false (new blocking finding 4). | `cli.py:587-594`; `rfc.md:553-555` |
| **M4** instance detection needs registry read | **Fixed.** The single validation-time read is specified at `rfc.md:557-562`, against the rule at `GUIDE.txt:21-22` and `cli.py:848-854`. | `rfc.md:557-562` |

## 3. New findings

### Blocking

**N-B1. References denies Amendment 2. (spec defect, blocking)**
`rfc.md:1058` describes RFC-01's "Refusals and exit codes" as "(not amended by this RFC; see Design, 'Amendment to The Manual')". Design adds Amendment 2 to that exact section at `rfc.md:700-727`, with "This amendment MUST be written into RFC-01's 'Refusals and exit codes' section" at `rfc.md:726-727`. A builder following References drops the amendment. The parenthetical is stale version-2 text. What must change: delete or correct the parenthetical so References names both amendments.

**N-B2. Refusals are not all exit-2 validation refusals. (spec defect, blocking)**
`rfc.md:583-586` states the focus guard, loopback rule, `Browser.close`/`Browser.crash` refusals, and profile exclusivity apply per step, then concludes: "Because refusals are usage errors (exit 2), a Step List containing one is refused at validation and the run executes nothing." The hidden-tab input refusal is exit 1, per `GUIDE.txt:226-228`, and it requires a live browser read (`passthrough.py:256-264` sends `Runtime.evaluate` before deciding). It cannot be validated before step 1. The `Browser.close`/`Browser.crash` refusals apply only over `--endpoint` (`passthrough.py:297-298`), which steps cannot carry (`rfc.md:345-346`), so there is nothing to validate there either. Only the method-name refusals (`passthrough.py:240-253`) are statically checkable exit-2. What must change: split the paragraph by refusal class, with the hidden-tab failure routed to the exit-1 runtime row of the Error Handling table.

**N-B3. The no-match clear has no locus consistent with "outside unchanged". (spec defect, blocking)**
`rfc.md:474-475` places the MUST on the run: "The run MUST clear the selected frame id when re-resolution after a navigation finds no match." Re-resolution happens inside `frame_manager.handle_frame_navigated` (`frame_manager.py:349-353`), which knows no run. Either the manager changes globally, which contradicts "Outside a Step Run nothing changes" at `rfc.md:495` and "A Step Run changes exactly one thing" at `rfc.md:489-493`, or the run intercepts navigation events, which no section specifies. What must change: name the implementing layer and reconcile it with `rfc.md:495`.

**N-B4. Parser reuse does not cover `_run`-level semantic checks. (spec defect, blocking)**
`rfc.md:553-555` claims that because the parser is reused, "there is no case where a malformed step is discovered after step 1 has run." Required-input checks live outside argparse and are validated in `_run`: `click`/`fill` `--uid`/`--text` at `cli.py:607-608` and `cli.py:621-624`, `storage` sub-action at `cli.py:678-679`, `screencast --dir` and `removed_action` at `cli.py:704-717`, and `--target`/`--url` mutual exclusion at `cli.py:505-506`, `cli.py:520-521`, `cli.py:536-537`, `cli.py:550-551`. The docstring at `cli.py:246-249` states this split is deliberate. A step `click` with no `--uid` parses cleanly and fails semantically, so the mirror problem M3 named persists one layer down. What must change: extend the reuse requirement to the `_run` semantic checks, or add the mid-run usage-error row M3 originally asked for. The SystemExit/stderr interception mechanism for reused parsers is likewise unspecified and must be named.

### Major

**N-J1. The ordered contract describes the hazard; it does not contain it. (design disagreement, major, not decided)**
The contract at `rfc.md:651-657` is correct advice and Amendment 2 is narrow and honest. But nothing enforces the order. A caller that parses step entries first still reads a partial run as whole, and `rfc.md:724` ("the rule's purpose ... is served by the exit code and by `run.status`") holds only for callers that read them. This is the shape B2 asked for, so it stands; note only that a fail-closed envelope was available and was not chosen.

**N-J2. Flow B measures passthrough and screenshot, not the five verbs. (evidence gap, major)**
`rfc.md:172-175` claims flow B covers "the five that open their own One-Shot Session today." Its ten steps (`measurements.md:413-424`) are `Runtime.evaluate`, `Page.getNavigationHistory`, `Page.getLayoutMetrics`, `DOM.getDocument`, two screenshots, `Browser.getVersion`, `Page.getFrameTree`, and `Network.getAllCookies`. No step invokes `wait`, `console-list`, or `network-list`, whose duration windows (2 s defaults at `GUIDE.txt:115-122`, 30 s at `GUIDE.txt:159-166`) are the expensive characteristic of exactly those verbs. The floor side is also asymmetric: bare `cdp.send` calls skip the screenshot blank-guard retry loop (`curated.py:534-546`), file writes, and result rendering that the ten-invocation side pays. What must change: relabel flow B as passthrough-plus-screenshot, or measure the three missing verbs.

**N-J3. Five runs with a 42% spread cannot carry 90%. (evidence gap, major)**
`measurements.md:737-743` reports 59.1, 83.8, 82.7, 69.2, 83.2 ms. The median (82.7) sits near the max, the min is 28% below it, and n=5. The medians themselves verify (sorted flow-B floor is 59.1, 69.2, 82.7, 83.2, 83.8; sorted invocations 776.4, 778.5, 787.8, 791.9, 831.9). The arithmetic is honest; the sample is thin. What must change: more runs, or a stated interval instead of a point headline.

**N-J4. The Runtime exemption reopens the replay hole for Runtime-domain steps. (spec defect, major)**
`console-list` subscribes to `Runtime.consoleAPICalled` (`list_verbs.py:57`) and enables the `Runtime` domain per event name (`list_verbs.py:100-108`). `Runtime` belongs to the run and is never disabled (`rfc.md:415-418`). So a second `console-list` in one run still gets a no-op enable, and `rfc.md:419-423` ("restores first-enable semantics for the next step") fails for exactly this verb. The practical damage is small, since `consoleAPICalled` is live-fire rather than replayed state, but `wait --event Runtime.executionContextCreated` after any Runtime-enabling step observably differs from a bare invocation, contradicting `rfc.md:430-432` ("a step behaves the same inside a Step Run as it does as its own invocation, except for frame selection"). What must change: scope the sameness claim, or exempt only the frame-manager subscriptions rather than the whole domains.

### Minor

**N-m1. Decisions counts four questions, then answers five. (spec defect, minor)**
`rfc.md:923` says "All four questions this RFC raised were put to Kevin," then lists five items, and `rfc.md:989` and `rfc.md:1051` both say five. One stale numeral.

**N-m2. The Manual entry drops the Page/Runtime exemption. (spec defect, minor)**
`rfc.md:787-791` states "Domains a step turns on are turned off when that step ends" without the exemption normatively required at `rfc.md:415-418`. A reader of the Manual alone would disable `Page` and break the frame manager.

**N-m3. The Manual entry says a dead selection "goes away". (spec defect, minor)**
`rfc.md:780-785` says the selection "goes away when the selected frame goes away." Design at `rfc.md:480-488` retains the pattern across detach and allows resurrection on a later navigated event. The Manual sentence needs the resurrection qualifier.

**N-m4. Pattern retention after a no-match clear is unstated. (spec defect, minor)**
`rfc.md:474-475` requires clearing the id but does not say whether `_selected_url_pattern` is kept. Kept matches detach semantics (`frame_manager.py:312-314`); dropped matches `reset_frame` (`frame_manager.py:212-215`). Either is defensible; the choice determines whether a later navigation resurrects the selection.

**N-m5. Passthrough explicit enables vs the disable rule. (spec defect, minor)**
A step `Network.enable '{}'` explicitly enables a domain. The rule at `rfc.md:419-423` ("every other domain a step enables, that step MUST disable") either undoes an explicit caller action or silently excludes passthrough steps. Neither reading is stated.

**N-m6. Disable-failure handling is unspecified. (spec defect, minor)**
Enables are failure-tolerant by construction (`events.py:347-350`, `list_verbs.py:106-108` suppress `CDPError` since some domains lack enable). The disable path gets no equivalent guard, though some domains may lack disable. State it.

**N-m7. The "three call sites" omits two reads. (spec defect, minor)**
`rfc.md:73-76` lists `cdp_handler.py:816`, `:856`, `:863`. It omits the internal read at `frame_manager.py:240` and the display-only read at `cdp_handler.py:794`. Neither changes the one-consumer conclusion in Q5. List them for exactness.

## 4. The eight questions, in order

**1. Does the B1 fix work? Partly.** The model is now specified and internally coherent: browser-level attach (`rfc.md:383-385`), Page/Runtime pinned to the run (`rfc.md:415-418`), per-step disable plus handler removal (`rfc.md:419-428`, grounded in `events.py:367-368` and `list_verbs.py:114-116`). Three gaps. First, buildability: the handler's client cannot carry a session. `cdp_client.py:111-158` (`send` without `session_id`, `on` without filter) differs from the core client the rule assumes (`core/cdp_client.py:97-161`), and the RFC admits it "specifies no phasing" (`rfc.md:386-389`). Second, the exemption is sufficient for the frame manager, which subscribes only to Page and Runtime events (`cdp_handler.py:420-431`), but insufficient for the sameness claim, per N-J4. Third, a step that enables a domain a later step needs now works by re-enable with replay, which is the point of the rule; the unhandled cases are passthrough explicit enables (N-m5) and disable failures (N-m6).

**2. Does the B2 fix work? In letter, yes.** The amendment wording at `rfc.md:711-717` is correct and narrow: scoped to Step Runs, preserving the rule outside them, against the cited rule at `RFC-01:204`. The contract (exit code, then `run.status`, then steps) is stated in both Design and the Manual entry. It describes rather than contains; see N-J1. B2 as assigned is fixed.

**3. Is the frame no-match rule right? Description yes, requirement almost.** Every code claim verifies: assign-only-on-match (`frame_manager.py:349-353`), direct-path clear (`frame_manager.py:204-210`, cited as `:207-208`), cleared-id short-circuit (`frame_manager.py:223-224`), pattern fallback only when the id is set but stale (`frame_manager.py:228-231`, cited as `:229-230`), detach keeps pattern (`frame_manager.py:312-314`). The requirement is consistent with `select_frame_by_url`, `get_selected_frame`, and `reset_frame` on the id. It is inconsistent on locus with `rfc.md:495` (N-B3) and silent on the pattern (N-m4).

**4. Does the argparse-reuse requirement hold up? No, not all the way.** Reuse is feasible for flag syntax: per-verb subparsers exist (`cli.py:90-236`, `cli.py:251-361`), and step-shaped argvs fit them. But two things defeat the "cannot arise" claim. Semantic checks live in `_run`, not argparse (citations in N-B4). And argparse failure exits via SystemExit with its own stderr output, so validation reuse requires an interception and diagnostic-substitution mechanism the RFC never names. Partly fixed at best.

**5. Is the withdrawn motivation handled honestly? Yes, with a two-line undercount.** Production consumers of selection state are `_handle_get_frame_storage` (`cdp_handler.py:856`, `:863`), the confirmation print in `_handle_select_frame` (`cdp_handler.py:816`), the display marker (`cdp_handler.py:794`), and the internal read (`frame_manager.py:240`). The only behavior consumer is `get_frame_storage`, i.e. `storage get`, which already has `--key` (`GUIDE.txt:128-131`). No other frame-scoped read exists in the verb surface. The RFC does not overcorrect: "wins nothing here today" (`rfc.md:78-79`) matches the tree.

**6. Do the two measured flows support what Motivation claims? Flow A yes within its label; flow B no.** Flow A medians verify against the appendix (1696.3 ms from 1683.5/1694.0/1696.3/1732.1/1737.5; 849.1 ms from 841.8/846.9/849.1/852.0/853.1) and the handler list matches `flow.sh` modulo the disclosed redundant select. Flow B is not a fair comparison for its claim: the floor probe calls `one_shot_page_session` directly (`measurements.md:427-431`) sending bare CDP, skipping verb-level work the invocation side pays, and contains none of the three verbs with duration behavior. Five runs over a 59.1 to 83.8 ms spread cannot support a 90% headline. The "whole difference is the intrinsic waits" sentence (`rfc.md:185-186`) is roughly right arithmetically (per-step tax 85 vs 70 ms) but inherits flow B's asymmetry.

**7. Did 440 new lines introduce contradictions? Yes: N-B1, N-B2, N-B3, N-B4, N-m1, N-m2, N-m3 above.** The two self-contradictions the earlier review found are gone; these are new. The Decisions section is otherwise consistent with the body, and the five decided questions are correctly recorded as decided.

**8. Verification of every cited line is in section 5.** All hold.

## 5. Citation audit (version 3 additions and changes)

All citations below were opened and read. Every one says what the RFC claims. Line windows are approximate by one line where the claim spans a statement.

| Citation | Claim | Holds? |
|---|---|---|
| `cdp_handler.py:404-413` | handler page-level sessionless connection | Yes (`:404-405` resolve page WS, `:408-409` connect, `:412-413` sends without `sessionId`) |
| `cdp_handler.py:411-413` | sends carry no `sessionId` | Yes |
| `cdp_handler.py:816` | selection consumer inside `select_frame` | Yes |
| `cdp_handler.py:856`, `:863` | selection consumers inside `get_frame_storage` | Yes, both |
| `cdp_client.py:158-161` (core) | session-filtered dispatch | Yes (`:158-161`; loop at `:159`, filter at `:160`) |
| `events.py:344` | wait registers before enable | Yes |
| `events.py:350` | enable send | Yes |
| `events.py:323-330` | subscribe-first docstring | Yes |
| `events.py:367-368` | unregister in `finally` | Yes |
| `frame_manager.py:207-208` | direct path clears on no-match | Yes (within `:204-210`) |
| `frame_manager.py:223-224` | cleared id short-circuits | Yes |
| `frame_manager.py:229-230` | pattern fallback below the short-circuit | Yes (within `:228-231`) |
| `frame_manager.py:313-314` | detach clears id, keeps pattern | Yes (within `:312-314`) |
| `frame_manager.py:349-353` | navigation assigns only on match | Yes |
| `list_verbs.py:114-116` | collector unregisters in `finally` | Yes |
| `cli.py:76` | `REGISTRY_VERBS` omits `window-border` | Yes |
| `cli.py:848-854` | `_split_leading_instance` registry rule | Yes |
| `RFC-01:182-184` | `window-border` among never-`--endpoint` verbs | Yes (`:182-184`) |
| `RFC-01:194` | attach isolation per session | Yes |
| `RFC-01:200-207` | exit-code rules | Yes in substance (rules at `:200`, `:202`, `:204-206`) |
| `RFC-01:204` | success-field rule | Yes, verbatim |

## 6. What is sound

- The per-invocation tax table and the Abstract's 67.7 ms figure follow the raw appendix.
- The connection-model diagnosis (page-level handler vs browser-level One-Shot Session) is correct against both clients.
- The domain-replay probe is real evidence and correctly scoped to `Runtime.enable`.
- Amendment 2's wording and the ordered caller contract, as prose, are narrow and honest.
- The step-surface derivation, the `window-border` drift note, and the explicit-enumeration rationale are correct.
- Instance resolution (one read, validation-time) is consistent with `GUIDE.txt:21-25` and `cli.py:828-854`.
- UID validity, timeouts, packaging, alternatives, and the provisional Run Document sections carry no defect found here.
- The M4 fix, the M1 code descriptions, and the flow-A disclosure are exact.
- The five Decisions are recorded as decided; design disagreements above (N-J1) do not reopen them.

---

## What version 4 did with each finding

| Finding | Disposition |
|---|---|
| **N-B1** References denies Amendment 2 | **Applied.** The parenthetical now names both amendments. |
| **N-B2** refusals are not all exit-2 | **Applied.** Split into three classes: statically-checkable method-name refusals (exit 2, before step 1); the hidden-tab refusal, which needs a live browser read and exits 1 at the step that hits it; and refusals unreachable inside a run at all. |
| **N-B3** the no-match clear has no locus | **Applied.** The clear moves into the frame manager, which owns the stale-id hazard. The contradiction with "outside nothing changes" is reconciled in both places. |
| **N-B4** parser reuse misses the `_run` checks | **Applied.** The reuse requirement extends to the `_run` semantic checks, with the five call sites named, and the `SystemExit`/stderr interception mechanic is specified. The false "no case" claim is deleted. |
| **N-J1** the ordered contract does not contain the hazard | **Accepted as stated; shape unchanged.** The reviewer notes the finding stands as the chosen shape. The rejected fail-closed envelope is now recorded in Alternatives Considered, so the choice is visible. |
| **N-J2** flow B measures passthrough and screenshot | **Applied.** Flow B is relabelled to what it covers, two One-Shot Session verbs, and Motivation says why `wait`, `console-list` and `network-list` are not covered and would not show the same saving. |
| **N-J3** five runs cannot carry 90% | **Applied, and the data was worse than thin.** Retaken at n=20. While retaking it, the version 3 harness was found to be driving the wrong browser, so the version 3 flow B figures are void, not merely under-sampled. The corrected figures are 870.1 ms against 83.0 ms, still 90%, with the range stated. The incident and the harness fix are recorded in the measurements file. |
| **N-J4** the Runtime exemption reopens the replay hole | **Applied.** `console-list` is named alongside `wait --event`, with `list_verbs.py:57`, and the sameness claim is scoped to two exceptions rather than one. |
| **N-m1** four questions, then five | **Applied.** |
| **N-m2** the Manual entry drops the Page/Runtime exemption | **Applied.** |
| **N-m3** the Manual entry says the selection "goes away" | **Applied**, with the resurrection case. |
| **N-m4** pattern retention after a no-match clear | **Applied.** The pattern is kept, matching detach. |
| **N-m5** passthrough explicit enables vs the disable rule | **Applied.** An enable the caller typed is excluded from the rule; the caller disables it with a later step. |
| **N-m6** disable-failure handling | **Applied.** Suppressed, matching the enable path. |
| **N-m7** the "three call sites" omits two reads | **Applied.** Both are listed, and neither changes the conclusion. |
