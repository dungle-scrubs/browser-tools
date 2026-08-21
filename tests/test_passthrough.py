"""Tests for raw CDP passthrough and live-schema help (RFC-01 #37).

These exercise instance-vs-method disambiguation, the passthrough dispatch
(against a fake ``core.cdp_client.CDPClient`` so no real browser or websocket
is involved), live-schema help (against a faked ``core.protocol`` schema
fetch), and the CLI-front exit-code mapping. All against an isolated
registry file, mirroring tests/test_cli_lifecycle.py.
"""

from __future__ import annotations

import json

import pytest

from browser_tools import cli, lifecycle, passthrough
from browser_tools.core import protocol as core_protocol
from browser_tools.core import registry as core_registry
from browser_tools.core.errors import CDPError
from browser_tools.lifecycle import LifecycleError
from browser_tools.passthrough import UsageError


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


def _seed(registry_path: str, entries: dict) -> None:
    core_registry._save_registry(entries, registry_path)


def _entry(port: int = 9222, **extra) -> dict:
    # A PID that cannot be alive/ours -> the vendored liveness ladder reads
    # dead. Fine for send()/lookup(), which do not gate on liveness; tests
    # that need a *live* instance patch lifecycle.instance_is_live instead.
    base = {
        "port": port,
        "pid": 2_000_000_000,
        "browser_version": "Chrome/1",
        "user_data_dir": "",
        "launched": "2026-01-01T00:00:00+00:00",
        "pid_start": None,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Fake CDP client -- shared client `send` path double
# ---------------------------------------------------------------------------


def make_fake_cdp_client_cls(responder=None, targets=None):
    """Build a fake ``core.cdp_client.CDPClient`` and its call log.

    Auto-answers ``Target.getTargets``/``attachToTarget``/``detachFromTarget``
    (the passthrough plumbing every call needs) and defers the actual
    ``Domain.method`` call to ``responder(method, params)``. Recording every
    call here is what proves a passthrough call reaches the shared client
    `send` path with its parsed params, not just that our own wrapper ran.
    """
    targets = targets if targets is not None else [
        {"targetId": "T1", "type": "page", "url": "https://example.com"}
    ]
    calls: list[tuple[str, dict | None, str | None]] = []

    class FakeCDPClient:
        def __init__(self, ws_url):
            self.ws_url = ws_url

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
                return {}
            if responder is not None:
                return responder(method, params)
            return {"echoed_method": method, "echoed_params": params}

    return FakeCDPClient, calls


@pytest.fixture
def fake_transport(monkeypatch):
    """Install a fake CDPClient/get_ws_url pair and return its call log."""

    def _install(responder=None, targets=None):
        fake_cls, calls = make_fake_cdp_client_cls(responder=responder, targets=targets)
        monkeypatch.setattr(passthrough, "CDPClient", fake_cls)
        monkeypatch.setattr(passthrough, "get_ws_url", lambda **kw: "ws://fake/browser")
        return calls

    return _install


# ---------------------------------------------------------------------------
# Instance-vs-method disambiguation
# ---------------------------------------------------------------------------


class TestDisambiguation:
    def test_known_instance_is_passthrough_head(self, registry_path):
        _seed(registry_path, {"site-01": _entry()})
        assert passthrough.is_passthrough_head("site-01", registry_path=registry_path)

    def test_domain_method_shape_is_passthrough_head(self, registry_path):
        _seed(registry_path, {})
        assert passthrough.is_passthrough_head("Page.navigate", registry_path=registry_path)

    def test_unknown_bare_word_is_not_passthrough_head(self, registry_path):
        _seed(registry_path, {})
        assert not passthrough.is_passthrough_head("bogus", registry_path=registry_path)

    def test_instance_prefixed_form_resolves(self, registry_path):
        _seed(registry_path, {"site-01": _entry()})
        instance, method, params_json = passthrough.resolve_passthrough_args(
            ["site-01", "Page.navigate", '{"url": "https://x"}'],
            registry_path=registry_path,
        )
        assert instance == "site-01"
        assert method == "Page.navigate"
        assert params_json == '{"url": "https://x"}'

    def test_bare_domain_method_form_resolves_instance_none(self, registry_path):
        _seed(registry_path, {})
        instance, method, params_json = passthrough.resolve_passthrough_args(
            ["Page.navigate", '{"url": "https://x"}'],
            registry_path=registry_path,
        )
        assert instance is None
        assert method == "Page.navigate"
        assert params_json == '{"url": "https://x"}'

    def test_instance_name_that_looks_like_a_method_still_resolves_as_instance(
        self, registry_path
    ):
        # A registered instance name always wins the disambiguation, even if
        # it happens to contain a dot (RFC-01: registry lookup first).
        _seed(registry_path, {"a.b": _entry()})
        instance, method, _params_json = passthrough.resolve_passthrough_args(
            ["a.b", "Page.navigate"], registry_path=registry_path
        )
        assert instance == "a.b"
        assert method == "Page.navigate"

    def test_instance_without_method_is_usage_error(self, registry_path):
        _seed(registry_path, {"site-01": _entry()})
        with pytest.raises(UsageError):
            passthrough.resolve_passthrough_args(["site-01"], registry_path=registry_path)

    def test_neither_instance_nor_method_is_usage_error(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(UsageError):
            passthrough.resolve_passthrough_args(["bogus"], registry_path=registry_path)

    def test_empty_args_is_usage_error(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(UsageError):
            passthrough.resolve_passthrough_args([], registry_path=registry_path)

    def test_help_args_empty_means_no_instance_no_query(self, registry_path):
        _seed(registry_path, {})
        assert passthrough.resolve_help_args([], registry_path=registry_path) == (None, None)

    def test_help_args_known_instance_only(self, registry_path):
        _seed(registry_path, {"site-01": _entry()})
        assert passthrough.resolve_help_args(["site-01"], registry_path=registry_path) == (
            "site-01",
            None,
        )

    def test_help_args_known_instance_plus_query(self, registry_path):
        _seed(registry_path, {"site-01": _entry()})
        assert passthrough.resolve_help_args(
            ["site-01", "Page.navigate"], registry_path=registry_path
        ) == ("site-01", "Page.navigate")

    def test_help_args_bare_query_no_instance(self, registry_path):
        _seed(registry_path, {})
        assert passthrough.resolve_help_args(["Page"], registry_path=registry_path) == (
            None,
            "Page",
        )

    def test_help_args_unknown_token_followed_by_query_is_usage_error(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(UsageError):
            passthrough.resolve_help_args(["bogus", "Page.navigate"], registry_path=registry_path)

    def test_help_args_too_many_is_usage_error(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(UsageError):
            passthrough.resolve_help_args(["a", "b", "c"], registry_path=registry_path)


class TestTargetFlagExtraction:
    def test_no_flags(self):
        remaining, target, url = passthrough.extract_target_flags(["Page.navigate", "{}"])
        assert remaining == ["Page.navigate", "{}"]
        assert target is None
        assert url is None

    def test_target_extracted_from_anywhere(self):
        remaining, target, url = passthrough.extract_target_flags(
            ["--target", "abc123", "Page.navigate", "{}"]
        )
        assert remaining == ["Page.navigate", "{}"]
        assert target == "abc123"
        assert url is None

    def test_url_extracted(self):
        remaining, target, url = passthrough.extract_target_flags(
            ["Page.navigate", "--url", "example.com"]
        )
        assert remaining == ["Page.navigate"]
        assert url == "example.com"
        assert target is None

    def test_both_target_and_url_is_usage_error(self):
        with pytest.raises(UsageError):
            passthrough.extract_target_flags(
                ["Page.navigate", "--target", "a", "--url", "b"]
            )


# ---------------------------------------------------------------------------
# Passthrough dispatch: reaches the shared client `send` path
# ---------------------------------------------------------------------------


class TestSend:
    def test_reaches_client_send_path_with_parsed_params(
        self, registry_path, fake_transport
    ):
        _seed(registry_path, {"site-01": _entry()})
        calls = fake_transport()

        result = passthrough.send(
            instance="site-01",
            method="Page.navigate",
            params_json='{"url": "https://example.com"}',
            registry_path=registry_path,
        )

        assert result == {
            "echoed_method": "Page.navigate",
            "echoed_params": {"url": "https://example.com"},
        }
        # The parsed params reached CDPClient.send, over an isolated session,
        # via the same call path Target.attachToTarget just opened.
        methods_sent = [c[0] for c in calls]
        assert methods_sent == [
            "Target.getTargets",
            "Target.attachToTarget",
            "Page.navigate",
            "Target.detachFromTarget",
        ]
        nav_call = calls[2]
        assert nav_call == ("Page.navigate", {"url": "https://example.com"}, "S1")

    def test_no_params_sends_none(self, registry_path, fake_transport):
        _seed(registry_path, {"site-01": _entry()})
        calls = fake_transport()
        passthrough.send(
            instance="site-01", method="Page.enable", params_json=None, registry_path=registry_path
        )
        nav_call = calls[2]
        assert nav_call == ("Page.enable", None, "S1")

    def test_instance_omitted_resolves_single_instance(self, registry_path, fake_transport):
        _seed(registry_path, {"only-01": _entry()})
        fake_transport()
        result = passthrough.send(
            instance=None, method="Page.enable", params_json=None, registry_path=registry_path
        )
        assert result == {"echoed_method": "Page.enable", "echoed_params": None}

    def test_instance_omitted_multiple_running_is_lifecycle_error(
        self, registry_path, fake_transport
    ):
        _seed(
            registry_path,
            {"a-01": _entry(port=9222), "b-01": _entry(port=9223)},
        )
        fake_transport()
        with pytest.raises(LifecycleError):
            passthrough.send(
                instance=None, method="Page.enable", params_json=None, registry_path=registry_path
            )

    def test_unknown_instance_is_lifecycle_error(self, registry_path, fake_transport):
        _seed(registry_path, {})
        fake_transport()
        with pytest.raises(LifecycleError):
            passthrough.send(
                instance="ghost", method="Page.enable", params_json=None, registry_path=registry_path
            )

    def test_malformed_json_params_is_usage_error(self, registry_path, fake_transport):
        _seed(registry_path, {"site-01": _entry()})
        fake_transport()
        with pytest.raises(UsageError):
            passthrough.send(
                instance="site-01",
                method="Page.navigate",
                params_json="{not json",
                registry_path=registry_path,
            )

    def test_non_object_json_params_is_usage_error(self, registry_path, fake_transport):
        _seed(registry_path, {"site-01": _entry()})
        fake_transport()
        with pytest.raises(UsageError):
            passthrough.send(
                instance="site-01",
                method="Page.navigate",
                params_json="[1, 2, 3]",
                registry_path=registry_path,
            )

    def test_cdp_error_is_lifecycle_error(self, registry_path, fake_transport):
        _seed(registry_path, {"site-01": _entry()})

        def boom(method, params):
            raise CDPError(code=-32000, message="Node not found")

        fake_transport(responder=boom)

        with pytest.raises(LifecycleError) as exc:
            passthrough.send(
                instance="site-01",
                method="DOM.querySelector",
                params_json=None,
                registry_path=registry_path,
            )
        assert "-32000" in str(exc.value)
        assert "Node not found" in str(exc.value)

    def test_target_flag_selects_among_multiple_targets(self, registry_path, fake_transport):
        _seed(registry_path, {"site-01": _entry()})
        targets = [
            {"targetId": "AAAA1111", "type": "page", "url": "https://a.example"},
            {"targetId": "BBBB2222", "type": "page", "url": "https://b.example"},
        ]
        calls = fake_transport(targets=targets)

        passthrough.send(
            instance="site-01",
            method="Page.enable",
            params_json=None,
            target="BBBB2222",
            registry_path=registry_path,
        )
        attach_call = calls[1]
        assert attach_call[0] == "Target.attachToTarget"
        assert attach_call[1] == {"targetId": "BBBB2222", "flatten": True}

    def test_ambiguous_target_without_flag_is_lifecycle_error(
        self, registry_path, fake_transport
    ):
        _seed(registry_path, {"site-01": _entry()})
        targets = [
            {"targetId": "AAAA1111", "type": "page", "url": "https://a.example"},
            {"targetId": "BBBB2222", "type": "page", "url": "https://b.example"},
        ]
        fake_transport(targets=targets)
        with pytest.raises(LifecycleError):
            passthrough.send(
                instance="site-01",
                method="Page.enable",
                params_json=None,
                registry_path=registry_path,
            )


# ---------------------------------------------------------------------------
# Live-schema help
# ---------------------------------------------------------------------------


_FAKE_SCHEMA = {
    "domains": [
        {
            "domain": "Page",
            "description": "Page domain",
            "commands": [{"name": "navigate", "description": "Navigate."}],
            "events": [],
        }
    ]
}


class TestHelp:
    def test_no_instances_prints_static_usage(self, registry_path, capsys):
        _seed(registry_path, {})
        passthrough.run_help(None, None, registry_path=registry_path)
        assert capsys.readouterr().out == passthrough.STATIC_HELP

    def test_multiple_live_instances_and_no_explicit_instance_prints_static_usage(
        self, registry_path, capsys, monkeypatch
    ):
        _seed(
            registry_path,
            {"a-01": _entry(port=9222), "b-01": _entry(port=9223)},
        )
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda inst: True)
        passthrough.run_help(None, None, registry_path=registry_path)
        assert capsys.readouterr().out == passthrough.STATIC_HELP

    def test_explicit_running_instance_prints_live_schema(
        self, registry_path, capsys, monkeypatch
    ):
        _seed(registry_path, {"site-01": _entry(port=9222)})
        monkeypatch.setattr(
            core_protocol, "fetch_protocol_schema", lambda port: _FAKE_SCHEMA
        )
        passthrough.run_help("site-01", None, registry_path=registry_path)
        out = capsys.readouterr().out
        assert "Page" in out

    def test_single_live_instance_auto_resolves_without_explicit_name(
        self, registry_path, capsys, monkeypatch
    ):
        _seed(registry_path, {"site-01": _entry(port=9222)})
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda inst: True)
        monkeypatch.setattr(
            core_protocol, "fetch_protocol_schema", lambda port: _FAKE_SCHEMA
        )
        passthrough.run_help(None, None, registry_path=registry_path)
        out = capsys.readouterr().out
        assert "Page" in out

    def test_unreachable_instance_falls_back_to_static_usage(
        self, registry_path, capsys, monkeypatch
    ):
        _seed(registry_path, {"site-01": _entry(port=9222)})

        def boom(port):
            raise ConnectionError("no browser")

        monkeypatch.setattr(core_protocol, "fetch_protocol_schema", boom)
        passthrough.run_help("site-01", None, registry_path=registry_path)
        assert capsys.readouterr().out == passthrough.STATIC_HELP

    def test_unknown_instance_is_lifecycle_error(self, registry_path):
        _seed(registry_path, {})
        with pytest.raises(LifecycleError):
            passthrough.run_help("ghost", None, registry_path=registry_path)

    def test_unknown_query_against_live_schema_is_usage_error(
        self, registry_path, monkeypatch
    ):
        _seed(registry_path, {"site-01": _entry(port=9222)})
        monkeypatch.setattr(
            core_protocol, "fetch_protocol_schema", lambda port: _FAKE_SCHEMA
        )
        with pytest.raises(UsageError):
            passthrough.run_help("site-01", "NoSuchDomain", registry_path=registry_path)


# ---------------------------------------------------------------------------
# CLI front: end-to-end dispatch and exit codes
# ---------------------------------------------------------------------------


class TestCliFront:
    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch, tmp_path):
        self._registry_path = str(tmp_path / "registry.json")
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, self._registry_path)

    def test_bare_domain_method_dispatches_and_prints_json(self, capsys, fake_transport):
        _seed(self._registry_path, {"only-01": _entry()})
        fake_transport()
        rc = cli.main(["Page.navigate", '{"url": "https://x"}'])
        assert rc == cli.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "echoed_method": "Page.navigate",
            "echoed_params": {"url": "https://x"},
        }

    def test_instance_prefixed_dispatches_and_prints_json(self, capsys, fake_transport):
        _seed(self._registry_path, {"site-01": _entry()})
        fake_transport()
        rc = cli.main(["site-01", "Page.enable"])
        assert rc == cli.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out == {"echoed_method": "Page.enable", "echoed_params": None}

    def test_malformed_params_json_exits_usage(self, capsys, fake_transport):
        _seed(self._registry_path, {"site-01": _entry()})
        fake_transport()
        rc = cli.main(["site-01", "Page.navigate", "{not json"])
        assert rc == cli.EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_cdp_error_exits_operational(self, capsys, fake_transport):
        _seed(self._registry_path, {"site-01": _entry()})

        def boom(method, params):
            raise CDPError(code=-32000, message="boom")

        fake_transport(responder=boom)
        rc = cli.main(["site-01", "DOM.querySelector"])
        assert rc == cli.EXIT_OPERATIONAL
        assert "error:" in capsys.readouterr().err

    def test_unrecognized_verb_still_argparse_usage_error(self):
        # "bogus" is neither a known instance nor Domain.method-shaped, so it
        # falls through to argparse exactly as before this ticket.
        _seed(self._registry_path, {})
        with pytest.raises(SystemExit) as exc:
            cli.main(["bogus"])
        assert exc.value.code == cli.EXIT_USAGE

    def test_help_without_running_instance_prints_static_usage(self, capsys):
        _seed(self._registry_path, {})
        rc = cli.main(["help"])
        assert rc == cli.EXIT_OK
        assert capsys.readouterr().out == passthrough.STATIC_HELP

    def test_help_with_instance_prints_live_schema(self, capsys, monkeypatch):
        _seed(self._registry_path, {"site-01": _entry(port=9222)})
        monkeypatch.setattr(
            core_protocol, "fetch_protocol_schema", lambda port: _FAKE_SCHEMA
        )
        rc = cli.main(["help", "site-01"])
        assert rc == cli.EXIT_OK
        assert "Page" in capsys.readouterr().out

    def test_target_flag_reaches_passthrough(self, capsys, fake_transport):
        _seed(self._registry_path, {"site-01": _entry()})
        targets = [
            {"targetId": "AAAA1111", "type": "page", "url": "https://a.example"},
            {"targetId": "BBBB2222", "type": "page", "url": "https://b.example"},
        ]
        calls = fake_transport(targets=targets)
        rc = cli.main(["site-01", "Page.enable", "--target", "BBBB2222"])
        assert rc == cli.EXIT_OK
        attach_call = calls[1]
        assert attach_call[1] == {"targetId": "BBBB2222", "flatten": True}
