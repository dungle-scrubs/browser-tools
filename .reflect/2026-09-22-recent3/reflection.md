# Reflection: recent 3, browser-tools registry-lock session (2026-09-22)

## Sessions
- Active pi session 01a0c7f4, cwd /Users/kevin/dev/browser-tools (this conversation;
  used as digest via input packet, not raw transcript).
- claude 987a6c16-cb86-4c2b-af48-c5ddf80f4e0c, same cwd, 2026-09-21 (sampled for recurrence).
- muse 01a0c1a0-dbb0-76e2-ba59-bb253e42d97c, same cwd, 2026-09-21 (coverage gap:
  export returned a limits/capabilities envelope, no transcript content).

Requested width: default single reviewer (all lenses + synthesis by one worker).
Session count: recent 3. Authorization: review only; no source edits approved.

## Coverage limits
- Pi transcript unavailable; packet + named artifacts are observations, not a full sequence.
- Claude export sampled selectively; no raw historical test/CI output verified.
- Muse export unusable; no finding relies on it.
- No tests or reproductions run during review; passing counts and CI rerun are reported outcomes.
- One worker did all lenses and synthesis; no independent corroboration.
- Input packet classified privacy Normal; see privacy incident below.

## Privacy incident
During review, a sampled Claude message printed credential values from an earlier
incident report into the hosted review session. The worker reported it at once and
stopped transcript reading. Credentials are not repeated in the report or here.
Action needed: rotate the credentials named in that earlier incident report.
Further transcript inspection needs a local privacy route.

## Accepted

| ID | Problem | Concrete proposal | Scope and routing | Evidence | Recommendation and undo |
|---|---|---|---|---|---|
| A1 | GUIDE.txt explains registry status but gives no response when status disagrees with a browser still running. | Add under `status`: "If status returns [] while the browser you were using is still running, treat this as a registry/process mismatch. Before another launch, verify that browser's process identity, user-data-dir, and debugging port. To drive its verified loopback endpoint, use --endpoint URL; see EXTERNAL BROWSERS. This does not restore registry management. If identity cannot be established, stop and report the mismatch." | Project docs: /Users/kevin/dev/browser-tools/src/browser_tools/GUIDE.txt, LIFECYCLE VERBS / status. Ships through `bt guide`. Affects browser-tools operators and agents. | T1; ledger L:4-10; checked GUIDE.txt, CONTEXT.md, lifecycle.resolve_cdp_port, endpoint.py. | Approve. Supplies a missing decision branch with existing functionality. Undo: remove the paragraph; no runtime state changes. |

## Rejected
- R1 (causal-evidence rule for diagnose-bugs): already covered by that skill; missing work is incident attribution, kept as B3.
- R2 (fixed-workspace rule or placement repeat): already fixed in GUIDE.txt by #145; broader scope lacks evidence and authorization.
- R3 (locking/testing reminders for shared skills): guidance already clear; testable gaps belong in B1/B2.
- R4 (new registry-recovery or crash skill): existing owners fit; no separate responsibility shown.

## Backlog (proposed follow-ups, not filed)
- B1: Behavioral regression tests through production lifecycle mutations (concurrency test
  must fail if a production lock is removed). Scope: tests/test_cli_lifecycle.py.
- B2: Scope registry-lock re-entry by lock path (current thread-wide depth can skip a
  different registry's lock). Scope: lifecycle.py registry_lock + tests.
- B3: Resolve crash and registry-loss attribution gaps (verdict stronger than experiment;
  also check wrapper/older-build/retry alternative for the empty-registry launches).
  Scope: local investigation in .scratch/chrome-orphan-crash/.
- B4: Investigate recurring event-loop CI failures (295 async errors on py3.14, green on
  rerun; similar signature in claude session). Scope: .github/workflows/ci.yml + fixtures.

## Pending decisions
- A1 applied via PR #146 (squash commit aca6037 on main). B2 applied in the same PR.
- B1, B3, B4 remain open; no authorization for follow-up work.
- Default fallback: files and runtime configuration unchanged.
