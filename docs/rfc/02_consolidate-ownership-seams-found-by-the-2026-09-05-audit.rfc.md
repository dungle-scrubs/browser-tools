---
number: 02
title: "CLI-only browser-tools: retire the MCP front and consolidate the remaining seams"
type: refactor
status: Implemented
author: Kevin Frilot
date: 2026-09-05
version: 3
---

# RFC-02: CLI-only browser-tools: retire the MCP front and consolidate the remaining seams

## Abstract

browser-tools is a CLI. The 2026-09-05 audit (`docs/audit-2026-09-05.md`)
found that the optional MCP front promised by RFC-01 has no shipped entry
point, that its session stack (about 5.5k lines: Daemon, McpBroker,
PageSelection, Session Tool Dispatch, Session Resolver, the persistent
controller) is reachable only through one function with one test caller,
and that most of the duplicated ownership in the package sits inside or
around that stack. The author has decided the product is CLI-only. This
RFC removes the MCP front and its stack, removes the `chrome-devtools-mcp`
Node dependency with it as RFC-01 Phase 4 already planned, and gives each
remaining concept the audit named one owner: the Named Profile, the CDP
client, process command-line reading, tool routing, target selection, and
the CDPHandler transport. It preserves working CLI capabilities, adds the
`--target`/`--url` slot and a `tool` verb for one-call handler tools, and
replaces the persistent screencast workflow with a single CLI capture
process. Camoufox automation remains a follow-up design decision.

## Introduction

### Problem statement

RFC-01 rebuilt browser-tools as a CLI over a vendored CDP core and demoted
the MCP daemon to an optional front. The audit found that the front was
never shipped: `pyproject.toml` installs `browser-tools`, `bt`, and
`browser-tools-profiler` only; `browser_session.create_tool_proxy_handlers`
is the sole entry to the session stack and its only caller is a test; the
retired tool-proxy adapter imported a module that no longer exists; README
Quick Start runs a command the parser rejects. Inside that stack the audit
found the three High defects (three Named Profile roots with no name
validation, an unvalidated repository config that can choose the browser
endpoint, a `--user-data-dir` parser that stops at the first space) and
most of the Medium ones (two CDP clients, four teardown paths, prose
interstitial results, routing sets outside `tool_registry`, a pass-through
argument threaded through six modules, a Daemon spawn that cannot see its
child exit).

Keeping the stack means fixing all of it. Removing it means the CLI keeps
what it has, the defects that live only in the stack disappear with it,
and the remaining seams are few enough to consolidate in one pass.

### Scope

**In scope:**

- Removal of the MCP front, the Daemon, the persistent-controller stack,
  the `chrome-devtools-mcp` dependency, and the tests, docs, and assets
  that exist for them.
- One owner for each concept that survives: Named Profile (Profile Root,
  Profile Name), CDP transport, process command-line reading, tool
  routing, target selection, CDPHandler transport, Interstitial Detection
  policy.
- The correctness fixes the audit found in surviving code (spaced
  user-data-dir paths, interstitial duplicate entries, JS literal
  building, supervisor process group, launcher preferences overwrite).
- CLI additions that preserve existing capabilities:
  `--target`/`--url` on every curated verb, and a `tool` verb for the
  CDP-backed tools that today have no verb; `screencast record` owns a
  complete capture in one process, as specified in section 6.
- The docs pass, version source, CI additions, and dead-code deletion.

**Out of scope, with the reason each candidate was declined:**

- Removing `core/domains` (audit L1). It is used by the profiler and is
  the typed layer a library user would reach for; the author does not
  want functionality removed. Kept as RFC-01's optional layer.
- A CLI equivalent of Project Config (`.browser-tools.json`). It supplied
  launch defaults to the MCP front only. A CLI version widens the CLI and
  is declined for this RFC; `launch` flags and environment variables are
  the launch defaults.
- A new background capture service or cross-process start/stop protocol.
  A foreground capture process serves the existing recording use case;
  service discovery, control sockets, and remote cancellation widen the
  product beyond this revision's authorized capture command.
- Driving Camoufox from the CLI. The only Camoufox driver is
  `CamoufoxSession`, an in-process Playwright session reachable only
  through MCP tools. `launch --engine camoufox` starts a browser the CLI
  cannot drive (Camoufox has no CDP port). A command channel into
  `camoufox_runner` is protocol design and is carried as Open Question 1
  for a follow-up RFC; `camoufox_session.py` is kept for that work and is
  not deleted here.
- The default-forwarded Node tools (`navigate_page`, `evaluate_script`,
  `hover`, `drag`, `press_key`, `upload_file`, `fill_form`,
  `handle_dialog`, `list_pages`, and peers). RFC-01 Phase 4 already
  specified deleting the Node dependency; the CLI equivalents are the raw
  protocol line, `console-list`, `network-list`, `screenshot`, and the
  curated verbs.
- Any change inside a verbatim vendored module. RFC-01's vendoring rules
  stand. The adapted modules (`core/launcher.py`, `core/supervisor.py`)
  MAY change under the rules RFC-01 set for them.
- Windows support and captcha solving (unchanged from RFC-01).

### Motivation

The audit's High findings H2 (repository config selects the endpoint) and
H1's traversal path are reachable today by an agent running the MCP stack
in a cloned repository. Removing the stack closes them. The remaining
defects (H3, L7, L9, L11) are small once the stack is gone, and the
remaining ownership splits (M1, M6, M8, L5, L6) are the ones that would
otherwise be re-created by the next feature.

### Context

- `docs/audit-2026-09-05.md` is the evidence base; requirements cite its
  candidate ids.
