"""The merged browser-tools CLI front (RFC-01 Phase 1, layer 4).

New code that owns argument parsing, verb dispatch, and exit codes. It calls
the vendored core (registry, launcher, instance status) through
``browser_tools.lifecycle``, and the raw-protocol/live-schema verbs through
``browser_tools.passthrough``; the vendored ``cli.py`` is not shipped and its
``main()`` is never reached.

Two verbs do not fit argparse's fixed subcommand set: the raw-protocol line
(``[INSTANCE] Domain.method '{...}' [--target SPEC]``) and ``help
[INSTANCE] [Domain.method]``. Their leading token is caller-supplied (an
instance name or a CDP ``Domain.method``), so ``main`` disambiguates it
against the registry before argparse ever runs (RFC-01 #37, "Instance-vs-
method disambiguation").

Ships as two console scripts naming one program: ``browser-tools`` (canonical)
and ``bt`` (alias). Both resolve to ``main``.

Exit codes (RFC-01 "Exit codes"): 0 success; 1 operational failure (browser
error, CDP error, timeout); 2 usage error (argparse). Machine-readable output
is JSON on stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import curated, events, lifecycle, list_verbs, passthrough, user_settings
from .lifecycle import LifecycleError
from .passthrough import UsageError as PassthroughUsageError

PROG = "browser-tools"

EXIT_OK = 0
EXIT_OPERATIONAL = 1
EXIT_USAGE = 2

#: Verbs argparse owns directly. Any other leading token is a candidate for
#: raw-protocol dispatch (RFC-01 "Instance names": a bare leading token
#: resolves as an instance name if the registry knows it, else as a
#: Domain.method); a token that fits neither shape falls through to argparse,
#: which rejects it as an unknown verb, unchanged from before this ticket.
_KNOWN_VERBS = {
    "launch",
    "status",
    "stop",
    "cleanup",
    "guide",
    "window-border",
    "help",
    "attach",
    "wait",
    "console-list",
    "network-list",
    "snapshot",
    "click",
    "fill",
    "wait-idle",
    "wait-stable",
    "detect",
    "frames",
    "storage",
    "screenshot",
    "screencast",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser and its lifecycle subcommands."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Registry-backed browser lifecycle (launch, status, stop, cleanup, guide).",
    )
    sub = parser.add_subparsers(dest="command", metavar="VERB")

    launch = sub.add_parser("launch", help="Launch a browser and register it")
    launch.add_argument(
        "--engine",
        choices=list(lifecycle.VALID_ENGINES),
        default=lifecycle.DEFAULT_ENGINE,
        help="Browser engine (default: chrome)",
    )
    launch.add_argument("--profile", metavar="NAME", help="Named profile to record for this instance")
    launch.add_argument("--channel", metavar="NAME", help="Chrome release channel (stable/beta/dev/canary)")
    launch.add_argument("--headless", action="store_true", help="Run without a visible window")
    launch.add_argument("--port", type=int, metavar="PORT", help="CDP port (default: auto-allocate)")
    launch.add_argument("--fingerprint", metavar="FILE", help="Fingerprint profile file (launch flags only)")
    launch.add_argument(
        "--no-window-border",
        action="store_true",
        help=(
            "Do not mark this window at all (no tab-title prefix, no border). To keep "
            "the title prefix and only hide the border, use: window-border off"
        ),
    )
    launch.add_argument(
        "browser_args",
        nargs=argparse.REMAINDER,
        metavar="-- BROWSER_ARGS",
        help="Extra args after -- are passed verbatim to the browser",
    )

    status = sub.add_parser("status", help="Show registered instances and liveness")
    status.add_argument("instance", nargs="?", metavar="INSTANCE", help="Limit to one instance")

    stop = sub.add_parser("stop", help="Stop a browser or close one tab")
    stop.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance to stop (omit if only one)")
    stop.add_argument("--target", metavar="SPEC", help="Close a single tab instead of the browser")

    sub.add_parser("cleanup", help="Remove stale registry entries and session dirs")
    sub.add_parser("guide", help="Print the bundled agent manual")

    border = sub.add_parser(
        "window-border",
        help="Show, or persistently turn on/off, the border drawn in marked windows",
        description=(
            "The window marker draws a colored border and a corner badge over the page "
            "of every window it marks. They cover the page's outer edge and top-left "
            "corner. 'off' removes them from every running browser within a second and "
            "keeps them off for later launches, until 'on'. The tab-title prefix is not "
            "affected. With no argument, prints the current setting."
        ),
    )
    border.add_argument("state", nargs="?", choices=["on", "off"], help="New setting")

    help_cmd = sub.add_parser(
        "help", help="Live CDP protocol help from a running instance, or static usage"
    )
    help_cmd.add_argument(
        "args",
        nargs="*",
        metavar="[INSTANCE] [Domain.method]",
        help="Optional instance name and/or a Domain or Domain.method query",
    )

    attach = sub.add_parser("attach", help="Stream subscribed CDP events as JSON lines")
    attach.add_argument(
        "args",
        nargs="*",
        metavar="[INSTANCE] +Domain.event ...",
        help="Optional instance name followed by one or more +Domain.event subscriptions",
    )
    attach.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    attach.add_argument("--url", metavar="SUBSTRING", help="Select the page target by URL substring")

    wait = sub.add_parser("wait", help="Block until one matching CDP event fires")
    wait.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    wait.add_argument("--event", required=True, metavar="Domain.event", help="CDP event to wait for")
    wait.add_argument("--match", metavar="SUBSTRING", help="Substring the event JSON must contain")
    wait.add_argument(
        "--timeout",
        type=float,
        default=events.DEFAULT_WAIT_TIMEOUT,
        metavar="SECONDS",
        help="Deadline in seconds (default: 30; 0 means no deadline)",
    )
    wait.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    wait.add_argument("--url", metavar="SUBSTRING", help="Select the page target by URL substring")

    console_list = sub.add_parser(
        "console-list", help="Collect console messages over a short attach window"
    )
    console_list.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    console_list.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    console_list.add_argument("--url", metavar="SUBSTRING", help="Select the page target by URL substring")
    console_list.add_argument(
        "--duration",
        type=float,
        default=list_verbs.DEFAULT_LIST_WINDOW_SECONDS,
        metavar="SECONDS",
        help="Collection window in seconds (default: 2.0)",
    )

    network_list = sub.add_parser(
        "network-list", help="Collect network requests/responses over a short attach window"
    )
    network_list.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    network_list.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    network_list.add_argument("--url", metavar="SUBSTRING", help="Select the page target by URL substring")
    network_list.add_argument(
        "--duration",
        type=float,
        default=list_verbs.DEFAULT_LIST_WINDOW_SECONDS,
        metavar="SECONDS",
        help="Collection window in seconds (default: 2.0)",
    )

    _add_curated_verbs(sub)

    return parser


def _add_curated_verbs(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Add the curated tool verbs (RFC-01 #50).

    Each verb fronts an existing curated tool through ``browser_tools.curated``.
    A leading ``[INSTANCE]`` is optional and omittable when exactly one instance
    is running, matching the other browser verbs. Required per-verb inputs
    (``--uid``, ``--text``, a ``frames``/``screencast`` sub-action) are left
    optional at the argparse layer and validated in ``_run`` so the parser still
    accepts the bare verb (the skill drift test parses ``VERB`` alone).
    """
    snapshot = sub.add_parser("snapshot", help="Native UID accessibility tree")
    snapshot.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")

    click = sub.add_parser("click", help="Native UID click")
    click.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    click.add_argument("--uid", metavar="N", help="UID from a prior snapshot")

    fill = sub.add_parser("fill", help="Native UID fill")
    fill.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    fill.add_argument("--uid", metavar="N", help="UID from a prior snapshot")
    fill.add_argument("--text", metavar="T", help="Text to fill")

    wait_idle = sub.add_parser("wait-idle", help="Wait for network idle")
    wait_idle.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    wait_idle.add_argument(
        "--timeout-ms", type=int, default=curated.DEFAULT_WAIT_TIMEOUT_MS, metavar="MS",
        help="Overall deadline in ms (default: 5000)",
    )
    wait_idle.add_argument(
        "--idle-ms", type=int, default=curated.DEFAULT_IDLE_MS, metavar="MS",
        help="Quiet window in ms (default: 500)",
    )

    wait_stable = sub.add_parser("wait-stable", help="Wait for DOM quiescence")
    wait_stable.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    wait_stable.add_argument(
        "--timeout-ms", type=int, default=curated.DEFAULT_WAIT_TIMEOUT_MS, metavar="MS",
        help="Overall deadline in ms (default: 5000)",
    )
    wait_stable.add_argument(
        "--stable-ms", type=int, default=curated.DEFAULT_STABLE_MS, metavar="MS",
        help="Quiescence window in ms (default: 300)",
    )

    detect = sub.add_parser("detect", help="Run interstitial detection against the current page")
    detect.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    detect_wait = detect.add_mutually_exclusive_group()
    detect_wait.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help="Retry a self-clearing challenge for up to SECONDS (default: 9)",
    )
    detect_wait.add_argument(
        "--no-wait",
        action="store_true",
        help="Report the current state at once; do not retry",
    )

    frames = sub.add_parser("frames", help="Inspect or select page frames")
    frames_sub = frames.add_subparsers(dest="frames_action", metavar="ACTION")
    fl = frames_sub.add_parser("list", help="List frames")
    fl.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    fs = frames_sub.add_parser("select", help="Select a frame by URL pattern")
    fs.add_argument("pattern", metavar="PATTERN", help="Frame URL substring/pattern")
    fs.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    fr = frames_sub.add_parser("reset", help="Clear frame selection")
    fr.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")

    storage = sub.add_parser("storage", help="Read a frame's storage")
    storage_sub = storage.add_subparsers(dest="storage_action", metavar="ACTION")
    sg = storage_sub.add_parser("get", help="Read the selected frame's storage")
    sg.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    sg.add_argument("--key", metavar="K", help="Frame URL pattern to select before reading")

    screenshot = sub.add_parser("screenshot", help="Capture a page screenshot")
    screenshot.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    screenshot.add_argument("--path", metavar="FILE", help="Write the PNG to a file instead of stdout")
    screenshot.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    screenshot.add_argument("--url", metavar="SUBSTRING", help="Select the page target by URL substring")

    screencast = sub.add_parser("screencast", help="Start or stop screencast capture")
    screencast_sub = screencast.add_subparsers(dest="screencast_action", metavar="ACTION")
    cast_start = screencast_sub.add_parser("start", help="Begin capture")
    cast_start.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    cast_start.add_argument("--format", dest="format", default="jpeg", metavar="FMT", help="jpeg or png (default: jpeg)")
    cast_start.add_argument("--max-frames", type=int, default=600, metavar="N", help="Frame cap (default: 600)")
    cast_stop = screencast_sub.add_parser("stop", help="Stop capture and write frames")
    cast_stop.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    cast_stop.add_argument("--dir", dest="dir", metavar="DIR", help="Directory to write frames into")


