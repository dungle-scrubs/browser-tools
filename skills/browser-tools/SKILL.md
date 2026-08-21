---
name: browser-tools
description: >-
  Drive a real browser from the command line: launch and track named Chrome or
  Camoufox instances, send any Chrome DevTools Protocol method, stream CDP
  events, and read the live protocol schema. Use when a task needs to open,
  inspect, automate, or debug a web page, capture console or network activity,
  wait on a browser event, or run an anti-detect browser against a bot-protected
  site. The CLI is the primary surface; an optional MCP front covers harnesses
  that cannot run a CLI.
---

# browser-tools

`browser-tools` (alias `bt`) is a registry-backed browser lifecycle CLI. It
tracks named browser instances, sends raw CDP, and streams CDP events. It runs
no inference and solves no captchas; anti-detection is launch-flag spoofing
only.

Every command prints machine-readable JSON to stdout and diagnostics to stderr.
Exit codes: `0` success, `1` operational failure (browser/CDP error, timeout),
`2` usage error.

Confirm the surface before you rely on it: `browser-tools --help`, then
`browser-tools VERB --help`. This document is written against that output and is
kept from drifting by `tests/test_skill_matches_help.py` (see "Keeping this
skill honest").

## The verb set

These are the top-level verbs the CLI accepts. The block below is
machine-checkable: the drift test parses it and asserts it equals the CLI's real
top-level verbs, so it can never silently fall out of date.

```verbs
launch
status
stop
cleanup
guide
help
attach
wait
console-list
network-list
```

Two more forms do not appear in that list because they are not fixed argparse
subcommands; the CLI disambiguates them against the registry before parsing:

- **Raw protocol**: `[INSTANCE] Domain.method '{...json...}' [--target SPEC]`
- **Live help**: `help [INSTANCE] [Domain.method]`

## The instance model

An **instance** is one running browser process tracked in a registry. Its
**name** is derived from the working directory (lowercase, hyphenated, stripped
to `[a-z0-9.-]`, fallback `chrome`), with a `-NN` suffix on collision.

Every verb that operates on a browser takes a leading `[INSTANCE]`. You may
**omit** it when exactly one instance is running. With several running, omitting
it fails and lists the candidates rather than guessing. A bare leading token is
read as an instance name if the registry knows it, else as a `Domain.method`.

## Lifecycle verbs

### launch
```
launch [--engine chrome|camoufox] [--profile NAME] [--channel NAME]
       [--headless] [--port PORT] [--fingerprint FILE] [--no-window-border]
       [-- BROWSER_ARGS]
```
Launch a browser and register it; prints the new instance as JSON (`name`,
`port`, `pid`, `engine`, `profile`, `browser_version`, `user_data_dir`).

- `--engine` — `chrome` (default) or `camoufox`. There is no other engine value.
  `chrome` is the native default engine (direct CDP, no Node). `camoufox` needs
  the `camoufox` extra (see "Anti-detect").
- `--profile NAME` — bind a persistent, per-profile user-data-dir (see
  "Profiles").
- `--channel NAME` — Chrome release channel: `stable`, `beta`, `dev`, `canary`.
  Resolved to an installed Chrome binary path before launch.
- `--headless` — run without a visible window. Headless launches get no
  supervisor, so no window marking.
- `--port PORT` — CDP port. Default: auto-allocate.
- `--fingerprint FILE` — a launch-flag fingerprint profile (see "Anti-detect").
- `--no-window-border` — suppress the agent-window marking (see "Window
  marking").
- Everything after `--` is passed verbatim to the browser.

### status
```
status [INSTANCE]
```
Show every registered instance with liveness, engine, profile, and page targets;
with `INSTANCE`, only that one. Liveness is engine-aware (Chrome: process
identity plus CDP port attribution; Camoufox: process identity plus
user-data-dir hold), never PID existence alone. An unparseable registry reads as
a single `{"status": "unknown", ...}` row, not as "no instances".

### stop
```
stop [INSTANCE] [--target SPEC]
```
Stop a browser (`Browser.close`, then session-dir cleanup), or close one tab
with `--target`. A profile's user-data-dir persists across `stop`; ephemeral
dirs are reaped. `INSTANCE` omittable only when exactly one instance runs.

### cleanup
```
cleanup
```
Remove stale registry entries and orphaned session directories. Live instances
are never touched. A corrupt registry is quarantined, and nothing is deleted on
its basis.

### guide
```
guide
```
Print the bundled agent manual (`lifecycle.guide_text()`). It is the same
lifecycle/raw-protocol summary the tool ships in-band; read it when you cannot
run `--help` for some reason.

## Raw protocol and live help

The passthrough is the core of the tool: any CDP method the installed browser
supports works without a curated verb for it.

```
[INSTANCE] Domain.method '{...json params...}' [--target SPEC]
```
Send `Domain.method` with JSON params straight to the browser over CDP and print
the JSON result. `--target SPEC` selects a page target (index or id).

```
help [INSTANCE] [Domain.method]
```
With a running instance, `help` reads the live CDP protocol schema **from that
browser**, not a bundled copy — so it matches exactly what the installed browser
supports. Without a running instance it prints static usage. A `Domain` or
`Domain.method` argument narrows the schema.

Curated high-level actions (snapshot, click, fill, screenshot, and similar) are
**not** their own verbs in this CLI. Reach them today through the passthrough by
sending the underlying CDP methods; `references/passthrough-recipes.md` has
worked examples. (The RFC reserves dedicated verbs for these in a later phase;
until they exist, they are not documented here, because this skill matches the
CLI as it actually is.)

## Events

```
attach [INSTANCE] +Domain.event [+Domain.event ...] [--target SPEC] [--url SUBSTRING]
```
Stream subscribed CDP events as JSON lines. Subscriptions are isolated per
session: two observers never see each other's subscription set. `--target` and
`--url SUBSTRING` are mutually exclusive ways to pick the page target.

```
wait [INSTANCE] --event Domain.event [--match SUBSTRING] [--timeout SECONDS] [--target SPEC] [--url SUBSTRING]
```
Block until one matching CDP event fires. `wait` subscribes **before** it starts
examining events, so an event that fires between subscription and examination is
buffered, not lost. `--match` is a substring test against the event's JSON.
`--timeout` defaults to 30 seconds; `--timeout 0` means no deadline. On match:
the event JSON on stdout, exit 0. On deadline: a timeout error on stderr, exit 1,
no partial stdout.

```
console-list [INSTANCE] [--target SPEC] [--url SUBSTRING] [--duration SECONDS]
network-list [INSTANCE] [--target SPEC] [--url SUBSTRING] [--duration SECONDS]
```
Collect console messages / network requests over a short attach window.
`--duration` defaults to `2.0` seconds. Both are thin wrappers over a brief
attach session.

## Profiles

A **profile** is a named, persistent browser identity (a user-data-dir plus
recorded launch attributes) bound at launch with `--profile NAME`. It is
**exclusive**: at most one live instance holds a profile at a time. `launch
--profile NAME` cleans a stale singleton lock from a dead run, then fails (exit
1, naming the holder) if a live instance already holds it — never a second
browser on the same dir, never a steal. A profile's directory persists across
`stop`; only unbound/ephemeral instances have their dir reaped.

A profile (the identity) is distinct from a fingerprint profile (launch flags).
The bare word "profile" always means the identity.

## Anti-detect

Two independent mechanisms, both flags-only (no JavaScript injection):

- **Fingerprint profiles** (`--fingerprint FILE`) alter what a page observes
  through Chrome launch flags. The file is JSON with `userAgent`, `viewport`
  (`{"width", "height"}`), `language`, and `timezone`; the CLI turns these into
  `--user-agent`, `--window-size`, `--lang`, and a `TZ` environment variable.
  When a fingerprint is active the window marking is suppressed automatically,
  because the border is page-observable.
- **Camoufox** (`--engine camoufox`) starts a Firefox-based anti-detect engine
  as a detached instance for bot-protected sites. It needs its extra:
  `pip install 'browser-tools[camoufox]'`. Camoufox exposes no Chrome debugging
  port, so its liveness is process identity plus user-data-dir hold, and page
  targets are not enumerated for it.

## Window marking

By default a headed launch marks its window: the per-instance supervisor draws a
colored border plus a corner badge and prefixes the tab title, so a human can
tell an agent-driven window from their own. `--no-window-border` suppresses it.
Marking is also suppressed under a fingerprint profile, and headless launches
are never marked (no window, no supervisor).

## Optional MCP front

The CLI is the primary surface. An **optional MCP front** (the daemon/broker in
`mcp_daemon.py`, `mcp_broker.py`, `daemon_supervisor.py`) remains available for
harnesses that cannot run a CLI. It exposes the frozen MCP tool surface — the
`tool_registry.py` tools plus the session-layer lifecycle tools
(`use_browser_session`, `attach_browser`, `close_browser`, `launch_camoufox` and
peers) — over the same merged dispatch the CLI uses. It is optional at import
time and is not required for any CLI verb.

Prefer the CLI. Use the MCP front only when the harness genuinely cannot invoke
a command-line program. The two are two spellings of one surface; some CLI verbs
map to frozen MCP tool names (for example `frames list` ↔ `list_frames`,
`screenshot` ↔ `take_screenshot`, `detect` ↔ `inspect_blocked`/`inspect_warn`),
but those curated verbs are not in the current CLI verb set — see "Raw protocol
and live help".

## Keeping this skill honest

The `verbs` block under "The verb set" is the single source of the documented
verb list. `tests/test_skill_matches_help.py` parses that fenced block and
asserts it exactly equals the CLI's real top-level verbs, derived from
`browser_tools.cli.build_parser()`. If a verb is added, removed, or renamed in
the CLI, the test fails until the block is updated. Never edit the block to match
prose; edit it to match the CLI, then rerun the test.