- RFC-01 is the architecture this RFC operates inside. This RFC
  supersedes RFC-01's "MCP compatibility contract" section and its
  promise of an optional MCP front, and completes its Phase 4 "Node
  dependency deleted".
- `CONTEXT.md` supplies the nouns; Migration Strategy lists the glossary
  entries that are removed and added.

## Terminology

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD
NOT, RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted
as described in RFC 2119.

Domain nouns are as defined in `CONTEXT.md` where they survive: Named
Profile, Interstitial, Interstitial Detection, CDPRuntime, One-Shot
Session. Nouns for removed concepts (Persistent Browser Session, Active
Page, MCP Subprocess, Daemon, Inspect Mode, McpBroker, PageSelection, Tool
Dispatch, LiveChrome, Session Tool Dispatch, Session Resolver, Automation
Backend) describe the old architecture and its removal.

New terms:

- **Profile Root**: the one directory under which every Named Profile's
  user-data-dir lives, for both engines.
- **Profile Name**: a validated string naming a Named Profile. Grammar
  `[A-Za-z0-9._-]+`, excluding `.` and `..`, at most 64 characters.
  `.ephemeral` is reserved, compared case-insensitively, and is not a
  Profile Name. `lifecycle` owns the reserved-name set.
- **Curated verb**: a CLI verb implemented over `CDPHandler`
  (`snapshot`, `click`, `fill`, `wait-idle`, `wait-stable`, `detect`,
  `frames`, `storage`, `screencast`) or over One-Shot Session
  (`screenshot`).
- **Handler tool**: one of the eighteen CDP-backed tools `CDPHandler`
  implements, whether or not a curated verb fronts it.
- **Capture**: one `screencast record` invocation, from connection through
  recording, finalization, and disconnection. Its runtime and buffered
  frames belong to that process only.
- **Target slot**: the `--target SPEC` / `--url SUBSTRING` pair that
  selects a page target, as defined for the raw protocol line in RFC-01.
- **Owner**: the one module that defines a concept's data and interface.

## Current State

Two fronts share one package. The CLI front (`cli`, `lifecycle`,
`one_shot`, `passthrough`, `events`, `list_verbs`, `curated`) runs over
the vendored `core/` and over `CDPHandler` for curated verbs. The MCP
front (`browser_session`, `session_store`, `persistent_browser`,
`daemon_supervisor`, `mcp_daemon`, `mcp_broker`, `daemon_client`,
`page_selection`, `session_reaper`, `profile_catalog`, `live_chrome`,
`automation_backend`, `chrome_config`, `chrome_utils`, `browser_state`,
`session_layout`, `project_identity`, `mcp_session`) runs a Daemon that
multiplexes a `chrome-devtools-mcp` Node subprocess and a second CDP
client. `CDPHandler`, `interstitial`, `screencast`, `screenshot_utils`,
`frame_manager`, `native_snapshot`, `native_interaction`, and
`tool_registry` are shared by both.

| Concept | Owners today | Audit id | Under CLI-only |
|---|---|---|---|
| Named Profile | `session_layout.profile_dir` (`~/.cache/tool-proxy/browser-tools/profiles`), `lifecycle.profile_user_data_dir` (`/tmp/browser-tools-profiles`), `camoufox_session` storage state; validation only on delete | H1 | one root, one grammar, in `lifecycle` |
| Project Config validation | two of three entry points | H2 | removed with the stack |
| Process command line | `process_utils` regex `\S+` | H3 | one argv reader |
| MCP Subprocess version, page-list format | `@latest` in three files; two regex parsers | H4 | removed with the dependency |
| MCP front entry | none shipped | H5 | removed; docs rewritten |
| CDP client | `cdp_client` and `core/cdp_client` | M1 | `core/cdp_client` only |
| Chrome teardown | six paths | M2 | `lifecycle._terminate_verified` and `core/registry.stop` remain; they already verify identity |
| Interstitial result | prose over the socket; three detectors | M3 | `curated.detect` already returns the structured dict; L7 fixes remain |
| Daemon spawn, transport errors, pass-through args | | M4, M5, M7 | removed with the stack |
| Routing data | `tool_registry` plus six local sets | M6 | one handler table; parser-derived verb lists |
| Curated verb target | tab 0, fixed | M8 | target slot on every curated verb |
| Output paths | resolved against the Daemon's cwd | M9 | curated verbs run in the caller's process; cwd is correct; cookie masking remains |
| Tests, CI | Daemon path under-tested; format not checked; pyright not on tests | M10 | Daemon tests deleted; CI additions remain |
| Docs, version | describe the pre-RFC code | M11 | rewritten for CLI-only |
| Dead code, script mode | L1, L2, L3 | | deleted |
| Target slot mapping, CDPHandler forwarders, JS literals, supervisor, launcher | L5, L6, L11, L9 | | consolidated |

## Proposed Changes

### 1. Remove the MCP front and its stack (H2, H4, H5, M2 to M5, M7, M9, L2, L3, L4, L10, L12, L13)

- The following modules MUST be deleted: `browser_session.py`,
  `session_store.py`, `persistent_browser.py`, `daemon_supervisor.py`,
  `mcp_daemon.py`, `mcp_broker.py`, `daemon_client.py`, `mcp_session.py`,
  `page_selection.py`, `session_reaper.py`, `profile_catalog.py`,
  `live_chrome.py`, `automation_backend.py`, `chrome_config.py`,
  `chrome_utils.py`, `browser_state.py`, `session_layout.py`,
  `project_identity.py`, `cdp_client.py`, `attach_chrome.sh`,
  `persistent-session-template.mjs`.
