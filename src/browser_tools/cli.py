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

from . import lifecycle, passthrough
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
_KNOWN_VERBS = {"launch", "status", "stop", "cleanup", "guide", "help"}


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

    help_cmd = sub.add_parser(
        "help", help="Live CDP protocol help from a running instance, or static usage"
    )
    help_cmd.add_argument(
        "args",
        nargs="*",
        metavar="[INSTANCE] [Domain.method]",
        help="Optional instance name and/or a Domain or Domain.method query",
    )

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

    if args.command == "help":
        instance, query = passthrough.resolve_help_args(args.args, registry_path=registry_path)
        passthrough.run_help(instance, query, registry_path=registry_path)
        return EXIT_OK

    # No verb given: usage.
    return EXIT_USAGE


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
