# Browser Tools

[![CI](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/dungle-scrubs/browser-tools/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)

Browser automation, debugging, and anti-detect browsing CLI. Provides:

- **Chrome DevTools MCP wrapper** — Snapshot-based page automation via
  [chrome-devtools-mcp](https://github.com/anthropics/chrome-devtools-mcp)
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

# Take a snapshot of a page
browser-tools --headless navigate --url https://example.com
browser-tools --headless take-snapshot

# Use a persistent profile
browser-tools --profile dev navigate --url https://example.com
```

### Development setup

```bash
# Clone and install in editable mode
git clone https://github.com/dungle-scrubs/browser-tools.git
cd browser-tools
uv sync
```

Runtime requirements:

- **Chrome** or **Chrome Canary** for DevTools automation
- **Node.js** (>=18) for `chrome-devtools-mcp`
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

Place a `.browser-tools.json` in your project root:

```json
{
  "preferredSession": {
    "mode": "headed-auth",
    "profile": "dev"
  }
}
```

This auto-selects a persistent headed browser session using the named profile.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.
All contributors are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) for responsible disclosure.
