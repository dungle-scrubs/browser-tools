"""One dispatch decides what a step does, whether or not it is in a run.

`cli.step_envelope` returns the JSON document a step-capable verb produces.
`_run` prints what it returns; a Step Run will collect it into the Run
Document instead. That is what makes a step's `result` byte-identical to what
the same step prints alone, which is the composability RFC-03 specifies.

A Step Run with its own dispatch table would be a second place to add a verb
and a second place to get it wrong. RFC-03 rules that out for the parser and
for the preconditions, and the reason is the same here.
"""

from __future__ import annotations

import argparse

import pytest

from browser_tools import cli, step_list


class FakeHandler:
    """Records that the session reached the verb."""

    def __init__(self):
        self.stopped = False
        self.seen: list[str] = []
        self.session = ("CLIENT", "SESSION-1")

    def stop(self):
        self.stopped = True

    def call_native(self, name, arguments):
        from browser_tools.mcp_response import make_text

        self.seen.append(name)
        return make_text("[uid=AB-1] RootWebArea")

    def call_tool(self, name, arguments):
        from browser_tools.mcp_response import make_text

        self.seen.append(name)
        return make_text("ok")

    def submit(self, coro, timeout=None):
        self.seen.append(getattr(coro, "__qualname__", "coroutine"))
        coro.close()
        return []


def _args(command: str, **fields) -> argparse.Namespace:
    base = {
        "command": command,
        "instance": None,
        "target": None,
        "url": None,
        "endpoint": None,
    }
    base.update(fields)
    return argparse.Namespace(**base)


class TestTheDispatchCoversTheStepSurface:
    """The guard. A verb that is a step must have somewhere to go."""

    def test_every_step_verb_is_dispatched(self, monkeypatch):
        """Not a single one may fall through to the refusal at the end."""
        monkeypatch.setattr(
            "browser_tools.lifecycle.registry_path_from_env", lambda: None
        )
        unreachable = []
        for verb in sorted(step_list.STEP_VERBS):
            try:
                cli.step_envelope(_args(verb), None, handler=FakeHandler())
            except cli.PassthroughUsageError as exc:
                if "cannot be a step" in str(exc):
                    unreachable.append(verb)
            except Exception:
                # Any other failure means the dispatch found the verb and the
                # verb itself objected, which is what this test wants.
                pass
        assert not unreachable, (
            f"{unreachable} is on the step surface with no branch in "
            "step_envelope. A Step Run would refuse a step validation accepted."
        )

    def test_a_verb_that_is_not_a_step_is_refused(self):
        with pytest.raises(cli.PassthroughUsageError, match="cannot be a step"):
            cli.step_envelope(_args("launch"), None)

    def test_the_refusal_names_the_verb(self):
        with pytest.raises(cli.PassthroughUsageError, match="attach"):
            cli.step_envelope(_args("attach"), None)


class TestTheSessionReachesTheVerb:
    """A step runs on the run's session, not one of its own."""

    def test_a_curated_verb_uses_the_supplied_handler(self):
        handler = FakeHandler()
        result = cli.step_envelope(_args("snapshot"), None, handler=handler)
        assert handler.seen == ["take_snapshot"]
        assert "RootWebArea" in result["snapshot"]

    def test_a_frames_sub_action_uses_it_too(self):
        handler = FakeHandler()
        cli.step_envelope(
            _args("frames", frames_action="list"), None, handler=handler
        )
        assert handler.seen == ["list_frames"]

    def test_a_list_verb_submits_to_the_handlers_loop(self):
        handler = FakeHandler()
        cli.step_envelope(
            _args("console-list", duration=0.1), None, handler=handler
        )
        assert handler.seen and "collect_on_session" in handler.seen[0]

    def test_no_verb_closes_the_session(self):
        handler = FakeHandler()
        cli.step_envelope(_args("snapshot"), None, handler=handler)
        assert handler.stopped is False


class TestWithoutAHandlerNothingChanges:
    """The bare path still opens its own session, as it always did."""

    def test_a_curated_verb_opens_one(self, monkeypatch):
        opened = []
        monkeypatch.setattr(
            "browser_tools.curated.snapshot",
            lambda **kw: opened.append(kw.get("handler")) or {"snapshot": "x"},
        )
        cli.step_envelope(_args("snapshot"), None)
        assert opened == [None], "a bare invocation was handed a session"
