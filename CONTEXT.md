# browser-tools - domain glossary

Canonical names for the concepts in this codebase. Architecture reviews
and code should use these terms, not ad-hoc synonyms.

The MCP front and the persistent-session stack behind it were deleted in
RFC-01 Phase 5. The nouns and modules that named them are gone from this
glossary rather than left to name code that does not exist.

## Domain nouns

- **Instance** - one running browser process tracked in the registry under
  a name derived from the working directory. Liveness is engine-aware:
  Chrome is process identity plus CDP port attribution, Camoufox is process
  identity plus a user-data-dir hold. Never PID existence alone.
- **Named Profile** - a login-bearing user-data-dir selected by
  `launch --profile NAME`, living under the profile root and reused across
  restarts. Unaffected by headed/headless switches, viewport, or the
  directory the command runs from. At most one live instance holds a given
  profile; a second launch into it fails naming the holder. Deletion paths
  are constrained: the supervisor retires through the lifecycle layer and
  never removes a profile directory, and `cleanup` checks the resolved
  profile root as a path rather than trusting the registry's `profile`
  field. `profile delete NAME` is the only path that removes one, and it
  refuses a name outside the instance-name character set, a resolved path
  outside the root, and a profile a live instance holds.
- **External Endpoint** - a browser this tool did not launch, driven per
  invocation with `--endpoint URL` and absent from the registry. Loopback
  only. The absence of a registry entry is the safety property: an external
  browser's user-data-dir is the person's real profile directory, and the
  registry is what `stop` and `cleanup` act on. `Browser.close` and
  `Browser.crash` are refused over an endpoint; everything else passes.
- **Interstitial** - an anti-bot challenge page (Cloudflare, DataDome,
  Akamai, PerimeterX, Imperva, AWS WAF). Detected by multi-signal
  heuristics after navigation; some types auto-retry.
- **UID** - the handle a snapshot gives a node, used by `click` and `fill`
  to address it. Valid for the lifetime of the document that produced it.

## Module names (architecture)

- **CDPRuntime** - the deep half of the CDP layer: owns the background
  thread, event loop, WebSocket connection, frame manager, and screencast
  recorder. Each CLI invocation builds one, uses it, and tears it down;
  nothing keeps it alive between calls. `CDPHandler` is the composition
  root and 18-tool handler registry above it, reaching the browser through
  the runtime's `client` / `frame_manager` / `screencast` seam instead of
  owning connection state. Both classes live in `cdp_handler`.
- **Interstitial Detection** - owns the post-navigation challenge-response
  policy end to end: detection-script loading, two-pass single-shot detect,
  auto-retry for JS-solvable challenges, dedupe, and formatting. The retry
  tuning (delay, max retries, retryable types) lives in the module next to
  the retry loop that reads it. `CDPHandler` exposes only a thread-safe
  `run_post_navigation_detection` that marshals onto its event loop; it owns
  no detection policy. The implementation module is `interstitial`.
- **One-Shot Session** - the connect/resolve-target/attach/detach protocol
  for a single CLI invocation: open the browser-level CDP connection, list
  and sort page targets, resolve one, attach an isolated `Target` session,
  yield it to the caller, and always detach afterward, best-effort. One seam
  shared by `passthrough` (raw `Domain.method` dispatch), `events` (`wait`),
  `list_verbs` (`console-list`/`network-list`), and `curated` (`screenshot`)
  instead of four byte-identical copies. A matching `cli_cdp_errors`
  decorator maps the seam's own failures (ambiguous/not-found target, no
  page, CDP, connection, unknown instance) to `LifecycleError`; each verb's
  own errors (`UsageError`, `WaitTimeout`) pass through untouched. The
  implementation module is `one_shot`.
- **Lifecycle** - the layer that owns profiles, the profile root, engine
  routing, and the registry call sites the verbatim vendored modules cannot
  carry. `launch`, `status`, `stop`, `cleanup` and `retire_instance` live
  here. A defect inside a verbatim vendored module is corrected here, at the
  call site, never in place. The implementation module is `lifecycle`.
