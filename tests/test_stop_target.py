"""`stop --target SPEC` resolves a spec, and a failed close fails (#90).

Two defects in the same two functions, both live violations of text RFC-01
carried before version 6:

- ``stop`` handed its ``--target`` argument to ``Target.closeTarget`` as a
  complete target ID. ``--target SPEC`` means one thing everywhere else in the
  CLI -- a 1-based index into the page targets sorted by target ID, or a target
  ID prefix, read as an index only when every character is a digit -- so the
  index form and the prefix form failed silently and only a full ID worked.
- When ``Target.closeTarget`` reported ``success: false``, ``stop`` returned a
  failure *sentence*, which the CLI wrapped in ``{"stopped": true}`` and exited
  0. An operation that failed must not exit 0 and must not report a success
  field.

The resolution now happens in ``stop`` ahead of the engine split, because the
ephemeral-Chrome branch closes tabs inside ``core/registry.py``, which is
verbatim vendored and must not be edited in place.
"""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING, Any

import pytest

from browser_tools import cdp_client, cli, lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError

if TYPE_CHECKING:
    from pathlib import Path

# Sorted by target ID, which is the normative order. Deliberately not the order
# a browser would hand them back, so a test that skipped the sort would fail.
PAGES = [
    {"id": "aa11", "type": "page", "url": "https://a.example", "title": "A"},
    {"id": "bb22", "type": "page", "url": "https://b.example", "title": "B"},
    {"id": "bb33", "type": "page", "url": "https://c.example", "title": "C"},
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed(registry_path: str, *, engine: str = "chrome", profile: str | None = None) -> str:
    """Register one instance the liveness ladder will read as live."""
    core_registry._save_registry(  # pyright: ignore[reportPrivateUsage]
        {
            "web-01": {
                "port": _free_port(),
                "pid": 2_000_000_000,
                "browser_version": "Chrome/1",
                "user_data_dir": "",
                "launched": "2026-01-01T00:00:00+00:00",
                "pid_start": None,
                "engine": engine,
                "profile": profile,
            }
        },
        registry_path,
    )
    return "web-01"


@pytest.fixture
def live_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A registry holding one instance, forced live, listing PAGES over HTTP."""
    registry_path = str(tmp_path / "registry.json")
    _seed(registry_path)
    monkeypatch.setattr(lifecycle, "instance_is_live", lambda ext: True)
    monkeypatch.setattr(
        cdp_client, "list_page_targets", lambda url: sorted(
            ({**p, "targetId": p["id"]} for p in PAGES), key=lambda p: p["targetId"]
        )
    )
    return registry_path


@pytest.fixture
def closed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the target ID each close was asked for; report success."""
    seen: list[str] = []

    def _fake_close(ext: Any, target_id: str) -> str:
        seen.append(target_id)
        return f"Closed tab {target_id[:8]} in {ext.name}"

    monkeypatch.setattr(lifecycle, "_close_tab", _fake_close)
    return seen


class TestTheSpecIsResolved:
    """One reading of --target, shared with every other verb."""

    def test_index_form_selects_by_sorted_position(
        self, live_registry: str, closed: list[str]
    ) -> None:
        lifecycle.stop(instance="web-01", target="1", registry_path=live_registry)
        assert closed == ["aa11"], "--target 1 must name the first page in the sort"

    def test_index_form_reaches_the_last_page(
        self, live_registry: str, closed: list[str]
    ) -> None:
        lifecycle.stop(instance="web-01", target="3", registry_path=live_registry)
        assert closed == ["bb33"]

    def test_prefix_form_resolves_a_partial_id(
        self, live_registry: str, closed: list[str]
    ) -> None:
        lifecycle.stop(instance="web-01", target="bb2", registry_path=live_registry)
        assert closed == ["bb22"]

    def test_a_complete_id_still_works(
        self, live_registry: str, closed: list[str]
    ) -> None:
        lifecycle.stop(instance="web-01", target="aa11", registry_path=live_registry)
        assert closed == ["aa11"]

    def test_an_ambiguous_prefix_is_operational_and_names_the_targets(
        self, live_registry: str, closed: list[str]
    ) -> None:
        with pytest.raises(LifecycleError) as excinfo:
            lifecycle.stop(instance="web-01", target="bb", registry_path=live_registry)
        message = str(excinfo.value)
        assert "bb22" in message and "bb33" in message
        assert closed == [], "nothing may be closed on an ambiguous spec"

    def test_an_unmatched_prefix_is_operational_and_names_the_targets(
        self, live_registry: str, closed: list[str]
    ) -> None:
        with pytest.raises(LifecycleError) as excinfo:
            lifecycle.stop(instance="web-01", target="zz", registry_path=live_registry)
        message = str(excinfo.value)
        assert "aa11" in message and "bb22" in message
        assert closed == []

    def test_an_out_of_range_index_is_operational(
        self, live_registry: str, closed: list[str]
    ) -> None:
        with pytest.raises(LifecycleError):
            lifecycle.stop(instance="web-01", target="4", registry_path=live_registry)
        assert closed == []


class TestTheTwoFormsNeverCross:
    """All digits is an index; anything else is a prefix. Never the other way."""

    def test_a_digits_only_spec_is_never_a_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed: list[str]
    ) -> None:
        """A page whose ID starts '2' must not answer to --target 2."""
        registry_path = str(tmp_path / "registry.json")
        _seed(registry_path)
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda ext: True)
        pages = [
            {"id": "1abc", "type": "page", "url": "u", "title": "first"},
            {"id": "2def", "type": "page", "url": "u", "title": "second"},
            {"id": "9xyz", "type": "page", "url": "u", "title": "third"},
        ]
        monkeypatch.setattr(
            cdp_client, "list_page_targets",
            lambda url: sorted(({**p, "targetId": p["id"]} for p in pages),
                               key=lambda p: p["targetId"]),
        )
        lifecycle.stop(instance="web-01", target="2", registry_path=registry_path)
        assert closed == ["2def"], "index 2 is the second page, not the '2' prefix"

    def test_a_non_digit_spec_is_never_an_index(
        self, live_registry: str, closed: list[str]
    ) -> None:
        """'1a' is a prefix. Read as an index it would truncate to 1."""
        with pytest.raises(LifecycleError):
            lifecycle.stop(instance="web-01", target="1a", registry_path=live_registry)
        assert closed == []


