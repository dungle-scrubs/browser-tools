"""Positive ownership of the browser that is about to receive credentials.

``bt login`` refuses to type a credential into a browser it cannot positively
name. A registry row alone is not that proof: a stale row whose port has since
been claimed by another browser - reproduced in round two - still describes a
drivable DevTools endpoint, and the vendored liveness ladder deliberately
treats an unattributable listener as alive ("never destroy on ambiguity"),
which is the right default for ``stop`` and the wrong default for credential
delivery. This module is the stricter boundary login needs, built from the
same process-identity utilities :mod:`browser_tools.lifecycle` already
trusts:

- :func:`verify_recorded_owner` reads one ``ExtendedInstance`` (registry
  parsing stays in lifecycle) and proves the recorded browser is the process
  this tool launched: a recorded user-data directory, a recorded start-time
  identity, a live process of ours matching that start token, and that
  process holding the recorded directory.
- :func:`verify_connected_browser` asks the connected root browser for its
  own process identity (``SystemInfo.getProcessInfo``, sent without a target
  session id) and refuses unless exactly one browser process answers with the
  recorded PID -- then proves the recorded identity again, so a browser that
  exited between the two checks, leaving its PID to be recycled, cannot pass.

The named-profile label is deliberately not part of this: an unnamed Chrome
launch has a recorded user-data directory (its throwaway session dir) exactly
as a profile-bound one does, and the directory-plus-process binding is what
decides where a credential would land. No profile management happens here.

Every refusal is a fixed sentence: no process argument, path or protocol text
is ever printed. The process utilities do inspect command lines and start
times; what this module keeps and prints is only their boolean and
identity-token results, never a raw argument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.utils import process_is_ours, process_start_time
from .lifecycle import ExtendedInstance, LifecycleError
from .process_utils import pid_holds_user_data_dir

logger = logging.getLogger(__name__)

#: Fixed diagnostics. None of these interpolate process or protocol text.
_NO_START_IDENTITY = (
    "the instance's registry row lacks the recorded start-time identity needed "
    "to prove the recorded browser is still the process this tool launched"
)
_NOT_OUR_BROWSER = (
    "the recorded browser process is not running as the process this tool "
    "launched; the registry row is stale"
)
_NO_RECORDED_DIRECTORY = (
    "the instance's registry row records no usable user-data directory, so "
    "its browser cannot be positively owned"
)
_DIRECTORY_NOT_HELD = (
    "the recorded browser process does not hold the instance's recorded "
    "user-data directory; the registry row does not describe the running "
    "browser"
)
_CONNECTED_MISMATCH = (
    "the browser answering the debug port is not the registered instance; no "
    "credential was read"
)
_CONNECTED_UNAVAILABLE = (
    "the browser did not report its process identity; no credential was read"
)


class OwnershipError(LifecycleError):
    """The instance's owner could not be positively established (exit 1)."""


@dataclass(frozen=True)
class OwnershipSnapshot:
    """The recorded owner identity every credential read is bound to."""

    pid: int
    pid_start: str
    user_data_dir: str


def _identity_holds(snapshot: OwnershipSnapshot) -> None:
    """Prove the recorded process identity now; raise when it does not hold.

    Every fact is re-derived from the process table, so the same function
    serves as the post-reply recheck: a browser that exited between the
    registry read and the connected-browser reply fails here exactly as it
    fails up front. ``process_is_ours`` treats an undeterminable start time
    as "no evidence" and passes it through; ownership needs the positive
    match, so the token is required to equal the recorded one and a missing
    token refuses.
    """
    pid: object = snapshot.pid
    # The annotation says int; the registry file says nothing. A tampered
    # row can hold any JSON value, so the boundary parses it here.
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise OwnershipError(_NOT_OUR_BROWSER)
    if not process_is_ours(pid=pid, expected_start=snapshot.pid_start):
        raise OwnershipError(_NOT_OUR_BROWSER)
    if process_start_time(pid) != snapshot.pid_start:
        raise OwnershipError(_NOT_OUR_BROWSER)
    if not pid_holds_user_data_dir(pid, Path(snapshot.user_data_dir)):
        raise OwnershipError(_DIRECTORY_NOT_HELD)


def verify_recorded_owner(entry: ExtendedInstance) -> OwnershipSnapshot:
    """Prove a registry row positively names a browser this tool launched.

    Requires a recorded user-data directory (any string shape a real launch
    records - a named profile's directory or an unnamed launch's throwaway
    session dir) whose recorded PID is a live process of ours matching the
    recorded start-time token and holding that directory. Fails closed on
    missing, unknown or stale evidence: the alternative is reading a
    credential first and discovering the wrong browser afterwards.
    """
    if not isinstance(entry.pid_start, str) or not entry.pid_start:
        raise OwnershipError(_NO_START_IDENTITY)
    user_data_dir: object = entry.user_data_dir
    # Same boundary rule as the pid: the registry file can hold any JSON
    # value where a real launch records a path string.
    if not isinstance(user_data_dir, str) or not user_data_dir:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise OwnershipError(_NO_RECORDED_DIRECTORY)
    snapshot = OwnershipSnapshot(
        pid=entry.pid,
        pid_start=entry.pid_start,
        user_data_dir=user_data_dir,
    )
    _identity_holds(snapshot)
    return snapshot


async def verify_connected_browser(
    cdp: Any, snapshot: OwnershipSnapshot, timeout: float
) -> None:
    """Prove the connected root browser is the recorded owner.

    ``SystemInfo.getProcessInfo`` is sent without a target session id, so the
    browser process itself answers rather than any attached page. Exactly one
    ``type=browser`` entry with a positive non-boolean integer id equal to the
    recorded PID passes; anything else -- an unavailable command, a malformed
    reply, another browser answering the port, or a recorded identity that no
    longer holds once the reply is in hand -- refuses before any credential
    is read. There is no weaker fallback to a listener, a registry row or an
    engine string.
    """
    try:
        reply = await cdp.send(
            method="SystemInfo.getProcessInfo", params={}, timeout=timeout
        )
    except Exception:
        logger.debug("connected-owner verification send failed")
        raise OwnershipError(_CONNECTED_UNAVAILABLE) from None
    processes = reply.get("processInfo") if isinstance(reply, dict) else None
    if not isinstance(processes, list):
        raise OwnershipError(_CONNECTED_UNAVAILABLE)
    browsers = [
        item
        for item in processes
        if isinstance(item, dict) and item.get("type") == "browser"
    ]
    if len(browsers) != 1:
        raise OwnershipError(_CONNECTED_UNAVAILABLE)
    browser_pid = browsers[0].get("id")
    if (
        isinstance(browser_pid, bool)
        or not isinstance(browser_pid, int)
        or browser_pid <= 0
    ):
        raise OwnershipError(_CONNECTED_UNAVAILABLE)
    if browser_pid != snapshot.pid:
        raise OwnershipError(_CONNECTED_MISMATCH)
    _identity_holds(snapshot)


__all__ = [
    "OwnershipError",
    "OwnershipSnapshot",
    "verify_connected_browser",
    "verify_recorded_owner",
]
