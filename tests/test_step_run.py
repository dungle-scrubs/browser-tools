"""What a Step Run guarantees, as distinct from what its code currently does.

RFC-03 states four MUSTs about the engine, and each one is a property a
caller relies on rather than a line of code:

- the steps run in order, and the run stops at the first failure;
- nothing rolls back, so a completed step stays completed;
- `run.status` is "ok" only when every step succeeded;
- a step's `result` is byte-identical to what that step prints alone, which
  is what makes a Run Document composable with what a caller already parses.

The tests below are written against those, so a rewrite of the loop that
keeps the guarantees passes and one that quietly drops a guarantee fails.
The composability test uses object identity for the same reason: a result
that was re-rendered rather than passed through is a different object, and
comparing text would not notice.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import time
from typing import Any

import pytest
from doubles import FakeHandler

from browser_tools import step_list, step_run
from browser_tools.lifecycle import LifecycleError


def _step(number: int, text: str = "snapshot") -> step_list.Step:
    """One curated step, parsed the way `validate` parses it."""
    args = argparse.Namespace(
        command="snapshot", instance=None, target=None, url=None, endpoint=None
    )
    return step_list.Step(number=number, line=number, text=text, argv=[text], args=args)


def _raw_step(number: int, method: str = "Page.getNavigationHistory") -> step_list.Step:
    return step_list.Step(
        number=number,
        line=number,
        text=f"{method} {{}}",
        argv=[method, "{}"],
        args=None,
        method=method,
        params={},
    )


def _outcomes(monkeypatch, outcomes: list[Any], log: list[Any] | None = None):
    """Drive `_one_step` from a list: a value is returned, an exception raised."""
    calls: list[int] = []

    def fake(step, handler, registry_path):
        calls.append(step.number)
        if log is not None:
            log.append(("step", step.number))
        outcome = outcomes[step.number - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(step_run, "_one_step", fake)
    return calls


class TestTheStepsRunInOrderAndStopAtTheFirstFailure:
    def test_every_step_runs_when_none_fails(self, monkeypatch):
        calls = _outcomes(monkeypatch, [{"a": 1}, {"b": 2}, {"c": 3}])
        document, ok = step_run.execute([_step(1), _step(2), _step(3)], FakeHandler())
        assert calls == [1, 2, 3], "the steps did not run in the order they were written"
        assert ok is True
        assert document["run"] == {"steps": 3, "completed": 3, "status": "ok"}

    def test_a_failure_stops_the_steps_after_it(self, monkeypatch):
        calls = _outcomes(
            monkeypatch, [{"a": 1}, LifecycleError("the UID is stale"), {"c": 3}, {"d": 4}]
        )
        document, ok = step_run.execute([_step(i) for i in (1, 2, 3, 4)], FakeHandler())
        assert calls == [1, 2], "a step after the failure reached the browser"
        assert ok is False
        assert document["run"] == {"steps": 4, "completed": 1, "status": "failed"}

    def test_steps_never_reached_are_absent_from_the_document(self, monkeypatch):
        _outcomes(monkeypatch, [{"a": 1}, LifecycleError("nope"), {"c": 3}])
        document, _ = step_run.execute([_step(i) for i in (1, 2, 3)], FakeHandler())
        assert [entry["index"] for entry in document["steps"]] == [1, 2], (
            "a step that never ran has an entry, so a caller cannot tell "
            "which steps reached the browser"
        )

    def test_the_failing_step_is_the_last_entry(self, monkeypatch):
        _outcomes(monkeypatch, [{"a": 1}, LifecycleError("the UID is stale")])
        document, _ = step_run.execute([_step(1), _step(2)], FakeHandler())
        last = document["steps"][-1]
        assert last["status"] == "failed"
        assert last["error"] == "the UID is stale"
        assert "result" not in last, "a failed step carries an error, not a result"


class TestNothingRollsBack:
    """A step that has run has already reached the browser."""

    def test_a_completed_step_keeps_its_ok_entry_after_a_later_failure(self, monkeypatch):
        _outcomes(monkeypatch, [{"a": 1}, {"b": 2}, LifecycleError("boom")])
        document, _ = step_run.execute([_step(i) for i in (1, 2, 3)], FakeHandler())
        done = [entry for entry in document["steps"] if entry["status"] == "ok"]
        assert [entry["index"] for entry in done] == [1, 2]
        assert [entry["result"] for entry in done] == [{"a": 1}, {"b": 2}]

    def test_the_run_touches_the_browser_no_further_after_a_failure(self, monkeypatch):
        """No undo pass: the failure is the last thing that reaches the session."""
        log: list[Any] = []
        _outcomes(monkeypatch, [{"a": 1}, LifecycleError("boom"), {"c": 3}], log=log)
        handler = FakeHandler(log=log)
        step_run.execute([_step(i) for i in (1, 2, 3)], handler)
        after = log[log.index(("step", 2)) + 1 :]
        assert all(kind == "deadline" for kind, _ in after), (
            f"something reached the session after the failing step: {after}"
        )


class TestTheStatusIsOnlyOkWhenEveryStepSucceeded:
    @pytest.mark.parametrize(
        ("outcomes", "status"),
        [
            ([{"a": 1}], "ok"),
            ([LifecycleError("x")], "failed"),
            ([{"a": 1}, LifecycleError("x")], "failed"),
        ],
    )
    def test_the_status_reflects_the_run(self, monkeypatch, outcomes, status):
        _outcomes(monkeypatch, outcomes)
        document, ok = step_run.execute([_step(i + 1) for i in range(len(outcomes))], FakeHandler())
        assert document["run"]["status"] == status
        assert ok is (status == "ok")

    def test_completed_counts_successes_not_attempts(self, monkeypatch):
        _outcomes(monkeypatch, [{"a": 1}, {"b": 2}, LifecycleError("x")])
        document, _ = step_run.execute([_step(i) for i in (1, 2, 3)], FakeHandler())
        assert document["run"]["completed"] == 2, (
            "completed counted the failing step, so a caller reading it would "
            "believe a step reached the browser successfully when it did not"
        )
        assert len(document["steps"]) == 3, "the attempted step has no entry"


class TestAStepsResultIsWhatThatStepPrintsAlone:
    """Composability: identity, not equality. A copy is not good enough."""

    def test_the_result_is_the_dispatchs_own_object(self, monkeypatch):
        sentinel = {"snapshot": "[uid=AB-1] RootWebArea"}
        _outcomes(monkeypatch, [sentinel])
        document, _ = step_run.execute([_step(1)], FakeHandler())
        assert document["steps"][0]["result"] is sentinel, (
            "the run re-rendered the step's document instead of carrying it, "
            "so a caller cannot parse a step entry the way it parses the "
            "same step run alone"
        )

    def test_a_curated_step_goes_through_the_shared_dispatch(self, monkeypatch):
        """Not a second table: the step reaches `cli.step_envelope`."""
        from browser_tools import cli

        seen: list[Any] = []
        sentinel = {"snapshot": "x"}

        def fake_envelope(args, registry_path, handler=None):
            seen.append((args.command, handler))
            return sentinel

        monkeypatch.setattr(cli, "step_envelope", fake_envelope)
        handler = FakeHandler()
        document, _ = step_run.execute([_step(1)], handler, registry_path="/tmp/reg.json")
        assert seen == [("snapshot", handler)], "the step did not reach the shared dispatch"
        assert document["steps"][0]["result"] is sentinel

    def test_a_raw_step_sends_on_the_runs_session(self, monkeypatch):
        """Not `passthrough.send`, which would open a second connection."""
        from browser_tools import passthrough

        seen: list[Any] = []

        async def fake_send_on_session(cdp, session_id, method, params):
            seen.append((cdp, session_id, method, params))
            return {"entries": []}

        def refuse(*args, **kwargs):
            raise AssertionError("a step opened its own connection")

        monkeypatch.setattr(passthrough, "send_on_session", fake_send_on_session)
        monkeypatch.setattr(passthrough, "send", refuse)

        handler = FakeHandler()
        handler.submit = lambda coro, timeout=None: _drain(coro)
        document, ok = step_run.execute([_raw_step(1)], handler)

        assert ok is True
        assert seen == [("CLIENT", "SESSION-1", "Page.getNavigationHistory", {})]
        assert document["steps"][0]["result"] == {"entries": []}


def _drain(coro):
    """Run a coroutine that never awaits anything real, as the loop would."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("the coroutine awaited something this test cannot drive")


