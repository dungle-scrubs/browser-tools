# Review: RFC-02 Calibrated interstitial triage and handoff

## What was reviewed

- **RFC:** `docs/rfc/02_calibrated-interstitial-triage-and-handoff.rfc.md`
- **Version:** 1. Snapshot SHA-256
  `959c81e2a56d6d0fa7358b3efbd61448afd1f478569e4c49a84882789590c7d5`, which is the
  current file. The RFC changed during the review; the digest above is the state both
  reviewers finished against, so no finding here is stale.
- **Status:** Draft
- **Repository:** `browser-tools` at `84ee25a`, clean. The RFC is the only untracked
  file. Neither reviewer edited any file.
- **Reviewers:** two independent cross-family passes plus one author pass.
  - `gpt-6-astra@codex` (family openai), high effort, read-only, routed with
    `excludeFamilies: ["claude"]`. Findings prefixed **A**.
  - `muse-spark-1.3-contributor@muse` (family meta), high effort, read-only, routed
    with `excludeFamilies: ["claude", "openai"]` so it shares no family with either
    the drafter or the first reviewer. Findings prefixed **M**. It received the same
    brief, unchanged, and never saw the first review, so its agreements are
    independent rather than anchored.
  - One author pass by the drafting Claude session. **Not independent**; marked as
    such on the findings it produced.

  Both reviewers ran on their intended route with no fallback walk. Neither edited
  any file.
- **Drafter self-corrections before the cross-family pass returned:** six defects the
  author pass found in its own draft were fixed in place rather than filed here
  (Phase 0's contradictory script-shape requirement, the misplaced promotion
  threshold, the uncovered `HANDOFF -> BLOCKED` state, eight wrong `file:line`
  citations, an unused Terminology entry, and an unnamed timeout constant). The
  cross-family reviewers both read the corrected document. The openai pass confirmed
  two of those fixes under **Cleared**; the meta pass re-checked every `file:line`
  citation in the RFC one by one and found them accurate.
- **Full reviewer output:** `~/.agents/tasks/browser-tools-rfc-02-jev/reviewer-raw.md`
  (openai) and `reviewer-muse-raw.md` (meta).

## Structural results

`validate-structure.ts` output, verbatim:

```json
{
  "passed": true,
  "errors": [],
  "warnings": []
}
```

The openai pass ran it and got this result. The meta pass had no shell in its
session and could not run it, which is recorded under **Not reviewed**.

## Findings

Each cross-family finding was re-checked against the source before inclusion, per the
convention RFC-01's review set. The **Verified** line records what that check found,
not what the reviewer claimed. **Corroborated** means both independent reviewers,
from different families and without sight of each other, reached the same finding.

Findings 1 to 14 come from the openai pass; 15 to 19 are what the meta pass added.
The two passes are complementary rather than redundant: finding 1, the most serious,
was found only by openai, and finding 15, the next most serious, only by meta.

### 1. Evidence leaves the machine for ordinary authenticated pages

**Section:** Security Considerations, "Data sensitivity" and "Network egress".
**Rung: 4, you ran it.** The reviewer executed the detector against a synthetic
ordinary page carrying a DataDome cookie and got a detection.

> Detection does not establish that the page is a challenge. The DataDome detector
> fires solely on cookie presence at `src/browser_tools/detect_interstitial.js:132`.
> PerimeterX and AWS WAF have similar checks. [...] Under the proposed policy, that
> sends the page's title, body excerpt, controls, and path to the hosted API before
> the model can classify it as `none`.

**Verified: confirmed.** `detect_interstitial.js:132` is
`if (document.cookie.indexOf('datadome') !== -1)`. DataDome sets its cookie on every
page it fronts, not only on challenges, so every page of a DataDome-protected site
you are logged into triggers detection and therefore triage. The RFC's gate is "did
detection return anything", which is not the same question as "is this a challenge".
This is the most serious finding: it inverts the RFC's own privacy argument, which
assumed evidence only leaves on a challenge page.

### 2. Degraded triage cannot reproduce today's auth-wall promotion

