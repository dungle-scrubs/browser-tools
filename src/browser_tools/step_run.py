"""The Step Run engine: one invocation, one session, many steps.

A Step List is data, not code (RFC-03, "The execution model"). This module
reads one, runs its steps in order over a single session, and renders what
happened as the Run Document.

The three things it must not do, each of which the RFC states as a MUST:

- **Run a step twice, or out of order.** The list is an order, and the run
  stops at the first failure.
- **Roll anything back.** A step that has run has already reached the
  browser. The Run Document says which steps completed so the caller knows
  the state it is in; nothing here tries to undo one.
- **Dispatch a step its own way.** A step goes through ``cli.step_envelope``,
  the same dispatch a bare invocation uses, so a step's ``result`` is
  byte-identical to what that step prints alone. A second table here would
  be a second place to add a verb and a second place to get it wrong.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import curated, passthrough, step_list
from .events import WaitTimeout
from .lifecycle import LifecycleError
from .one_shot import cli_cdp_errors
from .passthrough import HiddenTargetError

if TYPE_CHECKING:
    from .endpoint import ResolvedEndpoint

#: ``bt run -`` reads the Step List from stdin.
STDIN_SOURCE = "-"

#: What ``run.status`` can be. ``"ok"`` only when every step succeeded.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"


def read_source(source: str) -> str:
    """The Step List text, from a file or stdin.

    An unreadable path is a usage error (exit 2): nothing has been sent or
    written, and the caller gets the path and the reason rather than a
    traceback.
    """
    if source == STDIN_SOURCE:
        return sys.stdin.read()
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise step_list.StepListError(f"cannot read the step list at '{source}': {reason}") from exc
    except UnicodeDecodeError as exc:
        # `bt run /usr/bin/python` is a plausible slip, and it exited 1 with a
        # traceback. A file that is not text is a malformed step list, which
        # is exit 2: nothing was sent.
        raise step_list.StepListError(
            f"the step list at '{source}' is not UTF-8 text: {exc.reason} at byte {exc.start}"
        ) from exc


@cli_cdp_errors
def _one_step(step: step_list.Step, handler: Any, registry_path: str | None) -> Any:
    """Run one validated step over the run's session and return its document.

    A passthrough step goes to ``send_on_session`` rather than
    ``passthrough.send``, because ``send`` opens a connection of its own and
    the run already holds one. The hidden-tab refusal travels inside
    ``send_on_session``, so a step cannot reach the browser around it.

    ``@cli_cdp_errors`` and the ``HiddenTargetError`` conversion below are the
    other half of what `passthrough.send` does around that same call. Without
    them a raw step's ``CDPError`` or ``ConnectionError`` left the engine
    uncaught: the process died with a traceback, printed no Run Document, and
    so told the caller nothing about the steps that had already run. A raw
    step now fails the way the same line fails as a bare invocation, which is
    exit 1 with a reason.
    """
    from . import cli

    if step.is_passthrough:
        method, params = step.as_method()
        # A domain this step turns on is the caller's, and no later step may
        # give it back. Recorded before the send, so a step that enables a
        # domain and then fails on something else still owns it.
        handler.record_caller_enable(method)
        cdp, session_id = handler.require_session()
        try:
            return handler.submit(passthrough.send_on_session(cdp, session_id, method, params))
        except HiddenTargetError as exc:
            raise LifecycleError(str(exc)) from exc

    return cli.step_envelope(step.as_verb(), registry_path, handler=handler)


def _why(exc: BaseException, timeout: float | None) -> str:
    """The step's own reason, with the run's deadline named when it caused it.

    The reason is kept either way. A caller told only "the deadline passed"
    cannot tell a step that was nearly done from one that was stuck, and the
    step already said which.
    """
    reason = str(exc) or "the step did not answer in time"
    if timeout is None or "deadline" in reason:
        return reason
    return f"the run's {timeout}s deadline passed: {reason}"


def execute(
    steps: list[step_list.Step],
    handler: Any,
    registry_path: str | None = None,
    timeout: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run the steps in order. Returns ``(run document, every step succeeded)``.

    ``timeout`` bounds the whole run, measured from the start of step 1.
    ``None`` and ``0`` both mean no whole-run deadline, matching ``wait``:
    every step already bounds itself, so a run only needs an outer bound when
    a step opts out of its own.

    The deadline reaches the step in flight, not only the gaps between steps.
    ``handler.set_deadline`` clamps every wait the session makes, which is why
    a run whose step 3 is ``wait --timeout 0`` still stops on time.
    """
    deadline = time.monotonic() + timeout if timeout else None
    handler.set_deadline(deadline)

    entries: list[dict[str, Any]] = []
    completed = 0
    status = STATUS_OK

    try:
        completed, status = _drive(steps, handler, registry_path, timeout, deadline, entries)
    finally:
        # In a `finally`, because the run does not own this handler. A caller
        # that supplied one still holds it after an exception the loop does
        # not catch, and a deadline left armed on it would refuse that
        # caller's next call for a run that is already over.
        handler.set_deadline(None)

    document = {
        "run": {"steps": len(steps), "completed": completed, "status": status},
        "steps": entries,
    }
    return document, status == STATUS_OK


