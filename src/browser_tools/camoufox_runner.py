"""Detached long-lived Camoufox host process for the registry-backed lifecycle.

The registry-backed CLI (``browser_tools.lifecycle``) tracks each browser
instance by the PID of a long-lived process and a user-data-dir hold. Camoufox
(a Firefox fork) exposes no Chrome debugging port, so its liveness is process
identity plus user-data-dir hold (``pid_holds_user_data_dir``). That check reads
a live process's command line for ``--user-data-dir=<dir>``.

This runner is exactly that process. It is spawned detached with
``--user-data-dir=<dir>`` on its own argv, opens a **persistent** Camoufox
context rooted at that directory, writes a readiness sentinel, and then blocks
until it is signalled. Because the flag is on the runner's own command line,
``pid_holds_user_data_dir(runner_pid, dir)`` attributes the hold to this PID
without depending on how Playwright spells Firefox's profile flag internally.

The in-process ``CamoufoxSession`` MCP tools are untouched; this is a separate
entry point for the CLI lifecycle only.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import threading
from pathlib import Path

READY_SENTINEL = ".camoufox-ready"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="browser-tools-camoufox-runner")
    # NOTE: the flag is spelled --user-data-dir= (with '='-joined value on the
    # process argv) so the liveness ladder's cmdline scan attributes the hold.
    parser.add_argument("--user-data-dir", required=True, dest="user_data_dir")
    parser.add_argument("--headless", action="store_true")
    return parser.parse_args(argv)


def _write_ready(user_data_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (user_data_dir / READY_SENTINEL).write_text("ok")


def main(argv: list[str] | None = None) -> int:
    """Launch a persistent Camoufox context and block until signalled."""
    args = _parse_args(argv)
    user_data_dir = Path(args.user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        sys.stderr.write(
            "The camoufox engine requires the 'camoufox' extra. "
            "Install it with: pip install 'browser-tools[camoufox]'\n"
        )
        return 3

    stop = threading.Event()

    def _handle_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    camoufox_kwargs: dict[str, object] = {
        "persistent_context": True,
        "user_data_dir": str(user_data_dir),
    }
    if args.headless:
        camoufox_kwargs["headless"] = True

    with Camoufox(**camoufox_kwargs):  # type: ignore[reportOptionalCall]
        _write_ready(user_data_dir)
        # Block until SIGTERM/SIGINT. The context manager closes Camoufox and
        # releases the profile on exit, preserving the persistent user-data-dir.
        stop.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