- Deletion MUST land in the same merge as sections 4 through 7. The
  implementation MUST convert surviving client and error imports, wire
  the handler runtime, and provide the complete CLI Capture before
  deleting their old dependencies. No intermediate broken state is a
  mergeable phase.
- `camoufox_session.py` MUST be kept, marked in its module docstring as
  the Camoufox driver reserved for Open Question 1, and excluded from
  the wheel's console scripts. It MUST NOT be imported by any module in
  the package until that RFC lands.
- The `README.md` requirement for Node.js MUST be removed. No module MAY
  spawn `npx`.
- `browser_tools.__init__` MUST export only `CDPClient`, `CDPError` (from
  `core`), `FrameManager`, and `__version__`. The lazy-export layer MUST
  be removed; nothing heavy remains behind it.
- Tests that exist only for deleted modules MUST be deleted:
  `test_attach_browser.py`, `test_auth_identity.py`,
  `test_automation_backend.py`, `test_browser_tools.py`,
  `test_close_and_real_mode.py`, `test_dispatch.py`,
  `test_e2e_backend_seam.py`, `test_inspect_mode.py`,
  `test_live_chrome.py`, `test_mcp_daemon.py`,
  `test_mcp_surface_contract.py`, `test_page_selection.py`,
  `test_persistent_browser.py`, `test_profile_liveness.py`,
  `test_profiles.py`, `test_session_dispatch.py`,
  `test_session_lifecycle.py`. `test_camoufox.py` and
  `test_e2e_camoufox.py` MUST be kept with `camoufox_session.py`.
  `test_new_tools.py` MUST keep its `CDPHandler` cases and drop its
  Daemon dispatch cases.
- `CONTEXT.md` entries for the removed concepts MUST be removed (see
  Migration Strategy).

### 2. Named Profile: one Profile Root, one Profile Name (H1)

`lifecycle` is the Owner of profile location and naming.

- `lifecycle` MUST expose `validate_profile_name(name) -> str` that
  returns the name unchanged when it matches the Profile Name grammar,
  is not reserved, and raises `LifecycleError` (CLI exit 1) otherwise.
  `launch --profile` MUST
  call it before any directory is created.
- The Profile Root default MUST be `~/.cache/browser-tools/profiles`.
  `BROWSER_TOOLS_PROFILES_DIR` MUST override it. `DEFAULT_PROFILES_ROOT`
  (`/tmp/browser-tools-profiles`) MUST be removed.
- Every directory created under the Profile Root, including
  `.ephemeral` Camoufox session dirs, MUST be created with mode `0o700`.
- `.ephemeral` MUST be used only as the container for unnamed Camoufox
  sessions. Validation and migration MUST use the same reserved-name set;
  a reserved directory MUST NOT be treated as a Named Profile. An old
  `.ephemeral` container MUST remain in place for its existing sessions.
- `profile_user_data_dir` MUST be the only function that joins a Profile
  Name onto a path.
- The registry stays at `/tmp/chrome-agent/registry.json` per RFC-01
  Question 5; this RFC does not move it.

### 3. Process command lines: one argv reader (H3)

`process_utils` is the Owner of reading another process's command line.
Its surviving consumers are `lifecycle._camoufox_is_alive`
(`pid_holds_user_data_dir`) and `lifecycle.launch`
(`clean_stale_singleton_lock`).

- `process_utils` MUST expose `read_process_args(pid) -> list[str] |
  None` returning the full argument vector: `sysctl kern.procargs2` on
  darwin and `/proc/<pid>/cmdline` on Linux. If exact argument boundaries
  cannot be recovered, it MUST return None. `ps -ww -o command=` remains
  a diagnostic fallback through `read_process_command`; its flattened
  text MUST NOT establish profile idleness or an exact directory match.
- `find_chrome_user_data_dir` MUST read the `--user-data-dir=` value from
  that vector so a value containing spaces is returned whole.
  `_USER_DATA_DIR_PATTERN` and `_DEBUG_PORT_PATTERN` MUST be removed.
- Functions with no surviving caller (`validate_local_endpoint`,
  `resolve_chrome_executable`, `resolve_system_profile_dir`,
  `build_browser_command`, `find_free_port`, `wait_for_devtools`,
  `is_devtools_available`, `enumerate_tabs`, `select_tab_by_url`,
  `find_chrome_debug_port`, `find_listeners_on_port`,
  `read_process_start_time`, `terminate_process`) MUST be deleted.
  `is_process_alive`, `read_singleton_lock_pid`,
  `clean_stale_singleton_lock`, `read_process_command` (as the fallback),
  `pid_holds_user_data_dir`, and `terminate_process_and_wait` remain.
- A regression test MUST cover a user-data-dir containing a space.

### 4. One CDP client (M1)

`core/cdp_client.CDPClient` is the Owner of CDP transport.

- `CDPRuntime` MUST connect through `core.cdp_client.CDPClient` at the
  browser level and MUST attach a flattened `Target` session to the
  resolved page target, using the same sequence One-Shot Session uses.
- `CDPRuntime` MUST expose `send(method, params=None) -> dict` bound to
  its session id so `cdp_handler`, `screencast`, `interstitial`, and the
  native engines keep their call shape.
- `cdp_handler._safe_cdp_send` MUST map `core.errors.CDPError` and
  `ConnectionError`. `_get_cdp_error_class` and
  `screencast._cdp_error_class` MUST be removed.
- Frame and execution-context events MUST be subscribed with the session
  id filter `core/cdp_client` provides.
