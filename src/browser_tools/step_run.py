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
from typing import Any

from . import curated, passthrough, step_list
from .lifecycle import LifecycleError

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


def _one_step(step: step_list.Step, handler: Any, registry_path: str | None) -> Any:
    """Run one validated step over the run's session and return its document.

    A passthrough step goes to ``send_on_session`` rather than
    ``passthrough.send``, because ``send`` opens a connection of its own and
    the run already holds one. The hidden-tab refusal travels inside
    ``send_on_session``, so a step cannot reach the browser around it.
    """
    from . import cli

    if step.is_passthrough:
        method, params = step.as_method()
        cdp, session_id = handler.require_session()
        return handler.submit(passthrough.send_on_session(cdp, session_id, method, params))

    return cli.step_envelope(step.as_verb(), registry_path, handler=handler)


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

    for step in steps:
        entry: dict[str, Any] = {"index": step.number, "step": step.text}
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("the run's deadline passed")
            result = _one_step(step, handler, registry_path)
        except TimeoutError as exc:
            # A step's own bound can raise this too, so the run's deadline is
            # what decides which of the two happened, not the exception type.
            timed_out = deadline is not None and time.monotonic() >= deadline
            status = STATUS_TIMEOUT if timed_out else STATUS_FAILED
            entry["status"] = status
            entry["error"] = (
                f"the run's {timeout}s deadline passed" if timed_out else str(exc) or "timed out"
            )
            entries.append(entry)
            break
        except LifecycleError as exc:
            # Exit 1 as a bare invocation, so exit 1 here. `WaitTimeout` is
            # one of these: a step's own deadline is a step failure, and the
            # run did not time out.
            status = STATUS_FAILED
            entry["status"] = STATUS_FAILED
            entry["error"] = str(exc)
            entries.append(entry)
            break
        else:
            entry["status"] = STATUS_OK
            entry["result"] = result
            entries.append(entry)
            completed += 1

    handler.set_deadline(None)
    document = {
        "run": {"steps": len(steps), "completed": completed, "status": status},
        "steps": entries,
    }
    return document, status == STATUS_OK


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
    endpoint: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Validate a Step List, then run it. Returns ``(document, succeeded)``.

    Validation happens before the session opens, so every exit-2 failure
    leaves the browser untouched and prints nothing on stdout. Raises
    ``StepListError`` for those, and ``LifecycleError`` when the browser
    cannot be reached at all - which is before step 1, so there is no
    document to print either.
    """
    steps = step_list.validate(read_source(source), registry_path=registry_path)
    with curated.run_session(instance, target, url, registry_path, endpoint) as handler:
        return execute(steps, handler, registry_path, timeout)


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
