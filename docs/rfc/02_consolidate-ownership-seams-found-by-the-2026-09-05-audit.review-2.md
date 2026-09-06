# Review of RFC-02, version 2

## What was reviewed

- RFC: `docs/rfc/02_consolidate-ownership-seams-found-by-the-2026-09-05-audit.rfc.md`
- Version: **2**. Status: **Draft**. Review date: 2026-09-05.
- RFC SHA-256: `d2ace76819a9585042ca61de2387adf287a0c73cacbb63ce66d47775216cfa83`.
- Code baseline: `1775e17fc3a30494d311210a6ac912bb7e56a4a8`, with the RFC and audit present as untracked documents.
- Single-reviewer, whole-document pass, with targeted code verification. The RFC is unchanged. This review does not decide whether to build it.

Locations below use repository-relative paths. `RFC:line` means the RFC named above.

## Structural results

Command run from the repository:

```text
npx tsx /Users/kevin/.agents/skills/draft-rfc/scripts/validate-structure.ts docs/rfc/02_consolidate-ownership-seams-found-by-the-2026-09-05-audit.rfc.md
```

Exit code: 0. Output, verbatim:

```json
{
  "passed": true,
  "errors": [],
  "warnings": []
}
```

## Findings

### 1. High: phase 1 deletes the transport that surviving curated verbs still require

**Lands in:** Proposed Changes 1 and Implementation Plan phases 1 and 3 (`RFC:188`, `RFC:516`, `RFC:523`).

Section 1 requires deleting `cdp_client.py`; phase 1 implements section 1 and requires no surviving import of a deleted name. The replacement transport is deferred to phase 3. These requirements cannot all hold at the phase-1 gate.

`src/browser_tools/cdp_handler.py:396` imports `CDPClient` and `get_page_ws_url` from the old client when connecting. `src/browser_tools/curated.py:129` constructs that handler for the surviving curated verbs. The error adapters also import the old error class (`src/browser_tools/cdp_handler.py:99`, `src/browser_tools/screencast.py:42`). Merely postponing deletion of the client file is insufficient if its dependencies are deleted first.

**Why it matters:** a literal phase-1 implementation breaks the surviving transport before its replacement can land. The phase cannot meet its own green-suite and import gates.

**Requested resolution:** move the client conversion and its error adapters into the removal merge, or retain the complete required dependency subset until the conversion merge. Make the deletion list and phase gates agree.

**Evidence: rung 2, pointed at code and conflicting requirements.** The phase-1 deletion was not applied or run; the predicted runtime failure is not reproduced.

### 2. High: handler-name reachability does not preserve screencast capture

**Lands in:** Abstract, Proposed Changes 5 and 6, Risk Assessment, and Testing Strategy (`RFC:26`, `RFC:300`, `RFC:310`, `RFC:461`, `RFC:498`).

The draft identifies Camoufox automation as the one capability that cannot survive removal without new design. Screencast capture also depends on the removed persistent runtime. The new `tool` verb dispatches a single handler call, and section 5 still constructs the runtime in the one-shot curated context. No surviving owner is specified for capture state between start and stop.

The limitation is explicit in `src/browser_tools/curated.py:346`: independent screencast start and stop processes cannot share frames, and capture is meaningful only against the persistent MCP front. Each context creates and then stops its handler (`src/browser_tools/curated.py:129`, `src/browser_tools/curated.py:148`). Each runtime owns a fresh recorder (`src/browser_tools/cdp_handler.py:320`), and a fresh recorder rejects stop because no capture is active (`src/browser_tools/screencast.py:165`). Routing these names through another verb does not change that lifetime.

**Why it matters:** after removing the persistent front, the package has no specified working screencast capture workflow. Session-filter and acknowledgement tests do not establish that separate CLI calls can produce an artifact. This limitation already exists in the CLI; the new loss is removal of the working persistent path without identifying it alongside Camoufox.

**Requested resolution:** specify a CLI capture lifetime that can complete and write frames, with an end-to-end artifact gate, or explicitly record screencast capture as another capability requiring a user decision. Do not treat the existence of dispatch entries as evidence of preservation.

**Evidence: rung 2, pointed at code and its documented lifetime limitation.** No live capture was run; the post-removal behavior is not runtime-verified.

### 3. High: the migration moves profiles without accounting for live holders

**Lands in:** Migration Strategy step 1, Risk Assessment, and Testing Strategy (`RFC:431`, `RFC:473`, `RFC:483`).

The first named launch must move every valid old-root directory whose destination is absent. The rule does not exclude directories held by running browsers, including profiles unrelated to the requested launch. It also does not specify coordination with concurrent launches or how registry paths remain valid during the move.

The existing launch contract refuses a live profile holder and tells the caller to stop it first (`src/browser_tools/lifecycle.py:385`, `src/browser_tools/lifecycle.py:407`). Registry entries retain the full `user_data_dir` (`src/browser_tools/lifecycle.py:109`, `src/browser_tools/lifecycle.py:170`), and Camoufox liveness checks that recorded path against the runner's command line (`src/browser_tools/lifecycle.py:190`). Moving the directory does not change either stored path. The normal holder check for the requested profile does not protect all the other directories this migration moves.

