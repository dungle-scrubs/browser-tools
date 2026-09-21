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
- **Profile Root** - the durable directory holding every named profile,
  resolved as `$BROWSER_TOOLS_PROFILES_DIR`, else
  `$XDG_DATA_HOME/browser-tools/profiles`, else
  `~/.local/share/browser-tools/profiles`, with an empty value falling through.
  It was `/tmp/browser-tools-profiles` until RFC-01 v6, where the operating
  system deleted every login at boot. The registry stays in `/tmp`; a cleared
  registry after a reboot is self-consistent, because no browser survives one.
  Unbound Camoufox session dirs live outside the root, so nothing throwaway
  lands in durable storage.
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
- **Bounded Capture** - the whole of `screencast`: one invocation starts the
  capture, buffers frames, writes them to `--dir`, and exits. It ends at the
  `--duration` or the `--max-frames` cap, whichever comes first. The frame
  buffer is process-local, so nothing outlives the invocation and no verb pair
  can span two processes.
- **Step Run** - the whole of `run`: one invocation, one CDP connection, one
  attached session, many steps. It is the only place a browser-driving verb
  runs over a session it did not open. Nothing outlives the invocation, so it
  holds the same property Bounded Capture does and is not a session daemon.
  The run resolves the instance, the endpoint and the page once, before step
  one; a step may name none of them. It stops at the first failing step and
  rolls nothing back, because a step that has run has already reached the
  browser. `--timeout` bounds the whole run and defaults to no deadline,
  since every step already bounds itself.
- **Step List** - what a Step Run runs: an ordered list of verb phrases, one
  per line, read from a file or stdin. It is data, not code. There are no
  variables, no conditions, no loops, and no way for one step to use another
  step's output, so reading an untrusted Step List is no more dangerous than
  accepting untrusted `bt` arguments. Lines are split the way a shell splits
  them. A line whose first non-blank character is `#` is a comment; `#`
  anywhere else is an ordinary character, so a URL fragment or a `--text`
  value carrying one does not need quoting for that reason. The whole list is
  validated against the same parser and the same preconditions a bare
  invocation uses, before step one runs, so a malformed list is exit 2 with
  nothing sent.
- **Run Document** - the single JSON document a Step Run prints, on success
  and on failure alike: a `run` object carrying the step count, how many
  completed, and the status, and a `steps` array with one entry per step
  *attempted*. A step's `result` is what that step prints alone, unchanged,
  which is what lets a caller parse a run with what it already has. Steps
  never reached are absent. The caller contract is ordered, and The Manual
  says so: read the exit code first, then `run.status`, then the steps.
- **UID** - the handle a snapshot gives a node, used by `click` and `fill`
  to address it. Valid for the lifetime of the document that produced it.
- **CPU Profiler** - `browser-tools-profiler`, the second console script the
  install puts down beside `bt` and `browser-tools`. It profiles the page's
  JavaScript through `Profiler.*` over the vendored core client, with `timed`
  for a fixed window and `watch` for threshold-triggered capture. It takes a
  debug port rather than an instance name, so it reads no registry. It is a
  separate command because a profile is an enable-start-stop sequence and
  domain enable state belongs to one session, which a `bt` invocation does not
  outlive. The implementation module is `profiler`.

- **Out-of-Process Frame** - a cross-origin iframe that Chrome's site
  isolation runs in its own renderer process, giving it its own CDP
  `iframe` target. It is absent from `Page.getFrameTree` on the page
  session and its lifecycle events do not arrive there, so nothing reaches
  it without attaching to that target. `--frames all` does; the default
  `--frames page` does not, and `frames list --frames all` marks one
  `[out-of-process]`.

- **Frame Session** - the CDP session attached to one Out-of-Process
  Frame's target, below the page session and owned by `frame_sessions`.
  It enables `Page` and `Runtime` on that target and reads its frame tree,
  which is what puts the frame in the listing. A frame the map holds names
  the Frame Session answering for it, or none, which means the page
  session. Bounded at 32 sessions and 10 levels of nesting; a frame past
  either bound is listed `[unreachable]`, never dropped in silence.

  It is not yet the session commands go to. The execution-context map is
  keyed by context id alone, and context ids are per-session, so a child's
  contexts cannot be recorded without colliding with the page's. Until
  that is fixed, selecting an Out-of-Process Frame gets you the selection
  and nothing that reads through it: `storage get` returns cookies only
  and `snapshot` shows the `Iframe` node empty.

- **Spliced Frame Tree** - the single tree `frames list` prints, built by
  attaching each Frame Session's own frame tree under the parent frame its
  root names in `parentId`. The attachment point is given by Chrome rather
  than guessed. A child whose parent is not in the map yet is held, not
  hung somewhere else, and spliced when the parent arrives. The invariant
  is the one the page tree already had: a frame the map holds is reachable
  from the root, exactly once, and a selection resolves in the order
  `frames list` prints.

- **The Manual** - `bt guide`, the complete `bt` surface and the only
  documentation an agent reads before driving a browser. It lives in
  `src/browser_tools/GUIDE.txt` and ships as package data.
  `tests/test_guide.py` enumerates the verbs from the parser and fails the
  build when one has no entry, and reads `[project.scripts]` so a console
  script with no manual entry fails the build too. The manual covers the CPU
  Profiler in its own section for that reason.

## Module names (architecture)

- **CDPRuntime** - the deep half of the CDP layer: owns the background
  thread, event loop, WebSocket connection, frame manager, and screencast
  recorder. Each CLI invocation builds one, uses it, and tears it down;
  nothing keeps it alive between calls. One invocation is one runtime, not
  one verb: a Step Run's steps all share the runtime the run opened, which
  is why a `frames select` step governs the steps after it. It also carries
  the run's deadline, so `--timeout` reaches a wait a step is already
  sitting in rather than only the gaps between steps. `CDPHandler` is the composition
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
  own errors (`UsageError`, `WaitTimeout`) pass through untouched. Inside a
  Step Run the seam is not entered per verb: the run attaches once and every
  step runs over `(client, sessionId)`, so the same verb body serves both
  shapes. `domains_enabled` lives here for that reason - a step gives back
  every domain it turned on, except `Page` and `Runtime`, which belong to the
  run. The implementation module is `one_shot`.
- **Lifecycle** - the layer that owns profiles, the profile root, engine
  routing, and the registry call sites the verbatim vendored modules cannot
  carry. `launch`, `status`, `stop`, `cleanup` and `retire_instance` live
  here. A defect inside a verbatim vendored module is corrected here, at the
  call site, never in place. The implementation module is `lifecycle`.
