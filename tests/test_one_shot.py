"""Tests for the one-shot CDP session seam (extracted from passthrough/events/
list_verbs/curated).

Exercises the seam exactly once against a fake ``core.cdp_client.CDPClient``
(the same double pattern ``test_passthrough.py``/``test_events.py`` use for
the full-path tests): the connect/getTargets/resolve/attach/detach protocol,
the no-page and target-resolution failures, and detach-always-runs. The four
verb modules no longer assert this protocol directly -- their own tests now
only monkeypatch or fake ``one_shot_page_session`` and check their body logic
plus verb-specific errors.

``cli_cdp_errors`` is exercised directly against a set of dummy functions
raising each shared error, and against ``UsageError``/``WaitTimeout`` to prove
those pass through untouched.
"""

from __future__ import annotations

import asyncio

import pytest

from browser_tools.core.attach import AmbiguousTargetError, TargetNotFoundError
from browser_tools.core.errors import CDPError, NoPageError
from browser_tools.core.registry import InstanceNotFoundError
from browser_tools.lifecycle import LifecycleError
from browser_tools.one_shot import cli_cdp_errors, one_shot_page_session

# ---------------------------------------------------------------------------
# Fake CDP client -- shared connect/getTargets/attach/detach double
# ---------------------------------------------------------------------------


def make_fake_cdp_client_cls(targets=None):
    """Build a fake ``core.cdp_client.CDPClient`` and its call log.

    Auto-answers ``Target.getTargets``/``attachToTarget``/``detachFromTarget``
    -- the seam's own plumbing -- and records every call so the protocol
    order and params are provable.
    """
    targets = (
        targets
        if targets is not None
        else [{"targetId": "T1", "type": "page", "url": "https://example.com"}]
    )
    calls: list[tuple[str, dict | None, str | None]] = []

    class FakeCDPClient:
        def __init__(self, ws_url):
            self.ws_url = ws_url
            self.detached = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, method, params=None, session_id=None):
            calls.append((method, params, session_id))
            if method == "Target.getTargets":
                return {"targetInfos": targets}
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            if method == "Target.detachFromTarget":
                self.detached = True
                return {}
            return {}

    return FakeCDPClient, calls


@pytest.fixture
def fake_transport(monkeypatch):
    """Install a fake CDPClient/get_ws_url pair over the seam and return its call log."""

    def _install(targets=None):
        fake_cls, calls = make_fake_cdp_client_cls(targets=targets)
        monkeypatch.setattr("browser_tools.one_shot.CDPClient", fake_cls)
        monkeypatch.setattr(
            "browser_tools.one_shot.get_ws_url", lambda **kw: "ws://fake/browser"
        )
        return calls, fake_cls

    return _install


# ---------------------------------------------------------------------------
# one_shot_page_session: connect/resolve/attach/detach protocol
# ---------------------------------------------------------------------------