class TestOneSessionForTheWholeRun:
    def test_every_step_gets_the_same_handler(self, monkeypatch):
        handlers: list[Any] = []

        def fake(step, handler, registry_path):
            handlers.append(handler)
            return {}

        monkeypatch.setattr(step_run, "_one_step", fake)
        handler = FakeHandler()
        step_run.execute([_step(i) for i in (1, 2, 3)], handler)
        assert handlers == [handler, handler, handler]

    def test_the_session_opens_once_before_any_step(self, monkeypatch, tmp_path):
        """`run` resolves the instance and the page once, not per step."""
        from browser_tools import curated

        opened: list[Any] = []
        handler = FakeHandler()

        import contextlib

        @contextlib.contextmanager
        def fake_session(instance, target, url, registry_path, endpoint, all_frames=False):
            opened.append((instance, target, url, endpoint))
            yield handler

        monkeypatch.setattr(curated, "run_session", fake_session)
        monkeypatch.setattr(step_run, "_one_step", lambda step, h, r: {})

        source = tmp_path / "steps.txt"
        source.write_text("snapshot\nsnapshot\nsnapshot\n")
        document, ok = step_run.run(
            instance="web-01", source=str(source), target="2", registry_path=None
        )
        assert opened == [("web-01", "2", None, None)], "the run opened more than one session"
        assert document["run"]["completed"] == 3
        assert ok is True


class TestTheRunDeadline:
    def test_no_timeout_means_no_deadline(self, monkeypatch):
        _outcomes(monkeypatch, [{}])
        handler = FakeHandler()
        step_run.execute([_step(1)], handler, timeout=None)
        assert handler.deadlines == [None, None], "a run with no --timeout got a deadline"

    def test_zero_means_no_deadline(self, monkeypatch):
        """`--timeout 0` matches `wait --timeout 0`: no deadline, not an instant one."""
        _outcomes(monkeypatch, [{}])
        handler = FakeHandler()
        _, ok = step_run.execute([_step(1)], handler, timeout=0)
        assert handler.deadlines[0] is None
        assert ok is True, "a zero timeout killed the run instead of meaning 'no deadline'"

    def test_the_deadline_is_set_before_the_first_step_runs(self, monkeypatch):
        """Set between the steps only, it would not reach the step in flight."""
        log: list[Any] = []
        _outcomes(monkeypatch, [{}, {}], log=log)
        handler = FakeHandler(log=log)
        step_run.execute([_step(1), _step(2)], handler, timeout=30)
        assert log[0] == ("deadline", True), (
            f"the first thing the run did was {log[0]}, not arm the deadline, so "
            "a step that blocks forever is not bounded by --timeout"
        )

    def test_the_deadline_is_cleared_when_the_run_ends(self, monkeypatch):
        _outcomes(monkeypatch, [{}])
        handler = FakeHandler()
        step_run.execute([_step(1)], handler, timeout=30)
        assert handler.deadlines[-1] is None, (
            "the run left its deadline armed on a session someone else may still hold"
        )

    def test_a_passed_deadline_stops_the_run_before_the_next_step(self, monkeypatch):
        calls = _outcomes(monkeypatch, [{}, {}, {}])

        def slow(step, handler, registry_path):
            calls.append(step.number)
            time.sleep(0.05)
            return {}

        monkeypatch.setattr(step_run, "_one_step", slow)
        calls.clear()
        document, ok = step_run.execute([_step(i) for i in (1, 2, 3)], FakeHandler(), timeout=0.06)
        assert ok is False
        assert document["run"]["status"] == "timeout"
        assert len(calls) < 3, "the run ran every step despite passing its deadline"
        assert document["steps"][-1]["status"] == "timeout"

    def test_a_timeout_reports_timeout_not_failed(self, monkeypatch):
        """The two are different states and the document must not conflate them."""

        def blocked(step, handler, registry_path):
            time.sleep(0.05)
            raise TimeoutError("the run's deadline passed")

        monkeypatch.setattr(step_run, "_one_step", blocked)
        document, _ = step_run.execute([_step(1)], FakeHandler(), timeout=0.02)
        assert document["run"]["status"] == "timeout"
        assert document["steps"][0]["status"] == "timeout"
        assert "deadline" in document["steps"][0]["error"]

    def test_a_steps_own_timeout_with_no_run_deadline_is_a_failure(self, monkeypatch):
        """Not every TimeoutError is the run's. Without a deadline none of them is."""

        def blocked(step, handler, registry_path):
            raise TimeoutError("the browser never answered")

        monkeypatch.setattr(step_run, "_one_step", blocked)
        document, ok = step_run.execute([_step(1)], FakeHandler(), timeout=None)
        assert ok is False
        assert document["run"]["status"] == "failed", (
            "a step's own timeout was reported as the run timing out, which "
            "tells the caller to raise --timeout when that would change nothing"
        )
        assert document["steps"][0]["error"] == "the browser never answered"