class TestAFailedCloseFails:
    """success: false is an operational failure, not a success envelope."""

    @pytest.fixture
    def refusing_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _send(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "Target.closeTarget"
            return {"success": False}

        monkeypatch.setattr(
            "browser_tools.core.cdp_client.get_ws_url_async",
            _immediate("ws://127.0.0.1:1/devtools/browser/x"),
        )
        monkeypatch.setattr("browser_tools.core.cdp_client.CDPClient.__aenter__", _self)
        monkeypatch.setattr("browser_tools.core.cdp_client.CDPClient.__aexit__", _none)
        monkeypatch.setattr("browser_tools.core.cdp_client.CDPClient.send", _send)

    def test_lifecycle_raises(self, live_registry: str, refusing_browser: None) -> None:
        with pytest.raises(LifecycleError) as excinfo:
            lifecycle.stop(instance="web-01", target="1", registry_path=live_registry)
        assert "Failed to close tab" in str(excinfo.value)

    def test_the_cli_exits_1_with_no_success_envelope(
        self,
        live_registry: str,
        refusing_browser: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, live_registry)
        code = cli.main(["stop", "web-01", "--target", "1"])
        captured = capsys.readouterr()

        assert code == cli.EXIT_OPERATIONAL
        assert captured.out.strip() == "", "a failed close wrote to stdout"
        assert "stopped" not in captured.out
        assert "error:" in captured.err

    def test_a_successful_close_still_reports_success(
        self,
        live_registry: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The other half: the envelope is right when the close happened."""

        async def _send(self: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"success": True}

        monkeypatch.setattr(
            "browser_tools.core.cdp_client.get_ws_url_async",
            _immediate("ws://127.0.0.1:1/devtools/browser/x"),
        )
        monkeypatch.setattr("browser_tools.core.cdp_client.CDPClient.__aenter__", _self)
        monkeypatch.setattr("browser_tools.core.cdp_client.CDPClient.__aexit__", _none)
        monkeypatch.setattr("browser_tools.core.cdp_client.CDPClient.send", _send)
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, live_registry)

        code = cli.main(["stop", "web-01", "--target", "1"])
        payload = json.loads(capsys.readouterr().out)

        assert code == cli.EXIT_OK
        assert payload["stopped"] is True
        assert "aa11" in payload["message"]


class TestTheRefusalsAreUnchanged:
    """Camoufox, a dead instance, and a corrupt registry all still refuse."""

    def test_camoufox_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed: list[str]
    ) -> None:
        registry_path = str(tmp_path / "registry.json")
        _seed(registry_path, engine="camoufox")
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda ext: True)
        with pytest.raises(LifecycleError, match="only supported for the chrome engine"):
            lifecycle.stop(instance="web-01", target="1", registry_path=registry_path)
        assert closed == []

    def test_a_dead_instance_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed: list[str]
    ) -> None:
        registry_path = str(tmp_path / "registry.json")
        _seed(registry_path, profile="dev")
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda ext: False)
        with pytest.raises(LifecycleError, match="not live"):
            lifecycle.stop(instance="web-01", target="1", registry_path=registry_path)
        assert closed == []

    def test_an_unparseable_registry_refuses_without_touching_anything(
        self, tmp_path: Path, closed: list[str]
    ) -> None:
        registry_path = tmp_path / "registry.json"
        registry_path.write_text("{ not json")
        with pytest.raises(LifecycleError, match="unparseable"):
            lifecycle.stop(instance="web-01", target="1", registry_path=str(registry_path))
        assert closed == []
        assert registry_path.exists(), "stop must not quarantine or delete"

    def test_a_browser_with_no_page_target_is_operational(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed: list[str]
    ) -> None:
        registry_path = str(tmp_path / "registry.json")
        _seed(registry_path)
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda ext: True)
        monkeypatch.setattr(cdp_client, "list_page_targets", lambda url: [])
        with pytest.raises(LifecycleError, match="no page target"):
            lifecycle.stop(instance="web-01", target="1", registry_path=registry_path)
        assert closed == []


class TestTheSelectorIsShared:
    """stop and the ws-url path resolve a spec to the same page."""

    def test_both_resolvers_agree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = [
            {**p, "targetId": p["id"], "webSocketDebuggerUrl": f"ws://x/{p['id']}"}
            for p in PAGES
        ]
        pages.sort(key=lambda p: p["targetId"])
        monkeypatch.setattr(cdp_client, "list_page_targets", lambda url: pages)

        for spec in ("1", "2", "3", "aa", "bb2", "bb3"):
            target_id = cdp_client.resolve_page_target_id("http://x", spec)
            ws_url = cdp_client.resolve_page_ws_url("http://x", spec)
            assert ws_url == f"ws://x/{target_id}", f"the two paths disagreed on {spec!r}"


def _immediate(value: object):
    async def _call(*args: object, **kwargs: object) -> object:
        return value

    return _call


async def _self(self: Any) -> Any:
    return self


async def _none(self: Any, *exc: object) -> None:
    return None