- Alternate local imports for script-mode execution in `cdp_handler`,
  `screencast`, and `cdp_constants` MUST be removed. `cdp_constants`
  MUST import its screenshot constants without a fallback.
- Guards for optional third-party dependencies MUST remain. In
  particular, `camoufox_session` MUST remain importable without Camoufox,
  and `camoufox_runner` MUST retain its missing-extra installation message
  and exit 3. The cleanup MUST NOT make Camoufox a base dependency.

### 5. CDPHandler transport and target selection (M8, L5, L6)

`one_shot` is the Owner of target resolution. `curated` owns the handler
transport.

- `one_shot.target_slot(target, url) -> tuple[str | None, str | None]`
  MUST be the one mapping from the flag pair to `(spec, by)`.
  `passthrough`, `events`, `list_verbs`, and `curated` MUST call it;
  `events._target_slot` MUST be removed.
- `one_shot` MUST expose `resolve_page_target(port, spec, by) -> str`
  (the list-sort-resolve part of `one_shot_page_session`) so `curated`
  can hand a target id to `CDPRuntime`.
- Every curated verb MUST accept `--target SPEC` and `--url SUBSTRING`
  with the raw-line semantics. With neither flag, resolution MUST follow
  `core.attach.resolve_target`'s rule: one page target selects itself,
  more than one is `AmbiguousTargetError` (CLI exit 1 naming the
  targets). This replaces today's silent tab 0.
- `curated._cdp_handler_session` MUST construct a `CDPRuntime` for the
  resolved target and `CDPHandler(runtime)`. The seven forwarding members
  on `CDPHandler` (`available`, `mode`, `run`, `stop`,
  `await_paint_ready`, `run_post_navigation_detection`, `_cdp_or_error`)
  and its four private read-through properties MUST be removed; handlers
  reach the runtime through the `runtime` attribute.
- `CDPRuntime` and `CDPHandler` MUST drop the `mode` and `stealth`
  parameters. Inspect Mode was an MCP-front policy and does not survive.

### 6. Handler tools and a complete CLI Capture (fit-checked)

Handler tools without a curated verb today: `ax_find`, `ax_node`,
`export_pdf`, `screenshot_element`, `get_text`, `get_html`, `get_attr`,
`element_exists`, `element_visible`, `get_frame_events`. Removing the MCP
front would otherwise remove them.

- The CLI MUST provide `tool [INSTANCE] NAME ['{...json arguments...}']
  [--target SPEC] [--url SUBSTRING]` that dispatches `NAME` through
  `CDPHandler.call_tool` with the parsed arguments and prints the
  envelope's text on stdout as JSON `{"result": "<text>"}`; an envelope
  with `isError` MUST exit 1 with the text on stderr.
- `NAME` MUST be validated against the handler table before any
  connection is opened; an unknown name MUST be a usage error (exit 2)
  that lists the names available through `tool`. Arguments MUST be a JSON
  object, default `{}`; malformed JSON or a non-object MUST exit 2 before
  connection.
- `tool` MUST reject `screencast_start` and `screencast_stop` before
  connection with exit 2 and a remedy naming `screencast record`. These
  are runtime operations used by Capture, not independent one-call
  workflows. Their table entries MUST declare `requires_capture: true`;
  all other entries MUST declare false. No second name list is permitted.
- The existing curated verbs remain; `tool` does not replace them.
- `export_pdf`, `screenshot_element`, and `screencast_stop` resolve
  relative paths against the CLI process's cwd, which is the caller's.
  No cwd handling is needed (audit M9 was a Daemon defect).
- Fit check: users are agents and humans driving a CDP browser from the
  CLI; the product is browser automation and debugging from the command
  line; the verb keeps ten existing capabilities reachable; scope holds.

#### Capture command and lifetime

```
bt screencast record [INSTANCE] --dir DIR [--duration SECONDS] [--format FORMAT] [--max-frames COUNT] [--target SPEC] [--url SUBSTRING]
```

- Fit check: agents and humans record page behavior while another CLI
  invocation drives the browser. This serves the stated automation and
  debugging purpose. Scope widens the CLI surface to preserve an existing
  recording capability; the author authorized the recommended single
  capture command when requesting this revision. It creates no service.
- `curated` MUST own Capture orchestration. It MUST keep one CDPRuntime,
  attached target session, and ScreencastRecorder alive from start through
  finalization. Other CLI processes MAY interact with that browser while
  Capture runs. Frame storage and writing remain owned by `screencast`.
- `--dir` is required and resolves against the caller's cwd. It MUST be
  absent or an empty directory; Capture MUST reject existing nonempty
  output before connecting. Format MUST be `jpeg` or `png`.
  Default format is `jpeg`; default maximum is
  600 frames. Duration, when supplied, MUST be a finite positive number
  of seconds measured from recording readiness; maximum frames MUST be
  a positive integer. Invalid options MUST exit 2 before connection.
- States are connecting, recording, finalizing, and finished. After
  attachment and successful recorder start, Capture MUST enter recording
  and print `Recording; send SIGINT or SIGTERM to this process to finish.`
  on stderr. The caller can wait for that line before driving the page.
  A failed connection or start MUST clean up and exit 1 without a success
  manifest. A stop signal while connecting MUST cancel startup, clean up,
  and exit 1 because no Capture became ready.
- While recording, the first SIGINT or SIGTERM, elapsed duration, or
  maximum-frame count MUST enter finalizing exactly once. Without duration,
  Capture waits for a signal or its frame limit. Repeated stop signals
  during finalization MUST NOT restart it or discard buffered frames.
