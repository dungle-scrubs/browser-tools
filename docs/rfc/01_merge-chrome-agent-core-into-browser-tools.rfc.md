---
number: 01
title: "Merge chrome-agent core into browser-tools"
type: refactor
status: Accepted
author: Kevin Frilot
date: 2026-08-21
version: 3
---

# RFC-01: Merge chrome-agent core into browser-tools

## Abstract

browser-tools and chrome-agent solve overlapping problems with competing lifecycle layers. This RFC specifies a directional merge: chrome-agent's core (instance registry, liveness model, raw CDP passthrough, event attach) becomes the foundation, and browser-tools' curated toolset, profiles, interstitial detection, Camoufox routing, and profiling are rebuilt on top of it as ordinary CDP consumers. The Node dependency on chrome-devtools-mcp is removed, the always-on MCP daemon is demoted to an optional front, and the tool-proxy integration is retired in favor of a CLI-first surface taught by an agent skill. Every design decision in this RFC was settled in the reviewed and approved merge plan; this document renders those decisions as a normative specification. Version 2 answered review `draft-2026-08-21`; version 3 records the resolution of all five open questions (decided by the author, 2026-08-21) and moves the RFC to Accepted.

## Introduction

### Problem statement

browser-tools carries three structural liabilities:

1. **A Node subprocess in the critical path.** Snapshot and UID interaction (`click --uid`, `fill`) run through chrome-devtools-mcp, a Node process the Python daemon spawns and supervises. This forces the always-on daemon (startup amortization), a second language runtime, and a protocol gate between the tool and the browser.
2. **A weaker lifecycle layer than chrome-agent's.** browser-tools' process tracking predates chrome-agent's liveness model (process identity plus port attribution, PID-namespace aware, suspend-tolerant). Both projects maintain a registry, a launcher, and a liveness check; chrome-agent's is the better implementation.
3. **A gated protocol surface.** browser-tools validates tool calls against a bundled registry. Chrome capabilities outside that registry are unreachable until the registry grows. chrome-agent's passthrough sends any `Domain.method` straight to the running browser and reads the schema live from it.

### Scope

**In scope:** vendoring chrome-agent's core modules into browser-tools; the merged CLI verb surface; the instance and profile model, including the registry schema and its extension; the MCP compatibility contract during and after the transition; the native snapshot rebuild and its parity gate; the `wait` verb design; window marking; profiling's place in the merged architecture; the packaging split; retirement of the tool-proxy `browser-tools` app.

**Out of scope:** any change to chrome-agent's upstream repository; new browser capabilities not present in either project today; captcha solving or any inference capability (neither project contains inference, and the merged tool MUST NOT add any); Windows support.

### Motivation

The merge direction and every conflict resolution were reviewed and approved (Lucid review of `.lucid/merge-plan.html`, 2026-08-21, all seven decision points agreed). The remaining risk is drift between the approved intent and the implementation. This RFC pins the contracts before Phase 0 vendors roughly 3,300 lines of core.

## Terminology

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted as described in RFC 2119.

