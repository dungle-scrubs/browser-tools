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
from typing import Any

from . import captures, curated, events, lifecycle, list_verbs, passthrough, step_run, user_settings
from .lifecycle import LifecycleError
from .passthrough import UsageError as PassthroughUsageError
from .usage import UsageError

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
    "profile",
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
    "eval",
    "press",
    "hover",
    "type",
    "wait-text",
    "network-get",
    "wait-idle",
    "wait-stable",
    "detect",
    "frames",
    "storage",
    "screenshot",
    "screencast",
    "run",
    "trace",
    "heap",
}


#: Verbs that MUST NOT accept ``--endpoint`` (#97). They read or write the
#: registry, or launch a browser, and an external browser has no entry there.
#: Keeping them flagless is what stops ``stop`` and ``cleanup`` from ever
#: seeing the user's real profile directory.
REGISTRY_VERBS = frozenset({"launch", "status", "stop", "cleanup", "guide", "profile"})

ENDPOINT_HELP = (
    "Drive an external browser at URL (loopback only, e.g. http://127.0.0.1:9222) "
    "instead of a registered instance"
)


def _add_endpoint(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Give one browser-driving verb the ``--endpoint URL`` flag."""
    parser.add_argument("--endpoint", metavar="URL", help=ENDPOINT_HELP)
    return parser


#: What `--frames all` buys, in one line for `--help`.
FRAMES_HELP = (
    "'all' also reaches cross-origin iframes, which run in their own process "
    "and are otherwise invisible to frames list, frames select and snapshot. "
    "Default 'page': same-process frames only."
)


def _add_frames(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Give one frame-reading verb the ``--frames`` flag (RFC-04).

    Off by default for this release. Reaching an Out-of-Process Frame opens a
    CDP session per frame, and that lifecycle is new, so a caller asks for it
    rather than every invocation paying for it.
    """
    parser.add_argument(
        "--frames", choices=("page", "all"), default="page", metavar="SCOPE", help=FRAMES_HELP
    )
    return parser


def _installed_version() -> str:
    """The running build's version, read from the installed distribution.

    Never a literal here. `__version__` was hardcoded once and read "0.1.0"
    through two releases, which is the failure this flag exists to expose.
    """
    from importlib import metadata

    try:
        return metadata.version("browser-tools")
    except metadata.PackageNotFoundError:  # a source checkout, never installed
        return "0.0.0+unknown"


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser and its lifecycle subcommands."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Registry-backed browser lifecycle (launch, status, stop, cleanup, guide).",
    )
    # Which build is on PATH is the first thing to establish when behaviour
    # does not match the manual. A stale copy from an older install answers
    # the same verbs and answers them differently, and without this the only
    # way to tell was to read a UID and recognise the format.
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_installed_version()}",
        help="Print the installed version and exit",
    )
    sub = parser.add_subparsers(dest="command", metavar="VERB")

    launch = sub.add_parser("launch", help="Launch a browser and register it")
    launch.add_argument(
        "--engine",
        choices=list(lifecycle.VALID_ENGINES),
        default=lifecycle.DEFAULT_ENGINE,
        help="Browser engine (default: chrome)",
    )
    launch.add_argument(
        "--profile", metavar="NAME", help="Named profile to record for this instance"
    )
    launch.add_argument(
        "--channel", metavar="NAME", help="Chrome release channel (stable/beta/dev/canary)"
    )
    launch.add_argument("--headless", action="store_true", help="Run without a visible window")
    launch.add_argument(
        "--port", type=int, metavar="PORT", help="CDP port (default: auto-allocate)"
    )
    launch.add_argument(
        "--fingerprint", metavar="FILE", help="Fingerprint profile file (launch flags only)"
    )
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
    stop.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance to stop (omit if only one)"
    )
    stop.add_argument("--target", metavar="SPEC", help="Close a single tab instead of the browser")

    sub.add_parser("cleanup", help="Remove stale registry entries and session dirs")

    profile = sub.add_parser("profile", help="List or delete named profiles")
    profile_sub = profile.add_subparsers(dest="profile_action", metavar="ACTION")
    profile_sub.add_parser("list", help="List every profile in the profile root")
    profile_delete = profile_sub.add_parser("delete", help="Remove one profile directory")
    profile_delete.add_argument("name", metavar="NAME", help="Profile to remove")
    profile_migrate = profile_sub.add_parser(
        "migrate", help="Move profiles left in the old /tmp root into durable storage"
    )
    profile_migrate.add_argument(
        "--back",
        action="store_true",
        help="Reverse the migration (the rollback for the root move)",
    )
    profile_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would move, and move nothing",
    )
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
    help_cmd.set_defaults(instance=None)
    _add_endpoint(help_cmd)

    attach = sub.add_parser("attach", help="Stream subscribed CDP events as JSON lines")
    attach.add_argument(
        "args",
        nargs="*",
        metavar="[INSTANCE] +Domain.event ...",
        help="Optional instance name followed by one or more +Domain.event subscriptions",
    )
    attach.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    attach.add_argument(
        "--url", metavar="SUBSTRING", help="Select the page target by URL substring"
    )
    attach.set_defaults(instance=None)
    _add_endpoint(attach)

    wait = sub.add_parser("wait", help="Block until one matching CDP event fires")
    wait.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    wait.add_argument(
        "--event", required=True, metavar="Domain.event", help="CDP event to wait for"
    )
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
    _add_endpoint(wait)

    console_list = sub.add_parser(
        "console-list", help="Collect console messages over a short attach window"
    )
    console_list.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    console_list.add_argument(
        "--target", metavar="SPEC", help="Select the page target (index or id)"
    )
    console_list.add_argument(
        "--url", metavar="SUBSTRING", help="Select the page target by URL substring"
    )
    console_list.add_argument(
        "--duration",
        type=float,
        default=list_verbs.DEFAULT_LIST_WINDOW_SECONDS,
        metavar="SECONDS",
        help="Collection window in seconds (default: 2.0)",
    )
    _add_endpoint(console_list)

    network_list = sub.add_parser(
        "network-list", help="Collect network requests/responses over a short attach window"
    )
    network_list.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    network_list.add_argument(
        "--target", metavar="SPEC", help="Select the page target (index or id)"
    )
    network_list.add_argument(
        "--url", metavar="SUBSTRING", help="Select the page target by URL substring"
    )
    network_list.add_argument(
        "--duration",
        type=float,
        default=list_verbs.DEFAULT_LIST_WINDOW_SECONDS,
        metavar="SECONDS",
        help="Collection window in seconds (default: 2.0)",
    )
    _add_endpoint(network_list)

    _add_curated_verbs(sub)
    _add_run_verb(sub)

    return parser