- Finalization MUST stop capture best-effort, unsubscribe its frame
  listener, and write timestamped frame files and `frames.json` using the
  recorder's existing manifest format. The stop request MUST have a
  five-second deadline so a lost connection cannot prevent writing.
  Runtime detach and close MUST run after writing, including on failure,
  with a combined five-second best-effort cleanup deadline.
  A normal stop with at least one written frame MUST exit 0 and print
  `{"result":"<recorder text>"}` on stdout. No progress goes to stdout.
- Target closure or connection loss during recording MUST finalize the
  frames already received and exit 1 with a diagnostic on stderr. Zero
  frames MUST exit 1, even after a requested normal stop. A write failure
  MUST exit 1 and report the output directory; a success manifest MUST
  be published only after every referenced frame has been written.
  Partially written files MUST remain available for inspection, without
  being reported as a successful Capture. SIGKILL cannot finalize data.
- `screencast start` MUST remain a parser alias for `record`, including
  the required `--dir`. Its existing format and frame-limit flags remain.
  `screencast stop` MUST remain recognized but exit 2 before connection
  with instructions to signal the owning capture process. It MUST NOT
  connect a fresh runtime or report that it stopped another process.
  This supersedes RFC-01's split start/stop workflow, which never shared
  recording state across CLI invocations.

### 7. Routing data in one table (M6)

- `tool_registry.py` MUST be reduced to the one table `CDPHandler` reads:
  tool name to handler method, with the `navigation` flag for the tools
  that trigger post-call Interstitial Detection in `curated`, and
  `requires_capture` as specified in section 6. The
  `interaction`, `inspect_blocked`, `inspect_warn`, `page_selecting`,
  `screenshot_gate`, and `single_tab` flags and their derived sets MUST
  be removed with the policies that read them. `cdp_handler._CDP_HANDLERS`
  and the parity `assert` MUST be replaced by that table.
- `cli._KNOWN_VERBS` and `cli._CURATED_COMMANDS` MUST be derived from the
  parser's registered subcommands, not hand-listed.
- `curated` MUST import its `wait-idle`/`wait-stable` defaults from
  `cdp_handler` constants.
- `lifecycle.guide_text` MUST document every registered verb; a test MUST
  fail when one is missing.

### 8. Interstitial Detection (M3, L7)

`interstitial` stays the Owner of detection policy.

- `detect_with_retry` MUST return each detection once when retries are
  exhausted. `format_interstitials` MUST compute the wait from
  `INTERSTITIAL_RETRY_DELAY_SECONDS`. `_run_detection` MUST catch
  `core.errors.CDPError` and return an empty list for that pass.
- The override script path MUST be `~/.config/browser-tools/detect-interstitial.js`.
  The `tool-proxy` path SHOULD be read for one release with a warning.
- `curated.detect` keeps returning the structured dict. When Open
  Question 1 lands, the Camoufox driver MUST run
  `interstitial.get_detection_script()` through `page.evaluate` and MUST
  NOT keep a separate detector; `CamoufoxSession._detect_interstitial`
  is deleted in that RFC.

### 9. Storage output (M9, decided)

- `bt storage get` MUST mask cookie values as `<len n>` unless
  `--reveal-values` is passed. `CDPHandler._handle_get_frame_storage`
  MUST take a `reveal_values` argument, default false.

### 10. Small seams (L8, L9, L11, L14)

- `one_shot.cli_cdp_errors` MUST rewrite `InstanceNotFoundError` text so
  the remedy reads `bt launch`, as `connection_failure_message` does for
  `ConnectionError`.
- `core/supervisor.spawn_supervisor` (adapted module) MUST pass
  `start_new_session=True`.
- `core/launcher.launch_browser` (adapted module) MUST write
  `Default/Preferences` only when the file does not exist.
- `cdp_handler` MUST build JavaScript string literals with `json.dumps`
  through one helper at the five `!r` sites.
- `src/browser_tools/py.typed` MUST exist, matching the `Typing :: Typed`
  classifier. `.lucid/` and `assets/` MUST be either tracked or ignored.
- `daemon_client`-style docstrings that mention deleted modules cannot
  survive; `interstitial.py`'s and `native_snapshot.py`'s references to
  `chrome-devtools-mcp` as a baseline MUST be reworded to name the parity
  corpus.

### 11. Docs and version (M11)

- README MUST be rewritten for a CLI-only product: Quick Start commands
  MUST pass `cli.build_parser()`; the Architecture section MUST list
  `cli`, `lifecycle`, `one_shot`, `passthrough`, `events`, `list_verbs`,
  `curated`, `cdp_handler`, `interstitial`, `native_snapshot`,
  `native_interaction`, `screencast`, `screenshot_utils`,
  `frame_manager`, `process_utils`, `camoufox_runner`, `profiler`,
  `core/`; the "Project Configuration" and "Keeping login state" sections
  MUST describe `launch --profile` and the Profile Root only.
- CONTRIBUTING "Architecture" MUST be rewritten to match; its "No circular
  imports" claim becomes true with section 1 and MUST be kept.
- `browser_tools.__version__` MUST be read from package metadata.
- `SECURITY.md` MUST name the supported version line that matches
  `pyproject.toml`. `CHANGELOG.md` is generated by the release process and
  MUST NOT be edited by hand.
- `docs/tool-proxy-retirement.md` MUST be updated to say the MCP front no
  longer exists.
- RFC-01's "Window marking" text is superseded by PR #55; parameter names
  (`draw_border`, `window_border`, `--no-window-border`) MUST stay for
  compatibility.

### Error handling