class TestTheDiagnostic:
    def test_a_successful_run_has_none(self):
        assert step_run.describe_failure({"run": {"status": "ok"}, "steps": []}) is None

    def test_it_names_the_step_index_the_step_and_the_failure(self, monkeypatch):
        _outcomes(monkeypatch, [{}, LifecycleError("UID 'AB-1' is stale")])
        document, _ = step_run.execute([_step(1), _step(2, "click --uid AB-1")], FakeHandler())
        message = step_run.describe_failure(document)
        assert message is not None
        assert "step 2" in message
        assert "click --uid AB-1" in message
        assert "UID 'AB-1' is stale" in message


class TestReadingTheStepList:
    def test_a_dash_reads_stdin(self, monkeypatch):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("snapshot\n"))
        assert step_run.read_source("-") == "snapshot\n"

    def test_a_path_is_read(self, tmp_path):
        source = tmp_path / "steps.txt"
        source.write_text("snapshot\n")
        assert step_run.read_source(str(source)) == "snapshot\n"

    def test_a_missing_file_is_a_usage_error(self, tmp_path):
        missing = tmp_path / "nope.txt"
        with pytest.raises(step_list.StepListError) as exc:
            step_run.read_source(str(missing))
        assert "nope.txt" in str(exc.value)
        assert "No such file" in str(exc.value), "the reason the read failed was not reported"

    def test_a_bad_step_list_never_opens_a_session(self, monkeypatch, tmp_path):
        """Exit 2 means nothing was sent: validation precedes the connection."""
        from browser_tools import curated

        def refuse(*args, **kwargs):
            raise AssertionError("the run connected to the browser before validating")

        monkeypatch.setattr(curated, "run_session", refuse)
        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])

        source = tmp_path / "steps.txt"
        source.write_text("snapshot\nlaunch\n")
        with pytest.raises(step_list.StepListError):
            step_run.run(instance=None, source=str(source), registry_path=None)


class TestARunIsNotAStep:
    def test_run_is_on_the_excluded_list(self):
        assert "run" in step_list.EXCLUDED_VERBS

    def test_a_nested_run_is_refused_with_a_reason(self, monkeypatch):
        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        with pytest.raises(step_list.StepListError) as exc:
            step_list.validate("snapshot\nrun other.txt\n")
        assert "cannot be a step" in str(exc.value)
        assert "nesting" in str(exc.value)


class TestAStepIsAVerbOrAMethodNeverBoth:
    """The invariant the two accessors promise."""

    def test_neither_is_refused(self):
        with pytest.raises(ValueError, match="neither"):
            step_list.Step(number=1, line=1, text="x", argv=["x"], args=None)

    def test_both_is_refused(self):
        with pytest.raises(ValueError, match="both"):
            step_list.Step(
                number=1,
                line=1,
                text="x",
                argv=["x"],
                args=argparse.Namespace(command="snapshot"),
                method="Page.enable",
            )

    def test_as_verb_refuses_a_raw_step(self):
        with pytest.raises(ValueError, match="not a verb"):
            _raw_step(1).as_verb()

    def test_as_method_refuses_a_verb_step(self):
        with pytest.raises(ValueError, match="not a raw CDP step"):
            _step(1).as_method()


class TestTheCliFront:
    """Exit codes, and the one verb that prints on stdout when it exits 1."""

    def _invoke(self, monkeypatch, document, ok):
        from browser_tools import cli

        monkeypatch.setattr(step_run, "run", lambda **kwargs: (document, ok))
        monkeypatch.setattr(
            "browser_tools.lifecycle.registry_path_from_env", lambda: None
        )
        return cli.main(["run", "steps.txt"])

    def test_a_whole_run_exits_zero_and_prints_the_document(self, monkeypatch, capsys):
        document = {"run": {"steps": 1, "completed": 1, "status": "ok"}, "steps": []}
        assert self._invoke(monkeypatch, document, True) == 0
        assert json.loads(capsys.readouterr().out) == document

    def test_a_failed_run_exits_one_and_still_prints_the_document(self, monkeypatch, capsys):
        document = {
            "run": {"steps": 3, "completed": 1, "status": "failed"},
            "steps": [
                {"index": 1, "step": "snapshot", "status": "ok", "result": {"snapshot": "x"}},
                {"index": 2, "step": "click --uid A-1", "status": "failed", "error": "stale"},
            ],
        }
        assert self._invoke(monkeypatch, document, False) == 1
        captured = capsys.readouterr()
        assert json.loads(captured.out) == document, (
            "exit 1 printed nothing on stdout, so a caller cannot see which "
            "steps already reached the browser"
        )
        assert "step 2 failed" in captured.err
        assert "stale" in captured.err

    def test_a_malformed_step_list_exits_two_and_prints_nothing(self, monkeypatch, capsys):
        from browser_tools import cli

        def refuse(**kwargs):
            raise step_list.StepListError("line 2: 'launch' cannot be a step")

        monkeypatch.setattr(step_run, "run", refuse)
        monkeypatch.setattr(
            "browser_tools.lifecycle.registry_path_from_env", lambda: None
        )
        assert cli.main(["run", "steps.txt"]) == 2
        captured = capsys.readouterr()
        assert captured.out == "", "an exit-2 run printed on stdout, where nothing ran"
        assert "cannot be a step" in captured.err

    def test_the_run_verb_takes_its_own_target_and_timeout(self, monkeypatch):
        from browser_tools import cli

        seen: list[Any] = []
        monkeypatch.setattr(
            step_run,
            "run",
            lambda **kwargs: (seen.append(kwargs), ({"run": {"status": "ok"}, "steps": []}, True))[
                1
            ],
        )
        monkeypatch.setattr(
            "browser_tools.lifecycle.registry_path_from_env", lambda: None
        )
        cli.main(["run", "steps.txt", "--timeout", "45", "--url", "checkout"])
        assert seen[0]["timeout"] == 45.0
        assert seen[0]["url"] == "checkout"
        assert seen[0]["source"] == "steps.txt"

    def test_target_and_url_together_is_a_usage_error(self, monkeypatch, capsys):
        from browser_tools import cli

        monkeypatch.setattr(
            "browser_tools.lifecycle.registry_path_from_env", lambda: None
        )
        assert cli.main(["run", "steps.txt", "--target", "1", "--url", "x"]) == 2
        assert capsys.readouterr().out == ""


