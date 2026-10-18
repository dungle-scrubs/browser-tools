# Browser Tools reference

This page covers command groups, configuration, browser lifetime, and the internal layout. Run `bt guide` for every command's syntax, output, and refusals. The manual ships with the installed version; `bt --version` identifies that version.

## Commands

`bt` and `browser-tools` are aliases. Browser-driving commands accept a leading instance name, for example `bt web-01 snapshot`.

| Task | Commands |
| --- | --- |
| Manage browsers | `launch`, `status`, `stop`, `retain`, `cleanup` |
| Manage saved logins | `profile list`, `profile delete NAME`, `profile migrate` |
| Read help | `guide`, `help`, `--help`, `--version` |
| Configure window marking | `window-border` |
| Read and interact with pages | `snapshot`, `click`, `fill`, `navigate`, `eval`, `press`, `hover`, `type` |
| Wait for page state | `wait-text`, `wait-idle`, `wait-stable`, `wait` |
| Inspect traffic and console output | `console-list`, `network-list`, `network-get`, `attach` |
| Inspect frames and storage | `frames list`, `frames select`, `frames reset`, `storage get` |
| Check challenge pages | `detect` |
| Capture pages and performance | `screenshot`, `screencast`, `trace`, `heap`, `insights` |
| Run several steps in one connection | `run` |
| Send Chrome DevTools Protocol directly | `Domain.method '{"key":"value"}'` |

`browser-tools-profiler` is a separate installed command for timed or CPU-threshold-triggered JavaScript profiles. It takes `--port`, not an instance name. Read the debugging port from the instance's launch or status output and pass it explicitly. Its default port, 9222, does not select a Browser Tools instance.

## Launch and browser lifetime

Chrome is the default engine. Without a channel or binary override, the launcher looks for regular Google Chrome stable, then Chromium where available. `--channel stable`, `--channel beta`, `--channel dev`, and `--channel canary` select an installed channel. Canary is not the default.

`BROWSER_TOOLS_CHROME_BINARY` overrides auto-detection and an explicit `--channel`. The value must be an executable file. Chrome for Testing or a headless-shell binary can be used for isolated tests.

| Launch option | Effect |
| --- | --- |
| `--headless` | Run without a visible window. The default is headed. |
| `--name NAME` | Choose the instance's base name, independent of the current directory. |
| `--profile NAME` | Reuse a persistent browser profile and its login state. |
| `--owner PID` | Close the browser when that process exits. |
| `--engine camoufox` | Use the optional Camoufox engine. |
| `--port PORT` | Use an explicit debugging port instead of auto-allocation from 9422 upward. |
| `--fingerprint FILE` | Pass a fingerprint profile's launch flags. This file is not a saved login. |
| `--no-window-border` | Disable the border, badge, and tab-title marking for this launch. |
| `-- BROWSER_ARGS` | Pass remaining arguments to the browser. |

An instance name is the base name plus the first free two-digit suffix. A launch from a directory called `web` starts with `web-01`; `--name demo` starts with `demo-01`. Names are lowercased. Read the actual name from launch output rather than assuming a suffix.

A plain launch runs until stopped. With `--owner PID`, a detached keeper checks the owner's process identity and closes the browser when the owner exits, including after SIGKILL. Choose a process that spans the task, not a temporary command shell. Task completion still needs `bt stop` if the owner remains alive.

`bt retain INSTANCE` cancels automatic owner-exit cleanup for a handoff. It does not turn a headless browser into a visible one. A browser whose automatic close has started cannot be retained. `status` reports lease state and cleanup failures; `stop` is the recovery command.

`stop` closes the browser and removes its registry entry. Persistent profiles remain. `cleanup` removes stale entries and orphaned session directories, never live instances or saved profiles. `profile delete NAME` deletes the profile's login state and refuses a profile held by a live instance.

## Profiles and configuration

A named profile is a browser user-data directory. One live instance can hold it at a time. Without `--profile`, each launch uses a fresh temporary directory and starts logged out. Chrome and Camoufox both need `--profile` to keep a login across restarts.

| Variable | Meaning and default |
| --- | --- |
| `BROWSER_TOOLS_PROFILES_DIR` | Persistent profile root. Takes precedence over `XDG_DATA_HOME`. |
| `XDG_DATA_HOME` | Profiles live under `browser-tools/profiles`; otherwise they use `~/.local/share/browser-tools/profiles`. |
| `BROWSER_TOOLS_REGISTRY` | Override the registry file, normally under `/tmp/chrome-agent/`. |
| `BROWSER_TOOLS_CHROME_BINARY` | Override the Chrome executable, including when `--channel` is present. |
| `BROWSER_TOOLS_LEGACY_PROFILES_DIR` | Override the old profile root, normally `/tmp/browser-tools-profiles`. |
| `XDG_CONFIG_HOME` | Settings live in `browser-tools/settings.json`; otherwise they use `~/.config/browser-tools/settings.json`. |

