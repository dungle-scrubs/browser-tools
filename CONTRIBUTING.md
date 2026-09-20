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

### The installed CLI is not this repo

`bt` on a development machine is usually a separate install (`uv tool install`),
so it can be older than the checkout. When observed behavior contradicts the
source, check which code is running before reading further:

```bash
cat ~/.local/share/uv/tools/browser-tools/uv-receipt.toml   # where it came from
uv tool install --force .                                   # install this checkout
```

A running instance also keeps its supervisor. The supervisor injects the window
marker into tabs and holds the CDP connection for the browser's lifetime, so an
instance launched by the previous install keeps the previous behavior until that
instance is stopped and relaunched.

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

See `README.md` for the five layers and the module dependency graph, and
`CONTEXT.md` for the canonical name of each concept. Key principles:

1. `cli.py` is the only surface. It parses arguments, dispatches verbs and
   owns exit codes; every verb reaches the browser through `lifecycle`,
   `passthrough`, `curated`, `events` or `list_verbs`.
2. `cdp_handler.py` + `cdp_constants.py` implement the CDP-backed tools and
   their constants. `curated.py` builds one handler per invocation and tears
   it down; nothing keeps a connection alive between calls.
3. `lifecycle.py` owns profiles, the profile root, engine routing and every
   registry call site. A defect inside a verbatim vendored module under
   `core/` is corrected here, at the call site, never in place.
4. An upper layer consumes only the layer below it, and there are no circular
   imports.

## Testing

- Unit tests: mocked CDP/Playwright/Chrome. Preferred for all new code.
- Import-surface tests: `tests/test_cli_surface.py` runs fresh interpreters to
  prove what the CLI, the profiler and the bare package do and do not load.
- Parity tests: `tests/parity/` compares this repo's native engine against a
  live `chrome-devtools-mcp` subprocess over a frozen local corpus. Marked
  `parity`; they skip when their browser is unavailable. See
  `tests/parity/CORPUS.md`.
- E2E tests: `tests/parity/test_e2e_camoufox.py` requires `camoufox fetch`.
  CI skips it with `--ignore=tests/parity/test_e2e_camoufox.py`.

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