class TestADeadlineBoundsWorkNotTeardown:
    """A run that stops must not leave the browser mid-step.

    Found live, not by the suite: with a 3-second `--timeout`, a
    `screencast` step reached its deadline, and the `screencast_stop` that
    would have ended the capture was refused for being past the deadline.
    `Page.stopScreencast` never reached the browser.
    """

    def test_a_passed_deadline_still_grants_a_budget(self):
        from browser_tools.cdp_handler import DEADLINE_GRACE_SECONDS, CDPRuntime

        runtime = CDPRuntime(None)
        runtime.set_deadline(time.monotonic() - 10)
        # Approximate, not exact. The budget is `(now + GRACE) - now` across
        # two clock reads, so float subtraction can land a few parts in 10^15
        # either side of the constant. CI caught an exact comparison failing
        # on 5.000000000000014 while the same assertion passed locally.
        assert runtime.bounded_timeout(30) == pytest.approx(DEADLINE_GRACE_SECONDS, abs=0.05), (
            "a call made while unwinding past the deadline was refused, so the "
            "step could not undo what it started"
        )

    def test_the_grace_is_enough_to_act_and_not_enough_to_work(self):
        from browser_tools.cdp_handler import DEADLINE_GRACE_SECONDS

        assert 0 < DEADLINE_GRACE_SECONDS <= 10

    def test_a_live_deadline_still_clamps(self):
        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)
        runtime.set_deadline(time.monotonic() + 2)
        assert 0 < (runtime.bounded_timeout(30) or 0) <= 2

    def test_no_deadline_leaves_the_timeout_alone(self):
        from browser_tools.cdp_handler import CDPRuntime

        assert CDPRuntime(None).bounded_timeout(30) == 30
        assert CDPRuntime(None).bounded_timeout(None) is None


class TestTheStatusComesFromTheClockNotTheExceptionType:
    """A step cut off by the run's deadline raises whatever its own layer raises.

    `wait` raises TimeoutError straight out. A curated verb's tool call turns
    the same event into an error envelope and so into LifecycleError. Reading
    the type alone reported the second one as `failed`, which tells the caller
    the page is broken when the truth is that `--timeout` was too small.
    """

    def test_a_lifecycle_error_after_the_deadline_is_a_timeout(self, monkeypatch):
        def late(step, handler, registry_path):
            time.sleep(0.05)
            raise LifecycleError("Error: the run's deadline passed")

        monkeypatch.setattr(step_run, "_one_step", late)
        document, _ = step_run.execute([_step(1)], FakeHandler(), timeout=0.02)
        assert document["run"]["status"] == "timeout", (
            "a curated step cut off by the deadline was reported as a plain "
            "failure, so the caller cannot tell to raise --timeout"
        )

    def test_a_lifecycle_error_before_the_deadline_is_a_failure(self, monkeypatch):
        _outcomes(monkeypatch, [LifecycleError("the UID is stale")])
        document, _ = step_run.execute([_step(1)], FakeHandler(), timeout=60)
        assert document["run"]["status"] == "failed"
        assert document["steps"][0]["error"] == "the UID is stale"

    def test_the_steps_own_reason_survives_a_timeout(self, monkeypatch):
        """"The deadline passed" alone cannot tell a stuck step from a slow one."""

        def late(step, handler, registry_path):
            time.sleep(0.05)
            raise LifecycleError("the element is covered by an overlay")

        monkeypatch.setattr(step_run, "_one_step", late)
        document, _ = step_run.execute([_step(1)], FakeHandler(), timeout=0.02)
        error = document["steps"][0]["error"]
        assert "the element is covered by an overlay" in error, error
        assert "0.02s deadline" in error, error

    def test_a_reason_that_already_names_the_deadline_is_not_doubled(self, monkeypatch):
        def late(step, handler, registry_path):
            time.sleep(0.05)
            raise LifecycleError("the run's deadline passed")

        monkeypatch.setattr(step_run, "_one_step", late)
        document, _ = step_run.execute([_step(1)], FakeHandler(), timeout=0.02)
        assert document["steps"][0]["error"] == "the run's deadline passed"


class TestALocalWaitClampsItself:
    """The one wait `bounded_timeout` cannot reach.

    The screencast capture polls in the calling thread, so no CDP call is in
    flight to cut short. Without its own clamp a `--timeout 3` run sat
    through a `--duration 20` capture and took 20.2 seconds.
    """

    class _Recorder:
        def __init__(self, deadline):
            self.deadline = deadline
            self.screencast_frame_count = 0

    def test_the_capture_stops_at_the_runs_deadline(self):
        from browser_tools import curated

        handler = self._Recorder(time.monotonic() + 0.2)
        started = time.monotonic()
        curated._capture_for(handler, duration=30, max_frames=1000)
        elapsed = time.monotonic() - started
        assert elapsed < 5, (
            f"the capture ran {elapsed:.1f}s past a deadline 0.2s away, so the "
            "run's --timeout does not bound a step that waits locally"
        )

    def test_without_a_run_it_keeps_its_own_duration(self):
        from browser_tools import curated

        handler = self._Recorder(None)
        started = time.monotonic()
        curated._capture_for(handler, duration=0.15, max_frames=1000)
        assert time.monotonic() - started >= 0.15, (
            "a bare screencast stopped early, so --duration no longer means what it says"
        )

    def test_a_full_buffer_still_ends_it_early(self):
        from browser_tools import curated

        handler = self._Recorder(None)
        handler.screencast_frame_count = 10
        started = time.monotonic()
        assert curated._capture_for(handler, duration=30, max_frames=10) == 10
        assert time.monotonic() - started < 1


