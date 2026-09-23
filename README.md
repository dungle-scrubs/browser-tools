# Browser Tools

[![CI](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)

Browser automation, debugging, and anti-detect browsing CLI. Provides:

- **Raw CDP passthrough** - Any `Domain.method` goes straight to the running
  browser, with protocol help read live from it
- **Snapshot-based page automation** - Accessibility-tree snapshots and UID
  interaction (`snapshot`, `click --uid`, `fill --uid`), in Python over CDP
- **Curated verbs for the common path** - `eval`, `press`, `hover`, `type`,
  `wait-text` and `network-get` beside those three: the interactions whose
  raw CDP form is JavaScript or prose quoted inside JSON inside a shell line
- **One answer for every dialog** - `--dialog` fixes the reply to every
  JavaScript dialog before the invocation starts, and every dialog it
  answered is reported back under `dialogs`
- **Named browser instances** - Long-lived Chrome with a registry and named
  profiles that keep a login across restarts
- **External browsers** - `--endpoint` and `--chrome-profile` drive a Chrome
  you started for debugging on a data directory of its own, writing nothing
  to the registry
- **Frame-aware tools** - Iframe/CDP frame tree management, execution context
  resolution, and storage inspection
- **Interstitial detection** - Multi-signal heuristic detection for Cloudflare,
  DataDome, Akamai, PerimeterX, Imperva, AWS WAF, and other challenge pages
- **Camoufox anti-detect browsing** - Fingerprint-injected Firefox-based
  browsing for bot-protected sites
- **Anything else CDP can do** - File upload, viewport emulation, cookies,
  PDF export and page management have no curated verb and need none; `bt
  guide` has a worked example of each
- **Performance captures** - `trace` writes Chrome trace JSON and can drive
  the interaction it is capturing; `heap` writes a `.heapsnapshot`; `insights`
  reads a trace back as English, behind the optional runtime below
- **CPU profiling** - `browser-tools-profiler`, a second command installed
  alongside `bt`, with timed capture and threshold-triggered capture

## Quick Start

### Installation

```bash
# Using uv (recommended)
uv tool install browser-tools

# Or with pip
pip install browser-tools
```

### Usage

```bash
# Run the CLI (`bt` is an alias for `browser-tools`)
bt --help

# Launch a browser. It is registered by name and outlives this command, so
# later verbs drive the same Chrome and share its page and login state.
bt launch

# Navigate through the raw CDP passthrough, then read the page.
bt Page.navigate '{"url": "https://example.com"}'
bt snapshot

# Interact by UID from that snapshot.
bt click --uid 1-13

# When the page is blocking, ask directly.
bt detect

bt status          # registered instances and their page targets
bt stop            # close the browser and retire its registry entry
```

Omit `INSTANCE` while exactly one instance is running; with several, every verb
names the candidates rather than guessing. `bt help Domain.method` reads the
protocol schema from the running browser.

`bt launch` derives an instance name from the current directory and prints the
chosen name in its JSON result. Repeated unnamed launches from one directory
succeed with the first free two-digit suffix, such as `browser-tools-01`,
`browser-tools-02`, then `browser-tools-03`. There is no caller-chosen name, so
use the printed value as the handle for later commands.

**`bt guide` is the manual.** It is the complete CLI surface -- every verb,
every refusal with its exit code, the login walkthrough, the UID rule, the
performance captures and the `--endpoint` rules -- and reading it is enough to
drive a browser with this tool. A test fails the build when a verb has no entry
in it.

To drive a Chrome you started yourself, pass its endpoint or its data directory
instead of an instance name. It has to be a Chrome started for debugging on a
directory of its own. The browser you already have open will not do: measured
on Chrome 153, it refuses remote debugging on its default data directory,
writes no port file, and starts no endpoint.

```
DevTools remote debugging requires a non-default data directory. Specify this
using --user-data-dir.
```

So start one:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$HOME/.local/share/bt-debug-profile" \
  --remote-debugging-port=0
```

`--remote-debugging-port=0` asks Chrome for a free port and records it in that
directory's `DevToolsActivePort` file. Log in by hand in that window, then
reach it by the directory or by the endpoint:

```bash
bt snapshot --chrome-profile ~/.local/share/bt-debug-profile
bt click --uid 0BDAEF756714-14 --chrome-profile ~/.local/share/bt-debug-profile
bt snapshot --endpoint http://127.0.0.1:PORT
bt snapshot --endpoint ws://127.0.0.1:PORT/devtools/browser/ID
```

Nothing is written to the registry, so `status` does not list it and `stop` and
`cleanup` cannot reach it -- which is what keeps them away from your own Chrome
profile directory. Only `127.0.0.1` and `::1` are accepted; forward a remote
browser with `ssh -L 9787:127.0.0.1:9787 <host>`. Do not use 9222 for any of
this: it is the DevTools default port, so on a machine someone is working on,
whatever answers there is most likely their own browser. `Browser.close` and
`Browser.crash` are refused over `--endpoint`.

Chrome 144 and later can ask the person to click Allow for each connection, and
show an automation banner while one is open. Neither has been seen on this
machine, so treat this path as one for an agent beside a person at the keyboard;
an unattended agent cannot supply an approval. The manual's EXTERNAL BROWSERS
section carries the refusals, the timeouts, and what a timeout does and does not
prove.

### Development setup

```bash
# Clone and install in editable mode
git clone https://github.com/dungle-scrubs/browser-tools.git
cd browser-tools
uv sync
```

Runtime requirements:

- **Chrome Canary** by default (the default channel is `canary`). Use another
  installed channel with `--channel stable|beta|dev`, e.g. `--channel stable`
  for regular Google Chrome.
- **Camoufox** (`camoufox fetch`) for anti-detect Firefox workflows, with the
  `camoufox` extra

The default install depends on `websockets` only, and `pip install
browser-tools` brings no Node.js with it.

One opt-in adds a Node runtime, and nothing else does. `bt insights --setup`
installs the pinned trace engine from a committed lockfile into the installed
package, so `bt insights` can read a trace back as English. It needs Node.js
22+ and npm on PATH, runs only `npm ci --omit=dev --ignore-scripts`, and is
never run for you. Skip it and the rest of the tool is unaffected; `bt
insights` is then exit 2 and names the command.

## Architecture

Five layers. Each upper layer consumes only the layer below it, and every
curated tool calls the same CDP `send` path the raw passthrough uses -- so any
method the installed browser supports works through `bt Domain.method` whether
or not a verb exists for it.

| Layer | Name | Contents |
|---|---|---|
| 4 | Front | CLI verbs (the only surface), `bt guide`, the agent skill that points at it |
| 3 | Policy | Named profiles and the profile root, fingerprint profiles, engine routing |
| 2 | Native toolset | Curated tools as plain CDP consumers; profiling; window marking |
| 1 | Core (vendored) | Registry, liveness, launcher, supervisor, attach, passthrough, live-schema help |
| 0 | Browser | Chrome/Chromium over CDP; Camoufox for anti-detect paths |

```
cli.py                       CLI entry point (argparse, verb dispatch, exit codes)
        |
        +-- lifecycle.py             launch / status / stop / cleanup over the registry
        |       +-- camoufox_runner.py   Camoufox host process (`launch --engine camoufox`)
        |       +-- process_utils.py     Chrome process and port utilities
        |
        +-- endpoint.py              `--endpoint URL`: loopback check, refusals
        |       +-- chrome_discovery.py  `--chrome-profile DIR`: read DevToolsActivePort
        |       +-- external_connection.py Bounded external connections, approval handshake
        |       +-- devtools_http.py     DevTools HTTP reads, to the validated address only
        |
        +-- passthrough.py           Raw `Domain.method` send + the focus guard
        +-- curated.py               The curated verbs over one short-lived CDP handler
        |       +-- curated_runtime.py   eval / press / hover / type / wait-text / network-get
        |       +-- dialog_policy.py     The invocation's fixed answer to every dialog
        |
        +-- captures.py              Bounded captures: `trace` and `heap`
        +-- insights.py              `insights --setup` and offline trace analysis
        +-- extras.py                Optional-dependency gating
        +-- step_run.py              A Step Run: one invocation, one session, many steps
        |       +-- step_list.py         Read a Step List; refuse a bad one before step 1
        |
        +-- one_shot.py              Connect, resolve a page target, attach, detach
        |       +-- attached_session.py  The handler's client over an attached session
        |
        +-- events.py                attach / wait / console-list / network-list
        +-- list_verbs.py            frames / storage
        +-- usage.py                 The one exception that means exit 2
        +-- user_settings.py         The persistent settings file (window border)
        |
        +-- core/                    Vendored chrome-agent core (RFC-01; see NOTICE)
        |       +-- registry.py          Named instances, liveness, retirement
        |       +-- launcher.py          Chrome launch (windowless, background first window)
        |       +-- supervisor.py        Per-instance window marking and retirement
        |       +-- cdp_client.py        CDP WebSocket client and target discovery
        |       +-- attach.py            Isolated event subscriptions
        |       +-- protocol.py          Live protocol schema for `help`
        |       +-- fingerprint.py       Launch-flag fingerprint profiles
        |
        +-- cdp_handler.py           CDP tool implementations (snapshot, click, fill, ...)
        |       +-- native_snapshot.py   Accessibility-tree snapshot and UID scheme
        |       +-- native_interaction.py UID click / fill
        |       +-- frame_manager.py     Frame tree management
        |       +-- frame_sessions.py    One session per out-of-process frame
        |       +-- interstitial.py      Challenge detection and the retry policy
        |       +-- screencast.py        Screencast capture state machine
        |       +-- screenshot_utils.py  Blank-frame detection
        |       +-- tool_registry.py     Single source of truth for CDP tool routing
        |       +-- mcp_response.py      Response envelope builders
        |
        +-- profiler.py              Standalone CPU profiler (`browser-tools-profiler`)
```

The CLI is the only surface. The optional MCP front and the persistent-session
stack behind it were deleted in RFC-01 Phase 5; there is no daemon, no broker,
and no listening socket.

## Keeping login state across calls

Login state lives in a browser profile directory and survives only while the
same directory is reused. To keep a session logged in:

- **Use a named profile**: `bt launch --profile <name>`. Named profiles persist
  across restarts and are unaffected by headed/headless switches, viewport, or
  which directory you invoke from.
- A profile is held by at most one live instance. Launching into a profile
  another instance holds fails naming the holder rather than opening a second
  browser on the same profile directory. This is profile exclusivity, not a
  limit on launches from one working directory: repeated unnamed launches from
  one working directory succeed with numbered instance-name suffixes.
- Without `--profile`, a launch gets a fresh ephemeral directory and starts
  logged out.
- `bt profile list` shows every profile with its path and its live holder, and
  `bt profile delete NAME` removes one. Deleting a profile deletes its login
  state; `cleanup` never removes a profile on age.

Profiles live in durable storage: `$BROWSER_TOOLS_PROFILES_DIR`, else
`$XDG_DATA_HOME/browser-tools/profiles`, else
`~/.local/share/browser-tools/profiles`. They used to live under `/tmp`, where
the operating system deleted every signed-in session at boot, silently. A
profile still there is listed with `"legacy": true`; `bt profile migrate`
moves them all across (`--dry-run` first, `--back` to reverse it), and
`launch --profile NAME` brings that one forward by itself.
- **Camoufox** persists login state only when you pass `--profile`; without it,
  every launch starts logged out.

## Development

```bash
uv sync
uv run ruff check src/ tests/
uv run pytest
```

## License

MIT - see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.
All contributors are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) for responsible disclosure.
