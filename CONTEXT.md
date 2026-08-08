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
  trigger. Data lives in the registry; policy lives here. (Candidate C.)
- **LiveChrome** - owns the resolution of the live Chrome backing a
  user-data-dir: read the SingletonLock PID, confirm it is alive, confirm it
  holds the directory (PID-recycle guard), find its debug port, confirm
  DevTools answers. Returns one structured result consumed by
  `PersistentChromeController`, `profile_catalog`, and `handle_attach_browser`.
  The implementation module is `live_chrome`.
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