class TestACallThatTimesOutSaysSomething:
    """`str(TimeoutError())` is "", which reached the caller as bare "Error"."""

    def test_call_tool_names_the_tool(self, monkeypatch):
        import asyncio as _asyncio

        from browser_tools.cdp_handler import CDPHandler, CDPRuntime

        handler = CDPHandler.__new__(CDPHandler)
        rt = CDPRuntime.__new__(CDPRuntime)
        loop = type("L", (), {"is_running": lambda self: True})()
        rt._loop = loop  # pyright: ignore[reportAttributeAccessIssue]
        handler._rt = rt

        class _Future:
            def result(self, timeout=None):
                raise TimeoutError

            def cancel(self):
                pass

        def fake(coro, loop):
            coro.close()
            return _Future()

        monkeypatch.setattr(_asyncio, "run_coroutine_threadsafe", fake)
        response = handler.call_tool("screencast_stop", {})
        text = response["result"]["content"][0]["text"]
        assert response["result"]["isError"] is True
        assert "screencast_stop" in text and "in time" in text, text


class TestTheRunDoesNotOwnTheHandler:
    """`execute` takes a handler it did not open, so it must leave no state on it."""

    def test_an_uncaught_exception_still_disarms_the_deadline(self, monkeypatch):
        def explode(step, handler, registry_path):
            raise RuntimeError("a defect the engine does not catch")

        monkeypatch.setattr(step_run, "_one_step", explode)
        handler = FakeHandler()
        with pytest.raises(RuntimeError):
            step_run.execute([_step(1)], handler, timeout=30)
        assert handler.deadlines[-1] is None, (
            "the run left its deadline armed on a handler its caller still "
            "holds, so that caller's next call is bounded by a run that is over"
        )

    def test_the_run_never_closes_the_handler(self, monkeypatch):
        closed: list[bool] = []
        handler = FakeHandler()
        handler.stop = lambda: closed.append(True)  # pyright: ignore[reportAttributeAccessIssue]
        _outcomes(monkeypatch, [{}, LifecycleError("x")])
        step_run.execute([_step(1), _step(2)], handler)
        assert closed == [], "the engine closed a session it did not open"


class _OpenedItsOwnSession(BaseException):
    """Raised by the seam a step must not reach. Not an `Exception`, on purpose."""


class TestNoStepOpensItsOwnSession:
    """The guard for the defect the reviewer found in `detect`.

    `step_envelope` grew a `handler=` argument per verb, and one branch was
    written without it. The verb then resolved its own instance, opened its
    own connection, and lost the run's `--endpoint` and `--target`: a run
    pointed at one browser silently drove another. Reachability was already
    guarded; this guards that the run's session is the one that gets used.
    """

    @pytest.mark.parametrize("verb", sorted(step_list.STEP_VERBS))
    def test_a_step_never_opens_a_connection(self, verb, monkeypatch):
        from browser_tools import cli, curated

        def refuse(*args, **kwargs):
            # Not an `Exception`: the call below suppresses those, because a
            # verb objecting to its own arguments is fine and only the
            # connection matters here. An `AssertionError` would be swallowed
            # with them, and this test passed against the very defect it names.
            raise _OpenedItsOwnSession(
                f"'{verb}' opened its own session instead of using the run's, "
                "so the run's --endpoint and --target did not reach it"
            )

        monkeypatch.setattr(curated, "_cdp_handler_session", refuse)
        monkeypatch.setattr(curated, "_resolve_port", refuse)
        monkeypatch.setattr("browser_tools.lifecycle.registry_path_from_env", lambda: None)

        args = argparse.Namespace(
            command=verb,
            instance=None,
            target=None,
            url=None,
            endpoint=None,
            uid="AB-1",
            text="x",
            timeout=1,
            timeout_ms=100,
            event="X.y",
            match=None,
            duration=0.01,
            path=None,
            dir="/tmp/unused",
            format="jpeg",
            max_frames=1,
            wait=None,
            no_wait=True,
            frames_action="list",
            storage_action="get",
            key=None,
            removed_action=None,
        )
        handler = FakeHandler()
        with contextlib.suppress(Exception):
            cli.step_envelope(args, None, handler=handler)


class TestTheRunOwnsTheSessionItOpened:
    """Two mutations survived the first round of tests: both are ownership."""

    def test_a_step_never_closes_the_runs_session(self, monkeypatch):
        """A borrowed handler closed after step 1 leaves step 2 nothing to use."""
        from browser_tools import cli

        handler = FakeHandler()
        for _ in range(3):
            cli.step_envelope(
                argparse.Namespace(
                    command="snapshot", instance=None, target=None, url=None, endpoint=None
                ),
                None,
                handler=handler,
            )
        assert handler.stopped == 0, (
            "a curated verb closed the session the run handed it, so the next "
            "step would reconnect or fail"
        )

    def test_the_run_closes_the_session_it_opened_itself(self, monkeypatch):
        """And the owner does close it, on the way out."""
        from browser_tools import curated

        handler = FakeHandler()
        closed: list[int] = []

        import contextlib as _ctx

        @_ctx.contextmanager
        def fake_cdp_session(port, spec=None, by=None, external=False, all_frames=False):
            try:
                yield handler
            finally:
                handler.stopped += 1
                closed.append(handler.stopped)

        monkeypatch.setattr(curated, "_cdp_handler_session", fake_cdp_session)
        monkeypatch.setattr(curated, "_resolve_port", lambda i, r, e: 9222)
        with curated.run_session(None, None, None, None, None) as opened:
            assert opened is handler
        assert closed == [1], "the run left the session it opened open"