**Section:** Design, "Data model"; State Machine; Implementation Plan, Phase 0.
**Rung: 4, you ran it**, for the current promotion and retry behavior.

> The fallback specifies only `self_solving` and `confidence`. It leaves required
> `vendor`, `needs_human`, and `is_auth_wall` values undefined. Also, `DEGRADED` can
> reach only `WAITING` or `BLOCKED`, never `HANDOFF`.

**Verified: confirmed.** The RFC's fallback rule sets `self_solving` and `confidence`
and says nothing about the other three fields, and the state machine has no
`DEGRADED -> HANDOFF` edge. A default install, or any install whose API key is
missing, would therefore stop promoting headless sessions on login walls, which works
today at `browser_session.py:666`. The RFC's central promise that the fallback is
today's behavior is false as written.

The reviewer adds a second defect in the same area, also confirmed: today's retry
policy operates on the whole detection list and appends first-pass non-retryable
detections on exhaustion (`interstitial.py:143`, `:162`). The RFC's rule about "the
detected type" is singular and does not specify mixed detections.

### 3. Nothing carries the Verdict to the promotion gate

**Section:** Design, "Shape" and "Gating the promotion"; Alternatives Considered.
**Rung: 2, you pointed at the code.**

> `src/browser_tools/mcp_daemon.py:497` receives structured detection results, then
> appends formatted text to the response. `_maybe_promote_on_auth_wall` receives that
> response in the session adapter at `src/browser_tools/browser_session.py:854`. It
> does not receive the detection result. [...] It also incorrectly says both gates run
> inside the daemon. Promotion runs in the session adapter.

**Corroborated** by the meta pass (M2), which independently traced the same boundary
and added that the RFC never specifies the gate's new signature, nor the text fallback
when a Verdict is absent, so Phase 3 is not implementable from this document.

**Verified: confirmed, and it reaches further than the reviewer states.** The daemon
calls `run_post_navigation_detection` and flattens the result to text; the session
adapter calls `_maybe_promote_on_auth_wall` with only the response dict. The
structured Verdict has no route across that boundary, and the RFC specifies none.
This is why `_response_signals_auth_wall` is a substring match in the first place:
the text is all that layer has.

The consequence the reviewer identifies but does not press: **the RFC's rejection of
the separate-package alternative rests on the claim that both gates run inside the
daemon.** That claim is wrong. The promotion gate runs in the session adapter, and
the CLI has fronts that bypass both (`passthrough.py:282` sends `Page.navigate`
directly, `curated.py:269` runs detection on its own). The placement decision should
be re-examined against the corrected execution model, not defended with it.

### 4. Valid model answers can demand conflicting transitions

**Section:** Design, "Gating the retry loop"; State Machine; Error Handling, E010.
**Rung: 2, you pointed at the code.**

> Independent questions can return high `self_solving`, high `needs_human`, and low
> `is_auth_wall`. The retry algorithm and state machine require waiting. E010 requires
> stopping. No precedence rule settles that case. [...] Separately, `vendor="none"`
> has no route to `CLEAR`.

**Corroborated and extended** by the meta pass, which split this into three: E010 is
unreachable because no state-machine guard reads `needs_human` at all (M1); `vendor="none"`
has no route to `CLEAR` (M4); and `HANDOFF` versus `WAITING` has no precedence rule when
both guards fire, which is the ordinary case of a Cloudflare-fronted login that would
clear itself (M5). The meta pass also notes the `PROMOTED` success path has no reporting
rule.

**Verified: confirmed.** The four questions are independent by design and cannot see
one another's answers, so no combination is excluded. The state machine's guards are
written as if they were mutually exclusive and are not. `vendor="none"` is in the
criteria set and has no transition. E010's "high" and "low" have no numeric
thresholds anywhere in the document. A decision table is missing, and no amount of
calibration supplies it.

### 5. The Evidence payload has no enforceable total bound

**Section:** Design, "Data model" and "Latency and cost budget"; Security
Considerations. **Rung: 4, you ran it.** The reviewer ran the script with a synthetic
20,000-character title and confirmed the detection retained it.

