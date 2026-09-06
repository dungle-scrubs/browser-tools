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
uv run pytest tests/test_cdp_runtime.py -v
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

## Architecture

Keep cohesive behavior behind one owner. Prefer direct imports to forwarding
aliases and do not split modules solely to meet a line limit.

- `cli.py` owns parsing and dispatch; `tool_registry.py` owns handler metadata.
- `lifecycle.py` owns instance and profile lifecycle, including migration locking.
- `one_shot.py` owns target selection and isolated page attachment.
- `cdp_handler.py` binds handlers to a `CDPRuntime` over `core.cdp_client`.
- `curated.py` owns CLI actions and foreground Capture lifetime.
- `screencast.py` owns frame buffering and private artifact writes.
- `interstitial.py` owns detection and retry policy.

No circular imports: dependencies flow from CLI orchestration to runtime and
protocol primitives.

The vendored core remains unchanged except for the explicitly adapted launcher
and supervisor. `NOTICE` and RFC-01 record its provenance and update rules.
Camoufox launch support is optional. Its reserved driver is not a second
curated automation path.

## Testing

Test behavior at public seams with fake CDP transports for deterministic failures.
Use real CLI subprocesses for lifecycle and Capture artifact contracts. Chrome
must be installed for Capture integration tests. Tests use disposable profiles
and isolated registries.

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/ -q --ignore=tests/test_e2e_camoufox.py
```

The optional Camoufox E2E suite additionally requires its extra and downloaded
browser (`camoufox fetch`). Native backend parity tests must pass before merging
transport changes. Do not replace artifact checks with mocked success responses.

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
