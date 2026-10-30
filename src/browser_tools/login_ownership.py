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
  parsing stays in lifecycle), validates the row's recorded port together
  with its ownership fields, and proves the recorded browser is the process
  this tool launched: a recorded user-data directory, a recorded start-time
  identity, a live process of ours matching that start token, and that
  process holding the recorded directory. The login then uses exactly that
  recorded port; it does not re-run the generic liveness lookup.
- :func:`verify_connected_browser` asks the connected root browser for its
  own process identity (``SystemInfo.getProcessInfo``, sent without a target
  session id) and refuses unless exactly one browser process answers with the
  recorded PID -- then proves the recorded identity again, so a browser that
  exited between the two checks, leaving its PID to be recycled, cannot pass.

Both checks draw their subprocess budgets from the one operation deadline:
every process fact is bounded by the remaining time (shrunk from the
utilities' own caps, never widened), the clock is checked before and after
each fact, and the post-reply recheck runs in a worker thread whose
underlying calls are themselves capped - not merely a cancelled executor
future, whose thread ``asyncio.run`` would still join past the deadline.

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

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import login_contract as contract
from .core.utils import process_is_ours
from .lifecycle import ExtendedInstance, LifecycleError
from .process_utils import pid_holds_user_data_dir, process_start_token

if TYPE_CHECKING:
    from .login_contract import Deadline

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
_NO_RECORDED_PORT = (
    "the instance's registry row records no usable debug port, so its "
    "browser cannot be reached for this login"
)
_CONNECTED_MISMATCH = (
    "the browser answering the debug port is not the registered instance; no "
    "credential was read"
)
_CONNECTED_UNAVAILABLE = (
    "the browser did not report its process identity; no credential was read"
)

#: Per-fact caps on the ownership subprocess calls, matching the utilities'
#: original fixed budgets (the core-style start-token reader's 5s, the
#: command/profile readers' 2s). The operation deadline can only shrink
#: them, never widen them.
_START_TOKEN_CAP_SECONDS = 5.0
_METADATA_CAP_SECONDS = 2.0


class OwnershipError(LifecycleError):
    """The instance's owner could not be positively established (exit 1)."""


@dataclass(frozen=True)
class OwnershipSnapshot:
    """The recorded owner identity every credential read is bound to.

    ``port`` is the row's validated debug port: the login connects to
    exactly this recorded port after verification, rather than re-running
    the generic liveness lookup.
    """

    pid: int
    pid_start: str
    user_data_dir: str
    port: int


def _remaining_or_fail(deadline: Deadline) -> float:
    """The remaining budget, or the operation timeout when none is left."""
    remaining = deadline.remaining()
    if remaining <= 0:
        raise contract.DeadlineExpiredError(contract.TIMED_OUT)
    return remaining


def _identity_holds(snapshot: OwnershipSnapshot, deadline: Deadline) -> None:
    """Prove the recorded process identity now; refuse when it does not hold.

    Every fact is re-derived from the process table, so the same function
    serves as the post-reply recheck: a browser that exited between the
    registry read and the connected-browser reply fails here exactly as it
    fails up front. ``process_is_ours`` does the ownership/existence signal
    check only; the start-time token is then read once, on the remaining
    budget, and must equal the recorded token - an unavailable token is "no
    evidence", and ownership requires the positive match. The clock is
    checked before and after each subprocess fact, and each call is capped
    at the remaining time so the inspection can never outlive the
    operation deadline.
    """
    pid: object = snapshot.pid
    # The annotation says int; the registry file says nothing. A tampered
    # row can hold any JSON value, so the boundary parses it here.
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise OwnershipError(_NOT_OUR_BROWSER)
    if not process_is_ours(pid=pid):
        raise OwnershipError(_NOT_OUR_BROWSER)
    remaining = _remaining_or_fail(deadline)
    token = process_start_token(pid, timeout=min(_START_TOKEN_CAP_SECONDS, remaining))
    remaining = _remaining_or_fail(deadline)
    if token != snapshot.pid_start:
        raise OwnershipError(_NOT_OUR_BROWSER)
    holds = pid_holds_user_data_dir(
        pid, Path(snapshot.user_data_dir),
        timeout=min(_METADATA_CAP_SECONDS, remaining),
    )
    _remaining_or_fail(deadline)
    if not holds:
        raise OwnershipError(_DIRECTORY_NOT_HELD)


def _identity_holds_bounded(
    snapshot: OwnershipSnapshot, deadline: Deadline
) -> BaseException | None:
    """One bounded identity recheck, returning its refusal instead of raising.

    Runs inside a worker thread. Returning the exception (rather than
    raising it) means an abandoned call - the awaiting operation was
    cancelled - can never surface a late exception from the default
    executor: the thread finishes inside its capped budget and its result
    is simply discarded.
    """
    try:
        _identity_holds(snapshot, deadline)
    except (OwnershipError, contract.DeadlineExpiredError) as failure:
        return failure
    return None


def verify_recorded_owner(
    entry: ExtendedInstance, deadline: Deadline
) -> OwnershipSnapshot:
    """Prove a registry row positively names a browser this tool launched.

    Requires a recorded user-data directory (any string shape a real launch
    records - a named profile's directory or an unnamed launch's throwaway
    session dir), a recorded debug port (a positive non-boolean integer
    within 65535), and a recorded start-time identity, whose recorded PID is
    a live process of ours matching that start token and holding that
    directory. Fails closed on missing, unknown or stale evidence: the
    alternative is reading a credential first and discovering the wrong
    browser afterwards. The whole inspection draws from the operation
    deadline and a final clock check refuses an expired one.
    """
    if not isinstance(entry.pid_start, str) or not entry.pid_start:
        raise OwnershipError(_NO_START_IDENTITY)
    user_data_dir: object = entry.user_data_dir
    # Same boundary rule as the pid: the registry file can hold any JSON
    # value where a real launch records a path string.
    if not isinstance(user_data_dir, str) or not user_data_dir:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise OwnershipError(_NO_RECORDED_DIRECTORY)
    port: object = entry.port
    # Same boundary rule again: a real launch records the allocated port,
    # a tampered or hand-edited row can hold anything.
    if (
        isinstance(port, bool)
        or not isinstance(port, int)  # pyright: ignore[reportUnnecessaryIsInstance]
        or not 0 < port <= 65_535
    ):
        raise OwnershipError(_NO_RECORDED_PORT)
    snapshot = OwnershipSnapshot(
        pid=entry.pid,
        pid_start=entry.pid_start,
        user_data_dir=user_data_dir,
        port=port,
    )
    _identity_holds(snapshot, deadline)
    _remaining_or_fail(deadline)
    return snapshot


async def verify_connected_browser(
    cdp: Any, snapshot: OwnershipSnapshot, deadline: Deadline
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

    The send draws its allowance from the operation deadline, and the
    post-reply recheck runs in a worker thread: the receive/event loop stays
    responsive, and the recheck's subprocess calls are themselves capped by
    the remaining budget, so neither a cancelled future nor asyncio's
    executor join can extend normal operation past the deadline. A query
    that exhausts the budget reports the operation timeout - the stronger,
    earlier fact - rather than identity unavailability.
    """
    timeout = deadline.command_timeout()
    try:
        reply = await cdp.send(
            method="SystemInfo.getProcessInfo", params={}, timeout=timeout
        )
    except contract.DeadlineExpiredError:
        raise
    except Exception:
        if deadline.remaining() <= 0:
            # The query outlived the operation budget: the timeout outranks
            # the identity diagnosis (fixed text, no new error kind).
            raise contract.DeadlineExpiredError(contract.TIMED_OUT) from None
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
    failure = await asyncio.to_thread(_identity_holds_bounded, snapshot, deadline)
    if failure is not None:
        raise failure


__all__ = [
    "OwnershipError",
    "OwnershipSnapshot",
    "verify_connected_browser",
    "verify_recorded_owner",
]
