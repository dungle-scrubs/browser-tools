# Contributing

## Setup

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and sync
git clone https://github.com/dungle-scrubs/browser-tools.git
cd browser-tools
uv sync
```

## Development

```bash
# Run lint
uv run ruff check src/ tests/

# Run type checks
uv run pyright src/

# Run tests
uv run pytest tests/ -q

# Run specific test file
uv run pytest tests/test_cdp_client.py -v
```

## Pre-commit

```bash
uv run pre-commit install
```

## Code Style

- Python 3.13+
- Line length: 100 characters
- Type annotations required on all public functions (via `from __future__ import annotations`)
- Docstrings follow Google-style (Args/Returns/Raises)
- Imports sorted with ruff (isort compatible)

## Module Size

Modules should stay under ~800 lines. When a module exceeds this, extract
a cohesive set of symbols into a new module and re-export from the original
for backward compatibility.

## Architecture

See `README.md` for the module dependency graph. Key principles:

1. `persistent_browser.py` is the hub — it imports from `process_utils`,
   `browser_state`, `mcp_session`, `daemon_client`, and `chrome_config`.
2. `cdp_handler.py` + `cdp_constants.py` implement all CDP-backed tools
   and constants; only `mcp_daemon.py` imports from them.
3. `browser_tools_session.py` is the CLI entry point — it imports from
   `persistent_browser`, `chrome_utils`, and `chrome_config`.
4. No circular imports — all imports form a DAG from CLI → controller →
   daemon → CDP handler.

## Testing

- Unit tests: mocked CDP/Playwright/Chrome. Preferred for all new code.
- Integration tests: `test_mcp_daemon.py` uses real subprocesses and
  local sockets. `test_attach_browser.py` uses a local HTTP server.
- E2E tests: `test_e2e_camoufox.py` requires `camoufox fetch`. Skip
  these in CI with `pytest tests/ --ignore=tests/test_e2e_camoufox.py`.

## Commit Conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org/).

Commit message format:

```
<type>: <description>

[optional body]

[optional footer]
```

### Types

| Type | Use for |
|------|---------|
| `feat` | New features |
| `fix` | Bug fixes |
| `docs` | Documentation only |
| `refactor` | Code restructuring (no behavior change) |
| `test` | Adding or fixing tests |
| `chore` | Build, CI, tooling, dependencies |
| `ci` | CI/CD pipeline changes |
| `style` | Formatting, no code change |

Examples:

```
feat: add screencast capture tool
fix: narrow except blocks in CDP event handler
docs: update README installation instructions
deps: bump camoufox to 0.4.12
```

### Pull Requests

- Create a branch from `main` (e.g. `feat/my-feature`, `fix/issue-123`).
- Squash merge into `main`.
- Reference issues in the PR description (e.g. "Closes #42").