> Only `body_excerpt` has a recommended numeric bound. [...] The proposal retains
> detections "as produced today." Their `details` field embeds the full title at
> `src/browser_tools/detect_interstitial.js:35` and `:126`. [...] Therefore the "well
> under one thousand tokens" assertion and its cost bound are unsupported.

**Verified: confirmed.** The RFC bounds `body_excerpt` and sanitizes `url`, then
carries `detections` through unbounded, and those detections embed the page title in
free text. Both the token-budget claim and the cost figure in "Latency and cost
budget" rest on a bound the document never imposes. Jev's 32k state limit makes this
a functional failure as well as a privacy one.

### 6. Typed output does not bound the damage as claimed

**Section:** Security Considerations, "Trust boundary" and "Blast radius".
**Rung: 4, you ran it**, for the teardown ordering.

> `_promote_headless_to_headed` closes the existing browser before attempting the
> replacement at `src/browser_tools/browser_session.py:733`. If replacement startup
> fails, it returns `None` at `:744`. [...] The claim that no browsing capability is
> lost is false.

**Corroborated** by the meta pass (M3), by a different route: because the page is
hostile input by the RFC's own statement, an attacker can trigger promotion at will with
"sign in" text, which makes attacker-triggerable screen takeover the issue rather than a
wrong number. Both reviewers reject the RFC's blast-radius claim.

**Verified: confirmed.** `close_owned_browser()` runs at line 733, the replacement is
constructed at 734, and `ensure_browser_state()` at 743 can raise into a `return None`
at 745. A wrong `is_auth_wall` probability can therefore destroy a live session rather
than merely open a window. The RFC's blast-radius paragraph says the opposite.

### 7. Re-triage does not guarantee fresh evidence

**Section:** Design, "Shape" and "Script return shape"; State Machine.
**Rung: 4, you ran it.** The reviewer ran `detect_once` with an auth wall followed by
an empty second observation and still got the auth wall.

> `detect_once` currently performs two observations and combines their detections at
> `src/browser_tools/interstitial.py:185`. [...] The new envelope does not specify
> which observation supplies its evidence.

**Verified: confirmed.** `detect_interstitials_async` runs two passes 500ms apart and
merges them, so a single `detect_once` already spans two documents. The RFC requires a
fresh Verdict per pass on the grounds that the page changes, then feeds it evidence
that may describe the earlier observation. The new object envelope does not say which
pass supplies `evidence`, nor what happens across a redirect.

### 8. The unknown-vendor coverage claim is unreachable

**Section:** Motivation; Design; Alternatives Considered, "Better regular expressions".
**Rung: 4, you ran it.**

> The RFC claims that `other` distinguishes an unknown challenge from a clean page. It
> also requires returning immediately on zero detections and forbids triage in that
> case. An unknown challenge that evades existing signatures therefore never reaches
> `other`.

**Verified: confirmed, and it is self-inflicted.** The RFC's own latency optimization
("Triage MUST NOT be attempted when detection returned no detections") makes its own
third motivation unreachable. The reviewer's closing point stands: the same limitation
is used to reject the cheaper regex alternative while surviving inside this proposal.

### 9. The timeout constant does not bound the cycle

**Section:** State Machine; Error Handling. **Rung: 4, you ran it.**

> `src/browser_tools/cdp_handler.py:515` submits a coroutine and waits on its future.
> On timeout it returns `None` without cancelling the future.

**Verified: confirmed.** `future.result(timeout=...)` raises, `except Exception`
swallows it, and the coroutine keeps running on the CDP event loop. Today that leaks
a detection pass. Under this RFC it would leave a hosted API request in flight after
the caller has given up, which is a new and worse consequence of an existing gap the
RFC does not address.

### 10. Phase 2 cannot be made decision-neutral by threshold choice

**Section:** Implementation Plan, Phase 2. **Rung: 2, you pointed at the code.**

> Once model probabilities feed the proposed comparisons, no fixed threshold preserves
> arbitrary type-based decisions. [...] Phase 2 needs an explicit observation-only
> mode.

