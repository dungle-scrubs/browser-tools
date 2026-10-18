# Browser Tools

Automate and debug Chrome from the command line.

Use `bt` to launch named browser instances, inspect rendered pages, click and fill elements, and read console or network activity. Commands reuse the running browser's page and cookies. Regular Google Chrome stable is the default. Optional Camoufox support covers browser launch and lifecycle, not CDP page commands.

```console
$ bt launch --headless -- https://example.com
...
$ bt wait-idle
...
$ bt eval 'document.title'
{
  "value": "Example Domain",
  "type": "string",
  ...
}
$ bt stop
...
```

## Install

Requires **Python 3.13+** and a local Chrome or Chromium installation. Regular **Google Chrome stable** is the default; Canary is not required.

```bash
uv tool install browser-tools
bt --version
```

Alternatively, install with `pip install browser-tools` in a Python environment. The install provides `bt`, its alias `browser-tools`, and `browser-tools-profiler`.

The default install depends on `websockets` only and brings no Node.js with it. After installation, run the example above to confirm browser access. Use `bt launch` without `--headless` for a visible browser.

## Usage

Unless a block includes `launch`, use a browser you already launched. Run `bt stop` when finished. Commands below omit the instance name; with several browsers, put the name from launch output before the command, as in `bt web-01 snapshot`.

### Read a page and save a screenshot

```bash
bt navigate https://example.com
bt wait-idle
bt snapshot
bt screenshot --path example.png
```

`navigate` does not wait for loading. A snapshot returns the accessibility tree with element UIDs; use those with `bt click --uid UID` or `bt fill --uid UID --text 'hello'`. Take a new snapshot after navigation.

### Keep a login across restarts

```bash
bt launch --profile demo
# Log in by hand in this window.
bt stop
bt launch --profile demo
bt snapshot
```

A named profile keeps cookies and login state across restarts. Without `--profile`, each launch starts with a fresh temporary profile; one live instance can hold a named profile at a time.

### Run dependent steps in one connection

```bash
bt run - <<'STEPS'
Page.navigate '{"url":"https://example.com"}'
wait-idle
eval 'document.title'
snapshot
STEPS
```

Each line is a command without `bt`; use raw `Page.navigate` inside a run. The run shares a CDP session and stops at the first failure, reporting completed steps without undoing them.

### Inspect console messages and network traffic

```bash
bt console-list --duration 2
bt network-get --url example.com --reload
bt network-list --duration 2
```

`network-get --reload` reloads the page and returns a matching response body. Network observation covers traffic during that command, not earlier commands; `console-list` also receives Chrome's buffered console messages.

### Capture a page load or memory snapshot

```bash
cat > load.steps <<'STEPS'
Page.navigate '{"url":"https://example.com"}'
wait-idle
STEPS
bt trace --steps load.steps --out load.json
bt heap --out page.heapsnapshot
```

Open the trace in Chrome DevTools' Performance panel and the heap snapshot in its Memory panel. Optional `bt insights` analyzes traces locally, but needs [explicit setup and a compatible trace version](docs/reference.md#captures-and-optional-dependencies).

### Launch Camoufox

```bash
uv tool install --with-executables-from camoufox 'browser-tools[camoufox]'
camoufox fetch
bt launch --engine camoufox --profile camoufox-demo
bt status
bt stop
```

Camoufox is an optional anti-detect Firefox browser; `camoufox fetch` downloads it. Browser Tools manages its launch, profiles, status, and stop, but page-interaction and CDP commands require Chrome or Chromium.

## Reference

```text
bt launch [--headless] [--profile NAME] [--name NAME] [--channel stable|beta|dev|canary]
bt [INSTANCE] COMMAND [OPTIONS]
bt [INSTANCE] Domain.method '{"key":"value"}'
bt status [INSTANCE]
bt stop [INSTANCE]
bt guide
```

`launch` appends the first free two-digit suffix to its base name, such as `web-01`. Without `--name`, the base comes from the current directory. Repeated unnamed launches from one directory succeed with successive suffixes. Each launch prints the chosen name in its JSON result.

A plain launch outlives the command until stopped. `--owner PID` opts into closing it when that process exits, and `bt retain INSTANCE` keeps it for a handoff.

| Task | Commands |
| --- | --- |
| Inspect and interact | `snapshot`, `click`, `fill`, `eval`, `press`, `hover`, `type`, `detect` |
| Wait for page changes | `wait-text`, `wait-idle`, `wait-stable` |
| Observe browser activity | `attach`, `wait`, `console-list`, `network-list`, `network-get` |
| Inspect frames and storage | `frames`, `storage get` |
| Capture and profile | `screenshot`, `screencast`, `trace`, `heap`, `insights`, `browser-tools-profiler` |
| Manage saved state | `profile list`, `profile delete`, `profile migrate`, `cleanup` |

`bt guide` is the full installed manual, including raw CDP recipes, dialog policy, frame selection, and command output. `bt COMMAND --help` lists flags; `bt help Domain.method` reads the protocol signature from a running browser.

See [the reference](docs/reference.md) for the full command inventory, configuration, external-browser setup, compatibility limits, and architecture. To drive a Chrome you started for debugging on its own user-data directory, use `--chrome-profile DIR` or `--endpoint URL`; your everyday Chrome profile is not attachable through this path.

### Configuration

Named profiles use `$BROWSER_TOOLS_PROFILES_DIR`, then `$XDG_DATA_HOME/browser-tools/profiles`, then `~/.local/share/browser-tools/profiles`. For example:

```bash
BROWSER_TOOLS_PROFILES_DIR="$HOME/.local/share/bt-profiles" bt launch --profile demo
```

`BROWSER_TOOLS_CHROME_BINARY` selects an explicit executable. Without an override, regular Chrome stable is auto-detected; `--channel` selects another installed channel. [Other settings](docs/reference.md#profiles-and-configuration) include the registry path and window border.

### Output and exit codes

Most commands print JSON to stdout. Help and the guide print text; `attach` streams JSON Lines. Diagnostics go to stderr. Check the exit code before parsing results, because a failed run or capture can still print partial output.

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Operational failure, such as a closed browser, timeout, or stale UID |
| `2` | Invalid or refused usage |

### Security

A debugging endpoint grants control of the browser and its cookies. Connect only to browsers you started or were authorized to use. External endpoints accept only `127.0.0.1` and `::1`; do not assume port 9222 belongs to your instance.

`stop` and `cleanup` do not manage external browsers. `Browser.close` and `Browser.crash` are refused over external endpoints. `profile delete NAME` deletes that profile's saved login state. Report vulnerabilities through [SECURITY.md](SECURITY.md).

## Contributing

```bash
git clone https://github.com/dungle-scrubs/browser-tools.git
cd browser-tools
uv sync
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/ -q
```

Run the checkout's CLI with `uv run bt`, since a separately installed `bt` can be older. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE) and third-party attribution in [NOTICE](NOTICE).