def _drive(
    steps: list[step_list.Step],
    handler: Any,
    registry_path: str | None,
    timeout: float | None,
    deadline: float | None,
    entries: list[dict[str, Any]],
) -> tuple[int, str]:
    """The loop itself. Appends to ``entries`` as it goes, so a caller that
    catches an exception from here still has every step attempted so far."""
    completed = 0

    def overran() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    for step in steps:
        if overran():
            # The run is over and this step has not started. It gets no
            # entry: a step that never reached the browser must not appear,
            # or a caller reading the last entry blames the wrong step.
            return completed, STATUS_TIMEOUT

        entry: dict[str, Any] = {"index": step.number, "step": step.text}
        try:
            result = _one_step(step, handler, registry_path)
        except (TimeoutError, LifecycleError) as exc:
            # The exception type does not say which of the two happened. A
            # step cut off by the run's deadline surfaces as whatever its own
            # layer raises: `wait` raises TimeoutError straight out, while a
            # curated verb's tool call turns it into an error envelope and so
            # into LifecycleError. The clock is the only reliable test for
            # those, and asking it here is what keeps `timeout` and `failed`
            # apart. A `WaitTimeout` is the exception: only a step's own wait
            # raises it, so it is the step's deadline however the clock reads.
            timed_out = overran() and not isinstance(exc, WaitTimeout)
            status = STATUS_TIMEOUT if timed_out else STATUS_FAILED
            entry["status"] = status
            entry["error"] = _why(exc, timeout if timed_out else None)
            entries.append(entry)
            return completed, status

        entry["result"] = result
        entries.append(entry)

        if overran():
            # The step returned, but the deadline passed while it was
            # running, so what it returned may be a part of what was asked
            # for: a `screencast` clamped to the deadline writes the frames it
            # got. The result is kept, because the step did that work and
            # nothing rolls back, and the entry says `timeout` so the caller
            # knows not to read it as a whole answer. It is not counted in
            # `completed`, which is the steps that finished inside the budget.
            entry["status"] = STATUS_TIMEOUT
            entry["error"] = f"the run's {timeout}s deadline passed while this step ran"
            return completed, STATUS_TIMEOUT

        entry["status"] = STATUS_OK
        completed += 1

    return completed, STATUS_OK


def describe_failure(document: dict[str, Any]) -> str | None:
    """The stderr diagnostic for a run that did not finish, or None.

    Names the step index, the step as written, and the failure, because the
    Run Document goes to stdout and a caller watching a terminal reads
    stderr. Returns None for a run that succeeded.
    """
    if document["run"]["status"] == STATUS_OK:
        return None
    last = document["steps"][-1] if document["steps"] else None
    if last is None:
        return "the run failed before its first step"
    what = "timed out" if last["status"] == STATUS_TIMEOUT else "failed"
    return f"step {last['index']} {what}: {last['step']}\n{last['error']}"


def run(
    *,
    instance: str | None,
    source: str,
    timeout: float | None = None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
    endpoint: str | ResolvedEndpoint | None = None,
    all_frames: bool = False,
    dialog: str = "dismiss",
) -> tuple[dict[str, Any], bool]:
    """Validate a Step List, then run it. Returns ``(document, succeeded)``.

    Validation happens before the session opens, so every exit-2 failure
    leaves the browser untouched and prints nothing on stdout. Raises
    ``StepListError`` for those, and ``LifecycleError`` when the browser
    cannot be reached at all - which is before step 1, so there is no
    document to print either.
    """
    from .dialog_policy import validate_policy

    validate_policy(dialog)
    steps = step_list.validate(
        read_source(source), registry_path=registry_path, endpoint=endpoint
    )
    with curated.run_session(
        instance, target, url, registry_path, endpoint, all_frames, dialog=dialog
    ) as handler:
        document, succeeded = execute(steps, handler, registry_path, timeout)
        document.update(handler.dialog_document())
        return document, succeeded


__all__ = [
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "STDIN_SOURCE",
    "describe_failure",
    "execute",
    "read_source",
    "run",
]
