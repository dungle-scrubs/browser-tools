"""The login browser transaction, driven over a fake CDP session.

The fake implements exactly the command surface ``login.py`` uses --
``Page.getFrameTree``, ``DOM.createIsolatedWorld``, ``Runtime.evaluate`` and
``Runtime.callFunctionOn`` -- with scriptable outcomes and a shared event
log that also records the relay reads, so tests can prove the *ordering*
that is the whole point of the transaction:

    validate everything -> read credentials -> revalidate -> fill ->
    revalidate -> submit -> clear -> wait for success

and that no sentinel credential ever appears in script source, diagnostics,
stdout or stderr. Real-Chrome behavior (the actual JS) is exercised by
``test_login_live.py``; this module proves the Python orchestration keeps
its side of the contract.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from browser_tools import credential_relay
from browser_tools import login as login_module
from browser_tools.core import registry as core_registry
from browser_tools.core.attach import AmbiguousTargetError, TargetNotFoundError
from browser_tools.core.errors import CDPError, NoPageError
from browser_tools.credential_relay import RelayError, Secret
from browser_tools.lifecycle import LifecycleError

ORIGIN = "https://example.com"
VAULT = "dqss6l4yprxhntxymzjaltchwe"
ITEM = "bbbbbbbbbbbbbbbbbbbbbbbbbb"
SENTINEL_USER = "SENTINEL-user"
SENTINEL_PASS = "SENTINEL-pass"

OK_OUTCOME = {"ok": True}
OK_FILL = {"ok": True}
OK_SUBMIT = {"ok": True}
OK_CLEAR = {"ok": True, "cleared": True}
AUTHENTICATED_PROBE = {"result": {"value": {"authenticated": True, "originOk": True}}}
UNAUTHENTICATED_PROBE = {"result": {"value": {"authenticated": False, "originOk": True}}}


def policy_file(tmp_path, *, origin: str = ORIGIN, alias: str = "example") -> str:
    document = {
        "version": 1,
        "identity": "browser",
        "vault": VAULT,
        "accounts": {
            alias: {
                "item": ITEM,
                "origin": origin,
                "username_selector": "#username",
                "password_selector": "#password",
                "submit_selector": "button[type=submit]",
                "success_selector": "[data-signed-in]",
            }
        },
    }
    path = tmp_path / "logins.json"
    path.write_text(json.dumps(document))
    return str(path)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """An isolated registry holding one live-looking Chrome instance."""
    path = str(tmp_path / "registry.json")
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", path)
    core_registry._save_registry(  # pyright: ignore[reportPrivateUsage]
        {"web-01": {
            "port": 9500,
            "pid": 2_000_000_000,
            "browser_version": "Chrome/1",
            "user_data_dir": "",
            "engine": "chrome",
        }},
        path,
    )
    return path


class FakeCDP:
    """The login command surface, scriptable and logged."""

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log
        self.loader_id = "L1"
        self.world_dead = False  # a replaced document: object ids are gone
        self.outcome: dict = OK_OUTCOME
        self.fill: dict | Exception = OK_FILL
        self.submit: dict | Exception = OK_SUBMIT
        self.clear: dict | Exception = OK_CLEAR
        self.probe: dict = AUTHENTICATED_PROBE
        self.raise_on: dict[str, Exception] = {}
        self.exception_details_on: set[str] = set()
        self._handlers: dict[str, list[Any]] = {}

    def on(self, event, callback, session_id=None) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def off(self, event, callback) -> None:
        self._handlers[event] = [
            cb for cb in self._handlers.get(event, []) if cb is not callback
        ]

    def navigate(self) -> None:
        """Replace the document: new loader, dead world."""
        self.loader_id = "L2"
        self.world_dead = True

    def emit(self, event: str, payload: dict) -> None:
        """Deliver one event to registered handlers, as the client would."""
        for callback in list(self._handlers.get(event, [])):
            callback(payload)

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.log.append((method, params))
        if method in self.raise_on:
            raise self.raise_on[method]
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1", "loaderId": self.loader_id}}}
        if method == "Page.enable":
            return {}
        if method == "Page.handleJavaScriptDialog":
            return {}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            # The Python side evaluates the form helper's begin call, plus
            # one tail call, in a freshly created isolated world; the small
            # standalone success probe evaluates without the helper at all.
            # A replaced document never reaches this branch; only the bound
            # session object (callFunctionOn) dies with it.
            expression = params.get("expression", "")
            tail = expression.rsplit(";", 1)[-1].strip()
            if tail.startswith("__btLogin.begin"):
                return {"result": {"objectId": "SESSION1"}}
            # Everything else evaluated in a fresh world is the probe.
            if "probe" in self.exception_details_on:
                return {
                    "result": {"type": "object"},
                    "exceptionDetails": {"text": SENTINEL_USER, "exception": {}},
                }
            return self.probe
        if method == "Runtime.callFunctionOn":
            if self.world_dead:
                raise CDPError(-32000, "Could not find object with given id")
            declaration = params.get("functionDeclaration", "")
            if "this.outcome" in declaration:
                if "outcome" in self.exception_details_on:
                    return {
                        "result": {"type": "object"},
                        "exceptionDetails": {"text": SENTINEL_USER, "exception": {}},
                    }
                return {"result": {"value": self.outcome}}
            if "this.fill" in declaration:
                self.log.append(("fill-arguments", params.get("arguments")))
                self.log.append(("fill-declaration", declaration))
                if isinstance(self.fill, Exception):
                    raise self.fill
                return {"result": {"value": self.fill}}
            if "this.submit" in declaration:
                if isinstance(self.submit, Exception):
                    raise self.submit
                return {"result": {"value": self.submit}}
            if "this.clear" in declaration:
                if isinstance(self.clear, Exception):
                    raise self.clear
                return {"result": {"value": self.clear}}
        raise CDPError(-32000, f"unexpected method {method}")


@pytest.fixture
def harness(monkeypatch, tmp_path, registry):
    """A login run over the fake CDP, with the relay stubbed and logged."""
    log: list[tuple[str, Any]] = []
    fake = FakeCDP(log)
    relay_reads: list[str] = []

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session(port, target_spec, target_by):
        log.append(("open-session", (port, target_spec, target_by)))
        try:
            yield fake, "S1"
        finally:
            log.append(("close-session", None))

    monkeypatch.setattr(login_module, "one_shot_page_session", fake_session)
    monkeypatch.setattr(credential_relay, "find_opchain", lambda: "/fake/bin/opchain")
    monkeypatch.setattr(credential_relay, "ensure_relay_supported", lambda *a, **k: None)

    def fake_read(*, field, **kwargs):
        relay_reads.append(field)
        log.append(("relay-read", field))
        value = SENTINEL_USER if field == "username" else SENTINEL_PASS
        return Secret(value.encode("utf-8"))

    monkeypatch.setattr(credential_relay, "read_reference", fake_read)

    config = policy_file(tmp_path)

    def run(**overrides):
        kwargs = {
            "instance": "web-01",
            "account": "example",
            "config": config,
            "timeout_ms": 2_000,
            "registry_path": registry,
        }
        kwargs.update(overrides)
        return login_module.login(**kwargs)

    import types

    return types.SimpleNamespace(
        fake=fake, log=log, relay_reads=relay_reads, run=run, config=config,
    )


class TestTheHappyPath:
    def test_full_order_and_success_document(self, harness):
        result = harness.run()
        assert result == {"account": "example", "status": "authenticated"}
        methods = [entry[0] for entry in harness.log]
        assert methods.index("relay-read", methods.index("Runtime.callFunctionOn")) > 0
        assert harness.relay_reads == ["username", "password"]
        # Validation completes before any relay read; fill comes after both.
        first_call = methods.index("Runtime.callFunctionOn")
        first_relay = methods.index("relay-read")
        assert first_call < first_relay
        # The pre-fill revalidation (second getFrameTree) precedes fill.
        frames = [i for i, m in enumerate(methods) if m == "Page.getFrameTree"]
        fill_at = methods.index("fill-arguments")
        assert len(frames) >= 2 and frames[1] < fill_at

    def test_success_probe_polls_in_a_fresh_world(self, harness):
        harness.run()
        evaluates = [p for m, p in harness.log if m == "Runtime.evaluate"]
        assert any(
            p and "globalThis.__btLogin" not in p.get("expression", "")
            for p in evaluates
        ), "the success poll must not re-evaluate the whole form helper"

    def test_success_probe_is_small_and_credential_free(self, harness):
        harness.run()
        probes = [
            p for m, p in harness.log
            if m == "Runtime.evaluate"
            and p and "globalThis.__btLogin" not in p.get("expression", "")
        ]
        assert probes
        for probe in probes:
            expression = probe["expression"]
            assert "SENTINEL" not in expression
            # Small: the whole helper is ~10 KB; the probe is a fragment.
            assert len(expression) < 1000, len(expression)
            assert "querySelector" in expression

    def test_values_travel_as_call_arguments_never_source(self, harness):
        harness.run()
        declaration = next(p for m, p in harness.log if m == "fill-declaration")
        arguments = next(p for m, p in harness.log if m == "fill-arguments")
        assert "SENTINEL" not in declaration
        assert arguments == [{"value": SENTINEL_USER}, {"value": SENTINEL_PASS}]

    def test_the_session_detaches_on_every_exit(self, harness):
        harness.fake.outcome = {"ok": False, "reason": "wrong-origin"}
        with pytest.raises(LifecycleError):
            harness.run()
        assert harness.log[-1][0] == "close-session", harness.log


class TestValidationRefusals:
    @pytest.mark.parametrize(
        "reason, message",
        [
            ("wrong-origin", "not at the configured origin"),
            ("hidden-tab", "hidden tab"),
            ("username-not-unique", "username selector"),
            ("password-wrong-type", "password input"),
            ("no-common-form", "same connected form"),
            ("not-post", "POST"),
            ("action-not-same-origin", "somewhere other than"),
            ("submit-wrong-form", "login form"),
            ("already-authenticated", "already holds"),
        ],
    )
    def test_each_refusal_is_fixed_english_without_credentials(self, harness, reason, message):
        harness.fake.outcome = {"ok": False, "reason": reason}
        with pytest.raises(LifecycleError, match=message):
            harness.run()
        assert harness.relay_reads == []

    def test_validation_failure_never_reaches_the_relay(self, harness):
        harness.fake.outcome = {"ok": False, "reason": "wrong-origin"}
        with pytest.raises(LifecycleError):
            harness.run()
        assert not any(m == "Runtime.callFunctionOn" and p and "this.fill" in str(p.get("functionDeclaration")) for m, p in harness.log)

    def test_unknown_reason_stays_fixed(self, harness):
        harness.fake.outcome = {"ok": False, "reason": "page-invented-reason"}
        with pytest.raises(LifecycleError, match="did not match the configured policy"):
            harness.run()


class TestFillTransportFailures:
    def test_missing_loader_cannot_confirm_that_unclearable_values_disappeared(self, harness):
        original_send = harness.fake.send
        dispatched = {"fill": False}

        async def broken_cleanup(method, params=None, session_id=None, timeout=None):
            declaration = (params or {}).get("functionDeclaration", "")
            if "this.fill" in declaration:
                dispatched["fill"] = True
                await original_send(method, params, session_id, timeout)
                raise CDPError(-32000, "SENTINEL-uncertain-fill")
            if "this.clear" in declaration:
                raise CDPError(-32000, "SENTINEL-uncertain-cleanup")
            if method == "Page.getFrameTree" and dispatched["fill"]:
                return {"frameTree": {"frame": {"id": "F1", "loaderId": None}}}
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = broken_cleanup
        with pytest.raises(login_module.LoginError, match="may still hold") as error:
            harness.run()
        assert error.value.fields_may_remain

    @pytest.mark.parametrize("response", [
        RuntimeError("SENTINEL-transport"), None, {"result": None}, {"result": {"value": []}},
    ])
    def test_unknown_failure_after_dispatched_fill_still_clears(self, harness, response):
        original_send = harness.fake.send

        async def broken_reply(method, params=None, session_id=None, timeout=None):
            result = await original_send(method, params, session_id, timeout)
            if params and "this.fill" in params.get("functionDeclaration", ""):
                if isinstance(response, Exception):
                    raise response
                return response
            return result

        harness.fake.send = broken_reply
        with pytest.raises(LifecycleError) as error:
            harness.run()
        assert "cleared" in str(error.value) and "not submitted" in str(error.value)
        assert "SENTINEL" not in str(error.value)
        assert any(m == "fill-arguments" for m, _ in harness.log)
        assert any("this.clear" in str(p) for _, p in harness.log)
        assert not any("this.submit" in str(p) for _, p in harness.log)


class TestNavigationAndReplacement:
    def test_navigation_before_fill_fails_with_nothing_inserted(self, harness, monkeypatch):
        def on_relay_read(field, **kwargs):
            harness.relay_reads.append(field)
            harness.fake.navigate()
            return Secret((SENTINEL_USER if field == "username" else SENTINEL_PASS).encode())

        monkeypatch.setattr(credential_relay, "read_reference", on_relay_read)
        with pytest.raises(LifecycleError, match="changed after validation"):
            harness.run()
        assert not any(m == "fill-arguments" for m, _ in harness.log)

    def test_dead_world_during_fill_reports_uncertainty_without_retry(self, harness):
        harness.fake.fill = CDPError(-32000, "Could not find object with given id")

        # The fill raise is mapped by _send; simulate by dead world instead.
        # (raise_on cannot target one callFunctionOn, so use world death.)
        original_send = harness.fake.send

        async def send_then_die(method, params=None, session_id=None, timeout=None):
            if params and "this.fill" in str(params.get("functionDeclaration", "")):
                harness.fake.navigate()
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = send_then_die  # type: ignore[method-assign]
        with pytest.raises(LifecycleError, match=r"changed while.*filled") as error:
            harness.run()
        assert "could not be confirmed" in str(error.value)
        assert "may still hold" not in str(error.value)
        assert not any(m == "fill-arguments" for m, _ in harness.log)
        assert not any(
            m == "Runtime.callFunctionOn" and "this.submit" in str(p)
            for m, p in harness.log
        )

    def test_uncertain_fill_on_live_document_says_fields_may_remain(self, harness):
        harness.fake.fill = CDPError(-32000, "boom")
        harness.fake.submit = OK_SUBMIT
        with pytest.raises(LifecycleError) as excinfo:
            harness.run()
        assert harness.fake.log  # the run reached the fake
        # _send converts the CDPError; the transaction then treats it as
        # uncertain on a live document (loader unchanged) and attempts clear.
        clears = [p for m, p in harness.log if m == "Runtime.callFunctionOn"
                  and p and "this.clear" in str(p.get("functionDeclaration", ""))]
        assert clears, "uncertain fill must attempt bounded cleanup"
        assert "may still hold" in str(excinfo.value) or "cleared" in str(excinfo.value)

    def test_navigation_at_submit_reports_unknown_outcome(self, harness):
        original_send = harness.fake.send

        async def send_then_die(method, params=None, session_id=None, timeout=None):
            if params and "this.submit" in str(params.get("functionDeclaration", "")):
                harness.fake.navigate()
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = send_then_die  # type: ignore[method-assign]
        with pytest.raises(LifecycleError, match="could not be confirmed") as error:
            harness.run()
        assert "not resubmitted" in str(error.value)
        assert "was not submitted" not in str(error.value)

    def test_clear_after_submit_navigation_is_clean_by_replacement(self, harness):
        original_send = harness.fake.send

        async def send_then_die(method, params=None, session_id=None, timeout=None):
            if params and "this.clear" in str(params.get("functionDeclaration", "")):
                harness.fake.navigate()
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = send_then_die  # type: ignore[method-assign]
        # The success probe runs in the replacement document's fresh world.
        harness.fake.world_dead = False
        result = harness.run()
        assert result["status"] == "authenticated"


class TestDeadlineCleanup:
    @pytest.mark.parametrize("cleanup_fails", [False, True])
    def test_expired_fill_still_attempts_cleanup(self, harness, cleanup_fails, monkeypatch):
        deadlines = []

        class ControlledDeadline(login_module._Deadline):
            def __init__(self, timeout_ms):
                super().__init__(timeout_ms)
                self.expired = False
                deadlines.append(self)

            def remaining(self):
                return 0.0 if self.expired else 1.0

        monkeypatch.setattr(login_module, "_Deadline", ControlledDeadline)
        original_send = harness.fake.send

        async def fail_after_deadline(method, params=None, session_id=None, timeout=None):
            declaration = (params or {}).get("functionDeclaration", "")
            if "this.fill" in declaration:
                deadlines[0].expired = True
                raise CDPError(-32000, SENTINEL_PASS)
            if cleanup_fails and "this.clear" in declaration:
                harness.log.append(("cleanup-attempt", None))
                raise CDPError(-32000, "cleanup failed")
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = fail_after_deadline
        with pytest.raises(LifecycleError) as error:
            harness.run(timeout_ms=10)
        attempted = any(
            method == "cleanup-attempt" or
            (method == "Runtime.callFunctionOn" and params and "this.clear" in params.get("functionDeclaration", ""))
            for method, params in harness.log
        )
        assert attempted, "an expired operation still owes sensitive-field cleanup"
        if cleanup_fails:
            assert "may still hold" in str(error.value)
        assert SENTINEL_PASS not in str(error.value)


class TestUnclearableFields:
    def test_unclearable_after_submit_reports_may_remain(self, harness):
        original_send = harness.fake.send

        async def failing_clear(method, params=None, session_id=None, timeout=None):
            if params and "this.clear" in str(params.get("functionDeclaration", "")):
                raise CDPError(-32000, "stalled")
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = failing_clear  # type: ignore[method-assign]
        with pytest.raises(LifecycleError, match="may still hold"):
            harness.run()

    def test_js_clear_unverified_reports_may_remain(self, harness):
        harness.fake.clear = {"ok": True, "cleared": False}
        with pytest.raises(LifecycleError, match="may still hold"):
            harness.run()



class TestSuccessWait:
    def test_timeout_when_success_never_matches(self, harness):
        harness.fake.probe = UNAUTHENTICATED_PROBE
        with pytest.raises(LifecycleError, match="success condition was not met"):
            harness.run(timeout_ms=700)

    def test_wrong_origin_after_submission_does_not_authenticate(self, harness):
        harness.fake.probe = {"result": {"value": {"authenticated": True, "originOk": False}}}
        with pytest.raises(LifecycleError, match="success condition was not met"):
            harness.run(timeout_ms=700)


SECRET_TABS = [
    {"targetId": "A" * 16, "url": "https://SENTINEL-tab.example/x", "title": "SENTINEL-title"},
    {"targetId": "B" * 16, "url": "https://SENTINEL-tab2.example/y", "title": "t2"},
]


class TestSessionEntryDiagnostics:
    """Failures to choose a tab or connect must not print browser text.

    The underlying errors carry every tab's URL and title, or arbitrary
    protocol text - page-provided data the fixed-diagnostic contract keeps
    out of both output channels.
    """

    def _entry_error(self, monkeypatch, error: BaseException) -> None:
        import contextlib

        @contextlib.asynccontextmanager
        async def failing(port, target_spec, target_by):
            raise error
            yield None, None

        monkeypatch.setattr(login_module, "one_shot_page_session", failing)

    @pytest.mark.parametrize(
        "error, message",
        [
            (
                AmbiguousTargetError(targets=SECRET_TABS),
                "several tabs are open; choose one with --target",
            ),
            (
                TargetNotFoundError(
                    message="No target matching ID prefix 'SENTINEL-target'",
                    targets=SECRET_TABS,
                ),
                "matches no open tab",
            ),
            (NoPageError(), "no open page"),
            (
                ConnectionError("websocket to ws://SENTINEL-host/devtools/browser/x failed"),
                "could not connect",
            ),
            (
                CDPError(-32000, "Target.getTargets exploded with SENTINEL-browser-text"),
                "could not be opened",
            ),
            (RuntimeError("SENTINEL-transport-response"), "session failed"),
        ],
    )
    def test_entry_failures_are_fixed_english_without_browser_text(
        self, harness, monkeypatch, error, message, capsys
    ):
        self._entry_error(monkeypatch, error)
        with pytest.raises(LifecycleError, match=message) as excinfo:
            harness.run()
        assert "SENTINEL" not in str(excinfo.value)
        captured = capsys.readouterr()
        assert "SENTINEL" not in captured.out + captured.err


class TestTeardownCannotHideTheBody:
    def test_dialog_cleanup_cancellation_keeps_the_sensitive_warning(self, harness, monkeypatch):
        import asyncio

        original_close = login_module.DialogGuard.close

        async def cancelled_close(guard):
            await original_close(guard)
            raise asyncio.CancelledError

        harness.fake.clear = {"ok": True, "cleared": False}
        monkeypatch.setattr(login_module.DialogGuard, "close", cancelled_close)
        with pytest.raises(login_module.LoginError, match="may still hold") as error:
            harness.run()
        assert error.value.fields_may_remain

    @pytest.mark.parametrize("body_failure", ["none", "ordinary", "sensitive"])
    def test_cancelled_detach_preserves_only_sensitive_warning(
        self, harness, monkeypatch, body_failure
    ):
        import asyncio
        import contextlib

        if body_failure == "ordinary":
            harness.fake.outcome = {"ok": False, "reason": "wrong-origin"}
        elif body_failure == "sensitive":
            harness.fake.clear = {"ok": True, "cleared": False}

        @contextlib.asynccontextmanager
        async def cancelled_detach(*args):
            try:
                yield harness.fake, "S1"
            finally:
                raise asyncio.CancelledError

        monkeypatch.setattr(login_module, "one_shot_page_session", cancelled_detach)
        if body_failure == "sensitive":
            with pytest.raises(login_module.LoginError, match="may still hold") as error:
                harness.run()
            assert error.value.fields_may_remain
        else:
            with pytest.raises(asyncio.CancelledError):
                harness.run()

    def test_stalled_detach_keeps_the_may_remain_warning(self, harness, monkeypatch):
        """A hung context-manager exit must not replace the body's diagnosis.

        The fields-may-remain warning is the security-relevant fact; a
        teardown that stalls past the cleanup grace may only report a
        timeout when there was nothing better to report.
        """
        import asyncio
        import contextlib

        @contextlib.asynccontextmanager
        async def stalling_session(port, target_spec, target_by):
            try:
                yield harness.fake, "S1"
            finally:
                await asyncio.Event().wait()  # the detach never completes

        monkeypatch.setattr(login_module, "one_shot_page_session", stalling_session)
        monkeypatch.setattr(login_module, "_CLEANUP_GRACE_SECONDS", 0.2)
        harness.fake.fill = CDPError(-32000, "fill stalled")
        harness.fake.clear = {"ok": True, "cleared": False}  # unclearable fill
        with pytest.raises(LifecycleError) as excinfo:
            harness.run()
        assert "may still hold" in str(excinfo.value)
        assert "timeout" not in str(excinfo.value).lower()


class TestSuccessProbeContextLoss:
    @pytest.mark.parametrize("error", [
        CDPError(-32601, "SENTINEL-unsupported-method"),
        CDPError(-32000, "SENTINEL-permanent-protocol-failure"),
    ])
    def test_permanent_probe_errors_are_not_retried(self, harness, error):
        original_send = harness.fake.send
        probes = []

        async def failing_probe(method, params=None, session_id=None, timeout=None):
            expression = (params or {}).get("expression", "")
            if method == "Runtime.evaluate" and "globalThis.__btLogin" not in expression:
                probes.append(method)
                raise error
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = failing_probe
        with pytest.raises(LifecycleError, match="session failed") as failure:
            harness.run(timeout_ms=1000)
        assert probes == ["Runtime.evaluate"]
        assert "SENTINEL" not in str(failure.value)

    def test_a_destroyed_probe_context_after_navigation_polls_on(self, harness):
        """The POST response can commit between the probe's two CDP calls.

        The evaluate targets a context the navigation just destroyed. That
        is a transient not-yet: polling must continue within the same
        deadline instead of failing a login that succeeded.
        """
        original_send = harness.fake.send
        probes = {"count": 0}

        def is_probe(params) -> bool:
            return "globalThis.__btLogin" not in (params or {}).get("expression", "")

        async def losing_context(method, params=None, session_id=None, timeout=None):
            if method == "Runtime.evaluate" and is_probe(params):
                probes["count"] += 1
                if probes["count"] == 1:
                    harness.fake.navigate()  # the response commits right here
                    raise CDPError(-32000, "Execution context was destroyed")
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = losing_context  # type: ignore[method-assign]
        result = harness.run()
        assert result["status"] == "authenticated"
        assert probes["count"] >= 2, "polling must continue after the context loss"


class TestDialogs:
    def test_dialogs_are_dismissed_fixed_without_their_text(self, harness, capsys):
        """A dialog during the fill is dismissed; its text never lands.

        The event's message and default prompt can quote credential
        values; the guard answers accept:false, stores nothing, and joins
        its tasks on exit.
        """
        original_send = harness.fake.send

        async def dialog_then_forward(method, params=None, session_id=None, timeout=None):
            if params and "this.fill" in str(params.get("functionDeclaration", "")):
                harness.fake.emit(
                    "Page.javascriptDialogOpening",
                    {"type": "prompt", "message": SENTINEL_USER, "defaultPrompt": SENTINEL_PASS},
                )
            return await original_send(method, params, session_id, timeout)

        harness.fake.send = dialog_then_forward  # type: ignore[method-assign]
        result = harness.run()
        assert result["status"] == "authenticated"
        dismissals = [p for m, p in harness.log if m == "Page.handleJavaScriptDialog"]
        assert dismissals == [{"accept": False}]
        for method, params in harness.log:
            if method != "Page.handleJavaScriptDialog":
                continue
            assert "SENTINEL" not in str(params)
        captured = capsys.readouterr()
        assert "SENTINEL" not in captured.out + captured.err
        assert not harness.fake._handlers.get("Page.javascriptDialogOpening")


class TestUntrustedText:
    def test_js_exception_details_never_surface(self, harness, capsys):
        harness.fake.exception_details_on.add("outcome")
        with pytest.raises(LifecycleError) as excinfo:
            harness.run()
        assert SENTINEL_USER not in str(excinfo.value)
        captured = capsys.readouterr()
        assert SENTINEL_USER not in captured.out + captured.err

    def test_probe_exception_details_map_to_fixed(self, harness):
        harness.fake.exception_details_on.add("probe")
        with pytest.raises(LifecycleError, match="browser session failed"):
            harness.run()

    def test_cdp_errors_map_to_fixed_phase_messages(self, harness):
        harness.fake.raise_on["Page.getFrameTree"] = CDPError(-32000, "secret-looking-text")
        with pytest.raises(LifecycleError, match="browser session failed during login"):
            harness.run()

    def test_debug_logs_do_not_include_cdp_exception_text(self, harness, caplog):
        harness.fake.raise_on["Page.getFrameTree"] = CDPError(-32000, SENTINEL_PASS)
        with caplog.at_level(logging.DEBUG), pytest.raises(LifecycleError):
            harness.run()
        assert SENTINEL_PASS not in caplog.text


class TestRelayFailuresSurfaceFixed:
    def test_relay_failure_after_validation_is_operational(self, harness, monkeypatch):
        def failing_read(**kwargs):
            raise RelayError("the credential relay refused or failed to read the value (relay: token)")

        monkeypatch.setattr(credential_relay, "read_reference", failing_read)
        with pytest.raises(LifecycleError, match="relay"):
            harness.run()

    def test_relay_unsupported_fails_before_the_browser_opens(self, monkeypatch, tmp_path, registry):
        monkeypatch.setattr(credential_relay, "find_opchain", lambda: "/fake/bin/opchain")

        def unsupported(*args, **kwargs):
            raise credential_relay.RelayUnsupportedError(
                "the installed opchain does not support the --value-fd private relay."
            )

        monkeypatch.setattr(credential_relay, "ensure_relay_supported", unsupported)
        opened = []
        import contextlib

        @contextlib.asynccontextmanager
        async def trapped_session(port, target_spec, target_by):
            opened.append(True)
            try:
                yield FakeCDP([]), "S1"
            finally:
                pass

        monkeypatch.setattr(login_module, "one_shot_page_session", trapped_session)
        with pytest.raises(LifecycleError, match="--value-fd"):
            login_module.login(
                instance="web-01", account="example",
                config=policy_file(tmp_path), registry_path=registry,
            )
        assert opened == []


class TestQuietWebsockets:
    def test_wire_logging_stays_quiet_through_session_teardown(
        self, harness, monkeypatch, caplog
    ):
        import contextlib

        @contextlib.asynccontextmanager
        async def wire_during_detach(*args):
            try:
                yield harness.fake, "S1"
            finally:
                logging.getLogger("websockets.client").debug("SENTINEL-late-dialog")

        monkeypatch.setattr(login_module, "one_shot_page_session", wire_during_detach)
        with caplog.at_level(logging.DEBUG):
            harness.run()
        assert "SENTINEL" not in caplog.text

    def test_websockets_loggers_are_forced_above_debug(self, harness):
        protocol_logger = logging.getLogger("websockets.protocol")
        saved = protocol_logger.level
        protocol_logger.setLevel(logging.DEBUG)
        try:
            seen = []
            original_send = harness.fake.send

            async def recording(method, params=None, session_id=None, timeout=None):
                seen.append((method, logging.getLogger("websockets").level,
                             logging.getLogger("websockets.protocol").level))
                return await original_send(method, params, session_id, timeout)

            harness.fake.send = recording  # type: ignore[method-assign]
            harness.run()
            assert seen
            for _, root_level, protocol_level in seen:
                assert root_level >= logging.WARNING
                assert protocol_level >= logging.WARNING
            assert logging.getLogger("websockets.protocol").level == logging.DEBUG
        finally:
            protocol_logger.setLevel(saved)