```
E010 - Invalid profile name (severity: warning)
       Raised by lifecycle.validate_profile_name as LifecycleError;
       CLI exit 1 naming the grammar.
E015 - Unknown handler tool (severity: warning)
       Raised by the tool verb as UsageError; CLI exit 2 listing names.
E016 - Handler tool error (severity: warning)
       An isError envelope from CDPHandler; CLI exit 1, text on stderr.
E017 - Profile migration deferred or failed (severity: warning)
       Report the name and reason on stderr; leave its source in place.
       If it is the requested profile, exit 1 without launching a new copy.
E018 - Capture failed or incomplete (severity: warning)
       CLI exit 1; preserve received frames where writing succeeds and
       report the output directory. Do not print success on stdout.
E019 - Invalid Capture invocation (severity: warning)
       CLI exit 2 before connection, including standalone tool start/stop
       and the retired split screencast stop workflow; name the remedy.
AmbiguousTargetError / TargetNotFoundError (existing)
       Now reachable from every curated verb; CLI exit 1 via
       cli_cdp_errors.
```

Retry policy is unchanged: `detect_with_retry` keeps its three retries;
the screenshot blank-frame guard keeps its retry budget.

## Migration Strategy

1. **Profile Root move.** When `BROWSER_TOOLS_PROFILES_DIR` is set,
   migration MUST be skipped and that root MUST continue to be used.
   Otherwise each `launch --profile` MUST retry eligible directories
   under `/tmp/browser-tools-profiles`, not just the first launch.
   `lifecycle` MUST serialize migration and named launch operations with
   an exclusive per-user lock at
   `~/.cache/browser-tools/profile-lifecycle.lock`. Its parent MUST be
   private and the file MUST be mode `0o600`; lock acquisition MUST fail
   with exit 1 after ten seconds. The lock MUST cover eligibility checks,
   moves, and the requested launch through registration. Named stop and
   cleanup operations MUST use the same lock when changing profile state.

   A candidate MUST be a real directory owned by the current user, pass
   `validate_profile_name`, and have no existing destination entry of any
   type. Symlinks and reserved names, including `.ephemeral` in any letter
   case, MUST be left in place and reported. Source and destination
   parents MUST be owned by the current user and MUST NOT be symlinks or
   writable by other users. A failed safety check MUST defer migration.

   Before each move, `lifecycle` MUST check registry process identities,
   singleton locks, and process arguments for holders of that source
   path, including unregistered processes. Live or uncertain holders
   MUST cause deferral; missing permissions or unavailable process
   inspection MUST NOT count as proof of idleness. The exact-argv reader
   from section 3 is a prerequisite. Checks MUST be repeated immediately
   before the move while holding the lifecycle lock. No live registry
   entry or process argument is rewritten. Stale registry entries remain
   subject to the existing lifecycle cleanup rules, not migration.

   The source directory's permissions MUST be restricted to `0o700`
   before rename; a permission-change failure MUST defer the move.
   Eligible directories MUST move by an atomic, non-overwriting rename
   on the same filesystem; migration MUST NOT fall back to copy-and-delete.
   A cross-filesystem destination, collision, or rename failure MUST
   leave the source at its original path and report the reason. Each completed
   move MUST be printed on stderr. A crash after rename is recovered by
   recognizing the destination on the next invocation, without a separate
   migration-complete flag. Deferred candidates remain eligible for retry.

   If the requested profile still exists only at the old root and cannot
   migrate, launch MUST exit 1 and MUST NOT create a second empty profile
   at the new root. If both roots contain the name, the destination wins,
   both copies remain, and the collision MUST be reported. A live or
   uncertain old-root holder of that name MUST still block launch at the
   destination. An unrelated deferred profile MUST NOT block an otherwise
   safe requested launch.

   Old `.ephemeral` sessions MUST retain their paths until stopped by
   normal lifecycle handling; migration MUST NOT sweep or move them.
   Directories under the MCP-front root
   (`~/.cache/tool-proxy/browser-tools/profiles`) MUST remain untouched.
   README MUST distinguish these retained data from migrated CLI profiles
   and explain that deleting them discards their login state. README MUST
   require users to stop concurrent old-version launch commands before
   upgrading: older code and external browser launchers do not participate
   in the new lifecycle lock. The automatic migration does not claim to
   coordinate with an external process that starts after the holder check.
2. **State files of the removed stack** (`~/.cache/tool-proxy/browser-tools/*.json`,
   `*.sock`, `*.daemon.pid`, `*.lock`) are not read by any surviving
   code. A running Daemon from a previous version keeps running until its
   own 30-minute idle timeout quits the Chrome it owns.
3. **CLI surface.** Additions: `--target`/`--url` on curated verbs, the
   `tool` verb, `--reveal-values` on `storage get`, and `screencast record`.
   Behavior change: a curated verb on an instance with several tabs now fails naming them
   instead of acting on tab 0. Screencast start becomes a foreground
   capture requiring `--dir`; stop is a diagnostic for the retired split
   workflow. Existing command names and flags remain recognized, but the
   split recording semantics are superseded as specified in section 6.
4. **Glossary.** `CONTEXT.md` MUST drop Persistent Browser Session,
   Active Page, MCP Subprocess, Daemon, Inspect Mode, McpBroker,
   PageSelection, Tool Dispatch, LiveChrome, Session Tool Dispatch,
   Session Resolver, Automation Backend, and the Mode vocabulary; MUST
   add Profile Root, Profile Name, Curated verb, Handler tool, Capture, Target
   slot; and MUST reword CDPRuntime and Interstitial Detection to drop
   the Daemon references.