def _add_run_verb(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Add ``run``: one ordered Step List, one invocation (RFC-03).

    ``--target``/``--url`` belong to the run, not to a step: the connection
    opens once, so the page is chosen once. A step carrying either is a usage
    error, refused by ``step_list.validate`` before step 1.
    """
    run_parser = sub.add_parser("run", help="Run an ordered list of steps in one invocation")
    run_parser.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    run_parser.add_argument(
        "file", metavar="FILE", help="The step list, one verb phrase per line; - reads stdin"
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Bound the whole run (default: none, because every step bounds itself)",
    )
    run_parser.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    run_parser.add_argument(
        "--url", metavar="SUBSTRING", help="Select the page target by URL substring"
    )
    _add_endpoint(run_parser)
    _add_frames(run_parser)


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
    _add_rfc05_verbs(sub)
    snapshot = sub.add_parser("snapshot", help="Native UID accessibility tree")
    snapshot.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    snapshot.add_argument(
        "--target", metavar="SPEC", help="Select the page target (1-based index or id)"
    )
    _add_endpoint(snapshot)
    _add_frames(snapshot)

    click = sub.add_parser("click", help="Native UID click")
    click.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    click.add_argument("--uid", metavar="N", help="UID from a prior snapshot")
    click.add_argument(
        "--target", metavar="SPEC", help="Select the page target (1-based index or id)"
    )
    _add_endpoint(click)
    _add_frames(click)

    fill = sub.add_parser("fill", help="Native UID fill")
    fill.add_argument("instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)")
    fill.add_argument("--uid", metavar="N", help="UID from a prior snapshot")
    fill.add_argument("--text", metavar="T", help="Text to fill")
    fill.add_argument(
        "--target", metavar="SPEC", help="Select the page target (1-based index or id)"
    )
    _add_endpoint(fill)
    _add_frames(fill)

    wait_idle = sub.add_parser("wait-idle", help="Wait for network idle")
    wait_idle.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    wait_idle.add_argument(
        "--timeout-ms",
        type=int,
        default=curated.DEFAULT_WAIT_TIMEOUT_MS,
        metavar="MS",
        help="Overall deadline in ms (default: 5000)",
    )
    wait_idle.add_argument(
        "--idle-ms",
        type=int,
        default=curated.DEFAULT_IDLE_MS,
        metavar="MS",
        help="Quiet window in ms (default: 500)",
    )
    _add_endpoint(wait_idle)

    wait_stable = sub.add_parser("wait-stable", help="Wait for DOM quiescence")
    wait_stable.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    wait_stable.add_argument(
        "--timeout-ms",
        type=int,
        default=curated.DEFAULT_WAIT_TIMEOUT_MS,
        metavar="MS",
        help="Overall deadline in ms (default: 5000)",
    )
    wait_stable.add_argument(
        "--stable-ms",
        type=int,
        default=curated.DEFAULT_STABLE_MS,
        metavar="MS",
        help="Quiescence window in ms (default: 300)",
    )
    _add_endpoint(wait_stable)

    detect = sub.add_parser("detect", help="Run interstitial detection against the current page")
    detect.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    _add_endpoint(detect)
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
    # A sub-action verb takes its instance ahead of the verb phrase
    # (`bt web-01 frames select checkout`), never inside it: `frames select
    # [INSTANCE] PATTERN` cannot be read with a single bare token. See
    # `_split_leading_instance`.
    fl = frames_sub.add_parser("list", help="List frames")
    fl.set_defaults(instance=None)
    _add_endpoint(fl)
    _add_frames(fl)
    fs = frames_sub.add_parser("select", help="Select a frame by URL pattern")
    fs.add_argument("pattern", metavar="PATTERN", help="Frame URL substring/pattern")
    fs.set_defaults(instance=None)
    _add_endpoint(fs)
    _add_frames(fs)
    fr = frames_sub.add_parser("reset", help="Clear frame selection")
    fr.set_defaults(instance=None)
    _add_endpoint(fr)
    _add_frames(fr)

    storage = sub.add_parser("storage", help="Read a frame's storage")
    storage_sub = storage.add_subparsers(dest="storage_action", metavar="ACTION")
    sg = storage_sub.add_parser("get", help="Read the selected frame's storage")
    sg.add_argument("--key", metavar="K", help="Frame URL pattern to select before reading")
    sg.set_defaults(instance=None)
    _add_endpoint(sg)
    _add_frames(sg)

    screenshot = sub.add_parser("screenshot", help="Capture a page screenshot")
    screenshot.add_argument(
        "instance", nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"
    )
    screenshot.add_argument(
        "--path", metavar="FILE", help="Write the PNG to a file instead of stdout"
    )
    screenshot.add_argument("--target", metavar="SPEC", help="Select the page target (index or id)")
    screenshot.add_argument(
        "--url", metavar="SUBSTRING", help="Select the page target by URL substring"
    )
    _add_endpoint(screenshot)

    for verb in ("trace", "heap"):
        capture = sub.add_parser(verb, help=f"Capture a bounded {verb} to a file")
        capture.add_argument("instance", nargs="?", metavar="INSTANCE")
        capture.add_argument("--out", required=True, metavar="FILE")
        capture.add_argument("--target", metavar="SPEC")
        _add_endpoint(capture)
        if verb == "trace":
            capture.add_argument("--duration", type=float, metavar="SECONDS")
            capture.add_argument("--steps", metavar="FILE")
            capture.add_argument("--timeout", type=float, metavar="SECONDS")
            capture.add_argument("--categories", action="append", metavar="LIST")

    screencast = sub.add_parser(
        "screencast", help="Capture a bounded screencast and write its frames"
    )
    screencast.add_argument(
        "--dir", dest="dir", metavar="DIR", help="Directory to write frames into"
    )
    screencast.add_argument(
        "--duration",
        type=float,
        default=curated.DEFAULT_SCREENCAST_DURATION_SECONDS,
        metavar="SECONDS",
        help="How long to capture for (default: 5)",
    )
    screencast.add_argument(
        "--format", dest="format", default="jpeg", metavar="FMT", help="jpeg or png (default: jpeg)"
    )
    screencast.add_argument(
        "--max-frames",
        type=int,
        default=curated.DEFAULT_SCREENCAST_MAX_FRAMES,
        metavar="N",
        help="Frame cap; reaching it ends the capture (default: 600)",
    )
    # `screencast start` / `screencast stop` were one verb pair that could not
    # work: the frame buffer is process-local, so the stop process never saw
    # the start process's frames (#99). Catching the old spelling here is what
    # turns "unrecognized arguments: start" into a message naming the new form.
    screencast.add_argument(
        "removed_action", nargs="?", metavar=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    screencast.set_defaults(instance=None)
    _add_endpoint(screencast)


def _run_profile(args: argparse.Namespace, registry_path: str | None) -> int:
    """Dispatch ``profile list|delete NAME``."""
    action = getattr(args, "profile_action", None)
    if action == "list":
        _print_json(lifecycle.profile_list(registry_path=registry_path))
        return EXIT_OK
    if action == "delete":
        _print_json(lifecycle.profile_delete(args.name, registry_path=registry_path))
        return EXIT_OK
    if action == "migrate":
        _print_json(
            lifecycle.migrate_profiles(
                back=args.back, dry_run=args.dry_run, registry_path=registry_path
            )
        )
        return EXIT_OK
    raise PassthroughUsageError("profile takes one sub-action: list, delete NAME, or migrate")


def _one_instance(leading: str | None, inline: str | None) -> str | None:
    """Reconcile a leading ``INSTANCE`` with one inside the verb's own args.

    ``help`` and ``attach`` read their instance out of a free-form positional
    list rather than a named argument, so the conflict ``main`` catches for
    every other verb has to be caught here instead.

    Raises:
        UsageError: Both spellings were used.
    """
    if leading is not None and inline is not None:
        raise PassthroughUsageError(
            f"instance named twice: '{leading}' before the verb and '{inline}' after it"
        )
    return leading if leading is not None else inline


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


#: Verbs whose ``--target`` and ``--url`` name the same thing two ways.
_TARGET_OR_URL_VERBS = frozenset({"attach", "wait", "console-list", "network-list", "run", "screenshot",
                                "eval", "press", "hover", "type", "wait-text"})


def url_selects_page(command: str) -> bool:
    """Whether --url selects the invocation's page rather than a response."""
    return command in _TARGET_OR_URL_VERBS


def _add_rfc05_verbs(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    for name, help_text in (
        ("eval", "Evaluate JavaScript; throws fail with exit 1"),
        ("press", "Press and release a key with native default actions"),
        ("hover", "Move the native pointer over a snapshot UID"),
        ("type", "Insert text at the caret without key events"),
        ("wait-text", "Wait for a substring in rendered text"),
        ("network-get", "Fetch a response from the run or a bounded observation window"),
    ):
        parser = sub.add_parser(name, help=help_text)
        if name in {"eval", "press", "type", "wait-text"}:
            parser.add_argument("operands", nargs="*", metavar="ARG")
            parser.set_defaults(instance=None)
        else:
            parser.add_argument("instance", nargs="?", metavar="INSTANCE")
        parser.add_argument("--target", metavar="SPEC")
        parser.add_argument("--url", metavar="SUB", help=(
            "Response URL substring" if name == "network-get" else "Page URL substring"
        ))
        _add_endpoint(parser)
        _add_frames(parser)
        if name == "eval":
            parser.add_argument("--await", dest="await_promise", action="store_true")
        elif name == "press":
            parser.add_argument("--modifiers", metavar="NAME[,NAME...]")
        elif name == "hover":
            parser.add_argument("--uid", metavar="UID")
        elif name == "type":
            parser.add_argument("--file", metavar="FILE")
        elif name == "wait-text":
            parser.add_argument("--timeout-ms", type=int, default=curated.DEFAULT_WAIT_TIMEOUT_MS)
        else:
            parser.add_argument("--request-id", metavar="ID")
            parser.add_argument("--response-file", metavar="PATH")
            parser.add_argument("--duration", type=float, default=2.0, metavar="SECONDS")
            parser.add_argument("--reload", action="store_true", help="Reload in standalone mode")


def _rfc05_operands(args: argparse.Namespace, known_instances: set[str] | None) -> None:
    if not hasattr(args, "operands"):
        return
    operands: list[str] = args.operands
    if len(operands) > 2:
        raise UsageError(f"{args.command} takes one operand and an optional instance")
    inline = None
    value = None
    if len(operands) == 2:
        inline, value = operands
    elif operands:
        value = operands[0]
        if args.command == "type" and args.file is not None:
            known = known_instances
            if known is None:
                known = {item.name for item in lifecycle.read_instances(
                    registry_path=lifecycle.registry_path_from_env()
                )}
            if value in known:
                inline, value = value, None
    args.instance = _one_instance(args.instance, inline)
    field = {"eval": "source", "press": "key", "type": "text", "wait-text": "substring"}[args.command]
    setattr(args, field, value)
    delattr(args, "operands")


def check_preconditions(args: argparse.Namespace, *, known_instances: set[str] | None = None) -> None:
    """Every per-verb requirement ``argparse`` deliberately leaves optional.

    ``build_parser`` accepts a bare verb on purpose, so ``click`` with no
    ``--uid`` parses cleanly and fails later. These are the checks that catch
    it, and they are gathered here rather than left inline because a Step Run
    validates every step through this same function before it runs any of them
    (RFC-03, "Validation, before any step runs"). A copied second set would let
    a step behave differently inside a run than outside it.

    Raises ``PassthroughUsageError`` (exit 2). Returns None when the verb
    carries everything it needs.
    """
    command = getattr(args, "command", None)
    _rfc05_operands(args, known_instances)

    if command in {"eval", "press", "wait-text"}:
        field = {"eval": "source", "press": "key", "wait-text": "substring"}[command]
        if getattr(args, field) is None:
            raise UsageError(f"{command} requires {field}")
    if command == "press":
        from .curated_runtime import key_event

        key_event(args.key, args.modifiers)
    if command == "hover" and not args.uid:
        raise UsageError("hover requires --uid UID from a prior snapshot")
    if command == "type" and (args.text is None) == (args.file is None):
        raise UsageError("type requires exactly one of text or --file FILE")
    if command == "wait-text" and args.timeout_ms < 0:
        raise UsageError("wait-text --timeout-ms must be non-negative")
    if command == "network-get":
        import math

        if not math.isfinite(args.duration) or args.duration < 0:
            raise UsageError("network-get --duration must be finite and non-negative")
    if command == "network-get" and (args.url is None) == (args.request_id is None):
        raise UsageError("network-get requires exactly one of --url SUB or --request-id ID")

    names_the_target_twice = (
        getattr(args, "target", None) is not None and getattr(args, "url", None) is not None
    )
    if command in _TARGET_OR_URL_VERBS and names_the_target_twice:
        raise PassthroughUsageError("cannot specify both --target and --url")

    if command == "click" and not getattr(args, "uid", None):
        raise PassthroughUsageError("click requires --uid N (a UID from a prior snapshot)")

    if command == "fill":
        if not getattr(args, "uid", None):
            raise PassthroughUsageError("fill requires --uid N (a UID from a prior snapshot)")
        if getattr(args, "text", None) is None:
            raise PassthroughUsageError("fill requires --text T")

    if command == "frames" and getattr(args, "frames_action", None) not in {
        "list",
        "select",
        "reset",
    }:
        raise PassthroughUsageError("frames takes one sub-action: list, select, or reset")

    if command == "storage" and getattr(args, "storage_action", None) != "get":
        raise PassthroughUsageError("storage takes one sub-action: get")

    if command == "trace":
        import math

        trace_duration = args.duration
        if trace_duration is None and args.steps is None:
            raise UsageError("trace requires --duration SECONDS or --steps FILE")
        if trace_duration is not None and (not math.isfinite(trace_duration) or trace_duration <= 0):
            raise UsageError("trace --duration must be finite and positive")
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout < 0):
            raise UsageError("trace --timeout must be finite and non-negative")
        if args.categories is not None and (
            len(args.categories) != 1 or not all(part.strip() for part in args.categories[0].split(","))
        ):
            raise UsageError("trace --categories takes one non-empty comma-separated list")

    if command == "screencast":
        removed = getattr(args, "removed_action", None)
        if removed is not None:
            raise PassthroughUsageError(
                f"screencast takes no sub-action: '{removed}' is not one. "
                "start and stop were replaced by a single bounded capture, because "
                "the frame buffer is process-local and a stop in a second process "
                "could never reach the first one's frames. Use: "
                "bt screencast --dir DIR [--duration SECONDS] [--format FMT] "
                "[--max-frames N]"
            )
        if not getattr(args, "dir", None):
            raise PassthroughUsageError(
                "screencast requires --dir DIR. It captures and writes the frames "
                "in one invocation; there is no separate start or stop."
            )
        curated.check_screencast_values(args.duration, args.format)

    if command == "detect":
        curated.check_detect_wait(None if args.no_wait else args.wait)


def _run(args: argparse.Namespace) -> int:
    """Dispatch one parsed verb. Raises LifecycleError for operational failures."""
    registry_path = lifecycle.registry_path_from_env()
    check_preconditions(args)

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

    if args.command == "profile":
        return _run_profile(args, registry_path)

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
        instance = _one_instance(getattr(args, "instance", None), instance)
        passthrough.run_help(instance, query, registry_path=registry_path, endpoint=args.endpoint)
        return EXIT_OK

    if args.command == "attach":
        instance, subscriptions = events.resolve_attach_args(args.args)
        instance = _one_instance(getattr(args, "instance", None), instance)
        events.run_attach(
            instance=instance,
            events=subscriptions,
            target=args.target,
            url=args.url,
            registry_path=registry_path,
            endpoint=args.endpoint,
        )
        return EXIT_OK

    if args.command == "wait":
        _print_json(step_envelope(args, registry_path))
        return EXIT_OK

    if args.command == "console-list":
        _print_json(step_envelope(args, registry_path))
        return EXIT_OK

    if args.command == "network-list":
        _print_json(step_envelope(args, registry_path))
        return EXIT_OK

    if args.command in _CURATED_COMMANDS:
        _print_json(step_envelope(args, registry_path))
        return EXIT_OK

    if args.command == "trace":
        document, succeeded = captures.trace(
            instance=args.instance, out=args.out, duration=args.duration, source=args.steps,
            timeout=args.timeout, target=args.target, endpoint=args.endpoint,
            registry_path=registry_path,
            categories=None if args.categories is None else [
                item.strip() for item in args.categories[0].split(",")
            ],
        )
        _print_json(document)
        if not succeeded:
            diagnostic = step_run.describe_failure(document["run"])
            print(f"error: {diagnostic}", file=sys.stderr)
        return EXIT_OK if succeeded else EXIT_OPERATIONAL

    if args.command == "run":
        return _run_step_list(args, registry_path)

    # No verb given: usage.
    return EXIT_USAGE


def _run_step_list(args: argparse.Namespace, registry_path: str | None) -> int:
    """Run a Step List and print its Run Document (RFC-03).

    The one verb that prints on stdout when it exits 1. A run that died at
    step 7 still completed six steps, and a caller that cannot see which ones
    does not know the state the browser is in. The exit code is what
    separates a partial run from a whole one, which is why The Manual states
    the caller contract in that order: exit code, then ``run.status``, then
    the steps.

    A failure before step 1 prints nothing, exactly as every other verb does:
    there is no document, because nothing ran.
    """
    document, succeeded = step_run.run(
        instance=args.instance,
        source=args.file,
        timeout=args.timeout,
        target=args.target,
        url=args.url,
        registry_path=registry_path,
        endpoint=args.endpoint,
        all_frames=getattr(args, "frames", "page") == "all",
    )
    _print_json(document)
    if succeeded:
        return EXIT_OK
    diagnostic = step_run.describe_failure(document)
    if diagnostic is not None:
        print(f"error: {diagnostic}", file=sys.stderr)
    return EXIT_OPERATIONAL


#: Curated verbs dispatched through ``browser_tools.curated`` (RFC-01 #50).
_CURATED_COMMANDS = frozenset(
    {
        "snapshot",
        "click",
        "fill",
        "eval",
        "press",
        "hover",
        "type",
        "wait-text",
        "network-get",
        "wait-idle",
        "wait-stable",
        "detect",
        "frames",
        "storage",
        "screenshot",
        "screencast",
        "heap",
    }
)


def step_envelope(
    args: argparse.Namespace,
    registry_path: str | None,
    handler: Any | None = None,
) -> Any:
    """The JSON document one step-capable verb produces.

    ``_run`` prints what this returns. A Step Run collects it into the Run
    Document instead, so a step's ``result`` is byte-identical to what the
    same step prints alone. That is what makes a Run Document composable
    with what a caller already parses.

    One dispatch, not two. A Step Run with its own table would be a second
    place to add a verb and a second place to get it wrong, and RFC-03 rules
    that out for the parser and the preconditions for the same reason.

    ``handler`` is the run's open session. Every verb below takes it and
    runs over it; passed ``None`` each opens its own, which is what a bare
    invocation does.
    """
    command = args.command

    if command == "wait":
        return events.wait(
            instance=args.instance,
            event=args.event,
            match=args.match,
            timeout=args.timeout,
            target=args.target,
            url=args.url,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
        )

    if command in ("console-list", "network-list"):
        collect = list_verbs.console_list if command == "console-list" else list_verbs.network_list
        return collect(
            instance=args.instance,
            target=args.target,
            url=args.url,
            duration=args.duration,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
        )

    if command in _CURATED_COMMANDS:
        return _curated_envelope(args, registry_path, handler)

    raise PassthroughUsageError(f"{command} cannot be a step")


def _curated_envelope(
    args: argparse.Namespace,
    registry_path: str | None,
    handler: Any | None = None,
) -> dict[str, Any]:
    """Dispatch one curated verb to its ``curated`` implementation.

    Each branch calls the same implementation the matching MCP tool uses.
    Missing required inputs raise ``PassthroughUsageError`` (exit 2); the
    operational failures the ``curated`` functions raise are ``LifecycleError``
    (exit 1), handled by the caller.
    """
    if args.command in {"eval", "press", "hover", "type", "wait-text", "network-get"}:
        common: dict[str, Any] = {
            "instance": args.instance, "target": args.target, "url": args.url,
            "registry_path": registry_path, "endpoint": args.endpoint, "handler": handler,
            "all_frames": getattr(args, "frames", "page") == "all",
        }
        if args.command == "eval":
            return curated.eval_js(source=args.source, await_promise=args.await_promise, **common)
        if args.command == "press":
            return curated.press(key=args.key, modifiers=args.modifiers, **common)
        if args.command == "hover":
            return curated.hover(uid=args.uid, **common)
        if args.command == "type":
            return curated.type_text(text=args.text, file=args.file, **common)
        if args.command == "wait-text":
            return curated.wait_text(substring=args.substring, timeout_ms=args.timeout_ms, **common)
        return curated.network_get(request_id=args.request_id, response_file=args.response_file,
                                   duration=args.duration, reload=args.reload, **common)

    if args.command == "snapshot":
        return curated.snapshot(
            instance=args.instance,
            target=args.target,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
            all_frames=getattr(args, "frames", "page") == "all",
        )

    if args.command == "click":
        return curated.click(
            instance=args.instance,
            uid=args.uid,
            target=args.target,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
            all_frames=getattr(args, "frames", "page") == "all",
        )

    if args.command == "fill":
        return curated.fill(
            instance=args.instance,
            uid=args.uid,
            text=args.text,
            target=args.target,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
            all_frames=getattr(args, "frames", "page") == "all",
        )

    if args.command == "wait-idle":
        return curated.wait_idle(
            instance=args.instance,
            timeout_ms=args.timeout_ms,
            idle_ms=args.idle_ms,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
        )

    if args.command == "wait-stable":
        return curated.wait_stable(
            instance=args.instance,
            timeout_ms=args.timeout_ms,
            stable_ms=args.stable_ms,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
        )

    if args.command == "detect":
        wait_seconds = 0.0 if args.no_wait else args.wait
        return curated.detect(
            instance=args.instance,
            registry_path=registry_path,
            endpoint=args.endpoint,
            wait_seconds=wait_seconds,
            handler=handler,
        )

    if args.command == "frames":
        return _frames_envelope(args, registry_path, handler)

    if args.command == "storage":
        return curated.storage_get(
            instance=args.instance,
            key=args.key,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
            all_frames=getattr(args, "frames", "page") == "all",
        )

    if args.command == "screenshot":
        return curated.screenshot(
            instance=args.instance,
            path=args.path,
            target=args.target,
            url=args.url,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
        )

    if args.command == "heap":
        return captures.heap(instance=args.instance, out=args.out, target=args.target,
                             endpoint=args.endpoint, registry_path=registry_path, handler=handler)

    if args.command == "screencast":
        return curated.screencast(
            instance=args.instance,
            out_dir=args.dir,
            duration=args.duration,
            fmt=args.format,
            max_frames=args.max_frames,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
        )

    raise PassthroughUsageError(f"{args.command} is not a curated verb")


def _frames_envelope(
    args: argparse.Namespace,
    registry_path: str | None,
    handler: Any | None = None,
) -> dict[str, Any]:
    """Dispatch ``frames list|select|reset`` and return its document."""
    action = getattr(args, "frames_action", None)
    if action == "list":
        return curated.frames_list(
            instance=args.instance,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
            all_frames=getattr(args, "frames", "page") == "all",
        )
    if action == "select":
        return curated.frames_select(
            instance=args.instance,
            pattern=args.pattern,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
            all_frames=getattr(args, "frames", "page") == "all",
        )
    if action == "reset":
        return curated.frames_reset(
            instance=args.instance,
            registry_path=registry_path,
            endpoint=args.endpoint,
            handler=handler,
        )
    raise PassthroughUsageError("frames takes one sub-action: list, select, or reset")


def _run_passthrough(argv: list[str], registry_path: str | None) -> int:
    """Dispatch the raw-protocol line: ``[INSTANCE] Domain.method '{...}'``.

    Bypasses argparse entirely -- the leading token is an arbitrary instance
    name or ``Domain.method``, never one of the fixed subcommand strings.
    Raises ``LifecycleError``/``PassthroughUsageError``; the caller maps
    those to exit codes 1/2.
    """
    remaining, target, url, endpoint = passthrough.extract_target_flags(argv)
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
        endpoint=endpoint,
    )
    _print_json(result)
    return EXIT_OK


