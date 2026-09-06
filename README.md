# Browser Tools

Browser automation and debugging from the command line. Launch a named browser,
inspect its accessibility tree, click and fill nodes, record page behavior, or
send any Chrome DevTools Protocol method. Commands share a running browser;
each command gets an isolated CDP target session.

## Quick Start

Install Python 3.13+ and Chrome, then:

```bash
uv tool install browser-tools
bt launch --headless
bt Runtime.evaluate '{"expression":"document.body.textContent = \"Hello from browser-tools\""}'
bt snapshot
bt stop
```

These commands omit the instance name and target only when one browser and one
page exist. `launch` prints the assigned instance name. With multiple browsers,
put that name after the verb. With multiple pages, use `--target 2`, an ID
prefix, or `--url SUBSTRING`. Ambiguity is an error, never a silent tab choice.

Chrome is the default engine. `--channel stable|beta|dev|canary` selects an
installed channel. Base installation requires websockets only.

## Commands

| Verb | Purpose |
| --- | --- |
| `launch` | Start and register Chrome or Camoufox |
| `status` | Show registered instances and liveness |
| `stop` | Stop an instance or close a target |
| `cleanup` | Remove stale registry entries and temporary sessions |
| `guide` | Read the bundled command manual |
| `help` | Inspect the running browser's live CDP schema |
| `attach` | Stream subscribed CDP events |
| `wait` | Wait for a matching event |
| `console-list` | Collect console messages |
| `network-list` | Collect network requests |
| `snapshot` | Read a native UID accessibility tree |
| `click` | Click a node with `--uid` |
| `fill` | Fill a node with `--uid` and `--text` |
| `wait-idle` | Wait for network quiet |
| `wait-stable` | Wait for DOM quiet |
| `detect` | Detect interstitial challenges |
| `frames` | List, select, or reset frame selection |
| `storage` | Read frame storage, masking cookie values by default |
| `screenshot` | Capture a PNG |
| `screencast` | Record frames in one foreground process |
| `tool` | Invoke a handler tool with JSON arguments |

Use `bt VERB --help` for options. All curated and event actions accept
`--target SPEC` or `--url SUBSTRING`. Raw CDP uses
`bt [INSTANCE] Domain.method '{...json params...}'` with the same target flags.
Results go to stdout as JSON; diagnostics go to stderr. Exit codes are 0 for
success, 1 for operational failure, and 2 for invalid usage.

```bash
bt tool get_text '{"selector":"h1"}'
bt tool ax_find '{"role":"button"}'
bt storage get --key example.com
bt storage get --key example.com --reveal-values
```

Frame selection lasts only for one invocation. `storage get --key` selects the
frame and reads it in the same invocation. `tool` reports the available names
when given an unknown name. It rejects standalone screencast start/stop calls
because those calls cannot share recording state across processes.

## Recording

```bash
bt screencast record --dir ./capture --duration 10 --format png
```

The command stays running while another CLI process drives the page. It reports
readiness on stderr. Send SIGINT or SIGTERM to the recording process to finish
early, or set `--duration` or `--max-frames` (default 600). A successful capture
writes frame files and `frames.json` to a new or empty directory. Files are
private and existing files are never overwritten. Connection loss saves any
received frames and exits 1; a process crash or SIGKILL can lose buffered frames.

`screencast start` is an alias for `record` and requires `--dir`.
`screencast stop` explains how to signal the owner; it does not control another
process. There is no background capture service.

## Project Configuration and login state

Use `bt launch --profile NAME` to retain login state across stops and restarts.
Profiles live under `~/.cache/browser-tools/profiles`; set
`BROWSER_TOOLS_PROFILES_DIR` to use a different root. Names use 1-64 characters
from `[A-Za-z0-9._-]`, excluding `.` and `..`. `.ephemeral`, in any letter case,
is reserved for unnamed Camoufox sessions. A live profile cannot be launched
again. Stopping a named instance preserves its directory.

The CLI does not read `.browser-tools.json`. Launch flags and environment
variables supply launch defaults. `BROWSER_TOOLS_REGISTRY` overrides the
registry path, whose default remains `/tmp/chrome-agent/registry.json`.

On named launch, eligible idle profiles from `/tmp/browser-tools-profiles` move
to the new root. Live or uncertain holders defer migration. Deferred profiles
are retried on later launches; an unmigrated requested profile never becomes a
new empty profile. Existing destination copies win collisions, with a warning.
Unsafe paths and cross-filesystem moves are refused. Setting
`BROWSER_TOOLS_PROFILES_DIR` bypasses migration.

Stop concurrent old-version launch commands before upgrading. The new lifecycle
lock coordinates this version's commands, not older or external launchers.
Old `.ephemeral` sessions retain their paths until normal cleanup. Old MCP
profiles under `~/.cache/tool-proxy/browser-tools/profiles` are left untouched;
deleting them discards their login state. To roll back a migrated profile, stop
its browser first and move its directory back only if the old destination is
absent.

## Camoufox and profiling

`uv tool install 'browser-tools[camoufox]'` adds the optional Camoufox engine.
`launch --engine camoufox` starts its persistent browser context. Camoufox has
no CDP port: CLI navigation, interaction, snapshots, and recording do not drive
it. Its retained driver awaits the command-channel RFC described in RFC-02.

`browser-tools-profiler` remains a separate CDP CPU profiler. The `profiling`
extra adds Pillow for enhanced screenshot blank-frame detection.

## Architecture

- `cli`: argument parsing, dispatch, and exit codes.
- `lifecycle`: registry adaptation, Named Profiles, migration, and lifecycle locks.
- `one_shot`: target selection and isolated CDP sessions.
- `passthrough`, `events`, `list_verbs`: raw protocol and event collection.
- `curated`: handler orchestration and foreground Capture.
- `cdp_handler`: session-bound CDPRuntime and tool implementations.
- `tool_registry`: one handler table and invocation policies.
- `interstitial`: detection, retry policy, and override loading.
- `native_snapshot`, `native_interaction`: accessibility trees and UID actions.
- `screencast`, `screenshot_utils`: frame recording and screenshot checks.
- `frame_manager`: frame tree and execution-context tracking.
- `process_utils`: exact process argv and process identity helpers.
- `camoufox_runner`: detached Camoufox lifecycle host.
- `profiler`: standalone CPU profiling.
- `core/`: vendored CDP client and registry, adapted launcher and supervisor,
  plus the optional typed `domains/` layer.

The MCP front and its Node subprocess were retired by RFC-02. The retained
`mcp_response` module only encodes and reads handler result envelopes.

## Development

```bash
uv sync
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest tests/ -q --ignore=tests/test_e2e_camoufox.py
```

Capture integration tests require Chrome. The Camoufox end-to-end suite also
requires its downloaded browser binary. See [CONTRIBUTING.md](CONTRIBUTING.md),
[SECURITY.md](SECURITY.md), and [LICENSE](LICENSE).

The development parity gate uses a pinned Node reference engine under `tests/parity`; Node is not a dependency of the installed CLI. Capture artifact finalization has a 30-second outer deadline, with five-second transport-stop and runtime-cleanup bounds.