5. **Rollback.** Each phase is one merge commit and reverts cleanly. The
   profile migration moves directories rather than copying, so a revert
   plus a manual move restores the old layout. Rollback MUST stop affected
   browser instances and Capture processes first; profile moves back MUST
   reject collisions and MUST NOT overwrite the old-root copy.

## Security Considerations

Profile directories contain login state. Name validation does not establish
filesystem ownership or idleness: Migration Strategy supplies the separate
ownership, symlink, holder, and lock checks. Migration MUST NOT kill a holder
to make progress. The lock coordinates this version's lifecycle operations;
it does not authenticate or control older or external launchers.

Capture artifacts can contain page content and credentials visible on screen.
Capture MUST create new output directories with mode `0o700` and frame and
manifest files with mode `0o600`, refuse symlink output entries, and avoid
overwriting existing files. An output conflict encountered after recording
starts MUST fail finalization without replacing the conflicting entry.
Artifact permissions do not change the browser's CDP trust model. Raw CDP
and handler calls retain the caller's existing browser access; cookie masking
is an output default, not an access-control boundary.

## Risk Assessment

- **Removed capabilities.** The MCP tool surface and the in-process
  Camoufox tools are gone. The author accepted the first; the second is
  Open Question 1 and is the one capability this RFC cannot preserve
  without new design. Blast radius: any harness that still speaks MCP to
  browser-tools stops working; the audit found none in this repository.
- **CDP client swap (section 4).** Highest-churn surviving change. The
  frame-event subscriptions and the screencast ack path depend on event
  routing with a session id. Mitigation: `test_new_tools.py`'s handler
  cases and the parity suite run against the `core` client first.
- **Ambiguous target on curated verbs.** Agents that relied on tab 0 with
  several tabs open now get exit 1 with the target list. This is the
  raw-line behavior RFC-01 chose; the message names the fix.
- **Profile migration.** A name present in both roots keeps the Profile
  Root copy and is reported; live or uncertain holders block relocation
  and duplicate launch. Unrelated deferred profiles do not block launch.
  Cross-filesystem moves require manual handling. Concurrent legacy or
  external launches are outside the cooperative lock, as disclosed above.
- **Capture lifetime.** Recording survives while its CLI process survives.
  A normal stop writes frames before disconnecting; connection loss produces
  an incomplete result and exit 1. SIGKILL or a process crash can lose
  buffered frames. The artifact gate tests the complete CLI workflow.
- **Large deletion.** About 5.5k source lines and 17 test files. The
  risk is a surviving import of a deleted name; `pyright src/` and the
  test run catch it at the phase gate.

## Testing Strategy

- The surviving suite MUST pass at every phase gate.
- New tests:
  - Profile Name grammar (accept; reject `..`, `/`, 65 chars, and
    `.ephemeral` in mixed letter case); migration from a temp source root;
    `0o700` on created and migrated dirs. Reserved containers stay put.
  - Migration defers live registered and unregistered holders, uncertain
    inspection, symlinks, unsafe ownership, and cross-filesystem moves.
    Launching another profile leaves a live profile's path and registry
    unchanged; after its holder exits, the next launch migrates it.
    A deferred requested profile does not create a fresh destination.
    Test collisions, concurrent cooperating launches, lock timeout,
    interrupted migration retry, and environment-override bypass.
  - Spaced `--user-data-dir` through `read_process_args` on the host
    platform plus a recorded-vector unit test;
    `pid_holds_user_data_dir` true for it.
  - `target_slot` and `resolve_page_target` parity with `passthrough`;
    every curated verb with `--target`, `--url`, and the ambiguous case.
  - `tool` verb: unknown name exit 2, error envelope exit 1, arguments
    parsed and forwarded, relative `path` resolved against cwd;
    malformed/non-object arguments and capture-only tools rejected before
    connection using table metadata.
  - Handler table: `bt guide` names every parser verb; `_KNOWN_VERBS`
    equals the parser's choices; the table is the only place a tool name
    maps to a handler.
  - Interstitial: each detection once after exhausted retries; delay from
    the constant; CDP error in one pass returns empty for that pass.
  - Cookie masking default and `--reveal-values`.
  - CDP client swap: every surviving `cdp_handler` test passes with a
    `core` client fake; frame events filtered by session id; screencast
    ack goes to the session.
  - Capture gate against a disposable browser page: start the actual CLI,
    wait for its readiness line, drive the page from a second CLI process,
    send SIGINT to the Capture process, and verify exit 0, a nonempty
    manifest, and decodable frame files. Repeat completion by duration
    and frame limit. This gate MUST run before the removal merge and
    MUST NOT count a skipped browser test as success.
  - Capture failure tests: invalid options, nonempty/symlink output,
    signal during startup, repeated signals, target closure, transport
    loss, stop timeout, zero frames, and write failure; verify cleanup,
    exit status, and no false success. Test start alias and stop diagnostic.
  - A base install without the Camoufox extra imports successfully;
    optional session import works, and the runner prints the installation
    remedy and exits 3 without a traceback.
  - `spawn_supervisor` process group; `Preferences` not overwritten on a
    second launch.
- CI MUST add `ruff format --check src/ tests/`; the 29 differing files
  MUST be formatted in phase 1.
- `pyright tests/` SHOULD be added to CI once its error count reaches
  zero; the deletion in phase 1 removes most of the 1265 errors, and each
  later phase SHOULD reduce the count for the files it touches.
- Coverage of `core/attach`, `core/registry`, `screenshot_utils`, and
  `process_utils` SHOULD reach 75 % by the end of phase 4 (audit M10).

## Implementation Plan