def _refuse_endpoint_misuse(argv: list[str]) -> str | None:
    """Reject ``--endpoint`` where it cannot mean anything, or return None.

    Two cases argparse would otherwise report as a bare "unrecognized
    arguments", which says nothing about why:

    - A registry verb (:data:`REGISTRY_VERBS`). ``status`` reports the
      registry, and an external browser is not in it; ``stop`` and ``cleanup``
      act on registry entries, which is exactly what the external path does not
      create.
    - ``--profile``. A profile is a launch-time identity, and driving an
      external browser launches nothing.
    """
    probe, endpoint = passthrough.strip_endpoint_flag(argv)
    if endpoint is None:
        return None
    if "--profile" in probe:
        return (
            "--endpoint and --profile cannot be combined. A profile is a "
            "launch-time identity and --endpoint launches nothing; the "
            "external browser already has whatever profile it was started "
            "with. Launch a profile with: bt launch --profile NAME"
        )
    verb = next((token for token in probe if not token.startswith("-")), None)
    if verb in REGISTRY_VERBS:
        return (
            f"{verb} does not take --endpoint. It reads or writes the registry, "
            "and an external browser has no registry entry -- that absence is "
            "what keeps stop and cleanup away from the browser's real profile "
            "directory. Drive the browser with a verb that acts on a page, for "
            "example: bt snapshot --endpoint URL"
        )
    return None