def _print_json(payload: object) -> None:
    """Emit machine-readable output to stdout as JSON."""
    print(json.dumps(payload, indent=2))


def _browser_args_from_remainder(remainder: list[str] | None) -> list[str]:
    """Return the verbatim BROWSER_ARGS that follow ``--``, or reject the remainder.

    ``launch`` takes no positional arguments. argparse.REMAINDER starts
    collecting at the first bare token and swallows every option after it, so
    ``bt launch e2e-01 --headless`` would silently hand ``e2e-01 --headless``
    to Chrome: Chrome opens ``http://e2e-01/`` and runs headless while
    browser-tools believes the launch is headed (spawning the supervisor,
    marking the window). A remainder that does not start with ``--`` is
    therefore a usage error, not browser args.
    """
    if not remainder:
        return []
    if remainder[0] == "--":
        return remainder[1:]
    raise PassthroughUsageError(
        f"launch takes no positional arguments (got {remainder[0]!r}); instance "
        f"names are assigned by the registry; use --profile NAME to bind a named "
        f"profile. Flags for the browser itself go after '--', e.g. "
        f"bt launch --headless -- --proxy-server=host:port"
    )


def _run(args: argparse.Namespace) -> int:
    """Dispatch one parsed verb. Raises LifecycleError for operational failures."""
    registry_path = lifecycle.registry_path_from_env()

    if args.command == "launch":
        instance = lifecycle.launch(
            engine=args.engine,
            profile=args.profile,
            channel=args.channel,
            headless=args.headless,
            port=args.port,
            fingerprint=args.fingerprint,
            window_border=not args.no_window_border,
            browser_args=_browser_args_from_remainder(args.browser_args),
            registry_path=registry_path,
        )
        _print_json(
            {
                "name": instance.name,
                "port": instance.port,
                "pid": instance.pid,
                "engine": instance.engine,
                "profile": instance.profile,
                "browser_version": instance.browser_version,
                "user_data_dir": instance.user_data_dir,
            }
        )
        return EXIT_OK

    if args.command == "status":
        _print_json(lifecycle.status(instance=args.instance, registry_path=registry_path))
        return EXIT_OK

    if args.command == "stop":
        message = lifecycle.stop(
            instance=args.instance,
            target=args.target,
            registry_path=registry_path,
        )
        _print_json({"stopped": True, "message": message})
        return EXIT_OK

    if args.command == "cleanup":
        removed = lifecycle.cleanup(registry_path=registry_path)
        _print_json({"removed": removed})
        return EXIT_OK

    if args.command == "guide":
        print(lifecycle.guide_text())
        return EXIT_OK

    if args.command == "window-border":
        if args.state is not None:
            user_settings.set_window_border(args.state == user_settings.ON)
        enabled = user_settings.window_border_enabled()
        _print_json(
            {
                "window_border": user_settings.ON if enabled else user_settings.OFF,
                "settings_path": str(user_settings.settings_path()),
            }
        )
        return EXIT_OK

    if args.command == "help":
        instance, query = passthrough.resolve_help_args(args.args, registry_path=registry_path)
        passthrough.run_help(instance, query, registry_path=registry_path)
        return EXIT_OK

    if args.command == "attach":
        if args.target is not None and args.url is not None:
            raise PassthroughUsageError("cannot specify both --target and --url")
        instance, subscriptions = events.resolve_attach_args(args.args)
        events.run_attach(
            instance=instance,
            events=subscriptions,
            target=args.target,
            url=args.url,
            registry_path=registry_path,
        )
        return EXIT_OK

    if args.command == "wait":
        if args.target is not None and args.url is not None:
            raise PassthroughUsageError("cannot specify both --target and --url")
        event = events.wait(
            instance=args.instance,
            event=args.event,
            match=args.match,
            timeout=args.timeout,
            target=args.target,
            url=args.url,
            registry_path=registry_path,
        )
        _print_json(event)
        return EXIT_OK

    if args.command == "console-list":
        if args.target is not None and args.url is not None:
            raise PassthroughUsageError("cannot specify both --target and --url")
        messages = list_verbs.console_list(
            instance=args.instance,
            target=args.target,
            url=args.url,
            duration=args.duration,
            registry_path=registry_path,
        )
        _print_json(messages)
        return EXIT_OK

    if args.command == "network-list":
        if args.target is not None and args.url is not None:
            raise PassthroughUsageError("cannot specify both --target and --url")
        requests = list_verbs.network_list(
            instance=args.instance,
            target=args.target,
            url=args.url,
            duration=args.duration,
            registry_path=registry_path,
        )
        _print_json(requests)
        return EXIT_OK

    if args.command in _CURATED_COMMANDS:
        return _run_curated(args, registry_path)

    # No verb given: usage.
    return EXIT_USAGE