**Why it matters:** the specified migration permits relocation of storage still used by a running browser while its process and registry refer to the old location. Safe browser behavior and lifecycle handling during that state are unspecified. Data loss is a risk, not a demonstrated outcome.

**Requested resolution:** define an idle-only migration rule, report and defer live or uncertain holders, and define how migration coordinates with launches. Include a test that a live profile stays at its old path when another named profile triggers migration, plus a later retry after the holder exits.

**Evidence: rung 2, pointed at the migration requirement and lifecycle code.** No user's profile was moved, and effects on a live browser were not reproduced.

### 4. Medium: `.ephemeral` is both a valid Profile Name and an internal container

**Lands in:** Terminology, Proposed Changes 2, and Migration Strategy step 1 (`RFC:138`, `RFC:232`, `RFC:431`).

The grammar excludes only `.` and `..`. It accepts `.ephemeral`, which the same section reserves for unnamed Camoufox session directories. Current code creates those sessions below `profiles_root() / ".ephemeral"` (`src/browser_tools/lifecycle.py:414`), while a named profile resolves directly below the same root (`src/browser_tools/lifecycle.py:94`).

Consequently `launch --profile .ephemeral` identifies the internal container as a Named Profile. The migration's rule to move every grammar-valid directory also treats the old `.ephemeral` container as a profile, potentially moving temporary sessions and their live holders. A name-grammar test alone cannot distinguish the two roles.

**Why it matters:** the proposed single owner still assigns one path two different lifecycles: persistent profile storage and a container for disposable sessions.

**Requested resolution:** reserve internal names in both validation and migration, or place ephemeral storage outside the Profile Name namespace. Specify treatment of an existing `.ephemeral` container and test that it is not migrated as a Named Profile.

**Evidence: rung 2, pointed at the grammar and path construction.** No conflicting profile was launched; the filesystem effects are not runtime-verified.

### 5. Medium: the import cleanup also removes optional-dependency handling

**Lands in:** Proposed Changes 4 (`RFC:279`).

The draft requires removing the `except ImportError` fallbacks in `camoufox_session` and `camoufox_runner` alongside script-mode import fallbacks. In these two files the handlers guard the optional third-party Camoufox dependency, not an alternate package import path.

`src/browser_tools/camoufox_session.py:19` catches an unavailable `camoufox.sync_api`. `src/browser_tools/camoufox_runner.py:56` catches the same import failure, prints the missing-extra installation message, and returns 3. Camoufox remains optional in `pyproject.toml` under `[project.optional-dependencies]`. Removing these handlers literally changes the missing-extra behavior; section 4 supplies no replacement.

**Why it matters:** a cleanup intended to remove script-mode compatibility also removes a deliberate optional-install boundary and its actionable failure message.

**Requested resolution:** restrict the deletion requirement to alternate local import paths. Keep the optional-dependency guard, or specify its replacement and verify the Camoufox command without the extra installed.

**Evidence: rung 2, pointed at imports and packaging configuration.** No installation without the extra was created; the changed error path is not reproduced.

## Cleared

- All sections of version 2 were read, including migration, risks, tests, phases, and open questions. The deterministic structural check passes as reported above.
- The shared target-resolution proposal matches the existing sequence: browser-level connection, page filtering and sorting, target resolution, and flattened attachment (`src/browser_tools/one_shot.py:74`). The core client already supports session-scoped send and event filtering (`src/browser_tools/core/cdp_client.py:82`, `src/browser_tools/core/cdp_client.py:97`). This is code-level support, not a live transport validation.
- The launcher and supervisor exceptions agree with the adapted-module allowance. The draft explicitly preserves the vendoring rules and the optional domains layer; RFC-01's Vendoring rules also retains that layer.
- Camoufox launch-only behavior is disclosed in Scope and Open Question 1. This review does not mistake retained `camoufox_session.py` source for a shipped CLI driver.
- Cookie masking has a stated default, explicit opt-in, and named tests. No contradictory default was found in this draft.
- Searched the full draft for live-holder migration handling and a cross-invocation capture lifetime. Neither is specified; the relevant occurrences describe existing processes, runtime construction, or acknowledgement tests. These negative findings are scoped to this draft, not to external plans.

## Not reviewed and evidence limits

- No implementation changes, browser launches, live profile migration, package build, or full test suite run. This is a review of the proposed contract, not a bug-fix task or implementation acceptance test. Findings stop at rung 2; none claims a reproduced production failure.
- No external harness inventory or verification of the author's historical decisions. The CLI-only decision is taken from the draft. Whether to retain Camoufox automation or add a capture workflow remains the author's scope decision.
- No fresh security audit, official protocol conformance review, or exhaustive validation of the audit's counts. RFC-01 was consulted for relevant contracts, not reviewed in full again. External links were not needed for the findings, which concern local code and internal specification consistency.
- Codebase-memory was used for symbol discovery, a bidirectional trace of `_cdp_handler_session`, and exact screencast snippets. Coverage checks for every relied-on source/document path reported `no_recorded_issue` and `metadata_match`, generation `2026-09-05T14:23:01Z`. This is a best-effort signal, not proof of complete call resolution. Material claims were checked against the source directly; graph edges alone were not treated as execution evidence.
