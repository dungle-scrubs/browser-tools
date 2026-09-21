"""``click`` and ``fill`` must act on a page the caller chose, and say so.

Chrome drops input sent to a background tab without an error, so a `bt click`
aimed at one did nothing and still printed a success result (#62). Two halves:

- The **focus guard** refuses input to a hidden tab, with the message the raw
  passthrough already uses (``passthrough._refuse_input_to_hidden_tab``), so
  the two surfaces give one answer.
- **``--target SPEC``** lets the caller name the page, which is what makes the
  refusal survivable: the handler transport otherwise always took the first
  page in ``/json/list``, with no way to reach any other.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from doubles import HandlerSurface

from browser_tools import curated
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError
from browser_tools.mcp_response import make_text


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


def _seed(registry_path: str, port: int = 9222) -> None:
    core_registry._save_registry(
        {
            "only-01": {
                "port": port,
                "pid": 2_000_000_000,
                "browser_version": "Chrome/1",
                "user_data_dir": "",
                "launched": "2026-01-01T00:00:00+00:00",
                "pid_start": None,
            }
        },
        registry_path,
    )


class FakeHandler(HandlerSurface):
    """A one-shot CDPHandler double that records its target and visibility."""

    visibility: ClassVar[str] = "visible"
    instances: ClassVar[list[FakeHandler]] = []

    def __init__(
        self, browser_url, mode="full", stealth=False, target_spec=None, target_by=None
    ):
        self.browser_url = browser_url
        self.mode = mode
        self.target_spec = target_spec
        self.target_by = target_by
        self.tool_calls: list[tuple[str, dict]] = []
        self.native_calls: list[tuple[str, dict]] = []
        FakeHandler.instances.append(self)

    @property
    def available(self) -> bool:
        return True

    def run(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.tool_calls.append((name, arguments))
        return make_text(f"{name}-ok")

    def call_native(self, name: str, arguments: dict) -> dict:
        self.native_calls.append((name, arguments))
        return make_text(f"{name}-ok")

    def page_visibility_state(self) -> str | None:
        return FakeHandler.visibility


@pytest.fixture
def fake_handler(monkeypatch):
    FakeHandler.instances = []
    FakeHandler.visibility = "visible"
    monkeypatch.setattr(curated, "CDPHandler", FakeHandler)
    return FakeHandler


class TestFocusGuard:
    def test_click_refuses_a_hidden_tab(self, registry_path, fake_handler):
        """A background tab drops the click, so the verb must not report success."""
        _seed(registry_path)
        fake_handler.visibility = "hidden"

        with pytest.raises(LifecycleError) as exc:
            curated.click(instance=None, uid="1-5", registry_path=registry_path)

        message = str(exc.value)
        assert "background tab" in message
        assert "visibilityState" in message
        assert "newWindow" in message, "the error must name the remedy"
        assert fake_handler.instances[0].native_calls == [], "the click was dispatched anyway"

    def test_fill_refuses_a_hidden_tab(self, registry_path, fake_handler):
        """fill drops the same way click does."""
        _seed(registry_path)
        fake_handler.visibility = "hidden"

        with pytest.raises(LifecycleError):
            curated.fill(instance=None, uid="1-5", text="x", registry_path=registry_path)

        assert fake_handler.instances[0].native_calls == []

    def test_click_proceeds_on_a_visible_tab(self, registry_path, fake_handler):
        """A visible tab is the ordinary case and must not be blocked."""
        _seed(registry_path)

        curated.click(instance=None, uid="1-5", registry_path=registry_path)

        assert [name for name, _ in fake_handler.instances[0].native_calls] == ["click"]

    def test_an_unreadable_visibility_does_not_block(self, registry_path, fake_handler):
        """If the state cannot be read, act rather than refuse a working page."""
        _seed(registry_path)
        fake_handler.visibility = None

        curated.click(instance=None, uid="1-5", registry_path=registry_path)

        assert ("click", {"uid": "1-5"}) in fake_handler.instances[0].native_calls

    def test_snapshot_is_not_guarded(self, registry_path, fake_handler):
        """Reading a hidden page is fine; only input is dropped by Chrome."""
        _seed(registry_path)
        fake_handler.visibility = "hidden"

        curated.snapshot(instance=None, registry_path=registry_path)

        assert fake_handler.instances[0].native_calls != []


class TestTargetSpec:
    @pytest.mark.parametrize(
        ("verb", "kwargs"),
        [
            ("snapshot", {}),
            ("click", {"uid": "1-5"}),
            ("fill", {"uid": "1-5", "text": "x"}),
        ],
    )
    def test_the_spec_reaches_the_handler(
        self, registry_path, fake_handler, verb: str, kwargs: dict[str, Any]
    ) -> None:
        """Each interaction verb must pass --target through to target selection."""
        _seed(registry_path)

        getattr(curated, verb)(
            instance=None, target="2", registry_path=registry_path, **kwargs
        )

        assert fake_handler.instances[0].target_spec == "2"

    def test_no_spec_is_the_default_page(self, registry_path, fake_handler):
        """Omitting --target keeps the existing single-page behaviour."""
        _seed(registry_path)

        curated.snapshot(instance=None, registry_path=registry_path)

        assert fake_handler.instances[0].target_spec is None
