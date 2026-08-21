# Review: RFC-01 Merge chrome-agent core into browser-tools

## What was reviewed

- **RFC:** `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md`
- **Version:** the frontmatter carries no version field. Reviewed the draft dated 2026-08-21 as of working-tree state (uncommitted, no git hash). This review names it `draft-2026-08-21`. Finding 7 asks for a version field so later reviews can name what they read.
- **Status:** Draft
- **Reviewers:** one Claude pass (Fable 5, this session) plus one cross-family pass (muse-spark-1.2-contributor via `muse exec`, read-only). The model registry warned no non-Claude candidate meets the high-stakes review bar; the cross-family findings were verified against the code before inclusion.

## Structural results

`validate-structure.ts` output, verbatim:

```json
{
  "passed": true,
  "errors": [],
  "warnings": []
}
```

## Findings

### 1. The MCP compatibility freeze cites the wrong module as the frozen surface

**Section:** Proposed Changes > MCP compatibility contract.
**What is wrong:** the contract freezes "the existing MCP tool names in `tool_registry.py`" and gives `use_browser_session` as an example. `use_browser_session` and `attach_browser` are not in `tool_registry.py`; they are lifecycle/session tools routed outside it (`src/browser_tools/automation_backend.py:16-18` states this; handlers live in `src/browser_tools/browser_session.py:48`).
**Why it matters:** as written, the freeze excludes exactly the lifecycle tools agents call first. A Phase 1 lifecycle cutover could rename or reshape them without violating the contract's letter.
**Evidence:** rung 2 (`automation_backend.py:16`, `browser_session.py:48`, and a grep of `tool_registry.py` that does not contain them).

### 2. The exit-code contract contradicts the vendored CLI's behavior, and no section says who owns argument parsing

**Section:** Proposed Changes > CLI surface.
**What is wrong:** the RFC requires exit code 2 for usage errors. chrome-agent's `cli.py` exits 1 everywhere, usage errors included; there is no `sys.exit(2)` in the file (rung 2: `~/dev/chrome-agent/src/chrome_agent/cli.py:43-389`, grep found only `sys.exit(1)`). Meeting the contract means modifying the vendored `cli.py` or fronting it, and the RFC does not say which. Risk Assessment separately advises vendoring core modules verbatim.
**Why it matters:** the first implementer of Phase 0/1 has to invent the answer, which is the drift this RFC exists to prevent.
**Evidence:** rung 2.

### 3. Tension between the shape freeze and the Phase 2 native rebuild is real and unaddressed

**Sections:** Proposed Changes > MCP compatibility contract; Proposed Changes > Native snapshot; Migration Strategy Phase 2.
**What is wrong:** the freeze holds MCP response shapes fixed through Phase 3, and Phase 2 replaces the backend behind `ax_find`/`ax_node`/snapshot. The parity gate compares engine outputs (node sets, UID targets, text), not MCP response shapes. Nothing requires MCP contract tests to run during Phase 2.
**Why it matters:** both normative statements can hold only if the native backend preserves MCP shapes, and no test is specified to prove it. The Risk Assessment names "MCP front drift" and proposes contract tests "through Phase 3", but the Testing Strategy section never includes them.
**Evidence:** rung 3 (traced the interplay of the three sections; no code claim).

### 4. `console-list` and `network-list` are governed by a requirement but absent from the verb list

**Section:** Proposed Changes > CLI surface.
**What is wrong:** the normative verb list does not include `console-list` or `network-list`, yet a following requirement specifies their reimplementation and output compatibility. Whether they are REQUIRED verbs of the merged CLI is undecidable from the document.
**Why it matters:** an implementer can satisfy the verb list while deleting both commands, or keep both and satisfy the wrapper requirement; the two readings produce different surfaces for the agent skill to document.
**Evidence:** rung 2 (the two passages in the same section).

### 5. `window marking` is used once and defined nowhere