- **Core**: the vendored chrome-agent modules (registry, instance status, launcher, supervisor, attach, session, connection, protocol dispatch, errors, fingerprint).
- **Verbatim modules**: the subset of the core vendored without modification: `registry.py`, `instance_status.py`, `attach.py`, `session.py`, `connection.py`. Adaptation happens at their call sites, never inside them (Phase 1 extends the registry schema; that extension is specified in this document, not improvised).
- **Adapted modules**: core modules the merge modifies with attribution kept: `launcher.py` (new parameters for profile, channel, engine), `cli.py` (replaced by the merged CLI front), `supervisor.py` (window marking configuration).
- **Instance**: one running browser process tracked by the registry, identified by name.
- **Liveness**: the determination that an instance's process is alive. Engine-aware: for Chrome, process identity plus CDP port attribution; for Camoufox, process identity plus user-data-dir hold. Never PID existence alone.
- **Passthrough**: sending a caller-supplied `Domain.method` and JSON parameters to the browser over CDP without validation against a bundled schema.
- **Curated tool**: a high-level verb (snapshot, click, wait-stable) implemented as a CDP consumer over the same client the passthrough uses.
- **Attach**: a persistent connection that streams subscribed CDP events as JSON lines, with per-session isolated subscriptions.
- **Profile**: a named, persistent browser identity (user data dir plus recorded launch attributes) bound at launch. Exclusive: one live instance per profile.
- **Fingerprint profile**: a set of launch flags that alter what pages observe about the browser. Flags only; no JavaScript injection. Distinct from a Profile; the bare word "profile" in this document always means the identity, never the flags.
- **MCP surface**: every tool a connected MCP client can call today: the tools listed or default-forwarded in `tool_registry.py`, plus the lifecycle and session tools routed in the session layer (`use_browser_session`, `attach_browser`, `close_browser`, `launch_camoufox` and peers; see `automation_backend.py:16-18` for the routing split).
- **MCP front**: an optional MCP server exposing the MCP surface over the merged dispatch.
- **Engine**: the browser implementation behind `launch`: Chrome/Chromium (default) or Camoufox (Firefox).
- **Window marking**: the visual border and badge the supervisor injects into windows it launched, so a human can tell agent-controlled windows from their own (chrome-agent `supervisor.py` overlay; opt out with `--no-window-border`).
- **Parity suite**: the test suite that runs the same pages through the native snapshot engine and the legacy Node engine and compares results under the operator defined in Testing Strategy.

## Current State

### browser-tools (this repository)

About 9,800 lines of Python across 32 modules (36 source files including JS and template assets) under `src/browser_tools/`. Key facts:

- Entry points `browser-tools` and `browser-tools-profiler`; Python >= 3.13.
- Runtime dependencies: `camoufox[geoip]`, `pillow`, `websockets>=14.0`, and an `aiohttp>=3.14.1` CVE override forced by camoufox.
- An always-on MCP daemon (`mcp_daemon.py`, `mcp_broker.py`, `daemon_supervisor.py`) exposes the MCP surface: ~35 explicit tools in `tool_registry.py` plus default-forwarded chrome-devtools-mcp tools, with lifecycle/session tools routed separately in the session layer.
- Snapshot and UID interaction run through chrome-devtools-mcp (Node) via `persistent-session-template.mjs`.
- `stealth.js` injects JavaScript anti-fingerprint patches (`cdp_handler.py` via `Page.addScriptToEvaluateOnNewDocument`).
- Lifecycle and process tracking live in `persistent_browser.py`, `process_utils.py`, `session_store.py`, `session_reaper.py`. `process_utils.py` owns `pid_holds_user_data_dir` and stale-singleton-lock cleanup.

The approved merge plan's module disposition table predates recent refactors on `main` (session-adapter dispatch, `automation_backend.py`, `live_chrome.py`, module splits). Dispositions in this RFC name the current modules; where the plan named an older module, the disposition follows the code that absorbed it.

### chrome-agent (`~/dev/chrome-agent`, v0.5.7)