**Verified: confirmed.** The RFC says Phase 2 sets thresholds "so the Verdict cannot
change a decision yet", which no threshold in `[0,1]` achieves. The reviewer's
observation-only design, where the model Verdict is recorded while the heuristic
Verdict controls behavior, is what Phase 2 actually needs, and it is also what would
produce the corpus Open Question 4 depends on.

### 11. The prediction target and acceptance criteria are undefined, not deferred

**Section:** Design, "The questions"; Phase 3; Open Questions 1, 3, 4.
**Rung: 2, you pointed at the code.**

> "A few more seconds" does not identify whether `self_solving` predicts clearance
> within the next three-second wait or the remaining cycle. No elapsed time or attempts
> remaining appears in Evidence.

**Verified: confirmed.** The Noul asks about "a few more seconds" while the loop waits
a specific 3.0s, up to 3 times, and the Evidence carries neither the elapsed time nor
the attempts remaining, so the model cannot condition on either. The reviewer's
distinction holds: numeric thresholds may wait for measurement, but the horizon, the
labels, and the pass/fail criteria must be fixed before a corpus is collected or the
measurement means nothing. The reviewer also notes Open Question 3's floor of 500ms
contradicts its own statement that 500ms per pass is unacceptable.

### 12. Text-only input does not prove captcha solving is impossible

**Section:** Abstract; Scope; Security Considerations. **Rung: 2, you pointed at the
code.**

> Text-only input excludes native image input. It does not exclude text challenges,
> textual representations of visual content, or choosing among supplied answers.
> TypeSafe's model documentation explicitly allows preprocessing non-text inputs into
> structured state.

**Corroborated** by the meta pass (M11), which adds that the RFC never states
"MUST NOT add a vision model or solving service" as a normative requirement. It lives as
prose in the scope boundaries and in Security Considerations. The argument should rest on
the output channel plus the prohibition, and the prohibition should be normed.

**Verified: confirmed, and this corrects the drafter.** `models.md` says
"Pre-process non-text inputs (images, audio, video, binaries) into text or structured
fields before sending them", which is an instruction to do exactly what the RFC treats
as impossible. The real guarantee is the bounded four-question interface plus the
explicit prohibition, not the input modality. The RFC's Abstract sentence "it does not
add captcha solving, and it cannot: the model accepts text only" overstates and should
be replaced with the interface argument. The reviewer is also right that the converse
claim, that adding vision necessarily means solving, is unsupported as stated.

### 13. The RFC-01 amendment misidentifies a line and prescribes an invalid status

**Section:** Design, "Amendment to RFC-01". **Rung: 4, you ran it**, for the status;
**rung 2** for the attribution errors.

> RFC-01 line 81 describes upstream `chrome-agent` v0.5.7. It is not another
> prohibition governing future browser-tools development. [...] The proposed status,
> `Accepted (amended by RFC-02)`, fails the supplied structural validator.

**Reviewers disagree here.** The meta pass checked the three RFC-01 quotations and
cleared them as accurate. The openai pass checked something different and stronger:
whether each quoted line is *amendable policy*. On `:81` it is not. Both can be right,
and the openai reading is the one that matters. Verified below.

**Verified: confirmed on all three counts.** RFC-01:81 sits under the heading
`### chrome-agent (~/dev/chrome-agent, v0.5.7)` and is a factual description of a
third-party codebase; amending it would rewrite history rather than policy.
`VALID_STATUSES` in `validate-structure.ts:66` is
`["Draft", "Review", "Accepted", "Implemented", "Superseded", "Withdrawn"]`, so the
proposed status string would fail RFC-01's own validation. And RFC-01:248 contains no
PyPI-distribution rationale; the drafter attributed its own reasoning to RFC-01, in
the RFC's scope boundaries and in the chat that preceded it. Only two of the three
cited lines, `:31` and `:248`, are amendable policy.

### 14. Names and current-behavior claims that still disagree with the code

**Section:** Abstract; Introduction; Terminology; Design. **Rung: 2, you pointed at
the code.** Four separate errors, all verified:

- The `vendor` criteria use `cloudflare`; the detector emits `cloudflare_challenge`.
  No normalization is specified, so the fallback cannot map between them.