class TestWhatARawStepRaises:
    """A raw step failed in ways the engine did not catch: no document at all.

    `passthrough.send` wraps the same call in `@cli_cdp_errors` and converts
    `HiddenTargetError`. `send_on_session` has neither, so a `CDPError` from a
    bad method name killed the process with a traceback and printed nothing -
    the caller lost the record of every step that had already run.
    """

    def _run_raw(self, monkeypatch, exc):
        from browser_tools import passthrough

        async def boom(cdp, session_id, method, params):
            raise exc

        monkeypatch.setattr(passthrough, "send_on_session", boom)
        handler = FakeHandler()
        handler.submit = lambda coro, timeout=None: _drain(coro)
        return step_run.execute([_step(1), _raw_step(2), _step(3)], handler)

    def test_a_cdp_error_becomes_a_step_failure(self, monkeypatch):
        from browser_tools.core.errors import CDPError

        document, ok = self._run_raw(monkeypatch, CDPError(-32601, "method not found"))
        assert ok is False
        assert document["run"]["status"] == "failed"
        assert document["steps"][0]["status"] == "ok", "the completed step lost its record"
        assert "method not found" in document["steps"][1]["error"]
        assert len(document["steps"]) == 2, "step 3 ran after a failure"

    def test_a_connection_error_becomes_a_step_failure(self, monkeypatch):
        document, ok = self._run_raw(monkeypatch, ConnectionError("websocket closed"))
        assert ok is False
        assert "websocket closed" in document["steps"][1]["error"]

    def test_a_hidden_tab_refusal_becomes_a_step_failure(self, monkeypatch):
        from browser_tools.passthrough import HiddenTargetError

        document, ok = self._run_raw(monkeypatch, HiddenTargetError("the tab is in the background"))
        assert ok is False
        assert "background" in document["steps"][1]["error"]


class TestTheRunsEndpointReachesTheRefusals:
    """RFC-03 called these unreachable inside a run. They were not.

    A step cannot carry `--endpoint`, which is what the RFC reasoned from.
    But the run carries it, and every step is attached to that external
    browser, so `Browser.close` on a line closed a browser `bt` does not own
    where the bare invocation refuses.
    """

    @pytest.mark.parametrize("method", ["Browser.close", "Browser.crash"])
    def test_a_lifetime_method_is_refused_under_the_runs_endpoint(self, method, monkeypatch):
        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        with pytest.raises(step_list.StepListError) as exc:
            step_list.validate(f"{method}\n", endpoint="http://127.0.0.1:9222")
        assert "refused over --endpoint" in str(exc.value)
        assert "line 1" in str(exc.value)

    @pytest.mark.parametrize("method", ["Browser.close", "Browser.crash"])
    def test_it_is_allowed_against_a_browser_the_tool_owns(self, method, monkeypatch):
        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        steps = step_list.validate(f"{method}\n", endpoint=None)
        assert steps[0].as_method()[0] == method

    def test_the_refusal_happens_before_any_step_runs(self, monkeypatch, tmp_path):
        from browser_tools import curated

        def refuse(*args, **kwargs):
            raise AssertionError("the run connected before refusing the lifetime method")

        monkeypatch.setattr(curated, "run_session", refuse)
        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        source = tmp_path / "steps.txt"
        source.write_text("snapshot\nBrowser.close\n")
        with pytest.raises(step_list.StepListError):
            step_run.run(
                instance=None,
                source=str(source),
                endpoint="http://127.0.0.1:9222",
                registry_path=None,
            )


class TestARunThatOverranIsNeverOk:
    """A run that blew its `--timeout` exited 0 when the last step absorbed it.

    `screencast --duration 3` under `--timeout 0.05` clamped its capture,
    returned normally, and the loop ended with nothing left to check the
    clock against. `run.status` read "ok" and the exit code was 0, for a run
    that had already passed the deadline the caller set.
    """

    def test_the_last_step_absorbing_the_deadline_still_fails_the_run(self, monkeypatch):
        def slow_but_successful(step, handler, registry_path):
            time.sleep(0.05)
            return {"frames": 1}

        monkeypatch.setattr(step_run, "_one_step", slow_but_successful)
        document, ok = step_run.execute([_step(1, "screencast --dir d")], FakeHandler(), timeout=0.02)
        assert ok is False, "a run that passed its deadline reported success"
        assert document["run"]["status"] == "timeout"

    def test_the_interrupted_step_keeps_its_result_and_says_it_was_cut(self, monkeypatch):
        """Nothing rolls back: the frames it did capture are real output."""

        def slow_but_successful(step, handler, registry_path):
            time.sleep(0.05)
            return {"frames": 1}

        monkeypatch.setattr(step_run, "_one_step", slow_but_successful)
        document, _ = step_run.execute([_step(1)], FakeHandler(), timeout=0.02)
        entry = document["steps"][0]
        assert entry["result"] == {"frames": 1}, "the work the step did was discarded"
        assert entry["status"] == "timeout", (
            "a partial result was labelled ok, so a caller reads it as the whole answer"
        )
        assert document["run"]["completed"] == 0, (
            "a step cut off by the deadline counted as completed"
        )

    def test_a_step_after_it_gets_no_entry_at_all(self, monkeypatch):
        """The timeout must not be attributed to a step that never started."""
        calls: list[int] = []

        def slow_first(step, handler, registry_path):
            calls.append(step.number)
            if step.number == 1:
                time.sleep(0.05)
            return {}

        monkeypatch.setattr(step_run, "_one_step", slow_first)
        document, _ = step_run.execute(
            [_step(1, "screencast --dir d"), _step(2, "frames reset")],
            FakeHandler(),
            timeout=0.02,
        )
        assert calls == [1], "the second step ran past the deadline"
        assert [entry["index"] for entry in document["steps"]] == [1], (
            "a step that never started has an entry, and it is the one the "
            "timeout is pinned to"
        )