- Single runtime dependency: `websockets>=16.0`; Python >= 3.11; entry point `chrome-agent`.
- Core modules (~3,300 lines): `registry.py`, `instance_status.py`, `launcher.py`, `supervisor.py`, `attach.py`, `session.py`, `connection.py`, `cdp_client.py`, `protocol.py`, `fingerprint.py`, `errors.py`, `utils.py`, `cli.py`.
- `domains/`: 54 typed domain modules (plus package init) over `send()`; never a gate.
- CLI verbs: `launch`, `status`, `attach`, `help`, `cleanup`, `stop`, `guide`, plus bare `INSTANCE Domain.method` passthrough. `launch` takes `[--port PORT] [--fingerprint PATH] [--headless] [--no-window-border] [-- CHROME_ARGS]`. `attach` and passthrough take `--target SPEC` and `--url SUBSTRING` for target selection. All error paths exit 1.
- Upstream's registry file is `/tmp/chrome-agent/registry.json` (the merged tool keeps `/tmp` semantics; Question 5, resolved); an entry stores `port`, `pid`, `browser_version`, `user_data_dir`, `launched`, `pid_start` (`registry.py:262-268`). Liveness comes from process identity plus port attribution. Corruption and retirement are distinct states: an unparseable registry reads as `unknown`, a parseable file without the instance reads as `retired`, and attach survives torn reads on that distinction.
- Instance names derive from the working directory (lowercase, hyphenate, strip to `[a-z0-9.-]`, fallback `chrome`), with `-NN` suffixes on collision, and the CLI disambiguates an instance name from a `Domain.method` token by registry lookup.
- One-shot CDP round trip measured by upstream at 50-80 ms (upstream's number, not re-measured). Attach mode streams isolated event subscriptions as JSON lines.
- No AI or ML inference of any kind. Anti-detection is launch-flag spoofing only.

## Proposed Changes

### Architecture

Five layers. Each upper layer consumes only the layer below it.

| Layer | Name | Contents |
|---|---|---|
| 4 | Fronts | CLI verbs (primary), agent skill, optional MCP front, `guide` |
| 3 | Policy | Named profiles, project config, fingerprint profiles, engine routing |
| 2 | Native toolset | Curated tools as plain CDP consumers; profiling; window marking config |
| 1 | Core (vendored) | Registry, liveness, launcher, supervisor, session, attach, passthrough, live-schema help |
| 0 | Browser | Chrome/Chromium with CDP; Camoufox for anti-detect paths |

The property the design depends on: layer 2 tools MUST call the same CDP client `send` path the passthrough uses. Any method the installed Chrome supports MUST work through the passthrough without a tool existing for it.

### Vendoring rules

- chrome-agent's core modules are copied into `src/browser_tools/core/` (Question 1, resolved).
- Vendored files MUST retain chrome-agent's copyright and MIT license notice. A `NOTICE` entry or file header block satisfies this.
- The vendored core is a hard fork. browser-tools MAY cherry-pick upstream fixes but MUST NOT depend on chrome-agent at runtime.
- The verbatim/adapted split in Terminology is normative. Only the adapted modules may change; a needed change to a verbatim module is a spec change to this RFC first.
- The `domains/` typed classes are vendored as an OPTIONAL convenience layer. No curated tool may require them; they MUST NOT gate the passthrough.

### CLI surface (normative)

The merged CLI front is new code in layer 4. It ships as two console scripts naming one program: `browser-tools` (canonical in documentation) and `bt` (alias; Question 2, resolved). It owns argument parsing, verb dispatch, and exit codes; the vendored `cli.py` is not shipped. It MUST provide these verbs. Every verb that operates on a browser accepts a leading `[INSTANCE]`; `INSTANCE` MAY be omitted when exactly one instance is running, and when omitted with multiple instances running the command MUST fail with the instance list rather than guess.

```
# lifecycle
launch [--headless] [--port PORT] [--profile NAME] [--channel NAME] [--fingerprint FILE] [--engine chrome|camoufox] [--no-window-border] [-- BROWSER_ARGS]
status [INSTANCE]
stop [INSTANCE] [--target SPEC]
cleanup
guide

# raw protocol
INSTANCE Domain.method '{...json params...}' [--target SPEC]   # INSTANCE omittable per the rule above
help [INSTANCE] [Domain.method]

# events
attach [INSTANCE] +Domain.event [+Domain.event ...] [--target SPEC] [--url SUBSTRING]
wait [INSTANCE] --event Domain.event [--match SUBSTRING] [--timeout SECONDS]

# curated tools (leading [INSTANCE] as above)
snapshot | click --uid N | fill --uid N --text T
wait-idle | wait-stable | detect
console-list | network-list
frames list | frames select N | frames reset | storage get --key K
screenshot | screencast start|stop
```

Requirements:

- **Flag provenance.** `--port`, `--fingerprint`, `--headless`, `--no-window-border`, and the `--` args passthrough are the vendored launch flags and survive unchanged. `--profile`, `--channel`, and `--engine` are policy-layer flags: the CLI front resolves them to launcher parameters (user data dir, binary path, engine) before calling the adapted launcher. `--target SPEC` and `--url SUBSTRING` are the vendored target-selection flags and apply to passthrough, `attach`, `wait`, and `stop`.
- **Instance names.** The merged tool adopts upstream's derivation and collision rules normatively: name derived from the working directory (lowercase, hyphenate, strip to `[a-z0-9.-]`, fallback `chrome`), `-NN` suffix on collision, and a bare token is resolved as an instance name if the registry knows it, else as a `Domain.method`.
- `help` with a running instance MUST read the protocol schema from that browser, not from a bundled copy. Without a running instance it MUST print static usage. `guide` prints the bundled agent manual.
- **`wait` design.** `wait` is new code in the core layer (upstream ships the pattern as `scripts/cdp-wait.py`, not a verb). It MUST open an attach session, subscribe to the requested event, and only then begin examining events, so an event that fires between subscription and examination is buffered, not lost. `--match` is a substring test against the event's JSON serialization. `--timeout` defaults to 30 seconds; `--timeout 0` means no deadline. On match: the event JSON on stdout, exit 0. On deadline: a timeout error on stderr, exit 1, no partial output on stdout.
- `attach` subscriptions MUST be isolated per session: two attached observers MUST NOT see each other's subscription set, and a retiring observer MUST NOT disturb the other's stream.
- `console-list` and `network-list` are REQUIRED verbs, implemented as thin wrappers over a short attach session. Their output SHOULD remain compatible with today's output; incompatibilities MUST be listed in the changelog.
- `detect` runs interstitial detection against the current page and reports what it found (the detection browser-tools runs automatically post-navigation and exposes as `inspect_blocked`/`inspect_warn`, surfaced as an explicit verb).
- **CLI-to-MCP mapping.** The CLI names and the frozen MCP names are two spellings of one surface. The mapping is normative; the MCP names never change while the freeze holds:

| CLI verb | Frozen MCP tool |
|---|---|
| `frames list` | `list_frames` |
| `frames select N` | `select_frame` |
| `frames reset` | `reset_frame` |
| `storage get` | `get_frame_storage` |
| `wait-idle` / `wait-stable` | `wait_idle` / `wait_stable` |
| `screenshot` | `take_screenshot` |
| `detect` | `inspect_blocked` / `inspect_warn` |

- **Exit codes.** 0 success; 1 operational failure (browser error, CDP error, timeout); 2 usage error. The merged CLI front implements this; the vendored modules' internal `sys.exit(1)` paths are unreachable from it because the front calls core functions, not the vendored `main()`. Machine-readable output MUST go to stdout as JSON; diagnostics to stderr.

### Instance and profile model

- Instances and profiles are orthogonal. An instance is a running process; a profile is a persistent identity. `launch --profile NAME` binds a profile to a new instance.
- **Registry schema.** The merged registry entry extends the vendored schema. Vendored fields: `port`, `pid`, `browser_version`, `user_data_dir`, `launched`, `pid_start`. Added by Phase 1: `engine` (string, `"chrome"` or `"camoufox"`) and `profile` (string or null). An entry missing the added fields MUST be read as `engine="chrome"`, `profile=null`, so a registry written by the vendored code stays readable. The Phase 1 gate covers the extended schema.
- **Liveness is engine-aware.** Chrome instances: process identity plus CDP port attribution (the vendored check, unchanged). Camoufox instances expose no Chrome debugging port; their liveness is process identity plus user-data-dir hold (`pid_holds_user_data_dir`, which the merged tool keeps for this purpose). PID reuse after reboot or namespace changes MUST NOT produce a false "alive" for either engine.
- **Profile exclusivity.** A profile MUST be held by at most one live instance. `launch --profile NAME` while a live instance holds that profile MUST fail, exit 1, naming the holding instance; it MUST NOT launch a second browser on the same user data dir and MUST NOT steal the profile. Stale singleton locks left by a dead process are cleaned before this check (the existing `clean_stale_singleton_lock` behavior).
- **Registry corruption is not retirement.** The merged tool adopts upstream's distinction normatively: an unparseable or unreadable registry file reads as status `unknown`; a parseable file that lacks the instance reads as `retired`. On `unknown`, `status` MUST report it as such, `stop` MUST refuse to signal anything, and `cleanup` MUST NOT delete registry entries or session directories - it MAY move the corrupt file aside with a warning. `cleanup` removes an entry only after the entry itself was read and its liveness check failed.
- `cleanup` MUST NOT touch live instances.
- Project config (`.browser-tools.json`) supplies launch defaults only. It MUST NOT be able to redirect an existing instance.

### Anti-detection

- `stealth.js` and its injection path are deleted. The merged tool MUST NOT inject JavaScript for fingerprint purposes. Rationale (from chrome-agent's audit, accepted in review): each JS override is independently detectable.
- Chrome paths MAY use fingerprint profiles: launch flags only.
- Camoufox remains the answer when the engine itself must change, behind `launch --engine camoufox`, and requires the `camoufox` extra.

### Window marking

Windows launched by the tool carry the supervisor's visual border and badge so a human can tell agent-controlled windows from their own. Marking is on by default and disabled per launch with `--no-window-border`. The merged tool vendors this with the supervisor (adapted module); Phase 4's deliverable is wiring the flag through the merged CLI front and documenting it in the skill.

### Profiling

The CPU profiler keeps its own long-lived process and its `browser-tools-profiler` entry point. It is rebuilt to speak the core client for target discovery and CDP transport, and MUST NOT depend on the MCP front or the daemon. Its dependencies move behind the `profiling` extra; invoking it without the extra fails with the exact install line. A smoke test (start, profile a page, stop, artifact exists) joins the suite in Phase 3.

### MCP compatibility contract

- The MCP daemon becomes an OPTIONAL front. The core MUST NOT require a daemon for any CLI verb.
- **The frozen surface is the MCP surface as defined in Terminology**: the `tool_registry.py` tools (explicit and default-forwarded) and the session-layer lifecycle tools (`use_browser_session`, `attach_browser`, `close_browser`, `launch_camoufox` and peers). While the MCP front is running, every tool on that surface MUST keep its current name, argument shape, and response shape through the end of Phase 3. Breaking changes, if any, land in Phase 4 and MUST be listed in the changelog.
- MCP contract tests (schema-level, covering names, argument shapes, and response shapes for the whole frozen surface) MUST exist from Phase 1 and run in every phase gate through Phase 3. This is what holds the freeze and the Phase 2 rebuild together: the native snapshot backend MUST pass the same contract tests the Node-backed tools pass today.
- Screencast and the profiler own their long-lived processes; they MUST NOT depend on the MCP front.

### Native snapshot

- Snapshot and UID interaction are rebuilt on the CDP Accessibility domain. The existing `ax_find`/`ax_node` implementation is the starting point.
- UIDs MUST be stable for the lifetime of a snapshot: a UID returned by `snapshot` MUST resolve to the same node for subsequent `click --uid`/`fill --uid` calls until the next snapshot or navigation.
- `--engine mcp` remains as a transitional flag routing snapshot and UID interaction through the Node engine. The flag and the Node path are removed only after the parity gate passes (see Testing Strategy).

### Packaging

- The default install (`pip install browser-tools`) MUST depend on `websockets` only, with the floor at `>=16.0` (the higher of the two projects' floors; the vendored core requires it).
- `camoufox[geoip]` and the `aiohttp` CVE pin move behind the `camoufox` extra; `pillow` and profiling dependencies behind the `profiling` extra; `all` bundles every extra (Question 4, resolved). The CVE pin disappears from the default install.
- Commands that need an absent extra MUST fail with the exact `pip install` line naming the extra.
- Python requirement: >= 3.13 (browser-tools' current floor; chrome-agent's >= 3.11 is compatible).

### Out of tool-proxy

- The CLI is the canonical agent surface. An agent skill ships from this repository (deployed to `~/.agents/skills`) and documents the final verb set.
- The tool-proxy `browser-tools` app is retired in Phase 4, together with the global "route browser automation through tool-proxy" instruction. The optional MCP front remains for harnesses that cannot run a CLI.

## Migration Strategy

Five phases. Each phase MUST leave `main` releasable, and a go/no-go gate closes each phase.

**Phase 0 - Vendor the core (size S).** Copy the core modules with license headers into `src/browser_tools/core/`. No user-visible change. Gate: the existing test suite passes unmodified.

**Phase 1 - Lifecycle cutover (size S).** `launch`, `status`, `stop`, and `cleanup` speak the vendored registry with the extended schema (`engine`, `profile`). Named profiles and project config become launch attributes; profile exclusivity enforced. MCP contract tests written and green. Gate: lifecycle e2e tests pass against the extended registry, including a Camoufox launch registered and reported live; the old lifecycle code paths are deleted, not left dormant.

**Phase 2 - Native snapshot (size L).** UID tools on the Accessibility domain, `--engine mcp` transitional flag, parity suite built and running. Gate: the parity gate defined in Testing Strategy, plus the MCP contract tests still green against the native backend.

**Phase 3 - Daemon demotion and events (size M).** MCP front optional; `attach`, `wait`, `console-list`, `network-list` land; `stealth.js` removed; fingerprint profiles arrive; profiler rebuilt over the core client; Node engine behind the flag only. Gate: every CLI verb works with no daemon running; MCP contract tests green.

**Phase 4 - Packaging and surface retirement (size S).** Extras split; Node dependency deleted; agent skill authored against the final verb set; tool-proxy app retired; bundled agent guide and window marking wired through the CLI front. Gate: clean-machine install of the default package runs the lifecycle and passthrough verbs; the skill's documented verbs match `--help` output exactly.

**Rollback.** Phases 0-1 roll back by reverting the phase's merge commit. Phase 2-3 rollback re-enables the Node engine path (kept intact until the Phase 2 gate passes). Phase 4 is not rolled back; it is only entered after Phases 0-3 are stable.

## Risk Assessment

- **Parity gap in native snapshot (highest risk).** The Node engine's snapshot semantics may encode behavior the Accessibility domain does not directly expose. Mitigation: the parity suite is built before the Node path is touched, and the transitional flag keeps both engines available until the gate passes.
- **Liveness regressions on macOS suspend/resume.** The vendored liveness model is the merge's main prize; a porting mistake here corrupts every verb. Mitigation: the verbatim-module rule - `registry.py`, `instance_status.py`, `attach.py`, `session.py`, `connection.py` are vendored unmodified and adapted only at call sites. The launcher, CLI, and supervisor are adapted modules with the changes specified in this document.
- **Registry divergence during Phase 1.** Old session store and new registry coexisting can double-track one browser. Mitigation: Phase 1 deletes the old paths in the same change that cuts over; no dual-tracking period.
- **MCP front drift.** Demoting the daemon while promising tool-shape stability invites silent shape changes. Mitigation: the MCP contract tests in Testing Strategy, written in Phase 1 and run in every gate through Phase 3.
- **Blast radius.** Worst case is loss of browser automation on the machines using this tool; no production systems or external users depend on it today (Alpha status, single maintainer).

## Testing Strategy

- **Existing suite as the floor.** The current test suite MUST pass at every phase gate. Phase 0 runs it unmodified.
- **MCP contract tests (Phase 1 onward).** Schema-level tests covering the whole frozen MCP surface: names, argument shapes, response shapes. Run in every gate through Phase 3. The Phase 2 native backend MUST pass them unchanged.
- **Parity gate (Phase 2).** The parity suite runs an agreed page corpus (Question 3: the page list is settled by a survey of the current e2e fixtures before Phase 2 implementation starts) through both engines. The comparison operator: snapshot node sets compared order-insensitively on (role, name, value) tuples; UID resolution compared by the backend node a click or fill resolves to; text extraction compared exactly. The corpus MUST include iframe and shadow DOM cases and is frozen before Phase 2 implementation starts. The gate passes when results match on the full corpus for two consecutive runs, flake-free. Only then is the Node path removable.
- **Liveness tests.** Registry tests MUST cover: PID reuse, port reuse by an unrelated process, machine suspend/resume, stale registry entries after a crash, a corrupt registry file (read as `unknown`, nothing deleted, nothing signaled), and a Camoufox instance's liveness via user-data-dir hold.
- **Profile exclusivity test.** A second `launch --profile NAME` against a live holder MUST fail naming the holder; after the holder dies, the same launch MUST succeed following stale-lock cleanup.
- **Event tests.** `wait` MUST have a test proving it catches an event fired between attach and wait, and a test for deadline behavior (exit 1, stderr diagnostic, empty stdout). Attach isolation MUST have a two-observer test.
- **Profiler smoke test (Phase 3).** Start, profile a page, stop, artifact exists, no daemon running.
- **Install matrix (Phase 4).** Clean-environment installs of the default package and each extra, verifying import, verb availability, and the failure message for missing extras.

## Security Considerations

- **Trust boundaries.** The CLI trusts its caller completely: passthrough grants arbitrary control of the browser, including cookies, storage, and any authenticated session in the profile. This is the tool's purpose, not a defect, and it matches both projects today. The merged tool MUST bind CDP ports to loopback only.
- **Registry file.** The registry lives in a world-readable temp directory and records PIDs, ports, and profile paths. It MUST NOT record credentials, cookies, or page content. Registry contents MUST be treated as untrusted input, with the corruption semantics pinned in the Instance and profile model: a corrupt file reads as `unknown`, and on `unknown` nothing is signaled and nothing is deleted. A registry entry MUST NOT cause the tool to signal or kill a process it cannot first verify ownership of via the liveness check.
- **Injected content.** With `stealth.js` deleted, the tool injects no JavaScript for fingerprint purposes. The window-marking overlay is injected into windows the tool itself launched, is visible by design, and carries no page data out. Page-side JS execution remains available to callers through explicit tools and passthrough (`Runtime.evaluate`); that is caller-directed, not tool-initiated.
- **MCP front.** The optional MCP server MUST listen only on loopback or a unix socket with user-only permissions, as the daemon does today.
- **Anti-detection scope.** Fingerprint profiles and Camoufox exist for authorized automation of sites that block all automation indiscriminately. The tool contains no captcha solving and no inference, and this RFC forbids adding either. Nothing in the merge changes what a caller could already do with Chrome and a CDP client.
- **Vendored code.** Phase 0 vendors third-party code wholesale. The vendored modules MUST be read in review before merge, not trusted by reputation.

## Implementation Plan

Phases 0-4 as specified in Migration Strategy, in order, each behind its gate. Phases 0 and 1 SHOULD land as separate PRs. Phase 2 is the bulk of the work and SHOULD be broken into tracer-bullet tickets (snapshot read path, UID resolution, click, fill, parity suite) before implementation starts. Ticket slicing is a separate action after this RFC is accepted.

## Open Questions

None open. All five questions raised in versions 1-2 were decided by the author on 2026-08-21 (via the Lucid review of this document), each taking the recorded recommendation:

1. **Vendored namespace: resolved (a).** The core lives in `src/browser_tools/core/` with imports rewritten. An import rewrite is the one mechanical modification allowed inside verbatim modules; nothing else is.
2. **CLI binary name: resolved (a).** Two console scripts, `browser-tools` (canonical in docs) and `bt` (alias).
3. **Parity corpus: resolved as a process.** The page list is settled by a survey of the current e2e fixtures before Phase 2 implementation starts; the comparison operator and the iframe/shadow-DOM coverage requirement were already normative in Testing Strategy. The survey is Phase 2's first task, not a blocker on this RFC.
4. **Extras layout: resolved (a).** `camoufox` and `profiling` as separate extras, plus `all`.
5. **Registry location: resolved (a).** Keep `/tmp` semantics; a cleared registry after reboot is self-consistent because no browser survives reboot. Persistent state belongs to profiles, not the registry.

## Changes in this revision

**Version 3** (2026-08-21): the five open questions were answered by the author through the Lucid review of this document, all taking the recorded recommendations; the Open Questions section now records the resolutions, the body cross-references were updated in place (namespace, binary names, extras, registry location, parity corpus), and the status moved Draft -> Accepted.

**Version 2** answered review `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.review-draft-2026-08-21.md`. One line per finding:

1. Frozen surface redefined as the full MCP surface including session-layer lifecycle tools (Terminology; MCP compatibility contract).
2. The merged CLI front owns parsing and exit codes; vendored `cli.py` is not shipped; verbatim/adapted module split added (Terminology; CLI surface; Risk Assessment).
3. MCP contract tests specified, written in Phase 1, run through Phase 3, and required to pass against the native backend (MCP compatibility contract; Testing Strategy; Phase 2 gate).
4. `console-list` and `network-list` added to the normative verb list as REQUIRED.
5. Window marking defined in Terminology and specified in its own subsection.
6. Profile exclusivity specified: second launch fails naming the holder; stale-lock cleanup precedes the check; test added.
7. `version: 2` added to the frontmatter.
8. `detect` defined and mapped to `inspect_blocked`/`inspect_warn`.
9. Curated verbs carry the leading `[INSTANCE]` grammar explicitly.
10. `guide`, `--target`, `--url` added to the verb table with flag provenance stated.
11. Current State now marks `/tmp` as upstream's location and defers the merged location to Open Question 5.
12. Registry schema specified: vendored six fields plus `engine` and `profile`, with defaulted reads for old entries (finding 12).
13. `wait` mechanism specified: attach-first subscription, buffering, 30 s default deadline, timeout exit behavior (finding 13).
14. Launch flag drift resolved in both directions: upstream flags kept, policy flags resolved by the front, launcher listed as an adapted module (finding 14).
15. Normative CLI-to-MCP mapping table added; `frames reset` included (finding 15).
16. Upstream name derivation, collision suffixing, and instance-vs-method disambiguation adopted normatively (finding 16).
17. Corruption-vs-retirement semantics pinned; `cleanup` on a corrupt file quarantines, never deletes or signals (finding 17).
18. Liveness made engine-aware; Camoufox liveness via user-data-dir hold; Phase 1 gate includes a live Camoufox instance (finding 18).
19. websockets floor set to >=16.0; parity comparison operator defined; profiling given a delivering section and a smoke test (finding 19).

## References

**Normative**

- `.lucid/merge-plan.html` (version 5, approved 2026-08-21) - the reviewed merge plan whose decisions this RFC renders; the review record lives in `.lucid/merge-plan/`.
- `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.review-draft-2026-08-21.md` - the review this version answers.
- [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) - keyword semantics.
- `~/dev/chrome-agent` at v0.5.7 - the source of the vendored core.

**Informative**

- [chrome-agent repository](https://github.com/captivus/chrome-agent) - upstream home of the vendored core.
- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) - the protocol both layers speak.
- `src/browser_tools/tool_registry.py` and `src/browser_tools/automation_backend.py` - the two halves of the frozen MCP surface.
- `~/dev/chrome-agent/scripts/cdp-wait.py` - the pattern the `wait` verb is built from.