class TestOneShotPageSession:
    def test_happy_path_connects_resolves_attaches_and_detaches(self, fake_transport):
        calls, _ = fake_transport()

        async def _run():
            async with one_shot_page_session(9222, None, None) as (cdp, session_id):
                assert session_id == "S1"
                return await cdp.send(method="Page.enable", session_id=session_id)

        asyncio.run(_run())

        methods = [c[0] for c in calls]
        assert methods == [
            "Target.getTargets",
            "Target.attachToTarget",
            "Page.enable",
            "Target.detachFromTarget",
        ]
        attach_call = calls[1]
        assert attach_call[1] == {"targetId": "T1", "flatten": True}
        detach_call = calls[3]
        assert detach_call[1] == {"sessionId": "S1"}

    def test_no_page_targets_raises_no_page_error(self, fake_transport):
        fake_transport(targets=[])

        async def _run():
            async with one_shot_page_session(9222, None, None) as _:
                pass  # pragma: no cover

        with pytest.raises(NoPageError):
            asyncio.run(_run())

    def test_ambiguous_target_propagates(self, fake_transport):
        fake_transport(
            targets=[
                {"targetId": "AAAA1111", "type": "page", "url": "https://a.example"},
                {"targetId": "BBBB2222", "type": "page", "url": "https://b.example"},
            ]
        )

        async def _run():
            async with one_shot_page_session(9222, None, None) as _:
                pass  # pragma: no cover

        with pytest.raises(AmbiguousTargetError):
            asyncio.run(_run())

    def test_not_found_target_propagates(self, fake_transport):
        fake_transport(
            targets=[{"targetId": "AAAA1111", "type": "page", "url": "https://a.example"}]
        )

        async def _run():
            async with one_shot_page_session(9222, "ZZZZ9999", "id") as _:
                pass  # pragma: no cover

        with pytest.raises(TargetNotFoundError):
            asyncio.run(_run())

    def test_detach_runs_even_when_body_raises(self, fake_transport):
        calls, _fake_cls = fake_transport()

        class Boom(Exception):
            pass

        async def _run():
            async with one_shot_page_session(9222, None, None) as _:
                raise Boom("body failed")

        with pytest.raises(Boom):
            asyncio.run(_run())

        methods = [c[0] for c in calls]
        assert methods == [
            "Target.getTargets",
            "Target.attachToTarget",
            "Target.detachFromTarget",
        ]

    def test_detach_failure_is_suppressed(self, monkeypatch):
        """A detach that itself raises must not mask the body's result."""

        class FakeCDPClient:
            def __init__(self, ws_url):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def send(self, method, params=None, session_id=None):
                if method == "Target.getTargets":
                    return {
                        "targetInfos": [
                            {"targetId": "T1", "type": "page", "url": "https://example.com"}
                        ]
                    }
                if method == "Target.attachToTarget":
                    return {"sessionId": "S1"}
                if method == "Target.detachFromTarget":
                    raise ConnectionError("detach failed")
                return {}

        monkeypatch.setattr("browser_tools.one_shot.CDPClient", FakeCDPClient)
        monkeypatch.setattr(
            "browser_tools.one_shot.get_ws_url", lambda **kw: "ws://fake/browser"
        )

        async def _run():
            async with one_shot_page_session(9222, None, None) as (_cdp, _session_id):
                return "ok"

        assert asyncio.run(_run()) == "ok"


# ---------------------------------------------------------------------------
# cli_cdp_errors: shared error mapping, verb-specific errors pass through
# ---------------------------------------------------------------------------


class _WaitTimeoutLike(LifecycleError):
    """Stand-in for ``events.WaitTimeout``: a LifecycleError subclass that
    must NOT be re-wrapped by the decorator (it is not one of the mapped
    shared error types)."""


class _UsageErrorLike(Exception):
    """Stand-in for ``passthrough.UsageError``: must pass through untouched."""


class TestCliCdpErrors:
    def test_ambiguous_target_error_maps_to_lifecycle_error(self):
        @cli_cdp_errors
        def fn():
            raise AmbiguousTargetError(targets=[])

        with pytest.raises(LifecycleError):
            fn()

    def test_target_not_found_error_maps_to_lifecycle_error(self):
        @cli_cdp_errors
        def fn():
            raise TargetNotFoundError(message="nope", targets=[])

        with pytest.raises(LifecycleError):
            fn()

    def test_no_page_error_maps_to_lifecycle_error(self):
        @cli_cdp_errors
        def fn():
            raise NoPageError()

        with pytest.raises(LifecycleError):
            fn()

    def test_instance_not_found_error_maps_to_lifecycle_error(self):
        @cli_cdp_errors
        def fn():
            raise InstanceNotFoundError(name="ghost", available=[])

        with pytest.raises(LifecycleError):
            fn()

    def test_cdp_error_maps_to_lifecycle_error_with_code_and_message(self):
        @cli_cdp_errors
        def fn():
            raise CDPError(code=-32000, message="Node not found")

        with pytest.raises(LifecycleError) as exc:
            fn()
        assert "-32000" in str(exc.value)
        assert "Node not found" in str(exc.value)

    def test_connection_error_maps_to_lifecycle_error(self):
        @cli_cdp_errors
        def fn():
            raise ConnectionError("no browser")

        with pytest.raises(LifecycleError):
            fn()

    def test_usage_error_passes_through_untouched(self):
        @cli_cdp_errors
        def fn():
            raise _UsageErrorLike("bad args")

        with pytest.raises(_UsageErrorLike):
            fn()

    def test_wait_timeout_passes_through_untouched(self):
        @cli_cdp_errors
        def fn():
            raise _WaitTimeoutLike("deadline")

        with pytest.raises(_WaitTimeoutLike):
            fn()

    def test_successful_call_returns_value(self):
        @cli_cdp_errors
        def fn(x):
            return x * 2

        assert fn(21) == 42
