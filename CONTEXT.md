# browser-tools - domain glossary

Canonical names for the concepts in this codebase. Architecture reviews
and code should use these terms, not ad-hoc synonyms.

## Domain nouns

- **Persistent Browser Session** - a long-lived Chrome instance reused
  across CLI/MCP invocations, identified by a session key and backed by
  an on-disk user-data-dir. State is persisted in a `BrowserState` file.
- **Named Profile** - a login-bearing user-data-dir selected by name,
  persisted across restarts and unaffected by headed/headless switches.
- **Active Page** - the currently-selected browser tab. Page IDs are not
  stable across MCP session restarts, so the Active Page is tracked by
  URL and re-resolved to a fresh ID on each call. The implementation
  module is `PageSelection`.
- **Interstitial** - an anti-bot challenge page (Cloudflare, DataDome,
  Akamai, PerimeterX, Imperva, AWS WAF). Detected by multi-signal
  heuristics after navigation; some types auto-retry.
- **MCP Subprocess** - the upstream `chrome-devtools-mcp` Node process
  that owns snapshot-based page automation. browser-tools talks to it
  over JSON-RPC on stdio.
- **Daemon** - the long-lived Unix-socket broker process that keeps the
  MCP Subprocess and CDP client alive between invocations so listeners
  accumulate across a session.
- **Inspect Mode** - a read-only access mode that refuses page-mutating
  tools. Which tools are blocked is a `tool_registry` flag, not a
  hand-maintained set.

## Module names (architecture)

- **McpBroker** - the JSON-RPC-over-stdio request multiplexer: id
  remapping, pending-response queues, locking, timeout, stdout reader.
  Sits between the Daemon and the MCP Subprocess. (Candidate A.)
- **PageSelection** - owns the Active Page: restore-before-call,
  update-from-response, param normalize, URL re-resolution. (Candidate B.)
- **Tool Dispatch** - routing policy over `tool_registry`: inspect-gate,
  CDP/local/forward routing, screenshot paint-gate, post-navigation detection
  trigger. Data lives in the registry; policy lives here. (Candidate C.) The
  session-adapter layer has its own counterpart (Session Tool Dispatch below).
- **LiveChrome** - owns the resolution of the live Chrome backing a
  user-data-dir: read the SingletonLock PID, confirm it is alive, confirm it
  holds the directory (PID-recycle guard), find its debug port, confirm
  DevTools answers. Returns one structured result consumed by
  `PersistentChromeController`, `profile_catalog`, and `handle_attach_browser`.
  The implementation module is `live_chrome`.
- **Session Tool Dispatch** - the session-adapter counterpart of Tool Dispatch:
  routes a tool to the Camoufox or Chrome backend, the session-management
  tools, or the live-profile-conflict gate, then applies the two Chrome
  cross-cutting policies (single-tab reuse via `SINGLE_TAB_TOOLS`, headless
  to headed auth-wall promotion via `NAVIGATION_TOOLS`) straight from
  `tool_registry` flags rather than inline branches. The implementation is
  `dispatch_session_tool` + `SessionDispatchContext` in `browser_session`,
  exposed as a seam so the routing order is testable through one interface with
  fakes - the session-adapter sibling of the Daemon's `DispatchContext`.
  `create_tool_proxy_handlers` returns a `call_tool` closure that delegates to
  it.
- **Session Resolver** - owns the Active-Session resolution priority (explicit
  override > project preference > recent external attach > default selection:
  this project's own live session, else a sole live named profile, else a fresh
  headless Chrome), returning `(controller, source, conflict)`. A single owner
  consumed by both `create_session` (bootstrap) and `browser_session_status`
  (diagnostics) so the two report the same choice instead of re-deriving the
  priority independently. The implementation is `resolve_session_controller` +
  `SessionResolution` in `browser_session`; `select_default_controller` and
  `choose_live_profile_fallback` remain its tested building blocks.
- **CDPRuntime** - the deep half of the CDP layer: owns the background thread,
  event loop, WebSocket connection, frame manager, screencast recorder, and the
  two thread-safe marshal methods the Daemon calls (`await_paint_ready`,
  `run_post_navigation_detection`). `CDPHandler` is the composition root and
  18-tool handler registry above it, reaching the browser through the runtime's
  `client` / `frame_manager` / `screencast` seam instead of owning connection
  state. Both classes live in `cdp_handler`.
- **Interstitial Detection** - owns the post-navigation challenge-response
  policy end to end: detection-script loading, two-pass single-shot detect,
  auto-retry for JS-solvable challenges, dedupe, and formatting. The retry
  tuning (delay, max retries, retryable types) lives in the module next to the
  retry loop that reads it. CDPHandler exposes only a thread-safe
  `run_post_navigation_detection` that marshals onto its event loop; it owns
  no detection policy. The implementation module is `interstitial`.
- **Automation Backend** - the seam behind the two browser backends: Chrome
  (via `PersistentChromeController`) and Camoufox (via `CamoufoxSession`). One
  interface, `invoke(tool, args) -> mcp_response`; the Camoufox adapter owns
  the browser-tools to Camoufox tool-name mapping, arg translation, and result
  wrapping. Lifecycle and session tools (`launch_camoufox`, `attach_browser`,
  ...) are not automation tools and stay routed in `browser_session`. The
  implementation module is `automation_backend`.

## Mode vocabulary

Defined once in `browser_state.py` so the wrapper, override handler, and
controller factory cannot drift:

- **HEADED_AUTH_MODES** - `headed`, `headed-auth`, `auth`, `auth-headed`:
  launch/reuse a headed, persistent, login-bearing session.
- **HEADLESS_AUTH_MODES** - `headless-auth`: persistent profile, headless.
