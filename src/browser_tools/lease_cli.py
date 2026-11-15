"""The command-line surface of the lease feature (#195).

Two commands touch an instance's idle timeout: ``bt launch --idle-timeout
DURATION`` sets the timeout of the instance the launch creates, and ``bt
configure INSTANCE --idle-timeout DURATION`` sets it on a running one and
starts its countdown again.

The lease itself lives in :mod:`browser_tools.task_lease`, and the duration
grammar in :mod:`browser_tools.idle`. This module owns only the arguments
and the dispatch, so ``cli.py`` keeps one thin call site per command and the
lease feature grows in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import idle, lifecycle
from .usage import UsageError

if TYPE_CHECKING:
    import argparse


def add_launch_timeout(parser: argparse.ArgumentParser) -> None:
    """Give ``launch`` the ``--idle-timeout`` flag.

    The default is the one-hour timeout every managed launch starts with,
    written the way the duration grammar spells it.
    """
    parser.add_argument(
        "--idle-timeout",
        metavar="DURATION",
        default=idle.DEFAULT_IDLE_TIMEOUT_TEXT,
        help=(
            "Close this browser after this long without activity "
            "(default: 1h; e.g. 90s, 30m, 2h, 1h30m, 1d)"
        ),
    )


def add_configure_verb(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Add ``configure``: change a managed browser's idle timeout."""
    configure = sub.add_parser(
        "configure", help="Change a managed browser's idle timeout"
    )
    configure.add_argument("instance", nargs="?", metavar="INSTANCE")
    configure.add_argument(
        "--idle-timeout",
        metavar="DURATION",
        help=(
            "Close this browser after this long without activity, and start the "
            "countdown again now (e.g. 90s, 30m, 2h, 1h30m, 1d)"
        ),
    )


def check_timeout(args: argparse.Namespace) -> None:
    """Read the timeout the command named, before anything runs.

    A launch validates its timeout before it launches anything: a bad
    duration must not leave a browser running that nobody asked for. An
    empty value is a bad duration, not an absent one.

    Raises:
        UsageError: The command named no timeout where one is required, or
            named one that is not a duration (CLI exit 2).
    """
    command = getattr(args, "command", None)
    if command == "configure":
        if not getattr(args, "idle_timeout", None):
            raise UsageError(
                "configure requires --idle-timeout DURATION (for example: "
                "--idle-timeout 2h)"
            )
        args.idle_timeout_seconds = idle.parse_duration(args.idle_timeout)
        return
    if command == "launch":
        text = getattr(args, "idle_timeout", None)
        if text is None:
            text = idle.DEFAULT_IDLE_TIMEOUT_TEXT
        args.idle_timeout_seconds = idle.parse_duration(text)


def configure_payload(args: argparse.Namespace, registry_path: str | None) -> dict[str, Any]:
    """Set the named instance's idle timeout and report the new deadline.

    The instance may be left out when exactly one is registered, the same
    way every other instance-taking command resolves it.
    """
    from . import task_lease

    instance = getattr(args, "instance", None)
    if instance is None:
        instance = lifecycle.resolve_single_instance(registry_path=registry_path)
    return task_lease.configure(
        instance,
        idle_timeout=args.idle_timeout_seconds,
        registry_path=registry_path,
    )


def launch_fields(name: str, registry_path: str | None) -> dict[str, Any]:
    """The lease fields the launch output carries.

    Both fields are always present, even for an instance whose lease could
    not be read: the output shape does not change with the lease.
    """
    from . import task_lease

    summary = task_lease.summary(name, registry_path)
    return {
        "idle_timeout": summary.get("idle_timeout"),
        "idle_deadline": summary.get("idle_deadline"),
    }
