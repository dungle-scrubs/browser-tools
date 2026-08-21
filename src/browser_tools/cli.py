"""The merged browser-tools CLI front (RFC-01 Phase 1, layer 4).

New code that owns argument parsing, verb dispatch, and exit codes. It calls
the vendored core (registry, launcher, instance status) through
``browser_tools.lifecycle``; the vendored ``cli.py`` is not shipped and its
``main()`` is never reached.

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

from . import lifecycle
from .lifecycle import LifecycleError

PROG = "browser-tools"

EXIT_OK = 0
EXIT_OPERATIONAL = 1
EXIT_USAGE = 2


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
        help="Do not draw the agent-window marking border",
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

    return parser


def _print_json(payload: object) -> None:
    """Emit machine-readable output to stdout as JSON."""
    print(json.dumps(payload, indent=2))


def _strip_arg_separator(browser_args: list[str] | None) -> list[str]:
    """Drop the leading ``--`` argparse.REMAINDER keeps in front of BROWSER_ARGS."""
    if not browser_args:
        return []
    if browser_args[0] == "--":
        return browser_args[1:]
    return browser_args


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
            browser_args=_strip_arg_separator(args.browser_args),
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

    # No verb given: usage.
    return EXIT_USAGE


def main(argv: list[str] | None = None) -> int:
    """Entry point for both the ``browser-tools`` and ``bt`` console scripts."""
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


if __name__ == "__main__":
    sys.exit(main())
