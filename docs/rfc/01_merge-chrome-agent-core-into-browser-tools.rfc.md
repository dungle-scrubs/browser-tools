---
number: 01
title: "Merge chrome-agent core into browser-tools"
type: refactor
status: Accepted
author: Kevin Frilot
date: 2026-08-21
version: 6
---

# RFC-01: Merge chrome-agent core into browser-tools

## Abstract

browser-tools and chrome-agent solve overlapping problems with competing lifecycle layers. This RFC specifies a directional merge: chrome-agent's core (instance registry, liveness model, raw CDP passthrough, event attach) becomes the foundation, and browser-tools' curated toolset, profiles, interstitial detection, Camoufox routing, and profiling are rebuilt on top of it as ordinary CDP consumers. The Node dependency on chrome-devtools-mcp is removed, the MCP front and the persistent-session stack behind it are deleted, and the CLI becomes the only surface, taught by the bundled `bt guide` manual. Named profiles move from `/tmp` to the XDG data directory, a UID becomes `<docToken>-<backendNodeId>` valid for the lifetime of the document, `--endpoint URL` drives a Chrome the tool did not launch, and `bt profile list` / `bt profile delete` manage the stored identities. Versions 1 to 5 rendered the approved merge plan and brought the spec back in line with the shipped CLI; version 6 renders the six decisions of wayfinder map [#82](https://github.com/dungle-scrubs/browser-tools/issues/82) and pins correct behavior for the four defects the map found.

## Introduction

### Problem statement

browser-tools carried three structural liabilities at merge start:

1. **A Node subprocess in the critical path.** Snapshot and UID interaction (`click --uid`, `fill`) ran through chrome-devtools-mcp, a Node process the Python daemon spawned and supervised. This forced the always-on daemon (startup amortization), a second language runtime, and a protocol gate between the tool and the browser.
2. **A weaker lifecycle layer than chrome-agent's.** browser-tools' process tracking predated chrome-agent's liveness model (process identity plus port attribution, PID-namespace aware, suspend-tolerant). Both projects maintained a registry, a launcher, and a liveness check; chrome-agent's is the better implementation.
3. **A gated protocol surface.** browser-tools validated tool calls against a bundled registry. Chrome capabilities outside that registry were unreachable until the registry grew. chrome-agent's passthrough sends any `Domain.method` straight to the running browser and reads the schema live from it.

Version 6 answers a fourth liability, found by map [#82](https://github.com/dungle-scrubs/browser-tools/issues/82) after Phases 0 to 4 landed: **an agent cannot yet trust the surface it is given.** A named profile holding a login is destroyed on every normal browser close. A UID from one snapshot silently resolves to a different node in the next invocation, so a `fill` lands in the wrong field with no error. `stop --target` accepts a specification it does not implement, and a failed tab close reports success. The MCP front that the merge demoted is still in the tree, so two surfaces document two different contracts. Each is specified below.

### Scope

**In scope:** vendoring chrome-agent's core modules into browser-tools; the merged CLI verb surface; the instance and profile model, including the registry schema, its extension, and the durable profile root; the native snapshot rebuild, the UID scheme, and its parity gate; the `wait` verb design; window marking; the focus guard; external endpoints; profile verbs; profiling's place in the merged architecture; the packaging split; retirement of the tool-proxy `browser-tools` app; retirement of the MCP front and the persistent-session stack behind it; the content contract for `bt guide`.

**Out of scope, with the reason each candidate does not fit:**

- Any change to chrome-agent's upstream repository. The vendored core is a hard fork.
- New browser capabilities present in neither project today.
- Captcha solving and any inference capability. Neither project contains inference, and the merged tool MUST NOT add any.
- Windows support.
- **1Password and any automated credential entry.** Ruled out by the driving dev on map [#82](https://github.com/dungle-scrubs/browser-tools/issues/82): logins are established by hand until a safe mechanism exists. It returns only as a fresh effort, not as a fold-in here.
- **`.browser-tools.json` project configuration.** `--profile NAME` already serves the need explicitly, and an implicit per-directory default is a thing an agent gets surprised by. Its only reader, `handle_use_browser_session`, leaves with the MCP front. Version 5 specified it as a launch-defaults source; version 6 removes it.
- **A non-loopback escape hatch for `--endpoint`.** The retired `handle_attach_browser` carried one. A CDP endpoint is unauthenticated full control of a logged-in browser, so a non-loopback endpoint is a remote takeover channel. `ssh -L 9222:127.0.0.1:9222 <host>` reaches a remote browser through loopback, so the safe pattern is the only pattern.
- **A registry entry for an externally attached Chrome.** Rejected in [#88](https://github.com/dungle-scrubs/browser-tools/issues/88) on the evidence of [#85](https://github.com/dungle-scrubs/browser-tools/issues/85): two vendored paths `rmtree` whatever `user_data_dir` an entry names, so an entry would aim that defect at the user's real Chrome profile.
- **A long-lived screencast recorder process.** Considered and declined while resolving the code-versus-spec disagreements below. Version 6 replaces the `start` / `stop` pair with one bounded capture instead.
- **Rewriting the agent skill at `~/dev/skills/browser-tools`.** Separate repository, separate effort. This RFC makes the repository the source of truth so that effort has something to point at.
- **Documentation for agents contributing to browser-tools** (`CONTEXT.md` architecture and vendoring guides). A different audience from the agents this RFC serves.

### Motivation

The merge direction and every conflict resolution were reviewed and approved (Lucid review of `.lucid/merge-plan.html`, 2026-08-21, all seven decision points agreed). This RFC pinned the contracts before Phase 0 vendored roughly 3,300 lines of core.

Version 6 has a second motivation. Map [#82](https://github.com/dungle-scrubs/browser-tools/issues/82) closed six decisions that together span the CLI surface, the vendored core, the registry, this document, the bundled manual, and roughly 11,300 lines of deletion. Two of them rewrite normative text on their own: the UID lifetime rule and the MCP front retirement. One constraint belongs to no single ticket and would be lost if the decisions were cut straight into implementation tickets: **supervisor retirement MUST preserve profile directories before the profile root moves**, or the move relocates a destructive defect into durable storage. That constraint is why the revision is written once, here, and the tickets are cut from it.

## Terminology

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted as described in RFC 2119.

- **Core**: the vendored chrome-agent modules (registry, instance status, launcher, supervisor, attach, connection, protocol dispatch, errors, fingerprint).
- **Verbatim modules**: the subset of the core vendored without modification: `registry.py`, `instance_status.py`, `attach.py`, `connection.py`. Adaptation happens at their call sites, never inside them (Phase 1 extends the registry schema; that extension is specified in this document, not improvised). An import rewrite is the one mechanical modification allowed inside them.
- **Adapted modules**: core modules the merge modifies with attribution kept: `launcher.py` (new parameters for profile, channel, engine), `cli.py` (replaced by the merged CLI front), `supervisor.py` (window marking configuration, and the lifecycle-owned retirement path specified below).
- **Instance**: one running browser process tracked by the registry, identified by name.
- **Liveness**: the determination that an instance's process is alive. Engine-aware: for Chrome, process identity plus CDP port attribution; for Camoufox, process identity plus user-data-dir hold. Never PID existence alone.
- **Passthrough**: sending a caller-supplied `Domain.method` and JSON parameters to the browser over CDP without validation against a bundled schema.
- **Curated tool**: a high-level verb (snapshot, click, wait-stable) implemented as a CDP consumer over the same client the passthrough uses.
- **Attach**: a persistent connection that streams subscribed CDP events as JSON lines, with per-session isolated subscriptions.
- **Profile**: a named, persistent browser identity (user data dir plus recorded launch attributes) bound at launch. Exclusive: one live instance per profile.
- **Profile root**: the directory holding every named profile, resolved by the precedence in Profile storage. From version 6 it is durable storage, not `/tmp`.
- **Fingerprint profile**: a set of launch flags that alter what pages observe about the browser. Flags only; no JavaScript injection. Distinct from a Profile; the bare word "profile" in this document always means the identity, never the flags.
- **Engine**: the browser implementation behind `launch`: Chrome/Chromium (default) or Camoufox (Firefox).
- **UID**: the identifier `snapshot` prints for a node and `click` and `fill` accept. From version 6 it is `<docToken>-<backendNodeId>`.
- **docToken**: the document-identity half of a UID, derived from the page's CDP `loaderId`. It changes on navigation and on nothing else.
- **External endpoint**: a CDP endpoint the tool did not launch, driven per invocation through `--endpoint URL` and never recorded in the registry.
- **Window marking**: what the supervisor injects into windows it launched so a human can tell agent-controlled windows from their own: a tab-title prefix that always runs, plus a border and badge that follow a persistent setting. Opt out per launch with `--no-window-border`. Specified in Window marking.
- **Guide**: the bundled agent manual that `bt guide` prints. From version 6 it is the complete manual for the verb surface, enforced by a test.
- **Parity suite**: the test suite that runs the same pages through the native snapshot engine and the legacy Node engine and compares results under the operator defined in Testing Strategy. From version 6 the Node engine survives only inside this suite, as a test oracle.
- **MCP surface (retired in Phase 5)**: every tool a connected MCP client could call: the tools listed or default-forwarded in `tool_registry.py`, plus the lifecycle and session tools routed in the session layer (`use_browser_session`, `attach_browser`, `close_browser`, `launch_camoufox` and peers). The term survives so the retirement record below reads.
- **MCP front (retired in Phase 5)**: the OPTIONAL MCP server that exposed the MCP surface over the merged dispatch.

## Current State

### browser-tools at merge start

About 9,800 lines of Python across 32 modules (36 source files including JS and template assets) under `src/browser_tools/`. Key facts:

- Entry points `browser-tools` and `browser-tools-profiler`; Python >= 3.13.
- Runtime dependencies: `camoufox[geoip]`, `pillow`, `websockets>=14.0`, and an `aiohttp>=3.14.1` CVE override forced by camoufox.
- An always-on MCP daemon (`mcp_daemon.py`, `mcp_broker.py`, `daemon_supervisor.py`) exposed the MCP surface: about 35 explicit tools in `tool_registry.py` plus default-forwarded chrome-devtools-mcp tools, with lifecycle and session tools routed separately in the session layer.
- Snapshot and UID interaction ran through chrome-devtools-mcp (Node) via `persistent-session-template.mjs`.
- `stealth.js` injected JavaScript anti-fingerprint patches (`cdp_handler.py` via `Page.addScriptToEvaluateOnNewDocument`).
- Lifecycle and process tracking lived in `persistent_browser.py`, `process_utils.py`, `session_store.py`, `session_reaper.py`. `process_utils.py` owns `pid_holds_user_data_dir` and stale-singleton-lock cleanup.

The approved merge plan's module disposition table predates recent refactors on `main` (session-adapter dispatch, `automation_backend.py`, `live_chrome.py`, module splits). Dispositions in this RFC name the current modules; where the plan named an older module, the disposition follows the code that absorbed it.

### chrome-agent (`~/dev/chrome-agent`, v0.5.7)

- Single runtime dependency: `websockets>=16.0`; Python >= 3.11; entry point `chrome-agent`.
- Core modules (about 3,300 lines): `registry.py`, `instance_status.py`, `launcher.py`, `supervisor.py`, `attach.py`, `session.py`, `connection.py`, `cdp_client.py`, `protocol.py`, `fingerprint.py`, `errors.py`, `utils.py`, `cli.py`.
- `domains/`: 54 typed domain modules (plus package init) over `send()`; never a gate.
- CLI verbs: `launch`, `status`, `attach`, `help`, `cleanup`, `stop`, `guide`, plus bare `INSTANCE Domain.method` passthrough. `launch` takes `[--port PORT] [--fingerprint PATH] [--headless] [--no-window-border] [-- CHROME_ARGS]`. `attach` and passthrough take `--target SPEC` and `--url SUBSTRING` for target selection. All error paths exit 1.
- Upstream's registry file is `/tmp/chrome-agent/registry.json` (the merged tool keeps `/tmp` semantics for the registry; Question 5, resolved); an entry stores `port`, `pid`, `browser_version`, `user_data_dir`, `launched`, `pid_start` (`registry.py:262-268`). Liveness comes from process identity plus port attribution. Corruption and retirement are distinct states: an unparseable registry reads as `unknown`, a parseable file without the instance reads as `retired`, and attach survives torn reads on that distinction.
- Instance names derive from the working directory (lowercase, hyphenate, strip to `[a-z0-9.-]`, fallback `chrome`), with `-NN` suffixes on collision, and the CLI disambiguates an instance name from a `Domain.method` token by registry lookup.
- One-shot CDP round trip measured by upstream at 50 to 80 ms (upstream's number, not re-measured). Attach mode streams isolated event subscriptions as JSON lines.
- No AI or ML inference of any kind. Anti-detection is launch-flag spoofing only.

### State at version 6 (`main` at `30c6815`)

Phases 0 to 3 have landed and Phase 4 has landed in part. The merged CLI front, the vendored core, the native snapshot engine, the event verbs, the focus guard, window marking, and the supervisor are all shipped. The default install is Node-free and depends on `websockets>=16.0` only, with `camoufox` and `profiling` extras. The tool-proxy `browser-tools` app is retired, and the session-adapter dispatch path is deleted (version 5). The transitional `--engine mcp` flag is gone; `--engine` now takes `chrome` or `camoufox`.

Baseline for every count in this revision: 946 passed, 4 skipped, 950 collected; ruff clean; pyright 0 errors.

What Phase 5 still finds in the tree:

- **The MCP front and the persistent-session stack behind it**: 22 source files (5,751 lines) and 20 test files (5,531 lines), including `mcp_daemon.py` (591), `persistent_browser.py` (830), `browser_session.py` (559), `daemon_supervisor.py` (405), `camoufox_session.py` (401), `session_store.py` (343) and `core/session.py` (241). An import-closure analysis with `cli` and `profiler` as roots reaches none of them.
- **The Node assets**: `persistent-session-template.mjs` and `attach_chrome.sh` still ship as package data, and `pyproject.toml:16` still carries the `mcp` keyword.

Four defects, all verified in source or live in this session:

1. **The supervisor deletes named profiles on normal browser close.** The headed launch path starts a supervisor (`core/launcher.py:255,268`). When the browser goes away, the supervisor calls the vendored `deregister()` (`core/supervisor.py:880`), which calls `_remove_session_dir(entry["user_data_dir"])` (`core/registry.py:581,587`). That helper retries `shutil.rmtree` for six seconds against whatever path the entry names, checking neither the `profile` field nor whether the path lies under a session root (`core/registry.py:548-565`). `lifecycle.stop()` is profile-aware and preserves the directory (`lifecycle.py:778,787`); the supervisor path is not. Verified live: a named profile was populated, then destroyed about two seconds after a normal `Browser.close`. `bt stop` also races the supervisor, and loses the profile when the supervisor deregisters first. No existing test covers supervisor retirement; `tests/test_profile_liveness.py:216,245` stub out process termination and exercise only the branch that works.
2. **UID drift silently fills the wrong field.** Every CLI invocation builds a new `NativeSnapshotReader` whose generation counter starts at 1 (`curated.py:136`, `cdp_handler.py:596`, `native_snapshot.py:197,225`), and `click` and `fill` each take their own snapshot before acting (`curated.py:241`, `curated.py:258-262`). A caller's UID is therefore resolved against a different tree carrying an identical `1-` prefix, and resolution checks only that the UID exists and has a backend node id, never that the node is an Element (`native_interaction.py:213,296`). Reproduced: `1-3` named the password textbox before a fill and a text node after it. The reported `Node is not an Element` error was luck; when a shifted ordinal lands on an Element, focus succeeds and the value goes into the wrong field with no error.
3. **`stop --target` ignores the `--target SPEC` contract.** `_close_tab` passes the supplied string straight to `Target.closeTarget` as a complete target ID (`lifecycle.py:737-752`), so the 1-based index and target-ID-prefix forms both fail silently and only a full target ID works. The `--target SPEC` meaning is normative and lists `stop`, so this is a spec violation, not a gap.
4. **A failed tab close reports success.** `_close_tab` returns the string `"Failed to close tab ..."` when `Target.closeTarget` returns `success: false`, and `cli.py:353-360` wraps whatever string comes back in `{"stopped": true, "message": ...}` with exit 0 (`lifecycle.py:737-752`, `cli.py:353-360`).

## Proposed Changes

### Architecture

Five layers. Each upper layer consumes only the layer below it.

| Layer | Name | Contents |
|---|---|---|
| 4 | Front | CLI verbs (the only surface), `guide`, the agent skill that points at `guide` |
| 3 | Policy | Named profiles and the profile root, fingerprint profiles, engine routing, external endpoints |
| 2 | Native toolset | Curated tools as plain CDP consumers; profiling; window marking config |
| 1 | Core (vendored) | Registry, liveness, launcher, supervisor, attach, passthrough, live-schema help |
| 0 | Browser | Chrome/Chromium with CDP; Camoufox for anti-detect paths |

The property the design depends on: layer 2 tools MUST call the same CDP client `send` path the passthrough uses. Any method the installed Chrome supports MUST work through the passthrough without a tool existing for it.

### Vendoring rules

- chrome-agent's core modules are copied into `src/browser_tools/core/` (Question 1, resolved).
- Vendored files MUST retain chrome-agent's copyright and MIT license notice. A `NOTICE` entry or file header block satisfies this.
- The vendored core is a hard fork. browser-tools MAY cherry-pick upstream fixes but MUST NOT depend on chrome-agent at runtime.
- The verbatim/adapted split in Terminology is normative. Only the adapted modules may change; a needed change to a verbatim module is a spec change to this RFC first.
- **`core/session.py` is not vendored from Phase 5 onward.** It served the MCP session layer and has no caller once that layer is deleted.
- **A defect inside a verbatim module is corrected at the call site, never in place.** `core/registry.py` is verbatim, so `deregister()` and `cleanup()` MUST NOT be made profile-aware inside it. Preservation belongs in `lifecycle.py`, and the supervisor MUST reach retirement through a lifecycle-owned operation rather than calling the vendored `deregister()` directly. `core/launcher.py` and `core/supervisor.py` are adapted and may change with a documented header note.
- The `domains/` typed classes are vendored as an OPTIONAL convenience layer. No curated tool may require them; they MUST NOT gate the passthrough.

### CLI surface (normative)

The merged CLI front is new code in layer 4. It ships as two console scripts naming one program: `browser-tools` (canonical in documentation) and `bt` (alias; Question 2, resolved). It owns argument parsing, verb dispatch, and exit codes; the vendored `cli.py` is not shipped. It MUST provide these verbs. Every verb that operates on a browser accepts a leading `[INSTANCE]`, sub-action verbs included; `INSTANCE` MAY be omitted when exactly one instance is running, and when omitted with multiple instances running the command MUST fail with the instance list rather than guess.

```
# lifecycle and registry
launch [--headless] [--port PORT] [--profile NAME] [--channel NAME] [--fingerprint FILE] [--engine chrome|camoufox] [--no-window-border] [-- BROWSER_ARGS]
status [INSTANCE]
stop [INSTANCE] [--target SPEC]
cleanup
guide
window-border [on|off]        # no argument prints the current setting
profile list
profile delete NAME

# raw protocol
INSTANCE Domain.method '{...json params...}' [--target SPEC]   # INSTANCE omittable per the rule above
help [INSTANCE] [Domain.method]

# events
attach [INSTANCE] +Domain.event [+Domain.event ...] [--target SPEC] [--url SUBSTRING]
wait [INSTANCE] --event Domain.event [--match SUBSTRING] [--timeout SECONDS] [--target SPEC] [--url SUBSTRING]

# curated tools (leading [INSTANCE] as above)
snapshot [--target SPEC]
click --uid UID [--target SPEC] | fill --uid UID --text T [--target SPEC]
wait-idle | wait-stable
detect [--wait SECONDS | --no-wait]
console-list [--target SPEC | --url SUBSTRING] [--duration SECONDS]
network-list [--target SPEC | --url SUBSTRING] [--duration SECONDS]
frames list | frames select PATTERN | frames reset | storage get --key K
screenshot [--path FILE] [--target SPEC | --url SUBSTRING]
screencast --dir DIR [--duration SECONDS] [--format FMT] [--max-frames N]

# on the browser-driving verbs only, never on launch, status, stop,
# cleanup, guide, window-border or profile
--endpoint URL
```

Requirements:

- **Flag provenance.** `--port`, `--fingerprint`, `--headless`, `--no-window-border`, and the `--` args passthrough are the vendored launch flags and survive unchanged. `--profile`, `--channel`, and `--engine` are policy-layer flags: the CLI front resolves them to launcher parameters (user data dir, binary path, engine) before calling the adapted launcher. `--target SPEC` and `--url SUBSTRING` are the vendored target-selection flags. `--target SPEC` applies to passthrough, `attach`, `wait`, `stop`, `console-list`, `network-list`, `screenshot`, `snapshot`, `click`, and `fill`; `--url SUBSTRING` to passthrough, `attach`, `wait`, `console-list`, `network-list`, and `screenshot`. The two are mutually exclusive wherever both appear, and supplying both is a usage error.
- **`--target SPEC` means one thing everywhere.** A 1-based index into the page targets sorted by target ID, or a target ID prefix. A value is read as an index only when every character is a digit; any other value is a prefix. The sort is normative, so two code paths that resolve a target MUST NOT disagree about which page `--target 1` names. **Every verb that accepts `--target SPEC` MUST resolve it through the shared target selector.** `stop --target` MUST NOT pass its argument to `Target.closeTarget` as a complete target ID (defect 3).
- **Instance names.** The merged tool adopts upstream's derivation and collision rules normatively: name derived from the working directory (lowercase, hyphenate, strip to `[a-z0-9.-]`, fallback `chrome`), `-NN` suffix on collision, and a bare token is resolved as an instance name if the registry knows it, else as a `Domain.method`.
- `help` with a running instance MUST read the protocol schema from that browser, not from a bundled copy. It auto-selects an instance only when exactly one live instance exists; with zero or several live instances, or when the selected instance is unreachable, it MUST print static usage. `guide` prints the bundled agent manual.
- **`wait` design.** `wait` is new code in the core layer (upstream ships the pattern as `scripts/cdp-wait.py`, not a verb). It MUST open an attach session, subscribe to the requested event, and only then begin examining events, so an event that fires between subscription and examination is buffered, not lost. `--match` is a substring test against the event's JSON serialization, not against its parameters alone. `--timeout` defaults to 30 seconds; `--timeout 0` means no deadline. On match: the event JSON on stdout, exit 0. On deadline: a timeout error on stderr, exit 1, no partial output on stdout.
- `attach` subscriptions MUST be isolated per session: two attached observers MUST NOT see each other's subscription set, and a retiring observer MUST NOT disturb the other's stream.
- `console-list` and `network-list` are REQUIRED verbs, implemented as thin wrappers over a short attach session whose duration defaults to 2 seconds. Both MUST subscribe before enabling their domains, so an event emitted during enable stays in the result. `network-list` correlates requests and responses by `requestId`, keeps rows in first-observation order, and keeps request-only and response-only rows with the missing fields null.
- `detect` runs interstitial detection against the current page and reports what it found (the detection browser-tools runs automatically post-navigation). Its retry budget is the caller's: `--wait SECONDS` bounds the wait for a self-clearing challenge and `--no-wait` reports the current state at once. `detect` MUST separate vendor presence from a challenge: a cookie or script tag proving a site uses a bot-protection vendor is present on every page of that site and MUST NOT by itself make a page an interstitial. Presence is reported alongside, never in place of, the blocked answer.
- **`frames select` takes a URL pattern.** The frame list is URL-keyed, so the selector is the pattern a caller has. `storage get --key K` selects a frame by URL pattern too; `--key` is not a cookie or local-storage key, and the manual MUST say so.
- **Machine-readable output MUST go to stdout; diagnostics to stderr.** Every verb emits one JSON document on stdout except three, which emit their documented alternative: `help` and `guide` print plain text, and `attach` prints JSON Lines, one event per line.

### Refusals and exit codes

Exit codes are fixed: 0 success, 1 operational failure (browser error, CDP error, timeout), 2 usage error. The merged CLI front implements this; the vendored modules' internal `sys.exit(1)` paths are unreachable from it because the front calls core functions, not the vendored `main()`.

- **A failed operation MUST NOT exit 0, and MUST NOT report a success field.** A tab close that `Target.closeTarget` reports as `success: false` is an operational failure: exit 1, diagnostic on stderr, no `{"stopped": true}` (defect 4). Any verb whose underlying call reports failure follows the same rule.
- The tool carries three deliberate refusal classes, each specified in its own section below: the **focus guard**, the **external endpoint** refusals, and **profile exclusivity**. Each refuses an action a caller explicitly asked for, so each MUST name the remedy in its diagnostic, and the named remedy MUST work.
- Every refusal is part of the contract an agent has to know. Each one MUST appear in `bt guide` with its exit code, under the enforcement rule in the agent manual section below.

### Instance and profile model

- Instances and profiles are orthogonal. An instance is a running process; a profile is a persistent identity. `launch --profile NAME` binds a profile to a new instance.
- **Registry schema.** The merged registry entry extends the vendored schema. Vendored fields: `port`, `pid`, `browser_version`, `user_data_dir`, `launched`, `pid_start`. Added by Phase 1: `engine` (string, `"chrome"` or `"camoufox"`) and `profile` (string or null). Added for the supervisor: `supervisor_pid` (integer or absent) and `supervisor_pid_start` (string or null). An entry missing any added field MUST be read as `engine="chrome"`, `profile=null`, and no recorded supervisor, so a registry written by the vendored code stays readable. Extended fields are written from the lifecycle layer through the registry's own atomic load/save helpers, because `core/registry.py` is a verbatim vendored module. The Phase 1 gate covers the extended schema.
- **Liveness is engine-aware.** Chrome instances: process identity plus CDP port attribution (the vendored check, unchanged). Camoufox instances expose no Chrome debugging port; their liveness is process identity plus user-data-dir hold (`pid_holds_user_data_dir`, which the merged tool keeps for this purpose). PID reuse after reboot or namespace changes MUST NOT produce a false "alive" for either engine.
- **Profile exclusivity.** A profile MUST be held by at most one live instance. `launch --profile NAME` while a live instance holds that profile MUST fail, exit 1, naming the holding instance; it MUST NOT launch a second browser on the same user data dir and MUST NOT steal the profile. **The check MUST be atomic against a concurrent launch.** Today `launch()` runs create-dir, clean-locks, check-holder, launch, register as five separate steps (`lifecycle.py:451,457,485,504`), so two concurrent launches can both see no holder and the contract is not met. A per-profile interprocess lock MUST be held across all five steps.
- **Stale singleton locks MUST be verified against the directory, not the PID alone.** `clean_stale_singleton_lock` preserves the singleton files whenever any live process carries the recorded PID, without checking that the process holds this directory (`process_utils.py:355,391`). `/tmp` used to erase the lock at reboot; a durable lock outlives the registry, and macOS reuses PIDs. The check MUST use `pid_holds_user_data_dir` (`process_utils.py:466,482`).
- **Registry corruption is not retirement.** The merged tool adopts upstream's distinction normatively: an unparseable or unreadable registry file reads as status `unknown`; a parseable file that lacks the instance reads as `retired`. On `unknown`, `status` MUST report it as such, `stop` MUST refuse to signal anything, and `cleanup` MUST NOT delete registry entries or session directories; it MAY move the corrupt file aside with a warning. `cleanup` removes an entry only after the entry itself was read and its liveness check failed.
- `cleanup` MUST NOT touch live instances.
- **`cleanup` MUST NOT delete a path under the profile root.** The lifecycle wrapper protects entries carrying a `profile` field (`lifecycle.py:851,860,866`), but the vendored cleanup deletes the recorded `user_data_dir` for every entry it judges dead (`core/registry.py:594,614`), and an old, partial, or malformed entry lacking that field can still name a durable path. Registry contents are untrusted input, so the field check is not sufficient: a path check against the resolved profile root is REQUIRED as well.
- **`cleanup` MUST NOT prune a profile on age.** Nothing prunes a profile today, and nothing should: a profile holds a login. `profile delete NAME` is the way a profile goes away.

### Profile storage

- **The profile root is durable storage.** It is resolved by this precedence, and an empty value at any level MUST fall through to the next:
  1. `BROWSER_TOOLS_PROFILES_DIR`, when non-empty.
  2. `XDG_DATA_HOME` when non-empty, plus `/browser-tools/profiles`.
  3. `~/.local/share/browser-tools/profiles`.
- **The registry stays in `/tmp`.** Question 5 settled this and it stays correct: a cleared registry after reboot is self-consistent, because no browser survives reboot. Persistent state belongs to profiles, not the registry.
- **Ordering is normative. Supervisor retirement MUST preserve profile directories before the profile root moves.** Defect 1 destroys a named profile on every normal headed close. Moving the root first relocates that defect from a directory the operating system clears at reboot into durable storage, where the loss is permanent. The retirement fix and the root move MUST NOT land in the same change unless the retirement fix is complete and tested within it.
- **Existing profiles MUST be migrated, not abandoned.** Changing the default without migrating creates fresh empty profiles at the XDG path and leaves the authenticated ones in `/tmp` until the operating system deletes them. Move the complete profile directory, never the cookie files alone; login state belongs to the whole Chrome profile directory. The sequence: resolve the destination by the precedence above; refuse while a live process holds the directory; remove only verified-stale singleton files; move to a temporary sibling at the destination; verify; rename into place. Migration MUST refuse on collision and MUST NOT merge two profiles.
- **Unbound sessions stay temporary.** Unbound Chrome gets a temp dir under the vendored session root and stays in `/tmp`. Unbound Camoufox creates `.ephemeral` under the profiles root today (`lifecycle.py:463-466`), which a bare root substitution would move into durable storage. Unbound Camoufox MUST use its own temp root outside the profile root.

### Profile verbs

`profile list` and `profile delete NAME` operate on the profile root resolved above, never on the retired `~/.cache/tool-proxy/browser-tools/profiles` layout.

- `profile list` reports every profile in the root with its name, path, and whether a live instance holds it. It requires no running instance.
- `profile delete NAME` removes one profile directory. It MUST validate `NAME` strictly against the same character set instance names use, and it MUST verify that the resolved path lies inside the profile root before deleting anything. A name that escapes the root is a usage error, exit 2.
- `profile delete NAME` MUST refuse a live holder, exit 1, naming the holding instance. Stopping the holder first is the remedy, and the diagnostic MUST say so.
- These rules are recovered from `handle_list_profiles` and `handle_delete_profile` (`browser_session.py:199,251`), which Phase 5 deletes. They are written here so the deletion does not take them with it.

### External endpoints

`--endpoint URL` drives a browser the tool did not launch. It is a flag, not a verb, and **nothing is written to the registry.**

```
bt --endpoint http://127.0.0.1:9222 snapshot
bt --endpoint http://127.0.0.1:9222 fill --uid 5C3C89-4 --text hunter2
```

- **The absence of a registry entry is the safety property, not an omission.** An external Chrome is the user's real browser, so its `user_data_dir` is the real Chrome profile directory. Two vendored paths `rmtree` whatever that field holds: `deregister()`, which the supervisor calls on close (`core/registry.py:581,587`), and `cleanup()` for any entry it judges dead (`core/registry.py:594,614`). A guard in the lifecycle layer would not hold, because the vendored `cleanup()` iterates the registry itself and a guard above it can be bypassed. With no entry, `stop` and `cleanup` cannot see the browser, so there is nothing to guard.
- **`--endpoint` applies to the browser-driving verbs**: raw passthrough, `snapshot`, `click`, `fill`, `detect`, `frames`, `storage`, `screenshot`, `screencast`, `attach`, `wait`, `console-list`, `network-list`, `wait-idle`, `wait-stable`, and `help`. It MUST NOT be accepted on `launch`, `status`, `stop`, `cleanup`, `guide`, or `profile`, which are registry and lifecycle verbs.
- **Loopback only, with no escape hatch.** An endpoint whose host is neither `127.0.0.1` nor `::1` MUST be refused, exit 2. A CDP endpoint is unauthenticated full control of a logged-in browser, cookies included. `ssh -L 9222:127.0.0.1:9222 <host>` presents a remote endpoint as loopback locally, so driving a remote browser is unaffected.
- **`Browser.close` and `Browser.crash` MUST be refused over `--endpoint`**, exit 2. Everything else passes, including navigation, input, cookies, and `Target.closeTarget` for a single tab. The reasoning parallels the focus guard: for a browser the tool launched, quitting is what `stop` is for and the loss is a throwaway session; for the user's real browser it is every window and tab they had open, reachable through one typo in a verb that accepts arbitrary method names.
- **`--endpoint` with `--profile` is a usage error**, exit 2. A profile is a launch-time identity, and external attach launches nothing.
- **The focus guard applies unchanged.** It lives in `passthrough.py:167-235` and `curated.py:194-220`, not in the supervisor, so it carries over with no work. Not taking the screen matters more in the user's own browser, not less.
- `--target SPEC` and `--url SUBSTRING` keep their meanings against the external browser, including the normative sort by target ID.
- **Accepted costs.** No instance name, so every invocation carries `--endpoint`. `status` does not list the browser, because status reports the registry. A browser that dies mid-session surfaces as a connection error on the next invocation, mapped through the existing one-shot error handling (`one_shot.py:232-300`); no entry is left pointing at a dead process.
- **Not carried forward** from the retired `handle_attach_browser`: the persistent controller, the project override, and profile-to-port discovery. Discovery existed to find a named profile's port; with an explicit endpoint and no registry entry, it has nothing to attach to. Carried forward: loopback validation, verifying that the listener PID holds the expected user-data directory and debugging port as a diagnostic when a connection fails, and reporting port collisions with PIDs and process arguments.

### Anti-detection

- `stealth.js` and its injection path are deleted. The merged tool MUST NOT inject JavaScript for fingerprint purposes. Rationale (from chrome-agent's audit, accepted in review): each JS override is independently detectable.
- Chrome paths MAY use fingerprint profiles: launch flags only.
- Camoufox remains the answer when the engine itself must change, behind `launch --engine camoufox`, and requires the `camoufox` extra. That path runs through `lifecycle._launch_camoufox` into `camoufox_runner` and never touches the MCP session layer, so it survives Phase 5 untouched.

### Focus guard

The tool drives a browser on a machine a person is using. Taking the screen interrupts them, so the merged tool MUST NOT raise a browser window over the user's work, and MUST NOT silently do nothing when it cannot act without doing so.

- **Refused outright.** `Target.activateTarget` and `Page.bringToFront` raise a window by definition. The passthrough MUST refuse both with a usage error (exit 2) naming how to work in the background instead.
- **Forced to the background.** `Target.createTarget` defaults to foreground in Chrome. The passthrough MUST set `background: true` when the caller omits it, and MUST refuse an explicit `background: false`.
- **Not refused.** `Browser.setWindowBounds` moves or resizes a window without raising it, and is a legitimate way to place an agent window. Resize, minimize, and restore are outside the guard. It MUST remain available.
- **Input to a background tab.** Chrome drops input sent to a tab that is not the selected tab of its window, without an error, so a caller that dispatches anyway reports a success that did not happen. Measured: a click into a background tab reached neither the top document nor a cross-origin iframe, while the same click into the selected tab of a window behind other windows landed every time. Any surface that delivers input MUST read `document.visibilityState` first and MUST fail with an error rather than dispatch into a hidden page. This covers the passthrough's `Input.*` delivery methods and the curated `click` and `fill`. An unreadable visibility state is unknown, not hidden, and MUST NOT block the call.
  - The refusal MUST name the remedy, and the remedy MUST work: open the page in its own window (`Target.createTarget` with `newWindow`) and address it with `--target`, or navigate the current tab. This is why `--target SPEC` is REQUIRED on `snapshot`, `click`, and `fill`; without it the refusal would have no escape.
  - Reads are not input. `snapshot`, `screenshot`, and the detection verbs MUST keep working against a background tab.
- **Launch.** A headed launch MUST NOT take focus. Chrome started normally opens a window and becomes the active app, so the browser is started with `--no-startup-window` and no URL argument, and its first window is created over CDP with `newWindow` and `background` set once the DevTools endpoint answers. Headless is exempt: it has no window.

### Window marking

Windows launched by the tool carry the supervisor's visual border and badge so a human can tell agent-controlled windows from their own.

- **The tab title prefix always runs.** It is the marking that cannot be turned off, because an unmarked agent window is indistinguishable from the user's own.
- **The border and badge follow a persistent setting.** They sit on the page's outer edge and its top-left corner, so a person who needs to see the UI underneath turns them off. The `window-border on|off` verb writes that setting, and `window-border` with no argument prints it. Every running supervisor MUST add or remove them on its open tabs within a second of the change, without a relaunch.
- **`--no-window-border` is per launch** and suppresses marking for that instance only. It does not write the setting.
- **A fingerprint profile suppresses the border.** The title prefix is page-observable, and bot-defended sites, exactly where fingerprinting is used, are where title-diffing detectors live.
- **The setting is a file.** One JSON object at `$XDG_CONFIG_HOME/browser-tools/settings.json`, falling back to `~/.config/browser-tools/settings.json`. It is the only persistent user setting the tool has; an unreadable or unparseable file MUST read as the default rather than fail a launch.
- An externally attached browser has no supervisor, so no marking applies to it and `window-border` does not reach it.

The merged tool vendors the marker with the supervisor (adapted module).

### Supervisor

One detached supervisor process per headed instance holds a browser-level CDP connection, marks the windows, and retires the instance from the registry when the browser closes. Headless instances get none: there is no window to mark, and their entries are reclaimed by cleanup.

- **It MUST outlive the shell that launched the browser.** The browser is started in its own session; the supervisor MUST be too, or closing the launching terminal ends it while the browser lives on, leaving an instance nothing marks and nothing retires.
- **It MUST record why it exited.** Its output goes nowhere, so an exit log next to the registry is the only trace that survives it. Every way out is covered: the clean retire, the watchdog stall, an unhandled exception, and a signal. A default-disposition signal kills the process without unwinding, so signals need an installed handler that logs, restores the default disposition, and re-raises. Logging is best-effort and MUST NOT keep a supervisor alive.
- **Its absence MUST be visible.** `status` reports a per-instance `supervisor` field: `running`, `missing`, or null when none was ever recorded. The PID is recorded when the supervisor is spawned, not by the supervisor itself, so one that started and died reads as `missing` rather than as an instance that never had one. Identity is the `pid`/`pid_start` pair the browser's own liveness uses, so a recycled PID is not mistaken for it.
- **It MUST NOT delete a profile directory.** Retirement MUST go through a lifecycle-owned operation that applies the same preservation rule `lifecycle.stop()` applies (`lifecycle.py:778,787`), instead of calling the vendored `deregister()` (`core/supervisor.py:880`). This is the fix for defect 1 and the precondition for moving the profile root.
- **`stop` MUST NOT race supervisor retirement into a deletion.** With retirement routed through the lifecycle layer, both paths preserve the directory, so the order they run in stops mattering.
- It MUST attach only to top-level page targets: cross-origin iframes, workers, and extension targets MUST NOT be left paused. A stalled supervisor MUST NOT wedge new documents; its watchdog exits the process.

### Profiling

The CPU profiler keeps its own long-lived process and its `browser-tools-profiler` entry point. It speaks the core client for target discovery and CDP transport, and MUST NOT depend on a daemon or any MCP module. Its dependencies sit behind the `profiling` extra; invoking it without the extra fails with the exact install line. A smoke test (start, profile a page, stop, artifact exists) is in the suite.

### Native snapshot and UIDs

- Snapshot and UID interaction are built on the CDP Accessibility domain. Child-frame trees are stitched under their owner iframe node, colliding child node IDs are namespaced, and a missing child frame degrades to the top-frame tree.
- **A UID is `<docToken>-<backendNodeId>`.** The docToken derives from the page's `loaderId`, which the CLI sees on `Page.navigate` and can read from `Page.getFrameTree`. The backendNodeId is the accessibility node's `backendDOMNodeId`, which every snapshot node already carries (`native_snapshot.py:95`).
- **UIDs MUST be stable for the lifetime of the document.** A UID returned by `snapshot` MUST resolve to the same node for subsequent `click --uid` and `fill --uid` calls until the page navigates. **Taking another snapshot MUST NOT invalidate a UID.** This replaces the version 5 rule, which made the next snapshot an invalidation event.
- **Navigation is the only invalidation event.** Navigation changes the `loaderId`, so a UID carrying a previous docToken MUST be refused with exit 1 and a diagnostic saying to take a new snapshot, before any interaction CDP traffic is sent. The docToken comparison is the staleness guard; no separate guard is specified, and none can ship ahead of this change.
- **`click` and `fill` MUST NOT take an internal snapshot.** With the backend node id carried in the UID there is nothing to look up in a tree: they validate the docToken and resolve directly. The internal snapshots at `curated.py:241` and `curated.py:261` are what make the current staleness check unable to fire, because the UID is always compared against a tree built moments earlier in the same process.
- **Evidence.** One handler drove three `take_snapshot` reads with a `fill` between the second and third. The ordinal moved for the password field (`1-8`, `2-8`, `3-10`) while `backendDOMNodeId` held at 3, 4, and 9 for the three nodes across all three reads, including across the fill.
- **Rejected alternatives.** A bare `backendNodeId` is stable across fills but carries no document provenance, so a stale UID would resolve to a live unrelated node in the next document, relocating the silent wrong-field failure instead of removing it. Keeping ordinals and persisting the snapshot to disk preserves the current UID appearance at the cost of a new on-disk artifact with its own lifetime, staleness, cleanup, and concurrent-access rules, in a tool whose registry already carries a set of corruption-handling rules for exactly that reason.
- The generation counter stops being part of identity. Keeping it on the snapshot object as a diagnostic or removing it is an implementation call, not a decision this RFC makes.
- The parity suite keeps the Node engine as a test oracle only. No CLI flag routes a caller through it, and `--engine` takes `chrome` or `camoufox`.

### Screencast

Screencast is one bounded capture in one invocation: `screencast --dir DIR [--duration SECONDS] [--format FMT] [--max-frames N]`. It starts capture, buffers frames, writes them to `DIR`, and exits.

- **The `screencast start` / `screencast stop` pair is removed.** Frame buffering is process-local (`curated.py:404-417`), so a `stop` in a second CLI process can never reach the frames a `start` in the first one buffered. Version 5 described screencast as owning a long-lived process; the code never did, and the verb pair could not work.
- **Screencast MUST NOT own a long-lived process.** A detached recorder was considered and declined: it would add a second long-lived process with its own lifetime, registry presence, and exit-logging rules, to serve a case one bounded capture already covers.
- Detached asyncio tasks inside the recorder need strong references. The event loop holds only weak ones, so pending frame acknowledgements can otherwise disappear during garbage collection.

### MCP front retirement

The MCP front and the persistent-session stack behind it are deleted in Phase 5. The CLI is the only surface.

- **What goes:** 22 source files (5,751 lines) and 20 test files (5,531 lines). An import-closure analysis with `browser_tools.cli` and `browser_tools.profiler` as roots reaches none of them; adding `mcp_daemon` as a root still leaves `automation_backend`, `camoufox_session`, `mcp_session`, and `core/session.py` with no caller anywhere.
- **What stays:** the CLI, the profiler, the direct CDP implementation, the native interaction code, and the Camoufox runner.
- **Three code moves are REQUIRED before the deletion**, because a raw deletion fails on the first import:
  1. `BrowserToolsError` moves out of `chrome_utils.py`. `cdp_client.py:24` imports it and `cdp_client.py:36` makes `CDPError` inherit from it. No retained caller depends on the base type, so `CDPError(Exception)` is the result.
  2. `McpBroker` moves into `tests/parity/node_broker.py`. `tests/parity/parity_engines.py:468` imports it to drive the Node parity baseline. That is a test oracle, not a front, and a production module MUST NOT stay alive to serve a test.
  3. `INITIAL_PAGE_URL` is pruned from `process_utils.py:30`, which imports it from `session_layout.py`. Only the legacy daemon and external-attach helpers use it, so those helpers are pruned instead of keeping the layout module.
- **Contracts that MUST move before their tests are deleted:** the exact `CDP_TOOLS` set, `CDPHandler` handler-table parity, the `make_text` / `make_error` / text-extraction behavior from `mcp_response.py`, and the handler completeness assertion currently in `test_inspect_mode.py`. `tool_registry.py` keeps its 18 CDP-routed entries and loses the MCP and session-only entries and derived sets.
- **The frozen MCP surface contract ends with the front.** Versions 2 to 5 froze the tool names, argument shapes, and response shapes of the MCP surface, and version 5 recorded the session-layer lifecycle tools leaving it. With no front, there is no surface to freeze and no client to hold compatible. The CLI-to-MCP verb mapping table that versions 2 to 5 carried is deleted with it. Anything that wants an MCP front back specifies it against a front that exists first.
- **The session reaper goes with the front.** It scans `~/.cache/tool-proxy/browser-tools`, recognizes only profile directories beneath that cache root, and has no production caller: `persistent_browser.py:67` only re-exports it.

### Packaging

- The default install (`pip install browser-tools`) MUST depend on `websockets` only, with the floor at `>=16.0` (the higher of the two projects' floors; the vendored core requires it).
- `camoufox[geoip]` and the `aiohttp` CVE pin sit behind the `camoufox` extra; `pillow` and profiling dependencies behind the `profiling` extra; `all` bundles every extra (Question 4, resolved). The CVE pin does not appear in the default install.
- Commands that need an absent extra MUST fail with the exact `pip install` line naming the extra.
- Python requirement: >= 3.13 (browser-tools' current floor; chrome-agent's >= 3.11 is compatible).
- **Phase 5 packaging changes.** Remove the `mcp` keyword (`pyproject.toml:16`). Remove `*.mjs` and `*.sh` from package data once `persistent-session-template.mjs` and `attach_chrome.sh` go. Keep `*.js` for `detect_interstitial.js`. Keep all three entry points, the base `websockets` dependency, and both extras: no dependency and no extra exists only for the front.

### The agent manual (`bt guide`)

`bt guide` is the complete manual for the CLI surface, and the only documentation an agent needs to read before driving a browser with it.

- **Every verb MUST have an entry**, and a test MUST fail the build when a verb has none. The manual today documents `launch`, `status`, `stop`, `cleanup`, `guide`, `window-border`, and the raw passthrough, and omits the rest.
- **Every deliberate refusal MUST appear with its exit code**, and every non-obvious contract MUST appear as a rule rather than as an example. The inventory that supplies the content covers the 21 verbs of the current surface, 34 refusals with exit codes, 29 non-obvious contracts, and 27 test-encoded traps, each carrying a `file:line` or a test name ([#87](https://github.com/dungle-scrubs/browser-tools/issues/87)). That inventory is the content source for the writing ticket; this RFC pins the completeness rule, not the prose.
- The manual MUST cover the end-to-end login walkthrough: `launch --profile NAME` headed, authenticate by hand, `stop`, relaunch with the same `--profile NAME`, and where the profile directory lives under the resolved root.
- The manual MUST state the UID rule as one rule: a UID is valid until the page navigates, re-snapshot after navigation, and there is no need to re-snapshot between fills.
- The manual MUST state the `--endpoint` rules: loopback only, the browser is not registered so `status`, `stop`, and `cleanup` neither see nor touch it, `Browser.close` and `Browser.crash` are refused, and the login is established by hand in that browser first.
- The manual MUST distinguish a profile (persistent identity) from a fingerprint profile (launch flags), and MUST say that `storage --key` selects a frame by URL and is not a storage key.
- **`guide` prints plain text**, not JSON. That is an exception to the machine-readable-output rule, stated in CLI surface.

### Out of tool-proxy

- The CLI is the only agent surface. An agent skill ships from this repository (deployed to `~/.agents/skills`) and points at `bt guide` rather than restating the verb set.
- The tool-proxy `browser-tools` app is retired, together with the global "route browser automation through tool-proxy" instruction. Version 5 recorded that the optional MCP front remained for harnesses that cannot run a CLI; Phase 5 removes it, so no front remains. `docs/tool-proxy-retirement.md` records this supersession.

## Migration Strategy

Six phases. Each phase MUST leave `main` releasable, and a go/no-go gate closes each phase.

**Phase 0 - Vendor the core (size S, landed).** Copy the core modules with license headers into `src/browser_tools/core/`. No user-visible change. Gate: the existing test suite passes unmodified.

**Phase 1 - Lifecycle cutover (size S, landed).** `launch`, `status`, `stop`, and `cleanup` speak the vendored registry with the extended schema (`engine`, `profile`). Named profiles become launch attributes; profile exclusivity enforced. Gate: lifecycle e2e tests pass against the extended registry, including a Camoufox launch registered and reported live; the old lifecycle code paths are deleted, not left dormant.

**Phase 2 - Native snapshot (size L, landed).** UID tools on the Accessibility domain, transitional Node flag, parity suite built and running. Gate: the parity gate defined in Testing Strategy.

**Phase 3 - Daemon demotion and events (size M, landed).** MCP front optional; `attach`, `wait`, `console-list`, `network-list` land; `stealth.js` removed; fingerprint profiles arrive; profiler rebuilt over the core client. Gate: every CLI verb works with no daemon running.

**Phase 4 - Packaging and surface retirement (size S, landed in part).** Extras split; agent skill authored against the final verb set; tool-proxy app retired; bundled agent guide and window marking wired through the CLI front. Gate: clean-machine install of the default package runs the lifecycle and passthrough verbs. Outstanding into Phase 5: the Node package-data assets and the `mcp` keyword.

**Phase 5 - Trustworthy surface (size L).** Seven steps. They are independent of each other except for the two ordering constraints stated below, both of which are normative.

- **5a - Retire the MCP front.** The three code moves, then the deletion of 22 source files and 20 test files, then the packaging changes. Gate: `pytest`, `ruff check src/ tests/`, `pyright`, and `uv build` all clean; the wheel and sdist contain no deleted module, no `persistent-session-template.mjs`, and no `attach_chrome.sh`; fresh-process import assertions prove `browser_tools.cli` and `browser_tools.profiler` load none of the removed stack, that `lifecycle` does not import `camoufox_runner` until a Camoufox launch, and that the package exposes no lazy attribute for a deleted module.
- **5b - Supervisor retirement preserves profiles.** Route the supervisor through a lifecycle-owned retirement operation; `core/registry.py` is verbatim and MUST NOT change. Gate: a headed named-profile instance closed normally leaves its directory intact, and a `stop` racing supervisor retirement leaves it intact in both orders.
- **5c - Move the profile root (REQUIRES 5b).** Resolve by the precedence in Profile storage, migrate the existing profiles, give unbound Camoufox its own temp root, add the durable-root path check to `cleanup`, fix `clean_stale_singleton_lock` to verify the directory hold, and make profile exclusivity atomic. Gate: the regression coverage listed in Testing Strategy is green, and the three profiles on the driving dev's machine survive the migration with their `Default/Cookies` intact.
- **5d - UID scheme.** `<docToken>-<backendNodeId>`; `click` and `fill` drop their internal snapshots; the inverted tests land with the change. Gate: two fills from one snapshot with no snapshot between them both land in the correct field, and a UID from a previous document is refused before any interaction CDP traffic.
- **5e - External endpoints.** `--endpoint URL` on the browser-driving verbs, with the loopback, `Browser.close` / `Browser.crash`, and `--profile` refusals. Gate: each refusal is tested at its exit code, and a live external Chrome is driven end to end without gaining a registry entry.
- **5f - Profile verbs.** `profile list` and `profile delete NAME` against the resolved root. Gate: name validation, path containment, and live-holder refusal are each tested.
- **5g - Complete the guide (REQUIRES 5a to 5f).** Every verb, refusal, and non-obvious contract of the final surface, plus the enforcing test. Gate: the enforcing test fails when a verb is removed from the manual, and passes on the shipped manual.
- Defect 3 (`stop --target`) and defect 4 (the success-looking tab-close failure) depend on none of the seven steps and SHOULD land first. Both are live violations of text this document already carried.

**Rollback.** Phases 0 to 1 roll back by reverting the phase's merge commit. Phase 2 to 3 rollback re-enabled the Node engine path, which is now a test oracle only. Phase 4 and Phase 5 are not rolled back wholesale; each Phase 5 step is its own revertible change, with one exception: **5c is not revertible by a revert**, because the profiles have moved on disk. Its rollback is the reverse migration, which the step MUST ship alongside the forward one.

## Risk Assessment

- **Destroying a login during the profile move (highest risk).** The move runs against directories holding the user's authenticated sessions, on a code path where two vendored functions already `rmtree` whatever a registry entry names. Mitigation: the 5b-before-5c ordering constraint is normative; the migration moves to a temporary sibling and renames into place rather than copying over a live path; it refuses on collision and while a live process holds the directory; and the reverse migration ships with the forward one.
- **Deleting something the CLI still needs in 5a.** A raw deletion of 11,282 lines fails on the first import, and a subtler miss would pass tests and fail at runtime for a user. Mitigation: the three code moves are specified before the deletion, the contracts to relocate are named, and the gate includes fresh-process import assertions plus a wheel and sdist inspection rather than a test run alone.
- **The UID change inverts existing tests.** Four tests assert the old rule, so a careless change makes them pass by weakening what they check. Mitigation: the inversions are named in Testing Strategy, and the gate is a new behavioral test (two fills from one snapshot) rather than the inverted ones.
- **`--endpoint` blast radius.** The flag points the tool at the user's real browser and everything authenticated in it. Mitigation: no registry entry, so no lifecycle path can reach it; loopback only with no escape hatch; `Browser.close` and `Browser.crash` refused.
- **Liveness regressions on macOS suspend and resume.** The vendored liveness model is the merge's main prize; a porting mistake here corrupts every verb. Mitigation: the verbatim-module rule, with adaptation only at call sites.
- **Blast radius overall.** Worst case is loss of browser automation and of the stored logins on the machines using this tool. No production systems or external users depend on it today (Alpha status, single maintainer).

## Testing Strategy

- **Existing suite as the floor.** The current test suite MUST pass at every phase gate. The baseline is 946 passed, 4 skipped, 950 collected, with ruff clean and pyright at 0 errors.
- **Parity gate (Phase 2, passed).** The parity suite runs an agreed page corpus through both engines. The comparison operator: snapshot node sets compared order-insensitively on (role, name, value) tuples; UID resolution compared by the backend node a click or fill resolves to; text extraction compared exactly. The corpus includes iframe and shadow DOM cases. The gate passes when results match on the full corpus for two consecutive runs, flake-free. The Node engine survives in this suite only, driven by `tests/parity/node_broker.py` after the 5a move.
- **Contract tests retained from the MCP suite (5a).** The exact `CDP_TOOLS` set, `CDPHandler` handler-table parity, and the `make_text` / `make_error` / text-extraction behavior MUST be covered by retained tests before `test_mcp_surface_contract.py` is deleted. The MCP schema-level surface tests go with the front.
- **Import assertions (5a).** Fresh-process tests that `browser_tools.cli` and `browser_tools.profiler` import none of the removed stack, that the top-level package import stays daemon-free, that `lifecycle` does not import `camoufox_runner` until a Camoufox launch, and that no lazy package attribute names a deleted module.
- **Profile durability tests (5b, 5c).** A normal headed close preserves a named profile. A `stop` racing supervisor retirement preserves it in both orders. A durable stale lock whose PID was reused by a process holding another directory is cleaned. Two concurrent `launch --profile NAME` calls leave the loser naming the winner. `cleanup` preserves a durable path when the entry's profile metadata is absent, and when it is malformed. All three precedence levels resolve, and an empty value at each level falls through. Every legacy profile migrates; migration refuses on collision and while a live process holds the profile. An unnamed Chrome session stays in `/tmp`, and an unbound Camoufox session stays temporary outside the profile root.
- **UID tests (5d).** Two fills from one snapshot, with no snapshot between them, both land in the correct field. A UID carrying a previous docToken is refused before any interaction CDP traffic. These tests invert with the change: `test_new_snapshot_supersedes_old_uids` becomes "a new snapshot preserves earlier UIDs", and `test_resolve_raises_for_stale_uid_after_new_snapshot` inverts with it. `test_navigation_invalidates_current_snapshot` and `test_navigation_bumps_generation_so_post_nav_uids_differ` keep their intent, now expressed as a docToken change. `test_uid_format_is_generation_dash_ordinal` is replaced by a docToken-plus-backend-id format test.
- **Endpoint tests (5e).** A non-loopback endpoint, `Browser.close`, `Browser.crash`, and `--endpoint` with `--profile` each refused at exit 2. A driven external browser gains no registry entry. A browser that dies mid-session surfaces as an operational failure on the next invocation.
- **Profile verb tests (5f).** Strict name validation; a name that escapes the profile root refused at exit 2; a live holder refused at exit 1 naming the holder.
- **Guide completeness test (5g).** The test enumerates the CLI's verbs and fails the build when one has no manual entry.
- **Defect tests.** `stop --target 1` closes the first page target by the normative sort, and `stop --target <prefix>` resolves a prefix. A `Target.closeTarget` returning `success: false` exits 1 with a stderr diagnostic and no `{"stopped": true}` on stdout.
- **Liveness tests.** Registry tests MUST cover: PID reuse, port reuse by an unrelated process, machine suspend and resume, stale registry entries after a crash, a corrupt registry file (read as `unknown`, nothing deleted, nothing signaled), and a Camoufox instance's liveness via user-data-dir hold.
- **Event tests.** `wait` MUST have a test proving it catches an event fired between attach and wait, and a test for deadline behavior (exit 1, stderr diagnostic, empty stdout). Attach isolation MUST have a two-observer test.
- **Install matrix.** Clean-environment installs of the default package and each extra, verifying import, verb availability, and the failure message for missing extras. After 5a, the wheel and sdist MUST contain no deleted module and no Node asset.

## Security Considerations

- **Trust boundaries.** The CLI trusts its caller completely: passthrough grants arbitrary control of the browser, including cookies, storage, and any authenticated session in the profile. This is the tool's purpose, not a defect, and it matches both projects today. The merged tool MUST bind CDP ports to loopback only.
- **The external endpoint is the one place that trust reaches a browser the caller did not create.** `--endpoint` is refused for any host other than `127.0.0.1` or `::1`, with no escape hatch, because a CDP endpoint is unauthenticated full control of a logged-in browser and a non-loopback endpoint is a remote takeover channel. `Browser.close` and `Browser.crash` are refused over `--endpoint` so one typo in a verb that accepts arbitrary method names cannot close every window the user had open. An external browser gains no registry entry, so no lifecycle path can delete its user data directory.
- **Durable profiles raise the cost of a wrong deletion.** From Phase 5c the profile root holds authenticated cookies in the user's home directory, where the operating system never clears it. Every deletion path is therefore constrained: the supervisor retires through the lifecycle layer and MUST NOT delete a profile directory; `cleanup` checks the resolved profile root as a path in addition to the entry's `profile` field, because registry contents are untrusted input; `cleanup` MUST NOT prune a profile on age; and `profile delete NAME` validates the name strictly and verifies path containment inside the profile root before deleting anything.
- **Registry file.** The registry lives in a world-readable temp directory and records PIDs, ports, and profile paths. It MUST NOT record credentials, cookies, or page content. Registry contents MUST be treated as untrusted input, with the corruption semantics pinned in the Instance and profile model: a corrupt file reads as `unknown`, and on `unknown` nothing is signaled and nothing is deleted. A registry entry MUST NOT cause the tool to signal or kill a process it cannot first verify ownership of via the liveness check.
- **Injected content.** With `stealth.js` deleted, the tool injects no JavaScript for fingerprint purposes. The window-marking overlay is injected into windows the tool itself launched, is visible by design, and carries no page data out. Page-side JS execution remains available to callers through explicit tools and passthrough (`Runtime.evaluate`); that is caller-directed, not tool-initiated.
- **No listening surface remains after Phase 5.** The MCP front was the tool's only server. With it deleted, the tool opens no socket of its own and accepts no connection; it is a one-shot client of a browser's CDP endpoint. This removes the bind-permissions question versions 2 to 5 carried.
- **Anti-detection scope.** Fingerprint profiles and Camoufox exist for authorized automation of sites that block all automation indiscriminately. The tool contains no captcha solving and no inference, and this RFC forbids adding either. Nothing in the merge changes what a caller could already do with Chrome and a CDP client.
- **Vendored code.** Phase 0 vendored third-party code wholesale. The vendored modules MUST be read in review before merge, not trusted by reputation.

## Implementation Plan

Phases 0 to 4 landed as specified in Migration Strategy. Phase 5 is the remaining work, and its steps 5a to 5g are the ticket slices: each is independently revertible, each carries its own gate, and only two orderings bind. **5b MUST precede 5c**, and **5g MUST follow 5a to 5f** because it documents the surface those steps leave behind. The defect fixes for `stop --target` and the success-looking tab-close failure are small and independent and SHOULD land first, since both are live spec violations.

5a and 5c SHOULD each land as their own PR: 5a deletes roughly 11,300 lines, and 5c touches the user's stored logins. Ticket slicing from this revision is a separate action, and the implementation tickets are cut from map [#82](https://github.com/dungle-scrubs/browser-tools/issues/82) against the step list above.

## Open Questions

None open. The five questions raised in versions 1 to 2 were decided by the author on 2026-08-21 (via the Lucid review of this document), each taking the recorded recommendation:

1. **Vendored namespace: resolved (a).** The core lives in `src/browser_tools/core/` with imports rewritten. An import rewrite is the one mechanical modification allowed inside verbatim modules; nothing else is.
2. **CLI binary name: resolved (a).** Two console scripts, `browser-tools` (canonical in docs) and `bt` (alias).
3. **Parity corpus: resolved as a process.** The page list was settled by a survey of the e2e fixtures before Phase 2 implementation; the comparison operator and the iframe and shadow-DOM coverage requirement were already normative in Testing Strategy.
4. **Extras layout: resolved (a).** `camoufox` and `profiling` as separate extras, plus `all`.
5. **Registry location: resolved (a).** Keep `/tmp` semantics for the registry; a cleared registry after reboot is self-consistent because no browser survives reboot. Persistent state belongs to profiles, not the registry. Version 6 moves the profiles and leaves this answer standing.

Version 6 resolved five recorded disagreements between the shipped code and versions 2 to 5 of this document. Two were decided by the driving dev during this revision; three were machine-made, taking the recommended answer, and are recorded here because they changed normative text:

6. **Screencast lifetime: decided by the driving dev.** The `start` / `stop` pair is replaced by one bounded capture. A detached recorder process was the alternative and was declined.
7. **Leading instance grammar: decided by the driving dev.** The code is normalized to the spec rule, so every browser verb accepts a leading `[INSTANCE]`, sub-action verbs included. The alternative was an exception in the spec and in `bt guide`.
8. **`--url` reach: machine-made.** The spec adopts the shipped flag set, so `--url SUBSTRING` is normative on `console-list`, `network-list`, and `screenshot` as well as passthrough, `attach`, and `wait`. Removing a working selector from three verbs would have narrowed the surface for nothing.
9. **Frame selector: machine-made.** `frames select` takes a URL `PATTERN`, not an index. The frame list is URL-keyed, so the pattern is what a caller has, and the MCP name mapping that once tied this to `select_frame` retires with the front.
10. **Machine-readable output: machine-made.** The JSON-on-stdout rule keeps three documented exceptions: `help` and `guide` print plain text, and `attach` prints JSON Lines. All three were deliberate in the code and none is machine-readable as one JSON document.

## Changes in this revision

**Version 6** (2026-09-20): renders the six decisions of wayfinder map [#82](https://github.com/dungle-scrubs/browser-tools/issues/82), each settled on its own closed ticket, and pins correct behavior for four defects the map found. One line per change:

1. **MCP front retired** ([#86](https://github.com/dungle-scrubs/browser-tools/issues/86)). New **MCP front retirement** section replacing the MCP compatibility contract: 22 source files and 20 test files go, three code moves precede the deletion, the contracts to relocate are named, and the frozen-surface promise ends with the front. The CLI-to-MCP mapping table is deleted, `core/session.py` leaves the vendored module list, "optional MCP front" leaves the architecture table and Terminology keeps the two terms marked retired, packaging loses the `mcp` keyword and the Node package data, and the MCP bind item leaves Security Considerations for a statement that no listening surface remains.
2. **Profile storage moved to XDG** ([#85](https://github.com/dungle-scrubs/browser-tools/issues/85), [#81](https://github.com/dungle-scrubs/browser-tools/issues/81)). New **Profile storage** section: the three-level precedence with empty values falling through, the registry staying in `/tmp`, the migration sequence, and unbound Camoufox getting its own temp root. The ordering constraint that no single ticket owns is normative here: supervisor retirement MUST preserve profile directories before the root moves.
3. **Supervisor stops deleting profiles** (defect 1). The **Supervisor** section gains the rule that retirement goes through a lifecycle-owned operation instead of the vendored `deregister()`, and **Vendoring rules** gains the general form: a defect inside a verbatim module is corrected at the call site.
4. **UID scheme and lifetime rewritten** ([#83](https://github.com/dungle-scrubs/browser-tools/issues/83), [#84](https://github.com/dungle-scrubs/browser-tools/issues/84), defect 2). The version 5 rule at line 224 said a UID is stable for the lifetime of a snapshot and invalidated by the next snapshot or navigation. It now reads: a UID is `<docToken>-<backendNodeId>`, stable for the lifetime of the document, invalidated by navigation alone, and a fresh snapshot invalidates nothing. `click` and `fill` MUST NOT take an internal snapshot. The measurement, the two rejected alternatives, and the test inversions are recorded.
5. **External endpoints added** ([#88](https://github.com/dungle-scrubs/browser-tools/issues/88)). New **External endpoints** section: `--endpoint URL` on the browser-driving verbs, no registry entry as the safety property, loopback only with no escape hatch, `Browser.close` and `Browser.crash` refused, `--endpoint` with `--profile` a usage error, and the focus guard carried over unchanged.
6. **Profile verbs added** ([#86](https://github.com/dungle-scrubs/browser-tools/issues/86) recovered the rules). New **Profile verbs** section: `profile list` and `profile delete NAME` against the resolved root, with strict name validation, path containment, and live-holder refusal, written down here so deleting `handle_list_profiles` and `handle_delete_profile` does not take the rules with them.
7. **`bt guide` becomes the enforced manual** ([#87](https://github.com/dungle-scrubs/browser-tools/issues/87)). New **The agent manual** section: every verb has an entry, a test fails the build when one does not, and the content contract names the refusals, the login walkthrough, the UID rule, and the endpoint rules.
8. **New Refusals and exit codes section.** The three deliberate refusal classes are named in one place, and a failed operation MUST NOT exit 0 or report a success field (defect 4).
9. **`stop --target` corrected** (defect 3). Every verb that accepts `--target SPEC` MUST resolve it through the shared target selector; `stop` MUST NOT pass its argument through as a complete target ID.
10. **Screencast rewritten** (Open Question 6). One bounded capture in one invocation; the `start` / `stop` pair is removed and the long-lived-process claim goes with it.
11. **Grammar and flag corrections** (Open Questions 7 to 10). Leading `[INSTANCE]` now normative for sub-action verbs too, `frames select PATTERN` replaces `frames select N`, `--url` is normative on `console-list`, `network-list`, and `screenshot`, and the JSON-on-stdout rule records its three exceptions.
12. **`.browser-tools.json` removed from scope.** Version 5 specified it as a launch-defaults source. Its only reader leaves with the MCP front, `--profile NAME` already serves the need explicitly, and the Introduction now records the decline with its reason.
13. **Current State gains a version 6 subsection**: what has landed, the baseline numbers, what Phase 5 still finds in the tree, and the four defects with their evidence.
14. **Migration Strategy gains Phase 5** with seven steps, their gates, the two binding orderings, and the note that 5c is not revertible by a revert. Risk Assessment replaces the MCP drift risk with the profile-destruction, deletion-scope, UID-inversion, and endpoint risks. Testing Strategy replaces the MCP contract tests with the retained contracts plus the new coverage for each step.

**Version 5** (2026-09-20): the session-layer lifecycle tools are recorded as having left the frozen MCP surface in Phase 4 (#68). Their only dispatcher had no production caller once the tool-proxy app was retired, and deleting it is the change this revision records. The `tool_registry.py` tools are unchanged, the shipped MCP front never reached the deleted path, and `launch --engine camoufox` keeps Camoufox. `browser_session.py` keeps the session resolver, which `session_store` reaches.

**Version 4** (2026-09-20): the spec caught up with the shipped CLI. Two commits had changed the surface without touching this document (229a536, ce30ad8), and six bug fixes since then changed it further (#69, #31, #32, #62, #63, #65). Nothing there reverses a decision; it records contracts the code already holds and the tests already cite. One line per change:

1. New **Focus guard** section: the refused methods, the forced-background `Target.createTarget`, `Browser.setWindowBounds` explicitly not refused (ce30ad8), the hidden-tab input refusal and its remedy, and the windowless headed launch on every launch path. Nothing in the repo recorded this as a spec; `tests/test_window_marking.py` cites "RFC-01 #48" for behavior the RFC did not describe.
2. **Window marking** rewritten: the title prefix always runs, the border and badge follow a persistent setting, `window-border on|off` writes it, running supervisors apply it within a second, `--no-window-border` stays per launch, and a fingerprint profile suppresses the border.
3. The settings file is specified: `$XDG_CONFIG_HOME/browser-tools/settings.json`, with the `~/.config` fallback and read-as-default on corruption.
4. New **Supervisor** section: it must outlive the launching shell, must record why it exited, and its absence must be visible in `status` (#65).
5. `window-border [on|off]` added to the normative verb list.
6. `--target SPEC` added to `snapshot`, `click`, and `fill`, with its meaning pinned (1-based index into page targets sorted by target ID, or a target ID prefix) so the handler and session transports cannot disagree (#62).
7. `detect` gains `--wait SECONDS` and `--no-wait`, and the presence-versus-challenge distinction is made normative (#69).
8. Registry schema extended with `supervisor_pid` and `supervisor_pid_start`, and the rule that extended fields are written from the lifecycle layer is stated, since `core/registry.py` is verbatim.

**Version 3** (2026-08-21): the five open questions were answered by the author through the Lucid review of this document, all taking the recorded recommendations; the Open Questions section records the resolutions, the body cross-references were updated in place (namespace, binary names, extras, registry location, parity corpus), and the status moved Draft to Accepted.

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
11. Current State marks `/tmp` as upstream's location and defers the merged location to Open Question 5.
12. Registry schema specified: vendored six fields plus `engine` and `profile`, with defaulted reads for old entries.
13. `wait` mechanism specified: attach-first subscription, buffering, 30 s default deadline, timeout exit behavior.
14. Launch flag drift resolved in both directions: upstream flags kept, policy flags resolved by the front, launcher listed as an adapted module.
15. Normative CLI-to-MCP mapping table added; `frames reset` included. (Deleted in version 6 with the MCP front.)
16. Upstream name derivation, collision suffixing, and instance-versus-method disambiguation adopted normatively.
17. Corruption-versus-retirement semantics pinned; `cleanup` on a corrupt file quarantines, never deletes or signals.
18. Liveness made engine-aware; Camoufox liveness via user-data-dir hold; Phase 1 gate includes a live Camoufox instance.
19. websockets floor set to >=16.0; parity comparison operator defined; profiling given a delivering section and a smoke test.

## References

**Normative**

- [Wayfinder map #82](https://github.com/dungle-scrubs/browser-tools/issues/82) - the decision map version 6 renders. Its six closed tickets hold each decision in full: [#83](https://github.com/dungle-scrubs/browser-tools/issues/83) and [#84](https://github.com/dungle-scrubs/browser-tools/issues/84) (UID mechanism and fix), [#85](https://github.com/dungle-scrubs/browser-tools/issues/85) (profile durability and the supervisor defect), [#86](https://github.com/dungle-scrubs/browser-tools/issues/86) (the MCP retirement boundary), [#87](https://github.com/dungle-scrubs/browser-tools/issues/87) (the `bt guide` checklist and two defects), [#88](https://github.com/dungle-scrubs/browser-tools/issues/88) (external endpoints).
- [#81](https://github.com/dungle-scrubs/browser-tools/issues/81) - the open implementation ticket for the profile move, carrying the fuller diagnosis.
- `.lucid/merge-plan.html` (version 5, approved 2026-08-21) - the reviewed merge plan whose decisions versions 1 to 5 render; the review record lives in `.lucid/merge-plan/`.
- `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.review-draft-2026-08-21.md` - the review version 2 answered.
- [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) - keyword semantics.
- `~/dev/chrome-agent` at v0.5.7 - the source of the vendored core.

**Informative**

- [chrome-agent repository](https://github.com/captivus/chrome-agent) - upstream home of the vendored core.
- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) - the protocol both layers speak.
- `src/browser_tools/lifecycle.py` - the layer that owns profiles, retirement, and the registry call sites the verbatim modules cannot carry.
- `~/dev/chrome-agent/scripts/cdp-wait.py` - the pattern the `wait` verb is built from.
- `docs/tool-proxy-retirement.md` - the record of the tool-proxy app's retirement, superseded in part by the MCP front retirement above.