Profiles left under the old temporary root appear with `"legacy": true` in `profile list`. `profile migrate --dry-run` previews migration to durable storage. `profile migrate` moves them, and `profile migrate --back` moves them back. Launching one legacy profile migrates that profile automatically. A live holder or a destination name collision prevents migration.

`bt window-border off` changes the persistent `window_border` setting for running and future marked windows. The tab-title prefix stays. `bt window-border on` restores the border. The settings file contains a JSON object:

```json
{"window_border": "on"}
```

## Page selection and state

With one registered instance, the instance name can be omitted. With several, specify it before the command. Page selectors are separate:

- `--target SPEC` selects a 1-based index in target-ID order, or a target-ID prefix.
- `--url SUBSTRING` selects a page by URL. It is mutually exclusive with `--target`.
- Without a selector, handler-routed curated commands and `run` select the first page. Raw CDP, standalone screenshots, and event commands refuse ambiguous page selection.

A snapshot UID has the form `<docToken>-<backendNodeId>`. It remains valid until navigation. A new snapshot does not invalidate earlier UIDs from that document. Nodes without a backing DOM element are readable but cannot be clicked or filled.

Every standalone command opens a new CDP session and detaches when it finishes. Cookies and the loaded page remain in the browser. Selected frames, enabled protocol domains, DOM `nodeId` values, and most emulation overrides do not carry between commands. Put dependent steps in one `bt run` invocation. The guide's WHAT DOES NOT CARRY BETWEEN INVOCATIONS section lists the exceptions and examples.

A Step List has one verb phrase per line, without `bt` or an instance name. Blank lines and full-line comments are ignored. There are no variables, loops, conditions, or substitutions from earlier output. A run validates the list before step 1, stops at the first failure, and does not undo completed steps.

Frame inspection defaults to same-process frames. `--frames all` also reaches cross-origin iframes for commands that support it. In a run, frame selection persists across steps. `storage get --key PATTERN` selects a frame by URL pattern for that read; `--key` is not a storage-key name.

`navigate` returns before the page finishes loading. Wait for relevant text or network idle before reading the new page. Within a Step List, use `Page.navigate` followed by `wait-idle`; `navigate` itself is not an allowed step.

The driving commands that support `--dialog` actively dismiss JavaScript dialogs by default. `--dialog accept` accepts them; `--dialog accept:TEXT` supplies prompt text. Answered dialogs appear under `dialogs` in the output. The policy applies during that invocation, not to future commands.

## External browsers

`--endpoint` and `--chrome-profile` drive a Chrome started for debugging on a data directory of its own. Chrome refuses remote debugging on its default data directory, so the everyday Chrome profile is not attachable through this path. On macOS, the launch shape is:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$HOME/.local/share/bt-debug-profile" \
  --remote-debugging-port=0
```

After that browser starts:

```bash
bt snapshot --chrome-profile "$HOME/.local/share/bt-debug-profile"
```

`--chrome-profile DIR` reads `DevToolsActivePort` from that user-data directory and verifies which loopback peer matches it. `--endpoint` accepts an HTTP debugging endpoint or a browser WebSocket URL. For example, `bt snapshot --endpoint ws://127.0.0.1:PORT/devtools/browser/ID` uses the endpoint from your browser. Only literal `127.0.0.1` and `::1` hosts are accepted, not `localhost` or remote addresses.

Use only a browser you started for the task. Port 9222 is Chrome's debugging default and may belong to someone else's browser. A CDP endpoint grants unauthenticated control of that browser, including its cookies.

External connections write nothing to the registry. `status` does not list them, and `stop`, `retain`, and `cleanup` cannot manage them. `Browser.close` and `Browser.crash` are refused over an external endpoint. These refusals do not make the remaining CDP commands read-only.

The guide's EXTERNAL BROWSERS section covers discovery, policy refusals, SSH forwarding, and connection deadlines. Measurements on Chrome 153 found no Allow prompt when Chrome was started with its own debugging profile. A timeout alone does not establish whether Chrome wants approval, is hung, or is not the process listening on that port.

## Captures and optional dependencies

The default Python install depends only on `websockets`. Optional extras are `camoufox` for the anti-detect Firefox launcher, `profiling` for Pillow-assisted blank-screenshot detection, and `all` for both. The `profiling` extra is not required for the CPU profiler command.

Camoufox support covers browser launch, profiles, liveness, and shutdown. Its runner holds a persistent browser context but exposes no CDP endpoint. Page-interaction commands, event observation, and performance captures use Chrome's CDP transport and do not automate Camoufox.

