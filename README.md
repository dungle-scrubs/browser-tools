# Browser Tools

[![CI](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)

Browser automation, debugging, and anti-detect browsing CLI. Provides:

- **Raw CDP passthrough** — Any `Domain.method` goes straight to the running
  browser, with protocol help read live from it
- **Snapshot-based page automation** — Accessibility-tree snapshots and UID
  interaction (`snapshot`, `click --uid`, `fill --uid`), in Python over CDP
- **Named browser instances** — Long-lived Chrome with a registry and named
  profiles that keep a login across restarts
- **Frame-aware tools** — Iframe/CDP frame tree management, execution context
  resolution, and storage inspection
- **Interstitial detection** — Multi-signal heuristic detection for Cloudflare,
  DataDome, Akamai, PerimeterX, Imperva, AWS WAF, and other challenge pages
- **Camoufox anti-detect browsing** — Fingerprint-injected Firefox-based
  browsing for bot-protected sites
- **CPU profiling** — Direct CDP-based JavaScript CPU profiling with
  threshold-triggered capture

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
names the candidates rather than guessing. `bt guide` prints the full manual,
and `bt help Domain.method` reads the protocol schema from the running browser.

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

The default install depends on `websockets` only. There is no Node.js
dependency: the Node `chrome-devtools-mcp` path was removed in RFC-01.

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
        +-- passthrough.py           Raw `Domain.method` send + the focus guard
        +-- curated.py               The curated verbs over one short-lived CDP handler
        +-- one_shot.py              Connect, resolve a page target, attach, detach
        +-- events.py                attach / wait / console-list / network-list
        +-- list_verbs.py            frames / storage
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
  browser on the same directory.
- Without `--profile`, a launch gets a fresh ephemeral directory and starts
  logged out.
- **Camoufox** persists login state only when you pass `--profile`; without it,
  every launch starts logged out.

## Development

```bash
uv sync
uv run ruff check src/ tests/
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.
All contributors are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) for responsible disclosure.