Five phases. Each phase is one merge; each gate is "surviving suite
green, new tests for the phase green, docs for the phase updated".

1. **Transport, CLI workflows, and removal (L).** Sections 1, 4, 5, 6,
   and 7; section 3's deletions; CI format check. In the working tree,
   convert the runtime and error adapters to the core client, integrate
   target resolution and the handler table, and build `tool` and Capture
   before deleting the old client, front, and dependencies. These changes
   MUST merge together. Closes H2, H4, H5, M1 to M8, M9 (path part),
   L2 to L6, L10, L12, L13. Gate: `pyright src/` clean; no surviving
   import of a deleted name; no `npx` in source; wheel contains no
   `.mjs`/`.sh`; handler tests and parity suite pass with the core client;
   Capture artifact gate passes; target, tool, guide, and missing-extra
   tests pass. `cdp_client.py` is deleted only at this gate.
2. **Profile Root and argv reader (S).** Sections 2 and 3, migration
   step 1. Closes H1, H3. Gate: spaced-path test green; `launch --profile
   ../x` exits 1; reserved-name, live-holder deferral/retry, collision,
   concurrent-launch, interrupted-migration, and override tests pass.
3. **Detection and storage (S).** Sections 8 and 9. Closes L7 and M9
   (cookie output). Gate: detection retry/error tests and cookie masking
   with explicit reveal pass through the CLI.
4. **Small seams (M).** Section 10. Closes L8, L9, L11, L14. Gate:
   error-remedy, process-group, Preferences preservation, and JS literal
   tests pass; the built wheel contains `py.typed`.
5. **Docs and version (S).** Section 11, migration steps 4 and 5. Closes
   M11. Gate: README Quick Start runs on a clean install.

Sizes: S under a day, M one to three days, L up to a week.

## Open Questions

1. **Driving Camoufox from the CLI.** After section 1 the CLI can launch
   Camoufox (`launch --engine camoufox`) but cannot navigate, click,
   fill, snapshot, wait for a human, or read cookies in it; those existed
   only as MCP tools over `CamoufoxSession`. Options: (a) a follow-up RFC
   adds a command channel (Unix socket, newline JSON) into
   `camoufox_runner` and routes the curated verbs to it when the
   instance's engine is `camoufox`, reusing `CamoufoxSession` as the
   driver; (b) accept launch-only Camoufox. Needed to decide: whether
   Camoufox automation is used. Recommendation: (a), scoped as its own
   protocol RFC, because the author does not want functionality removed
   and the driver already exists. User decision.
2. **Cache directory name.** Decided by the author's request for an
   opinion: the Profile Root moves to `~/.cache/browser-tools/profiles`
   (section 2). The `tool-proxy` path segment named a retired
   integration, the only remaining state under it is profiles, and one
   migration is cheaper than two. Recorded as the author's delegated
   choice.
3. **Profile Name grammar limits.** 64 characters and `[A-Za-z0-9._-]`
   are the audit's suggestion. Needed to decide: whether any existing
   profile name under `/tmp/browser-tools-profiles` fails it. The
   migrator reports such names; the author can widen the grammar before
   phase 2 if any appear. Machine-made default.

Previously resolved (answers of 2026-09-05):

- The product is CLI-only; the MCP front is removed (was Open Question 1
  of version 1).
- `--target`/`--url` on every curated verb: yes (was Open Question 3).
- `core/domains` stays (was Open Question 4).
- Cookie values masked by default with `--reveal-values` (was Open
  Question 5; delegated to the drafter).
- Pinning `chrome-devtools-mcp`: moot, the dependency is removed (was
  Open Question 6; delegated to the drafter).

## Revision 3 response to review 2

This revision answers
`docs/rfc/02_consolidate-ownership-seams-found-by-the-2026-09-05-audit.review-2.md`.
The author requested the revision and the recommended single-process capture
workflow. Status remains Draft; the Camoufox follow-up decision remains open.

1. **Deletion order:** sections 1 and 4 through 7 now land together in
   phase 1; surviving imports and working Capture are removal gates.
2. **Screencast preservation:** section 6 specifies one foreground Capture,
   stop triggers, artifact/error behavior, legacy command diagnostics, and
   a real CLI artifact gate. One-call tools cannot pretend to share state.
3. **Live-profile migration:** migration now defers live/uncertain holders,
   coordinates supported lifecycle operations, retries deferred moves, and
   refuses duplicate launch and unsafe/cross-filesystem relocation.
4. **Reserved profile namespace:** `.ephemeral` is reserved in validation
   and migration, including letter-case variants, and old sessions stay put.
5. **Optional imports:** section 4 removes only alternate local import paths;
   Camoufox's optional dependency handling and missing-extra tests remain.

## References

**Normative**

- `docs/audit-2026-09-05.md` - the findings this RFC answers.
- `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md` -
  architecture, vendoring rules, CLI surface, packaging rules this RFC
  operates inside; its MCP compatibility contract is superseded here.
- `CONTEXT.md` - domain vocabulary.
- [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) - requirement
  keywords.

**Informative**

- `docs/rfc/02_consolidate-ownership-seams-found-by-the-2026-09-05-audit.review-2.md`
  - the five findings answered by revision 3.
- `docs/tool-proxy-retirement.md` - the external adapter that was the
  MCP front's only consumer.
- `docs/rfc/01_...review-draft-2026-08-21.md` - the review that shaped
  RFC-01.
- [Chrome DevTools Protocol: Target domain](https://chromedevtools.github.io/devtools-protocol/tot/Target/)
  - the flattened-session attach section 4 relies on.