`trace` writes Chrome trace JSON for the Performance panel. `heap` writes a `.heapsnapshot` for the Memory panel. `screencast`, `trace`, and `heap` start and finish within one invocation. There are no separate start and stop commands. A trace needs `--duration`, `--steps`, or both. To capture a navigation or interaction, put that action inside the trace's Step List. A trace of an already-loaded idle page may have no navigation to analyze.

`bt insights --setup` installs the pinned trace engine into the installed Python package. It needs Node.js 22+, npm, network access to the npm registry, and write access to that package. It runs `npm ci --omit=dev --ignore-scripts` using the committed lockfile. No other command installs this runtime automatically. Analysis without setup exits 2.

`insights` reads a trace back as English without a running browser. Its pinned trace engine has provisional validation for Chrome 151.0.7922.34 and 153.0.0.0 only. Other or missing browser versions fail validation unless `--ignore-engine-mismatch` is explicit. This is a compatibility limit, not a claim that every Chrome build between those versions works.

Trace analysis does not produce Lighthouse scores. Lighthouse can connect to a headless Browser Tools instance by its actual debugging port; the guide includes the recipe and its browser-state effects.

## Output and exit codes

Most commands print a JSON document to stdout. `guide` and `help` print plain text, `attach` streams JSON Lines, and `insights --format text` prints text on success. CLI `--help` and `--version` are also plain text. Diagnostics go to stderr.

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Operational failure, such as a closed browser, deadline, held profile, or stale UID. |
| `2` | Invalid or refused usage. |

Do not parse stdout as proof of success. A failed Step Run prints a partial Run Document. A trace can still be written when its step run fails, and insights diagnostics or partial results can also be JSON. Check the exit code before reading results.

## Architecture

The CLI is the only interface. It exposes no MCP server or broker. Each browser-driving invocation opens its own connection. Chrome exposes the debugging endpoint; window supervisors and owner-exit keepers can outlive a command.

Each upper layer consumes the layer below it. Curated tools use the same CDP send path as raw passthrough, so a browser-supported protocol method does not need a dedicated verb.

| Layer | Name | Contents |
| --- | --- | --- |
| 4 | Front | CLI commands, `bt guide`, and the agent skill that points at it |
| 3 | Policy | Named profiles, profile roots, fingerprints, and engine routing |
| 2 | Native toolset | Curated CDP consumers, profiling, and window marking |
| 1 | Core (vendored) | Registry, liveness, launcher, supervisor, attach, passthrough, and live-schema help |
| 0 | Browser | Chrome or Chromium over CDP; optional Camoufox launch and lifecycle |

Source files are under `src/browser_tools/`:

```text
cli.py                     Argument parsing, dispatch, exit codes
lifecycle.py               Launch, status, stop, cleanup, profiles
  camoufox_runner.py       Camoufox host process
  process_utils.py         Process and port utilities
  task_lease.py            Owner-exit keeper and retain handoff
endpoint.py                External endpoint checks and refusals
  chrome_discovery.py      DevToolsActivePort discovery
  external_connection.py   Bounded external connections
  devtools_http.py         HTTP reads to validated addresses
passthrough.py             Raw CDP and focus guard
curated.py                 Curated commands over a short-lived handler
  curated_runtime.py       eval, press, hover, type, wait-text, network-get
  dialog_policy.py         Per-invocation dialog answers
captures.py                Trace and heap captures
insights.py                Analysis setup and offline trace analysis
extras.py                  Optional-dependency checks
step_run.py                One connection for a list of steps
  step_list.py             Step List parsing and validation
one_shot.py                Connect, select page, attach, detach
  attached_session.py      Handler client over an attached session
events.py                  attach, wait, console-list, network-list
list_verbs.py              Frame and storage commands
usage.py                   Usage error type
user_settings.py           Persistent window-border setting
core/                      Vendored chrome-agent core
  registry.py              Names, liveness, retirement
  launcher.py              Chrome launch
  supervisor.py            Window marking and retirement
  cdp_client.py            CDP WebSocket transport
  attach.py                Isolated event subscriptions
  protocol.py              Live protocol schema
  fingerprint.py           Launch-flag profiles
cdp_handler.py             CDP tool implementations
  native_snapshot.py       Accessibility tree and UIDs
  native_interaction.py    UID click and fill
  frame_manager.py         Frame tree
  frame_sessions.py        Out-of-process frame sessions
  interstitial.py          Challenge detection and retry policy
  screencast.py            Screencast capture
  screenshot_utils.py      Blank-frame detection
  tool_registry.py         CDP tool routing
  mcp_response.py          Response builders, not an MCP server
profiler.py                Standalone JavaScript CPU profiler
```

Vendoring and attribution are recorded in [NOTICE](../NOTICE). The [contributor guide](../CONTRIBUTING.md) covers module boundaries, and [CONTEXT.md](../CONTEXT.md) defines project terms. [Capability verification](capability-verification.md) records live checks. Design history lives in [RFCs](rfc/) and [ADRs](adr/).