- "Degraded triage" is defined as producing a Verdict "without calling the model", but
  the timeout and API-error paths reach it after a call was attempted.
- `_deduplicate` selects the highest-confidence detection *within* each type. The RFC
  says it "ranks types by it", which it does not do.
- Non-retryable-only pages return immediately (`interstitial.py:146`), and retryable
  pages can clear after the first wait (`:153`). So the Abstract's "waits nine seconds
  on pages that will never clear" and the Motivation's "every blocked navigation pays
  it" both overstate the baseline this RFC is measured against.

### 15. The six Evidence fields have no collection path, and the RFC forbids adding one

**Section:** Design, "Shape" and "Script return shape"; Scope. **Reviewer: meta (M8).**
**Rung: 2, you pointed at the code.**

> The RFC simultaneously says the JavaScript "keeps collecting exactly what it collects
> today" (Out of scope) and that the builder's input "is the detection list that
> `parse_detection_result` already returns" (Design, "Shape"). Neither statement yields
> the six new fields.

**Verified: confirmed, and this is the second most serious finding after 1.** The RFC
says both things, at line 87 and line 165. Today's script returns
`{type, confidence, signal, details}` per detection and nothing else. `title`,
`cookie_names`, `script_origins`, `control_labels`, `body_excerpt`, and `url` appear in
no current return value and in no specified DOM read. The widened `{detections, evidence}`
envelope says what the shape is; nothing says what fills it, which cookie filtering
applies, or where the URL is captured. The scope boundary that promises no change to
collection directly contradicts the data model that depends on new collection. The
openai pass did not find this.

### 16. The retry loop's new signature and execution context are unspecified

**Section:** Design, "Shape" and "Gating the retry loop". **Reviewer: meta (M7).**
**Rung: 2, you pointed at the code.**

> `detect_with_retry` takes only `detect_once` (`src/browser_tools/interstitial.py:118-120`).
> The RFC never gives its new signature [...] the loop runs marshalled onto the CDP event
> loop with an outer timeout (`src/browser_tools/cdp_handler.py:509-517`); doing blocking
> network I/O per pass inside that loop is never addressed.

**Verified: confirmed on the substance; one sub-claim does not hold.** The signature at
`interstitial.py:118-120` is `detect_with_retry(detect_once)` and the RFC never states the
replacement. The execution-context point is the serious half and is correct: the retry
loop runs on the CDP event loop, and a synchronous TypeSafe call per pass would block that
loop, with no executor, deadline interaction, or safety argument given.

The reviewer's import-direction claim does not hold. `interstitial.py` importing
`challenge_triage`, which alone imports `typesafe_sdk`, satisfies both stated constraints.
Recorded here so it is not treated as an open problem.

### 17. Phase 1's gate is circular

**Section:** Design, "Amendment to RFC-01"; Implementation Plan. **Reviewer: meta (M10).**
**Rung: 1, asserted from the document.**

> "RFC-01 MUST be edited in place... its version MUST increment" rewrites an Accepted
> decision record instead of superseding it, and Phase 1's gate ("Kevin accepts RFC-02")
> makes the amendment's landing condition the acceptance of the very RFC that orders it.

**Verified: confirmed.** Phase 1 is listed as an implementation phase whose gate is
acceptance of the document containing it. Either the amendment takes effect on acceptance,
in which case Phase 1 is not a phase, or it is work with a gate that cannot be its own
acceptance. The separate point about editing an Accepted record in place rather than
superseding it is a live question this RFC should settle rather than assume, and it
compounds finding 13's invalid status string.

### 18. The extra's name is already taken by a deferred capability

**Section:** Design, "Packaging". **Reviewer: meta (M12).** **Rung: 2, you pointed at the
code.**

**Verified: confirmed.** The RFC defers "semantic element selection" to a later RFC and
simultaneously names this RFC's extra `semantic`. The deferred verb would presumably live
behind the same extra, so the name promises more than this RFC delivers. Also unspecified:
whether `all` gains `semantic` (`pyproject.toml:61-63` currently reads
`browser-tools[camoufox,profiling]`), and the `typesafe-sdk` pin and its CVE posture,
against a file whose own convention is to document risky transitive dependencies
(`pyproject.toml:44-59`).