#: Curated verbs dispatched through ``browser_tools.curated`` (RFC-01 #50).
_CURATED_COMMANDS = frozenset(
    {
        "snapshot",
        "click",
        "fill",
        "wait-idle",
        "wait-stable",
        "detect",
        "frames",
        "storage",
        "screenshot",
        "screencast",
    }
)


def _run_curated(args: argparse.Namespace, registry_path: str | None) -> int:
    """Dispatch one curated verb to its ``curated`` implementation.

    Each branch calls the same implementation the matching MCP tool uses.
    Missing required inputs raise ``PassthroughUsageError`` (exit 2); the
    operational failures the ``curated`` functions raise are ``LifecycleError``
    (exit 1), handled by the caller.
    """
    if args.command == "snapshot":
        _print_json(curated.snapshot(instance=args.instance, registry_path=registry_path))
        return EXIT_OK

    if args.command == "click":
        if not args.uid:
            raise PassthroughUsageError("click requires --uid N (a UID from a prior snapshot)")
        _print_json(curated.click(instance=args.instance, uid=args.uid, registry_path=registry_path))
        return EXIT_OK

    if args.command == "fill":
        if not args.uid:
            raise PassthroughUsageError("fill requires --uid N (a UID from a prior snapshot)")
        if args.text is None:
            raise PassthroughUsageError("fill requires --text T")
        _print_json(
            curated.fill(
                instance=args.instance, uid=args.uid, text=args.text, registry_path=registry_path
            )
        )
        return EXIT_OK

    if args.command == "wait-idle":
        _print_json(
            curated.wait_idle(
                instance=args.instance,
                timeout_ms=args.timeout_ms,
                idle_ms=args.idle_ms,
                registry_path=registry_path,
            )
        )
        return EXIT_OK

    if args.command == "wait-stable":
        _print_json(
            curated.wait_stable(
                instance=args.instance,
                timeout_ms=args.timeout_ms,
                stable_ms=args.stable_ms,
                registry_path=registry_path,
            )
        )
        return EXIT_OK

    if args.command == "detect":
        wait_seconds = 0.0 if args.no_wait else args.wait
        _print_json(
            curated.detect(
                instance=args.instance,
                registry_path=registry_path,
                wait_seconds=wait_seconds,
            )
        )
        return EXIT_OK

    if args.command == "frames":
        return _run_frames(args, registry_path)

    if args.command == "storage":
        action = getattr(args, "storage_action", None)
        if action != "get":
            raise PassthroughUsageError("storage takes one sub-action: get")
        _print_json(
            curated.storage_get(instance=args.instance, key=args.key, registry_path=registry_path)
        )
        return EXIT_OK

    if args.command == "screenshot":
        _print_json(
            curated.screenshot(
                instance=args.instance,
                path=args.path,
                target=args.target,
                url=args.url,
                registry_path=registry_path,
            )
        )
        return EXIT_OK

    if args.command == "screencast":
        return _run_screencast(args, registry_path)

    return EXIT_USAGE


