"""The shared handler surface every `CDPHandler` double inherits.

A double that is missing a name production calls is only exercised on the one
path that reads it, so the suite passes while the verb is broken. It happened
four times in one sitting: `target_by`, `deadline`, `record_caller_enable`,
then `borrowed_frame_selection`, each found in CI or in a live run rather
than here.

:class:`HandlerSurface` carries every one of those names with an inert
default. A suite's own double subclasses it and overrides only what that
suite reads, so a new name on `CDPHandler` reaches all five doubles at once.
`TestTheDoublesMatchTheRealHandler` in `test_step_run.py` holds both halves
of the rule: this class has every name production calls, and no handler
double in `tests/` is declared without it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

from browser_tools.mcp_response import make_text


class HandlerSurface:
    """Every name a handler is asked for, answering inertly.

    The defaults are class attributes rather than `__init__` assignments,
    because two of the doubles replace `curated.CDPHandler` and are
    constructed with its signature. A subclass that never calls up still
    gets the whole surface.
    """

    #: The connection is up and no step has set a deadline.
    available = True
    connect_error: str | None = None
    deadline: float | None = None
    target_by: str | None = None
    caller_enabled: frozenset[str] = frozenset()
    screencast_frame_count = 0
    session: tuple[Any, str] | None = ("CLIENT", "SESSION-1")

    def run(self) -> None:
        """The runtime's own loop. A double is already `available`."""

    def stop(self) -> None:
        """Close the session. A double that cares records it instead."""

    def set_deadline(self, deadline: float | None) -> None:
        self.deadline = deadline

    def record_caller_enable(self, method: str) -> None:
        domain, _, call = method.partition(".")
        if call == "enable" and domain:
            self.caller_enabled = self.caller_enabled | {domain}

    def require_session(self) -> tuple[Any, str]:
        from browser_tools.lifecycle import LifecycleError

        if self.session is None or not self.available:
            raise LifecycleError(self.connect_error or "the CDP session is not connected")
        return self.session

    def submit(self, coro: Any, timeout: float | None = None) -> Any:
        coro.close()
        return {}


    def call_tool(self, name: str, arguments: dict) -> dict:
        return make_text(f"{name}-ok")

    def call_native(self, name: str, arguments: dict) -> dict:
        return make_text(f"{name}-ok")

    def run_post_navigation_detection(self, max_retries: int | None = None) -> dict | None:
        return None

    def page_visibility_state(self) -> str | None:
        """What the focus guard reads before it lets input through."""
        return "visible"

    @contextlib.contextmanager
    def borrowed_frame_selection(self) -> Generator[None]:
        """No frame manager behind a double, so nothing to give back."""
        yield


class FakeHandler(HandlerSurface):
    """The run's session, and a log of everything that happened to it.

    One log for the handler and the steps together, so a test can assert
    *ordering across the two* - that the deadline was set before step 1 ran,
    not merely that it was set.
    """

    def __init__(self, log: list[Any] | None = None):
        self.log: list[Any] = [] if log is None else log
        self.session = ("CLIENT", "SESSION-1")
        self.available = True
        self.connect_error = None
        self.deadline: float | None = None
        self.deadlines: list[float | None] = []
        self.caller_enabled: frozenset[str] = frozenset()
        self.recorded_enables: list[str] = []
        self.screencast_frame_count = 0
        self.stopped = 0

    def record_caller_enable(self, method: str) -> None:
        self.recorded_enables.append(method)
        domain, _, call = method.partition(".")
        if call == "enable" and domain:
            self.caller_enabled = self.caller_enabled | {domain}

    def set_deadline(self, deadline: float | None) -> None:
        self.deadline = deadline
        self.deadlines.append(deadline)
        self.log.append(("deadline", deadline is not None))

    def require_session(self):
        return self.session

    def stop(self) -> None:
        self.stopped += 1

    def submit(self, coro, timeout=None):
        coro.close()
        self.log.append(("submit", timeout))
        return {"sent": True}

    def call_tool(self, name, arguments):
        from browser_tools.mcp_response import make_text

        self.log.append(("tool", name))
        return make_text("Cookies (0):" if name == "get_storage" else "ok")

    def call_native(self, name, arguments):
        from browser_tools.mcp_response import make_text

        self.log.append(("native", name))
        return make_text("[uid=AB-1] RootWebArea")

    def run_post_navigation_detection(self, max_retries=None):
        self.log.append(("detect", max_retries))
        return {"detections": [], "auto_retried": False, "retries_used": 0}