def _split_leading_instance(
    argv: list[str], registry_path: str | None
) -> tuple[list[str], str | None]:
    """Pull a leading ``INSTANCE`` token off a verb phrase.

    Every verb that drives a browser accepts the instance ahead of the verb, so
    ``bt guide`` carries no grammar exception for the sub-action verbs (#99).
    The token is disambiguated by registry lookup, the same rule the raw
    protocol line already uses (RFC-01, "Instance names"):

        bt frames select checkout          # 'frames' is not a registry name
        bt web-01 frames select checkout   # 'web-01' is

    Only a token the registry knows *and* that is followed by a known verb is
    taken, so a bare ``bt web-01 Page.navigate '{...}'`` still routes to the
    raw protocol line.

    Returns:
        ``(remaining_argv, instance_or_None)``.
    """
    if (
        len(argv) >= 2
        and argv[1] in _KNOWN_VERBS
        and lifecycle.instance_is_registered(argv[0], registry_path=registry_path)
    ):
        return argv[1:], argv[0]
    return argv, None


def main(argv: list[str] | None = None) -> int:
    """Entry point for both the ``browser-tools`` and ``bt`` console scripts."""
    raw_argv = sys.argv[1:] if argv is None else argv

    refusal = _refuse_endpoint_misuse(raw_argv)
    if refusal is not None:
        print(f"error: {refusal}", file=sys.stderr)
        return EXIT_USAGE

    # The head test runs on an argv with --endpoint removed, so
    # `bt --endpoint URL Page.navigate '{...}'` reads the same as the flag at
    # the end. _run_passthrough still gets the full argv and pulls the flag
    # itself, because the raw-protocol line never reaches argparse.
    probe, endpoint = passthrough.strip_endpoint_flag(raw_argv)
    leading_instance: str | None = None
    if probe and probe[0] not in _KNOWN_VERBS and probe[0] not in ("-h", "--help"):
        registry_path = lifecycle.registry_path_from_env()
        probe, leading_instance = _split_leading_instance(probe, registry_path)
        if leading_instance is None and passthrough.is_passthrough_head(
            probe[0], registry_path=registry_path
        ):
            try:
                return _run_passthrough(raw_argv, registry_path=registry_path)
            except LifecycleError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_OPERATIONAL
            except UsageError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_USAGE
        # Neither a leading instance nor a known instance nor Domain.method-
        # shaped: fall through to argparse, which rejects it as an unknown verb.

    parser = build_parser()
    if leading_instance is not None:
        # Re-append the endpoint flag the probe removed. It belongs to the
        # verb's own parser, so it goes last; only `launch` has a REMAINDER
        # that could swallow it, and `launch` refuses --endpoint above.
        rest = [*probe, "--endpoint", endpoint] if endpoint is not None else probe
        args = parser.parse_args(rest)
        if getattr(args, "instance", None) is not None:
            print(
                f"error: instance named twice: '{leading_instance}' before the verb "
                f"and '{args.instance}' after it",
                file=sys.stderr,
            )
            return EXIT_USAGE
        args.instance = leading_instance
    else:
        args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        return _run(args)
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