def _run_frames(args: argparse.Namespace, registry_path: str | None) -> int:
    """Dispatch ``frames list|select|reset``."""
    action = getattr(args, "frames_action", None)
    if action == "list":
        _print_json(curated.frames_list(instance=args.instance, registry_path=registry_path))
        return EXIT_OK
    if action == "select":
        _print_json(
            curated.frames_select(
                instance=args.instance, pattern=args.pattern, registry_path=registry_path
            )
        )
        return EXIT_OK
    if action == "reset":
        _print_json(curated.frames_reset(instance=args.instance, registry_path=registry_path))
        return EXIT_OK
    raise PassthroughUsageError("frames takes one sub-action: list, select, or reset")


def _run_screencast(args: argparse.Namespace, registry_path: str | None) -> int:
    """Dispatch ``screencast start|stop``."""
    action = getattr(args, "screencast_action", None)
    if action == "start":
        _print_json(
            curated.screencast_start(
                instance=args.instance,
                fmt=args.format,
                max_frames=args.max_frames,
                registry_path=registry_path,
            )
        )
        return EXIT_OK
    if action == "stop":
        if not args.dir:
            raise PassthroughUsageError("screencast stop requires --dir DIR")
        _print_json(
            curated.screencast_stop(
                instance=args.instance, out_dir=args.dir, registry_path=registry_path
            )
        )
        return EXIT_OK
    raise PassthroughUsageError("screencast takes one sub-action: start or stop")