class TestTheGraceIsOneBudgetForTheWholeUnwind:
    """Per call, four teardown calls would each buy another five seconds."""

    def test_the_budget_shrinks_across_calls(self):
        from browser_tools.cdp_handler import DEADLINE_GRACE_SECONDS, CDPRuntime

        runtime = CDPRuntime(None)
        runtime.set_deadline(time.monotonic() - 1)
        first = runtime.bounded_timeout(30) or 0
        time.sleep(0.05)
        second = runtime.bounded_timeout(30) or 0
        assert second < first, (
            f"every call got its own grace ({first} then {second}), so a step "
            "making several can run arbitrarily far past the deadline"
        )
        assert first <= DEADLINE_GRACE_SECONDS

    def test_the_first_call_gets_no_more_than_the_grace_at_any_clock(
        self, monkeypatch
    ):
        """The flake this pins, reproduced without waiting for the right clock.

        `left` used to be `(now + DEADLINE_GRACE_SECONDS) - now`, which is
        not exactly the grace in binary floating point. On a runner about a
        minute past boot the round trip overshoots roughly 2% of the time,
        and CI duly failed with `5.000000000000014 <= 5.0`. The clock value
        below is one of the draws that does it.
        """
        import browser_tools.cdp_handler as cdp_handler
        from browser_tools.cdp_handler import DEADLINE_GRACE_SECONDS, CDPRuntime

        clock = 63.01849439829268
        assert (clock + DEADLINE_GRACE_SECONDS) - clock > DEADLINE_GRACE_SECONDS, (
            "this clock value no longer overshoots, so the test proves nothing"
        )
        monkeypatch.setattr(cdp_handler.time, "monotonic", lambda: clock)

        runtime = CDPRuntime(None)
        runtime._deadline = clock - 1
        assert runtime.bounded_timeout(30) <= DEADLINE_GRACE_SECONDS

    def test_a_spent_budget_still_leaves_enough_to_send_one_call(self):
        from browser_tools.cdp_handler import TEARDOWN_FLOOR_SECONDS, CDPRuntime

        runtime = CDPRuntime(None)
        runtime.set_deadline(time.monotonic() - 1)
        runtime._grace_until = time.monotonic() - 1  # the grace is gone too
        assert runtime.bounded_timeout(30) == TEARDOWN_FLOOR_SECONDS, (
            "a call handed zero fails before it is sent, so the teardown the "
            "grace exists for does not happen either"
        )

    def test_a_new_deadline_opens_a_new_budget(self):
        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)
        runtime.set_deadline(time.monotonic() - 1)
        runtime.bounded_timeout(30)
        assert runtime._grace_until is not None
        runtime.set_deadline(None)
        assert runtime._grace_until is None, "a finished run left its grace behind"


class TestEveryRuntimeWaitIsBounded:
    """A wait that does not call `bounded_timeout` is a hole in `--timeout`.

    The reviewer's surviving mutation: removing the bound from the runtime's
    waits changed nothing any test could see. These name the waits.
    """

    def test_no_result_call_takes_a_raw_timeout(self):
        """Parsed, not grepped: these calls wrap, and a line-local search
        read a wrapped one as having no timeout argument at all."""
        import ast
        from pathlib import Path as _Path

        source = _Path("src/browser_tools/cdp_handler.py")
        tree = ast.parse(source.read_text())
        unbounded: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "result"):
                continue
            argument = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
            if argument is None:
                continue
            text = ast.unparse(argument)
            # `submit` computes the bound one line up and passes it as `bound`.
            if "bounded_timeout" in text or text == "bound":
                continue
            unbounded.append(f"line {node.lineno}: .result(timeout={text})")
        assert not unbounded, (
            f"these waits ignore the run's deadline: {unbounded}. A step using "
            "one runs past --timeout without the run noticing."
        )

    def test_the_focus_guard_reads_visibility_under_the_deadline(self):
        """The guard above proves the call is bounded. This names the wait,
        so a rename that drops the bound is a failure here too."""
        from browser_tools.cdp_handler import VISIBILITY_READ_TIMEOUT_SECONDS, CDPRuntime

        runtime = CDPRuntime(None)
        runtime.set_deadline(time.monotonic() + 0.5)
        bound = runtime.bounded_timeout(VISIBILITY_READ_TIMEOUT_SECONDS) or 0
        assert bound < VISIBILITY_READ_TIMEOUT_SECONDS, (
            "the focus guard's five-second visibility read outlives a run "
            "with less than five seconds left"
        )


class TestAStepsOwnWaitIsNeverTheRunsTimeout:
    """`WaitTimeout` is raised only by a step's own deadline, whatever the clock says."""

    def test_a_wait_timeout_at_the_deadline_is_still_a_failure(self, monkeypatch):
        from browser_tools.events import WaitTimeout

        def own_deadline(step, handler, registry_path):
            time.sleep(0.05)
            raise WaitTimeout("timed out after 0.999s waiting for Page.loadEventFired")

        monkeypatch.setattr(step_run, "_one_step", own_deadline)
        document, _ = step_run.execute([_step(1, "wait --event X.y")], FakeHandler(), timeout=0.02)
        assert document["run"]["status"] == "failed", (
            "the step's own wait expiring was reported as the run timing out, "
            "which tells the caller to raise --timeout when the step's own "
            "--timeout is what expired"
        )
        assert "Page.loadEventFired" in document["steps"][0]["error"]


