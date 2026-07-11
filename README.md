# Browser Tools

[![CI](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)

Browser automation, debugging, and anti-detect browsing CLI. Provides:

- **Chrome DevTools MCP wrapper** — Snapshot-based page automation via
  [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)
- **Persistent browser sessions** — Long-lived Chrome instances with named profiles,
  daemon-based MCP reuse, and attach-to-running-browser support
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
# Run the CLI
browser-tools --help

# Navigate then snapshot. Pass --isolated (or --browser-url) so both commands
# reuse the SAME long-lived Chrome; a bare `browser-tools navigate` followed by
# a bare `browser-tools take-snapshot` would each spawn a throwaway browser and
# NOT share page or login state.
browser-tools --isolated navigate --url https://example.com
browser-tools --isolated take-snapshot
```

There is no `--profile` CLI flag. Named, login-bearing profiles are selected
through a project config file (see [Project Configuration](#project-configuration))
or the `use_browser_session` / `attach_browser` MCP tools. `--isolated` gives a
dedicated persistent profile directory that is separate from named profiles.

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
- **Node.js** (>=20.19; Node 22+ recommended) for `chrome-devtools-mcp`
- **Camoufox** (`camoufox fetch`) for anti-detect Firefox workflows

## Architecture

```
browser_tools_session.py     CLI entry point
        |
        +-- chrome_config.py          Tool schemas & validation
        +-- chrome_utils.py           MCP subprocess invocation, formatting
        +-- persistent_browser.py     Chrome lifecycle, daemon, profiles
        |       +-- browser_state.py     Persisted state dataclasses
        |       +-- mcp_session.py       Short-lived MCP session wrapper
        |       +-- daemon_client.py      Unix socket client
        |       +-- process_utils.py      Chrome process/port utilities
        |       +-- mcp_daemon.py         Long-lived MCP daemon
        |               +-- cdp_handler.py     CDP tool implementations
        |               +-- cdp_constants.py   CDP toolset definitions
        |               +-- cdp_client.py      CDP WebSocket client
        |               +-- frame_manager.py   Frame tree management
        |               +-- interstitial.py    Challenge detection
        |               +-- screenshot_utils.py Blank-frame detection
        +-- camoufox_session.py     Camoufox anti-detect wrapper
        +-- profiler.py            Standalone CPU profiler
```

## Development

```bash
uv sync
uv run ruff check src/ tests/
uv run pytest
```

### Project Configuration

Place a `.browser-tools.json` in your project root (searched upward from the
project working directory):

```json
{
  "preferredSession": {
    "mode": "headed-auth",
    "profile": "dev"
  }
}
```

This auto-selects a persistent headed browser session using the named profile.

The config is read either as a flat object or wrapped in a `preferredSession`
(or `preferred_session`) key. Recognized fields:

| Field      | Meaning                                                             |
| ---------- | ------------------------------------------------------------------- |
| `mode`     | `headless`, `headed-auth` (aliases: `headed`, `auth`, `auth-headed`), or `headless-auth` |
| `profile`  | Named profile that persists cookies/login across runs               |
| `endpoint` | Existing Chrome remote-debugging endpoint to attach to (loopback)   |
| `channel`  | `stable`, `canary` (default), `beta`, or `dev`                      |
| `viewport` | Initial window size, e.g. `1280x720`                                |
| `stealth`  | Inject anti-fingerprinting patches                                  |

### Keeping login state across calls

Auth/login state lives in a Chrome profile directory and survives only while
the same directory is reused. To keep a session logged in:

- **Use a named `profile`** (via the config above or
  `use_browser_session(mode="headed-auth", profile="<name>")`). Named profiles
  persist across restarts and are unaffected by headed↔headless switches,
  viewport, or which directory you invoke from.
- The **default/isolated** sessions are keyed per project and Chrome channel;
  they are not a stable place to keep a long-lived login.
- **Camoufox** persists login state only when you pass a `profile` to
  `launch_camoufox`; without it, every launch starts logged out.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.
All contributors are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) for responsible disclosure.