def _run_passthrough(argv: list[str], registry_path: str | None) -> int:
    """Dispatch the raw-protocol line: ``[INSTANCE] Domain.method '{...}'``.

    Bypasses argparse entirely -- the leading token is an arbitrary instance
    name or ``Domain.method``, never one of the fixed subcommand strings.
    Raises ``LifecycleError``/``PassthroughUsageError``; the caller maps
    those to exit codes 1/2.
    """
    remaining, target, url = passthrough.extract_target_flags(argv)
    instance, method, params_json = passthrough.resolve_passthrough_args(
        remaining, registry_path=registry_path
    )
    result = passthrough.send(
        instance=instance,
        method=method,
        params_json=params_json,
        target=target,
        url=url,
        registry_path=registry_path,
    )
    _print_json(result)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point for both the ``browser-tools`` and ``bt`` console scripts."""
    raw_argv = sys.argv[1:] if argv is None else argv

    if (
        raw_argv
        and raw_argv[0] not in _KNOWN_VERBS
        and raw_argv[0] not in ("-h", "--help")
    ):
        registry_path = lifecycle.registry_path_from_env()
        if passthrough.is_passthrough_head(raw_argv[0], registry_path=registry_path):
            try:
                return _run_passthrough(raw_argv, registry_path=registry_path)
            except LifecycleError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_OPERATIONAL
            except PassthroughUsageError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_USAGE
        # Neither a known instance nor Domain.method-shaped: fall through to
        # argparse, which rejects it as an unknown verb (unchanged).

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        return _run(args)
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    except PassthroughUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