**Section:** Migration Strategy, Phase 4.
**What is wrong:** "window marking" appears as a Phase 4 deliverable and in no other section. It is not in Terminology, not in Proposed Changes, and not in the Introduction's scope list.
**Why it matters:** the Phase 4 gate cannot verify an unspecified deliverable.
**Evidence:** rung 2 (single occurrence in the RFC; the term originates as a chip in the approved plan's core layer).

### 6. Profile concurrency is an uncovered state

**Section:** Proposed Changes > Instance and profile model.
**What is wrong:** the model does not say what `launch --profile NAME` does when a live instance already holds that profile's user data dir. browser-tools carries `pid_holds_user_data_dir` in `process_utils.py` for exactly this situation (consolidated there by commit `de85a7c`), so the current code answers a question the spec does not.
**Why it matters:** two Chromes on one user data dir corrupt the profile; the registry section's per-instance record does not prevent it because profiles are the shared resource, not instances.
**Evidence:** rung 2 (`src/browser_tools/process_utils.py`; git log `de85a7c`).

### 7. No version field in the frontmatter

**Section:** frontmatter.
**What is wrong:** `number`, `status`, `date` exist; no `version`. This review cannot name a version and future reviews will collide with it.
**Why it matters:** a review of one draft sitting beside a later draft is stale invisibly.
**Evidence:** rung 2 (frontmatter lines 1-8).

### 8. `detect` is listed and never specified (minor)

**Section:** Proposed Changes > CLI surface.
**What is wrong:** `detect` appears in the curated verb list with no definition anywhere in the RFC. Context implies interstitial detection, but the document does not say so.
**Evidence:** rung 2.

### 9. Curated verbs show no INSTANCE slot (minor)

**Section:** Proposed Changes > CLI surface.
**What is wrong:** lifecycle and protocol verbs show where `INSTANCE` goes; curated verbs (`snapshot`, `click`, `frames list`) show no instance argument at all. The omitted-instance rule implies they take one, but the grammar never shows it.
**Evidence:** rung 2.

### 10. The RFC understates the chrome-agent verb set it vendors

**Section:** Current State > chrome-agent; Proposed Changes > CLI surface.
**What is wrong:** chrome-agent's CLI also ships a `guide` command (prints the bundled AGENTS.md) and a `--target`/`--url` flag pair extracted before routing (`src/chrome_agent/cli.py:21-49`). The merged CLI surface lists `guide` only as a layer-4 chip in the architecture table and never as a verb; `--target` appears nowhere.
**Why it matters:** Phase 0 vendors a CLI whose flags the normative surface does not mention; either they are in the contract or they are dropped, and the RFC does not say.
**Evidence:** rung 2 (`cli.py:21`, `cli.py:49`).

### 11. Registry location stated as fact is an Open Question elsewhere (minor)

**Sections:** Current State > chrome-agent; Open Questions 5.
**What is wrong:** Current State asserts the registry lives at `/tmp/chrome-agent/registry.json` (true for upstream, rung 2 via its README and `registry.py`), while Open Question 5 leaves the merged tool's registry location undecided. The Instance-model section's requirements ("the registry MUST record...") silently assume a location.
**Why it matters:** low; but a reader of the Instance model alone will take `/tmp` as decided.
**Evidence:** rung 2.

## Cross-family reviewer findings (muse-spark-1.2-contributor)

The `muse exec` read-only pass returned 17 findings (its full report is Appendix A). Its overlap with the Claude pass: its F1 confirms finding 2 (exit codes), F4 confirms finding 8 (`detect`), F6 confirms finding 3 (freeze vs. rebuild), F7 confirms finding 6 (profile concurrency), F12 confirms finding 10 (`guide` and flag omissions), and F13 overlaps finding 5 (`window marking`). The findings below are new. Each code claim marked verified was re-checked against the source in this session before inclusion, per the registry's warning that the reviewer sits below the high-stakes bar.

### 12. The normative registry schema adds fields the vendored code does not store

**Section:** Proposed Changes > Instance and profile model.
**What is wrong:** the RFC requires the registry to record `engine` and `bound profile`. `register()` writes `port`, `pid`, `browser_version`, `user_data_dir`, `launched`, `pid_start` and nothing else (`~/dev/chrome-agent/src/chrome_agent/registry.py:262-268`, verified). There is also no instance `name` inside the entry beyond the registry key.
**Why it matters:** the Phase 1 gate passes against the old schema and later phases break when they rely on `engine`/`profile`. The RFC MUST either specify the schema migration or drop the fields to a later phase.
**Evidence:** grade 2, verified (`registry.py:262-268`).

### 13. The `wait` verb exists in neither codebase and has no design

**Section:** Proposed Changes > CLI surface; Testing Strategy.
**What is wrong:** `OPERATIONAL_COMMANDS` is `{launch, status, attach, help, cleanup, stop, guide}` (`~/dev/chrome-agent/src/chrome_agent/cli.py:17`, verified) - no `wait`. The pattern exists only as a standalone script (`scripts/cdp-wait.py`). The RFC specifies syntax and a no-missed-events guarantee but no mechanism (long-lived attach with buffer, deadline behavior, overflow policy), no default timeout, and no timeout exit behavior.
**Why it matters:** `wait` is greenfield work disguised as vendoring; the conformance test in Testing Strategy cannot be written from the spec as it stands.
**Evidence:** grade 2 for the absence (verified); grade 1 for the claim that the spec is insufficient to implement from.

### 14. Launch flags drift in both directions

**Section:** Proposed Changes > CLI surface.
**What is wrong:** the vendored `launch` takes `[--port PORT] [--fingerprint PATH] [--headless] [--no-window-border] [-- CHROME_ARGS]` (`cli.py:70`, verified). The RFC's `launch` omits `--port`, `--no-window-border`, and the `-- CHROME_ARGS` passthrough, and adds `--profile`, `--channel`, `--engine`, which the vendored launcher does not accept (`launcher.py:73-82` signature, verified: no engine or profile parameter).
**Why it matters:** the RFC does not say whether the omitted upstream flags survive, and the added flags contradict the Risk Assessment mitigation to vendor modules verbatim and adapt call sites; `launch` requires modifying or fronting the vendored CLI, and no section owns that.
**Evidence:** grade 2, verified.

### 15. `frames` and `storage` subcommand spellings create a second naming authority

**Section:** Proposed Changes > CLI surface; MCP compatibility contract.
**What is wrong:** the CLI sketch says `frames list | frames select N | storage get --key K`; the existing tools are `list_frames`, `select_frame`, `reset_frame`, `get_frame_storage` (`tool_registry.py`). The RFC freezes the MCP names while introducing different CLI names, with no mapping between the two surfaces, and `reset_frame` has no CLI equivalent at all.
**Why it matters:** the agent skill (Phase 4) has to document one surface; the RFC creates two.
**Evidence:** grade 2 (muse citation `tool_registry.py:92-96`, consistent with this session's grep of the file).

### 16. Instance-name derivation and collision handling exist upstream but are not adopted

**Section:** Proposed Changes > CLI surface.
**What is wrong:** "derived from the working directory" is the whole spec. Upstream defines the derivation (lowercase, hyphenate, strip, `chrome` fallback) and collision suffixing (`{base}-{NN}`) in `registry.py:183-209`, plus dot-name disambiguation between instance names and `Domain.method` in `cli.py:498-514`. The RFC neither adopts nor replaces these rules.
**Why it matters:** the `INSTANCE Domain.method` grammar is ambiguous unless the name/method disambiguation rule is normative; two clones of one repo name-collide silently.
**Evidence:** grade 2 (muse citations, spot-consistent with the file regions read this session; the specific line ranges not independently re-read).

### 17. Registry corruption handling is not pinned

**Section:** Security Considerations; Instance and profile model.
**What is wrong:** upstream distinguishes a corrupt registry (load returns empty, status `unknown`) from a retired instance (parseable file, name absent) and attach relies on that distinction to survive a torn read (`registry.py:65-74`, `registry.py:363-395`, `attach.py:39-62`, per muse's trace). The RFC's "treat as untrusted input" plus "cleanup MUST remove entries whose liveness check fails" does not say whether `cleanup` may delete on a corrupt file or whether `stop`/`status` must refuse.
**Why it matters:** on a corrupt world-readable registry, one conforming implementation deletes everything and another keeps everything; the security guarantee is not decidable.
**Evidence:** grade 3 as traced by muse; this session verified the two-path structure exists but did not re-trace the attach dependency.

### 18. Camoufox instances cannot be represented by the vendored liveness model as specified

**Section:** Proposed Changes > Instance and profile model; Anti-detection.
**What is wrong:** liveness is defined by process identity plus CDP port attribution. Camoufox is Firefox driven through `camoufox_session.py`, which does not expose a Chrome `--remote-debugging-port`; `launch_browser` has no engine parameter (verified). The RFC routes `--engine camoufox` through the same registry and liveness model without saying how a Firefox instance satisfies port attribution.
**Why it matters:** Phase 1 cannot delete the old Camoufox lifecycle path until the registry can represent a Camoufox instance.
**Evidence:** grade 2 for the launcher/registry facts (verified); grade 1 for the claim about Camoufox's debugging surface (not inspected this session).

### 19. Smaller verified drifts

- **websockets floor mismatch:** browser-tools pins `websockets>=14.0`, chrome-agent `>=16.0` (both pyproject files, verified). The Packaging section says "websockets only" without reconciling the floor. Grade 2.
- **Parity gate has no comparison operator:** "results match" is undefined for node ordering, Shadow DOM, and iframe trees, and the corpus is Open Question 3, so the Phase 2 gate is not executable as written. Grade 1.
- **Profiling is in scope with no delivering section:** the Abstract and Scope claim profiling is rebuilt over the core; no Proposed Changes subsection, and no profiling tests in Testing Strategy. Grade 1, confirmed by text search.

## Cleared

- **Structural validation:** passed, output above, not re-derived by hand.
- **Terminology:** all 11 defined terms are used in the body; no defined-and-unused terms. Checked both directions; the gaps found are findings 5 and 8.
- **Scope delivery:** every item the Introduction claims in scope has a delivering section (vendoring rules, CLI surface, instance/profile model, MCP contract, native snapshot, packaging, tool-proxy retirement).
- **Line counts and dependency claims in Current State:** match `wc -l` and both `pyproject.toml` files (rung 4 for the counts, rung 2 for the deps).
- **cdp-wait pattern:** `~/dev/chrome-agent/scripts/cdp-wait.py` exists as the RFC assumes (rung 2).
- **Python floor:** >=3.13 is compatible with the vendored code's >=3.11 (rung 2, both pyproject files).
- **Phase rollback consistency:** Phases 0-1 revert cleanly by construction; Phase 2-3 rollback via the retained Node path is coherent given the parity gate ordering. One gap noted: Phase 3 rollback does not address `stealth.js` removal, but `stealth.js` deletion is independent of the Node engine and reverting the phase commit restores it, so not raised as a finding.
- **Security section:** trust boundaries, registry-as-untrusted-input, loopback binds, and the no-inference prohibition are present and consistent with both codebases' behavior.

## Not reviewed

- **The vendored modules' internals.** This review checked the RFC against the code's surfaces (CLI, tool routing, packaging), not the correctness of chrome-agent's registry/liveness implementation. The RFC's own Phase 0 requires that read at vendoring time.
- **The approved plan's decisions themselves.** Direction, daemon demotion, stealth deletion, packaging split were settled in the Lucid review; this review takes them as given and checked only whether the RFC renders them faithfully.
- **Performance claims** (50-80 ms round trip): upstream's own measurement, taken at rung 1 from its README; not re-measured.

## Appendix A: cross-family reviewer report, verbatim

The unedited report from muse-spark-1.2-contributor (`muse exec`, read-only, reasoning high). Findings above were merged from it after verification; where its numbering (F1-F17) differs from this review's, the mapping is given at the top of the cross-family section.

> # RFC-01 Review: Merge chrome-agent core into browser-tools
> 
> *File:* `/Users/kevin/dev/browser-tools/docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md` — status `Draft`, date `2026-08-21`, no `version` field. Read in full (236 lines) and verified against `/Users/kevin/dev/browser-tools` (`src/browser_tools/`) and `/Users/kevin/dev/chrome-agent` (`src/chrome_agent/`) on disk, read-only.
> 
> ---
> 
> ## 1. Summary
> 
> The RFC is internally consistent on the high-level merge direction but contains multiple whole-document gaps: a normative CLI verb (`wait`) and a curated verb (`detect`) that do not exist in either codebase, a normative registry schema that adds fields (`engine`, `bound profile`) the vendored code does not store, an exit-code contract the source CLI does not implement, and several states the spec names (timeouts, concurrent profile use, registry corruption vs. retirement, instance-name derivation) without handling them. The MCP-freeze vs. Phase-2 native-snapshot requirements conflict where the frozen surface includes the very tools the rebuild replaces.
> 
> ---
> 
> ## 2. Findings
> 
> ### F1 — CLI surface: exit-code contract contradicts source — drift from codebase
> 
> *RFC section:* `Proposed Changes / CLI surface (normative)` — `Exit codes: 0 success; 1 operational failure (browser error, CDP error); 2 usage error.`
> 
> *What is wrong:* `chrome-agent/src/chrome_agent/cli.py` does not implement a 1-vs-2 distinction. Every operational and usage error calls `sys.exit(1)` (`cli.py:43,121,125,137,168,187,211,257,260,270,285,301,320,327,330,363,372,376,386,389,396,410,453,456,459,532`). The only `sys.exit(2)` in the vendored core is `supervisor.py:270` for `python -m chrome_agent.supervisor` invoked with too few argv — not a CLI usage error surfaced to a user.
> 
> *Why it matters:* Adopters or tests that branch on exit code 2 for usage errors will never see it; the contract is unverifiable until the CLI is rewritten. Phase 3 gate `every CLI verb works with no daemon` cannot be checked against this contract.
> 
> *Evidence grade:* **rung 2** — pointed at code with file:line (`chrome-agent/src/chrome_agent/cli.py:43` et seq., `chrome-agent/src/chrome_agent/supervisor.py:270`).
> 
> ### F2 — Instance and profile model: registry schema adds fields that do not exist — drift
> 
> *RFC section:* `Proposed Changes / Instance and profile model` — `The registry MUST record, per instance: name, PID, process identity data sufficient for the liveness check, CDP port, engine, bound profile (if any), and launch time.`
> 
> *What is wrong:* `chrome-agent/src/chrome_agent/registry.py:262-266` `register()` writes `{"port","pid","browser_version","user_data_dir","launched","pid_start"}`. No `engine` key, no separate `profile` name. `user_data_dir` encodes a session dir under `/tmp/chrome-agent/session-*` (`launcher.py:24` `_SESSION_ROOT = "/tmp/chrome-agent"`), not a browser-tools named profile (`CACHE_DIR/profiles`). The vendored schema cannot satisfy the MUST without a migration.
> 
> *Why it matters:* Phase 1 gate (`lifecycle e2e tests pass against the registry; the old lifecycle code paths are deleted`) will pass against the old schema and then break when later phases rely on `engine`/`profile`.
> 
> *Evidence grade:* **rung 3** — traced execution: `registry.py:236-280` `register()` and `registry.py:26` `REGISTRY_PATH`, compared to RFC `instance and profile model` MUST.
> 
> ### F3 — CLI surface: `wait` verb is normative but exists in neither codebase — dangling + scope without deliverable
> 
> *RFC section:* `Proposed Changes / CLI surface` — `wait INSTANCE --event Domain.event [--match SUBSTRING] [--timeout SECONDS]` and `wait MUST match events that fired after the attach began but before the wait call examined them (the cdp-wait pattern)`.
> 
> *What is wrong:* `chrome-agent/src/chrome_agent/cli.py:17` `OPERATIONAL_COMMANDS = {"launch","status","attach","help","cleanup","stop","guide"}` — no `wait`. `grep -rn "--event|--timeout|--match" src/chrome_agent/*.py` returns nothing; only `attach.py:264` `await _subscribe(event_name)` for `attach`. `browser-tools` has `wait_idle`/`wait_stable` as MCP tools (`tool_registry.py:103-104`, `cdp_handler.py:256-257`), not a CLI `wait --event`. The RFC defines syntax, timeout, and buffering semantics for a verb that would be greenfield; no section specifies buffering, storage, or timeout default, and `Testing Strategy / Event tests` only says `wait MUST have a test proving it catches an event fired between attach and wait` without defining how `wait` is implemented (long-lived attach vs. short poll).
> 
> *Why it matters:* Implementers must invent the `wait` mechanism (attach + buffer + deadline) that the liveness and attach-isolation guarantees depend on, but the spec provides no design.
> 
> *Evidence grade:* **rung 3** — traced: searched `chrome-agent/src/chrome_agent/cli.py` operational set, `attach.py` full text, `browser-tools/src/browser_tools/tool_registry.py` entries; confirmed absence.
> 
> ### F4 — CLI surface: `detect` verb dangling
> 
> *RFC section:* `Proposed Changes / CLI surface` — `wait-idle | wait-stable | detect` on one line.
> 
> *What is wrong:* No definition of `detect` arguments, output, or CDP domains. `browser-tools` has `detect_interstitials_async` / `detect_with_retry` (`cdp_handler.py:27-28`, `interstitial.py`) invoked automatically post-navigation (`cdp_handler.py:502-523`), not a user verb. `tool_registry.py` lists no `detect` tool. The Architecture layer 2 calls `detect` a curated tool but its section `Native snapshot` and `Testing Strategy` never mention it.
> 
> *Why it matters:* A normative CLI line that is undefined will cause divergent `--help` vs. skill documentation (Phase 4 gate `skill's documented verbs match --help output exactly` cannot pass).
> 
> *Evidence grade:* **rung 2** — `browser-tools/src/browser_tools/tool_registry.py:62-110` no entry; `cdp_handler.py:502` internal method only.
> 
> ### F5 — CLI surface: `frames`/`storage` verb spellings drift from code
> 
> *RFC section:* `Proposed Changes / CLI surface` — `frames list | frames select N | storage get --key K`.
> 
> *What is wrong:* Actual curated tool names are `list_frames`, `select_frame`, `reset_frame`, `get_frame_storage` (`tool_registry.py:92-96`, `cdp_handler.py:250-253`). The RFC introduces space-separated subcommands with different names and an undocumented `--key` flag, without mapping to CDP methods or to the MCP names that `MCP compatibility contract` promises to freeze.
> 
> *Why it matters:* Phase 3 contract `existing MCP tool names in tool_registry.py (for example use_browser_session, take_screenshot, ax_find) MUST keep their current names` suggests hyphen/underscore stability, but the new CLI already renames frame/storage tools, creating a second naming authority.
> 
> *Evidence grade:* **rung 2** — `tool_registry.py:92-96` vs. RFC line 121.
> 
> ### F6 — Conflicting normative statements: MCP freeze vs. Phase 2 native snapshot rebuild
> 
> *RFC section:* `Proposed Changes / MCP compatibility contract` (`tool names, argument shapes, and response shapes ... MUST keep ... through the end of Phase 3`) vs. `Proposed Changes / Native snapshot` (`Snapshot and UID interaction are rebuilt on the CDP Accessibility domain. UIDs MUST be stable...`) and `Risk Assessment / Parity gap`.
> 
> *What is wrong:* `snapshot` / `click --uid` / `fill --uid` are currently `chrome-devtools-mcp` forwarded tools (default path via `mcp_broker.py:3`, `chrome_config.py:31` `npx chrome-devtools-mcp@latest`). Their response shape (node sets, UID assignment, text extraction) is what the parity suite (`Testing Strategy / Parity gate`) measures. A `SHOULD remain compatible` carve-out exists only for `console-list`/`network-list` (`Proposed Changes / CLI surface`), not for snapshot. The RFC simultaneously freezes the MCP surface (including snapshot if exposed via MCP) and rebuilds snapshot semantics under the same names. Both MUSTs cannot hold if native Accessibility output differs — which the Risk section admits is the highest risk.
> 
> *Why it matters:* Phase 2 gate `results match on the full corpus for two consecutive runs, flake-free` decides when `--engine mcp` can be removed, but implementers following the freeze literally cannot change snapshot shape, and following the rebuild literally must.
> 
> *Evidence grade:* **rung 2** — cited RFC lines; traced forwarded-tool path `tool_registry.py:35,61` `everything else is a default chrome-devtools-mcp tool forwarded as-is`.
> 
> ### F7 — Uncovered state: concurrent use of one profile
> 
> *RFC section:* `Proposed Changes / Instance and profile model` — `Instances and profiles are orthogonal. launch --profile NAME binds a profile to a new instance.`; `Security Considerations / Registry file` and `Testing Strategy / Liveness tests` require port/PID isolation.
> 
> *What is wrong:* No handling for two concurrent `launch --profile NAME` using the same `user_data_dir`. Chrome locks `--user-data-dir` (singleton `SingletonLock`); `browser-tools` has `clean_stale_singleton_lock` (`process_utils.py`, `persistent_browser.py:42` import) but the RFC never specifies locking, queuing, or error for concurrent launches, nor whether `launch --profile` reuses an existing live instance vs. creates a second. Profile catalog (`profile_catalog.py:18` `CACHE_DIR/profiles`) is inspected but not locked.
> 
> *Why it matters:* Two agents on one machine (the stated blast radius: `loss of browser automation on the machines using this tool`) will race on the same profile dir and corrupt the registry entry's `user_data_dir` without violating the stated liveness check.
> 
> *Evidence grade:* **rung 2** — absence confirmed by `grep -rn "profile|concurrent|lock" src/chrome_agent/registry.py` (no concurrency primitive) and RFC text search for `concurrent` (zero hits).
> 
> ### F8 — Uncovered state: timeouts
> 
> *RFC section:* `Proposed Changes / CLI surface` — `wait ... [--timeout SECONDS]` and `Risk Assessment` (suspend/resume) plus `Architecture` passthrough.
> 
> *What is wrong:* No default timeout for `wait`, `attach`, `launch` (actual `launcher.py:97,190` 30 s `TimeoutError`), or `cdp_client.send` (`browser-tools/src/browser_tools/cdp_client.py:52,147` 30 s default; `chrome-agent/src/chrome_agent/cdp_client.py` uses `urllib timeout 2` for protocol fetch). No behavior on `wait` timeout (exit code, stdout vs. stderr, partial event list). A caller that issues `wait --timeout 0` or omits timeout gets undefined behavior.
> 
> *Why it matters:* The cdp-wait buffering guarantee (`MUST NOT miss an event by racing its own subscription`) is timeout-sensitive; without a specified deadline and overflow policy, the conformance test in `Testing Strategy / Event tests` is unwritable.
> 
> *Evidence grade:* **rung 2** — `launcher.py:190` and `cdp_client.py` timeout lines vs. RFC absence.
> 
> ### F9 — Uncovered state: registry corruption vs. retirement — inconsistent handling
> 
> *RFC section:* `Security Considerations / Registry file` — `Registry contents MUST be treated as untrusted input: a corrupted or attacker-written registry entry MUST NOT cause the tool to signal or kill a process it cannot first verify ownership of via the liveness check.` plus `Instance and profile model / cleanup MUST remove registry entries whose liveness check fails`.
> 
> *What is wrong:* The vendored code has two paths: `_load_registry()` (`registry.py:65-74`) returns `{}` on `JSONDecodeError/OSError` and logs a warning, collapsing corruption to `unknown`; `registration_status()` (`registry.py:363-395`) distinguishes `retired` (non-empty parseable dict, name absent) from `unknown` (missing/empty/unparseable). `attach.py:58-60` `_liveness_verdict` relies on the distinction to avoid exiting on a torn read. The RFC never specifies which path the CLI must use, whether `cleanup` may delete on a corrupt file, or whether `stop`/`status` should refuse on corrupt entries. A whole-document reader cannot tell if `cleanup MUST remove stale entries` applies to a corrupted file.
> 
> *Why it matters:* On a corrupt `/tmp/chrome-agent/registry.json` (world-readable temp dir per `Security Considerations`), one implementation deletes all entries, another treats them as `unknown` and keeps them — the security guarantee is not pinned.
> 
> *Evidence grade:* **rung 3** — traced `registry.py:65-74` vs. `registry.py:363-395` vs. `attach.py:39-62`.
> 
> ### F10 — Uncovered state: instance-name collisions and derivation
> 
> *RFC section:* `Proposed Changes / CLI surface` — `The default instance name is derived from the working directory.` and `INSTANCE MAY be omitted when exactly one instance is running; when omitted with multiple instances running, the command MUST fail with the instance list rather than guess.`
> 
> *What is wrong:* No spec for derivation (lowercase, strip, `chrome` fallback) or for collision suffix (`-01`, `-02`). Vendor code `registry.py:183-209` `_derive_base_name` (lowercase, replace spaces with hyphens, strip `[^a-z0-9.-]`, collapse `--`, fallback `chrome`) and `_derive_unique_name` (`{base}-{suffix:02d}`) define this, but the RFC does not normatively adopt it, nor handle the case where a directory basename like `aroundchicago.tech-01` (`cli.py:501-512` dot-handling comment) collides with an existing instance name vs. a `Domain.method` lookup.
> 
> *Why it matters:* `launch` in two clones of the same repo directory will derive the same base, then silently suffix; a later `status` or `INSTANCE Domain.method` routed via `cli.py:506-514` `is_known_instance` check depends on the exact suffix format to disambiguate from CDP methods.
> 
> *Evidence grade:* **rung 3** — traced `registry.py:183-209`, `cli.py:498-514` dot-name disambiguation.
> 
> ### F11 — Drift: launch flags vs. source
> 
> *RFC section:* `Proposed Changes / CLI surface` — `launch [--headless] [--profile NAME] [--channel NAME] [--fingerprint FILE] [--engine chrome|camoufox]` and `Packaging` extras.
> 
> *What is wrong:* `chrome-agent` `cli.py:69-72` `launch [--port PORT] [--fingerprint PATH] [--headless] [--no-window-border] [-- CHROME_ARGS]` — no `--profile`, no `--channel`, no `--engine`. `--port` and `--no-window-border` and `-- CHROME_ARGS` passthrough are omitted from the RFC; `--profile`/`--channel`/`--engine` are browser-tools concepts (`browser_tools_session.py:37-49` `--channel stable|canary|beta|dev`, `profile_catalog.py` `CACHE_DIR/profiles`) that the vendored core does not support. The spec claims to vendor the core verbatim yet normatively adds launch flags that require new code.
> 
> *Why it matters:* Phase 1 `Named profiles and project config become launch attributes` cannot be satisfied by verbatim vendoring; call sites must be adapted, contradicting `Risk Assessment` mitigation `vendor the module verbatim ... adapt call sites rather than the module`.
> 
> *Evidence grade:* **rung 2** — `chrome-agent/src/chrome_agent/cli.py:69` vs. RFC line 105.
> 
> ### F12 — Drift: `guide`/`help` and static vs. live schema
> 
> *RFC section:* `Proposed Changes / CLI surface` requirements — `help with a running instance MUST read the protocol schema from that browser, not from a bundled copy.`
> 
> *What is wrong:* Minor fidelity: `chrome-agent/src/chrome_agent/protocol.py:56-72` `fetch_protocol_schema(port=9222)` fetches `http://localhost:{port}/json/protocol` via `urllib` with 5 s timeout; without a browser it raises `ConnectionError` and `cli.py:224-226` falls back to `_print_static_usage()`. The RFC correctly captures live-vs-static, but omits from its verb table the `guide` command that exists in `cli.py:17` and that the Architecture `Fronts` layer lists (`CLI verbs (primary), agent skill, optional MCP front, guide command`). Vice versa, `help [INSTANCE] [Domain.method]` query-disambiguation (`cli.py:214-241` `lookup` trial) is not specified.
> 
> *Why it matters:* Phase 4 gate `the skill's documented verbs match --help output exactly` will mismatch if `guide` is shipped from `chrome_agent` but omitted from the RFC verb table.
> 
> *Evidence grade:* **rung 2** — `cli.py:17,48-62,214-241`, `protocol.py:56-72`.
> 
> ### F13 — Terminology: defined and never used / used and never defined
> 
> *RFC section:* `Terminology`.
> 
> *What is wrong:*
> - Defined and never reused: `Engine` (defined as `Chrome/Chromium (default) or Camoufox`) appears only once more as a layer row (`Architecture` table `Engine routing`), never as a normative type in `Packaging` or `Instance and profile model` beyond `--engine` flag text. `Parity suite` defined but only reused in `Testing Strategy / Parity gate`. `Curated tool` defined with example `wait-stable` but `wait-stable` in code is `wait_stable` (`tool_registry.py:104`).
> - Used and never defined: `window marking` / `bundled agent guide` (`Migration Strategy / Phase 4`, `Out of tool-proxy`) — the supervisor's colored border/badge (`supervisor.py:73-100` `build_overlay_script`, `PALETTE` crimson, 6 px border) is nowhere defined as a term. `profiling` (`Abstract`, `Packaging`) — no term entry, and the profiler (`browser-tools/src/browser_tools/profiler.py:220` `CPU profile`) vs. `browser-tools-profiler` entry point is never mapped to `Layer 0-4`.
> - Mixed: `Profile` vs. `Fingerprint profile` — `Profile` is `a named, persistent browser identity (user data dir plus recorded launch attributes)`, `Fingerprint profile` is `a set of launch flags`. The RFC then uses bare `profile` for both (`launch --profile NAME` vs. `launch --fingerprint FILE`), inviting the concurrent-profile confusion of F7.
> 
> *Why it matters:* A reader cannot tell whether `window marking` is part of `Core` or `Policy` or `Browser` layer, nor whether `profiling` installs the default `websockets`-only package or requires an extra.
> 
> *Evidence grade:* **rung 1** — asserted from full-document term search (`grep -c` per term; zero hits for `window marking` outside Phase 4, zero definition for `profiling` term).
> 
> ### F14 — Scope claimed but no section delivers
> 
> *RFC section:* `Scope / In scope: ... profiling are rebuilt on top of it as ordinary CDP consumers.` and `Abstract: Camoufox routing, and profiling are rebuilt on top ... as ordinary CDP consumers.`
> 
> *What is wrong:* No `Proposed Changes` subsection specifies how profiling (`profiler.py:289`, `browser-tools-profiler` entry point `pyproject.toml:52`) is rebuilt over the vendored `cdp_client.send` path, whether its long-lived process (`Security Considerations / Screencast and the profiler own their long-lived processes`) changes, or how its extra (`Packaging` `browser-tools[profiling]`) interacts with the MCP freeze. `Testing Strategy` has no profiling tests.
> 
> *Why it matters:* A scope claim with no delivering section leaves a Phase 3/4 implementer to decide whether profiling still depends on `mcp_daemon` vs. the new core, risking the `MUST NOT depend on the MCP front` violation.
> 
> *Evidence grade:* **rung 1** — asserted: RFC search for `profiling` hits 3 times (Scope, Abstract, Packaging) with zero design text.
> 
> ### F15 — Drift: dependency versions, line counts, and entry points — mostly sound, one version mismatch
> 
> *RFC section:* `Current State` for both projects.
> 
> *What is in the RFC vs. code:*
> - `browser-tools 9,800 lines of Python across ~36 modules under src/browser_tools/` — actual `wc -l src/browser_tools/*.py` = 9807, `ls src/browser_tools/*.py | wc -l` = 32 `.py` + `attach_chrome.sh` + `detect_interstitial.js` + `persistent-session-template.mjs` + `stealth.js` = 36 files-level; **cleared** (see Cleared).
> - `Python >=3.13` (`pyproject.toml:7`) vs. `chrome-agent Python >=3.11` (`chrome-agent/pyproject.toml:14` `requires-python = ">=3.11"`) — correct, and `Packaging / Python requirement: >=3.13` resolves the floor.
> - `websockets`: RFC says default install `MUST depend on websockets only`, but current `browser-tools/pyproject.toml:37` `websockets>=14.0` vs. `chrome-agent/pyproject.toml:16` `websockets>=16.0` — a minor version floor mismatch that vendors will have to reconcile (not specified).
> - `aiohttp>=3.14.1 CVE override forced by camoufox` (`pyproject.toml:38-41`) — correctly described, and `Packaging` correctly moves it into the `camoufox` extra.
> - `domains/: 54 typed convenience classes` — actual `src/chrome_agent/domains/*.py` = 55 files including `__init__.py`, i.e. 54 domain modules; **cleared** within counting convention but the RFC should say `54 domains + package init`.
> 
> *Evidence grade:* **rung 2** — file:line citations above.
> 
> ### F16 — Drift: `engine` field again — liveness and Camoufox routing assume storage that does not exist
> 
> *RFC section:* `Proposed Changes / Instance and profile model` (registry MUST record `engine`) + `Anti-detection` (`launch --engine camoufox`, `camoufox` extra) + `Current State / chrome-agent ... No AI or ML inference ... Anti-detection is launch-flag spoofing only.` (`fingerprint.py:9-24` header explains why JS injection is not used).
> 
> *What is wrong:* `launcher.py:73-82` `launch_browser` signature has no `engine` parameter; `registry.py` stores no engine; Camoufox is a separate project (`browser-tools/src/browser_tools/camoufox_session.py:4` `custom Firefox` with `BrowserForge` fingerprint `camoufox_session.py:135`). The RFC merges `engine routing` into the registry without specifying where Firefox vs. Chrome is recorded, and how `status` or `liveness` (`process_is_ours` + `_cdp_port_claimants` linux-only `registry.py:96-142`) applies to Firefox/Camoufox which does not use `--remote-debugging-port`.
> 
> *Why it matters:* Phase 1 cannot delete old `persistent_browser.py` paths for Camoufox launches until the registry can represent them.
> 
> *Evidence grade:* **rung 2** — `launcher.py:73`, `registry.py:236-268`.
> 
> ### F17 — Under-specified parity gate — cannot be a gate as written
> 
> *RFC section:* `Testing Strategy / Parity gate: The gate passes when results match on the full corpus for two consecutive runs, flake-free.`
> 
> *What is wrong:* No tolerance for node ordering (`ax_find` vs. Accessibility tree order `cdp_handler.py:808-875`), no definition of `match` (exact node set vs. UID-stable subset per `Native snapshot / UIDs MUST be stable`), no handling for intentionally different semantics (Shadow DOM, iframe `list_frames` vs. Accessibility `getFrameTree`). Open Question 3 `Parity corpus` is still open, so the gate's subject is undefined.
> 
> *Why it matters:* Phase 2 `size L` cannot be judged `go/no-go` without a frozen corpus and comparison operator.
> 
> *Evidence grade:* **rung 1** — asserted from RFC text; confirmed `Open Questions / 3. Parity corpus` `Needs a survey of the current e2e fixtures before deciding.`
> 
> ---
> 
> ## 3. Cleared — checked and found sound
> 
> - **Browser-tools size:** `9807` lines `wc -l` matches `About 9,800 lines` (rung 4 — ran `wc -l`).
> - **Browser-tools dependencies:** `camoufox[geoip]>=0.4.11`, `pillow>=10.0.0`, `websockets>=14.0`, `aiohttp>=3.14.1` CVE override (`pyproject.toml:34-42`) matches description (rung 2).
> - **Browser-tools entry points:** `browser-tools = browser_tools.browser_tools_session:main` and `browser-tools-profiler = browser_tools.profiler:main` (`pyproject.toml:50-53`) (rung 2).
> - **MCP daemon file set:** `mcp_daemon.py`, `mcp_broker.py`, `daemon_supervisor.py` all present under `src/browser_tools/` (rung 2).
> - **Snapshot via Node:** `persistent-session-template.mjs` exists (`src/browser_tools/persistent-session-template.mjs`) and `chrome_config.py:31` `npx chrome-devtools-mcp@latest` (rung 2).
> - **`stealth.js` injection:** `src/browser_tools/stealth.js` exists, injected via `cdp_handler.py:398-440` `Page.addScriptToEvaluateOnNewDocument` (rung 2).
> - **Lifecycle modules:** `persistent_browser.py`, `process_utils.py`, `session_store.py`, `session_reaper.py` present (rung 2).
> - **chrome-agent source of core:** `registry.py`, `instance_status.py`, `launcher.py`, `supervisor.py`, `attach.py`, `session.py`, `connection.py`, `cdp_client.py`, `protocol.py`, `fingerprint.py`, `errors.py`, `utils.py`, `cli.py` all present (`src/chrome_agent/*.py` 3330 lines total, rung 2).
> - **chrome-agent runtime dep:** `websockets>=16.0` single dep, `requires-python >=3.11`, entry point `chrome-agent = chrome_agent.cli:main` (`chrome-agent/pyproject.toml:14-18`) (rung 2).
> - **Registry location `/tmp/chrome-agent/registry.json`:** `registry.py:26` `REGISTRY_PATH = "/tmp/chrome-agent/registry.json"`, `launcher.py:24` `_SESSION_ROOT = "/tmp/chrome-agent"` (rung 2).
> - **One-shot CDP latency / attach isolation claim:** `cdp_client.py` one-shot via `Target.attachToTarget`/`detachFromTarget` (`cli.py:429-448`) and `attach.py:160-391` per-session subscriptions with stdin re-subscription (`attach.py:300-311`) support the isolation sentence `attach subscriptions MUST be isolated per session` in principle; implementation verified to use isolated `sessionId` (rung 3 — traced).
> - **Fingerprint as launch flags only:** `fingerprint.py:9-24` header `intentionally does NOT inject JavaScript` matches `Anti-detection / MUST NOT inject JavaScript` (rung 2).
> - **MCP tool count ~40:** `tool_registry.py` 35 explicit `ToolFlags` entries; remainder are default-forwarded `chrome-devtools-mcp` tools (`tool_registry.py:35,61` note), so `~40` is a fair estimate, not a contradiction (rung 2).
> - **No inference / no captcha:** `grep -rn infer|captcha|ml|ai` hits only RFC's own prohibition; no model code in either repo (rung 2 — search that found nothing reported).
> 
> ---
> 
> ## 4. Not reviewed
> 
> - `.lucid/merge-plan.html` version 5 and `.lucid/merge-plan/` review record (cited as normative but not opened — review scope was RFC vs. codebases).
> - `chrome-devtools-mcp` external behavior and `persistent-session-template.mjs` runtime semantics (not executed).
> - Actual parity corpus pages and `ax_find` vs. Accessibility equivalence (no browser was launched; no CDP was exercised — rung 4 not attempted).
> - Windows support, macOS suspend/resume power events, and `/proc`-less platforms beyond the `OSError` fallback already traced.
> - Packaging extras resolution and `pip install` error-message wording (no install was run).
> 
> ---
> 
> ## 5. Methodology and evidence grades
> 
> - **Rung 1 — asserted:** F13, F14, F17 (term searches and open-question status; no code line to cite).
> - **Rung 2 — pointed at code with file:line:** F1, F2 (partial), F4, F5, F6, F11, F12, F15, F16.
> - **Rung 3 — traced execution:** F2 (registry write/read), F3 (absence traced across CLI + attach + registry), F7-F10 (control-flow tracing of profile handling, timeouts, corruption vs. retirement, derivation), F1/F9 isolation checks.
> - **Rung 4 — ran code:** only the `wc -l` / `ls` / `grep` shell probes that produced counts; no browser, no CDP round-trip, no install matrix was run.
> 
> *A search that found nothing is a result:* `grep -rn "--event|--timeout|--match" src/chrome_agent/` (nothing), `grep -rn "concurrent" docs/rfc/...` (nothing), `grep -rn "captcha|inference" src/browser_tools/ src/chrome_agent/` (nothing outside RFC) — each reported above.
> 
> 