### 19. Error-code mechanics are out of order and overlapping

**Section:** Error Handling. **Reviewer: meta (M13).** **Rung: 2, you pointed at the
code.**

**Verified: confirmed on every part.** E010 is at line 399, E012 at 406, E011 at 418, so
the codes are listed out of numeric order. E003, emitted by `format_interstitials`
(`interstitial.py:114`), overlaps E010 with no rule for which fires when both apply.
"E011 at most once per detection cycle" names no enforcement point that survives across
passes. And the reviewer closes a loop the drafter opened: the RFC promises the fallback
"reproduces today's behavior exactly", and today's behavior includes a defect. The
exhausted-retries path returns `detections + non_retryable` where `non_retryable` was
computed from the first pass and never reassigned (`interstitial.py:160-165`), so a
persistent non-retryable detection is reported twice. Specifying faithful reproduction
specifies reproducing the bug.

## Cleared

Checked and found sound, so the next reviewer need not repeat it:

- Jev's text-only input, the model id `jev-1.13.0`, the endpoint, the 64k total budget
  and the 32k state-plus-longest-question budget are all stated correctly.
- Batching Choice and Noul questions in one request matches the documented API. The
  RFC is right that vendor Choice confidence and Noul probability are different
  quantities, and right to ask `is_auth_wall` separately from the vendor Choice.
- The current-state claims about the retry set, the three retries, the three-second
  delay, the hand-assigned confidence strings, and the literal `auth_wall` substring
  check are accurate.
- Default dependencies are `websockets>=16.0`; `camoufox`, `profiling`, and `all`
  extras exist. The proposed `semantic` extra preserves RFC-01's base-install
  requirement.
- All three new domain nouns are defined and used. No wholly unused Terminology entry
  remains.
- The `HANDOFF -> BLOCKED` outcome is covered by E012, and bare-array override scripts
  are explicitly preserved. Both were drafter self-corrections and both hold.
- No E010, E011, or E012 code collision exists in the current source.
- Nineteen focused existing tests pass. The openai reviewer additionally exercised the
  real detector, retry, promotion, and timeout functions against synthetic dependencies.
- Independently cleared by the meta pass: the ten detection types with exactly two in
  `INTERSTITIAL_AUTO_RETRY_TYPES`; the nine-second arithmetic; and **every `file:line`
  citation in the RFC**, checked one by one after the drafter's own correction pass. The
  meta pass also confirms the `none` and `other` verdict options genuinely separate a
  clean page from an unknown vendor, so Motivation's third defect is real even though
  finding 8 shows the RFC's own guard makes it unreachable.

## Not reviewed

- No live browser navigation, real promotion, hosted inference call, real-page corpus,
  install matrix, latency measurement, or calibration experiment was run. Every
  quantitative claim in the RFC remains unmeasured.
- The full test suite was not run; some tests write files, which the read-only contract
  forbade.
- Issue #66's history and the site-specific Cloudflare clearance behavior were not
  independently verified.
- `how-to-build-with-system-one.md` could not be retrieved by the reviewer, so the
  approximately 100ms latency figure the RFC cites is unverified by the independent
  pass. The drafter retrieved it; that is a same-family observation.
- The meta pass could not run the structural validator (no shell in its session), so its
  frontmatter, placeholder, and keyword checks are unverified by that reviewer. The openai
  pass ran it.
- The meta pass did not fetch the TypeSafe Primitives, Confidence, fan-out, jaggedness, or
  Python SDK pages, so Choice-confidence semantics, Noul output shape, and the cost
  arithmetic are unverified by it. The openai pass verified the state and model budgets.
- Two independent families reviewed this document, so the single-route gap recorded in the
  first version of this review is closed. No third opinion was sought, and where the two
  reviewers disagree (finding 13) the disagreement is recorded rather than resolved by
  vote; the drafter verified that one against the source.
- This review does not say whether to build the thing. It says what you are deciding
  with.
