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
import json
import time
from typing import Any

import pytest

from browser_tools import step_list, step_run
from browser_tools.lifecycle import LifecycleError


class FakeHandler:
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
        self.deadlines: list[float | None] = []

    def set_deadline(self, deadline: float | None) -> None:
        self.deadlines.append(deadline)
        self.log.append(("deadline", deadline is not None))

    def require_session(self):
        return self.session

    def submit(self, coro, timeout=None):
        coro.close()
        self.log.append(("submit", timeout))
        return {"sent": True}


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
        def fake_session(instance, target, url, registry_path, endpoint):
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
        document, ok = step_run.execute([_step(1)], handler, timeout=0)
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
        assert "0.02s deadline" in document["steps"][0]["error"]

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