class TestAFileThatIsNotText:
    def test_a_binary_step_list_is_a_usage_error(self, tmp_path):
        source = tmp_path / "binary.steps"
        source.write_bytes(b"\xcf\xfa\xed\xfe\x00\x00")
        with pytest.raises(step_list.StepListError) as exc:
            step_run.read_source(str(source))
        assert "not UTF-8" in str(exc.value)

    def test_it_exits_two_and_prints_nothing(self, monkeypatch, tmp_path, capsys):
        from browser_tools import cli

        source = tmp_path / "binary.steps"
        source.write_bytes(b"\xcf\xfa\xed\xfe\x00\x00")
        monkeypatch.setattr("browser_tools.lifecycle.registry_path_from_env", lambda: None)
        assert cli.main(["run", str(source)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "not UTF-8" in captured.err


class TestADeadlineAlreadyPastRunsNothing:
    """`execute` can be handed a deadline that has already gone.

    A tiny `--timeout` against a connection that took longer to open than the
    budget arrives here with no time left. The pre-step check is what stops
    step 1 from running anyway, and it is reachable only in this case: after
    a successful step the post-step check has already returned.
    """

    def test_no_step_runs(self, monkeypatch):
        calls = _outcomes(monkeypatch, [{}, {}])
        handler = FakeHandler()
        document, ok = step_run.execute([_step(1), _step(2)], handler, timeout=-1)
        assert calls == [], "a step reached the browser after the run was already over"
        assert ok is False
        assert document["run"] == {"steps": 2, "completed": 0, "status": "timeout"}

    def test_and_no_step_gets_an_entry(self, monkeypatch):
        _outcomes(monkeypatch, [{}, {}])
        document, _ = step_run.execute([_step(1), _step(2)], FakeHandler(), timeout=-1)
        assert document["steps"] == [], (
            "a step that never started has an entry, so the caller cannot tell "
            "what reached the browser from what did not"
        )


class TestTheDoublesMatchTheRealHandler:
    """Four times in one sitting a handler gained a name and a double did
    not: `target_by`, `deadline`, `record_caller_enable`, then
    `borrowed_frame_selection`. Each time the suite passed and the break
    showed up in CI or in a live run, because a double missing a name is
    only exercised on the one path that reads it.

    The rule has two halves, and one without the other is no guard at all.
    `HandlerSurface` carries every name production calls on a handler, and
    every handler double in `tests/` inherits it. Checking only the first
    half leaves a suite free to declare its own bare double; checking only
    the second leaves the shared surface free to fall behind.
    """

    @staticmethod
    def _names_production_calls() -> set[str]:
        import re
        from pathlib import Path as _Path

        import browser_tools

        package = _Path(browser_tools.__file__).parent
        names: set[str] = set()
        for path in sorted(package.glob("*.py")):
            for match in re.finditer(r"\bhandler\.([a-z_][a-z0-9_]*)", path.read_text()):
                names.add(match.group(1))
        return names

    def test_the_scan_finds_something(self):
        found = self._names_production_calls()
        assert {"submit", "require_session", "call_tool"} <= found, (
            f"the scan is not finding handler attributes any more: {sorted(found)}"
        )

    def test_the_real_handler_has_every_name(self):
        """If this fails, production calls something `CDPHandler` lacks."""
        from browser_tools.cdp_handler import CDPHandler

        missing = sorted(n for n in self._names_production_calls() if not hasattr(CDPHandler, n))
        assert not missing, f"production calls handler.{missing} and CDPHandler has no such name"

    def test_the_shared_surface_has_every_name(self):
        from doubles import HandlerSurface

        missing = sorted(
            n for n in self._names_production_calls() if not hasattr(HandlerSurface, n)
        )
        assert not missing, (
            f"HandlerSurface is missing {missing}, which production calls on a "
            "handler. A test using a double only fails on the path that reads "
            "the missing name, so the suite can pass while the verb is broken."
        )

    #: Defining any of these makes a class a handler double, whatever it is
    #: called. Naming the methods rather than the class names is what stops a
    #: `_StubSession` from slipping past.
    HANDLER_METHODS = frozenset(
        {"call_tool", "call_native", "require_session", "record_caller_enable", "submit"}
    )

    @staticmethod
    def _doubles_declared_without_the_surface() -> list[str]:
        """Every handler double in `tests/` that does not inherit the surface.

        A base defined in the same file counts, so a suite may derive one
        double from another. The walk is transitive within the file, with
        `HandlerSurface` as the only root.
        """
        import ast
        from pathlib import Path as _Path

        offenders: list[str] = []
        for path in sorted(_Path(__file__).parent.glob("test_*.py")):
            tree = ast.parse(path.read_text())
            classes = {
                node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
            }

            def rooted(name, where, seen=frozenset()):
                node = where.get(name)
                if node is None or name in seen:
                    return False
                for base in node.bases:
                    if not isinstance(base, ast.Name):
                        continue
                    if base.id == "HandlerSurface" or rooted(base.id, where, seen | {name}):
                        return True
                return False

            for name, node in classes.items():
                methods = {
                    child.name
                    for child in node.body
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                }
                if methods & TestTheDoublesMatchTheRealHandler.HANDLER_METHODS and not rooted(
                    name, classes
                ):
                    offenders.append(f"{path.name}:{name}")
        return offenders

    def test_the_scan_for_doubles_finds_the_real_ones(self):
        """The check above is only as good as this walk, so pin the walk."""
        import ast
        from pathlib import Path as _Path

        found = set()
        for path in sorted(_Path(__file__).parent.glob("test_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.ClassDef):
                    continue
                methods = {
                    child.name
                    for child in node.body
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                }
                if methods & self.HANDLER_METHODS:
                    found.add(f"{path.name}:{node.name}")
        assert {
            "test_curated_verbs.py:FakeHandler",
            "test_step_dispatch.py:FakeHandler",
            "test_shared_session.py:FakeHandler",
        } <= found, f"the walk stopped finding handler doubles: {sorted(found)}"

    def test_a_double_that_replaces_cdphandler_takes_its_arguments(self):
        """The guard above covers attributes. This one covers the constructor.

        Two doubles are monkeypatched over `curated.CDPHandler`, so they are
        constructed with whatever `_cdp_handler_session` passes. Adding
        `all_frames` to `CDPHandler` broke both, and the attribute guard could
        not see it: a missing keyword argument is a `TypeError` at
        construction, not a missing name.
        """
        import inspect

        from browser_tools.cdp_handler import CDPHandler

        real = set(inspect.signature(CDPHandler.__init__).parameters) - {"self"}
        for module in ("test_curated_verbs", "test_interaction_targeting"):
            double = __import__(module).FakeHandler
            taken = set(inspect.signature(double.__init__).parameters) - {"self"}
            missing = sorted(real - taken)
            assert not missing, (
                f"{module}.FakeHandler stands in for CDPHandler and does not "
                f"take {missing}. Every verb that constructs one fails with a "
                "TypeError the moment production passes it."
            )

    def test_every_double_inherits_the_shared_surface(self):
        offenders = self._doubles_declared_without_the_surface()
        assert not offenders, (
            f"{offenders} look like handler doubles but do not inherit "
            "HandlerSurface, so a new name on CDPHandler will not reach them. "
            "Subclass `doubles.HandlerSurface` and override only what the "
            "suite reads."
        )
