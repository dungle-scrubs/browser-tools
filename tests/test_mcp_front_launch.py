"""The MCP front must not take the user's screen when it launches Chrome.

Chrome started normally opens a window and becomes the active app, taking
keyboard and window-manager focus from whatever the user is doing. 229a536
fixed that for the CLI launch (`core/launcher.py`): start with
``--no-startup-window``, then create the first window over CDP with
``newWindow`` and ``background`` set. The MCP front has its own launch path
and kept the old shape (#63). This applies the same pattern there.

Headless is unaffected: there is no window to steal focus with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from browser_tools.persistent_browser import INITIAL_PAGE_URL, build_browser_command


def _command(tmp_path: Path, *, headless: bool) -> list[str]:
    return build_browser_command(
        executable="/bin/chrome",
        port=9333,
        user_data_dir=tmp_path,
        headless=headless,
        viewport=None,
    )


class TestHeadedCommandOpensNoWindow:
    def test_headed_passes_no_startup_window(self, tmp_path: Path) -> None:
        """Without this flag Chrome opens a window itself and takes focus."""
        assert "--no-startup-window" in _command(tmp_path, headless=False)

    def test_headed_passes_no_url(self, tmp_path: Path) -> None:
        """A URL argument makes Chrome open a window for it, undoing the flag."""
        assert INITIAL_PAGE_URL not in _command(tmp_path, headless=False)

    def test_headless_is_unchanged(self, tmp_path: Path) -> None:
        """Headless has no window to steal focus with, so it keeps its shape."""
        command = _command(tmp_path, headless=True)
        assert "--headless=new" in command
        assert "--no-startup-window" not in command
        assert command[-1] == INITIAL_PAGE_URL


class TestFirstWindowOpensInTheBackground:
    def test_the_create_call_is_a_background_new_window(self, monkeypatch) -> None:
        """The window must be its own, and must not be activated."""
        from browser_tools import persistent_browser

        sent: list[tuple[str, dict[str, Any]]] = []

        class FakeCDP:
            def __init__(self, ws_url: str) -> None:
                self.ws_url = ws_url

            async def __aenter__(self) -> FakeCDP:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def send(self, method: str, params: dict[str, Any]) -> dict:
                sent.append((method, params))
                return {}

        monkeypatch.setattr(persistent_browser, "_first_window_cdp_client", FakeCDP)
        monkeypatch.setattr(
            persistent_browser, "_browser_ws_url", lambda _url: "ws://fake/browser"
        )

        persistent_browser.open_first_window("http://127.0.0.1:9333")

        assert sent == [
            (
                "Target.createTarget",
                {"url": INITIAL_PAGE_URL, "newWindow": True, "background": True},
            )
        ]


class TestLaunchWiring:
    @pytest.mark.parametrize("headless", [False, True])
    def test_only_a_headed_launch_opens_a_window(self, monkeypatch, tmp_path, headless):
        """Headless must not pay for a window it has no use for."""
        from browser_tools import persistent_browser

        opened: list[str] = []

        class FakeProcess:
            pid = 4242

        monkeypatch.setattr(
            persistent_browser.subprocess, "Popen", lambda *a, **k: FakeProcess()
        )
        monkeypatch.setattr(persistent_browser, "wait_for_devtools", lambda *a, **k: True)
        monkeypatch.setattr(persistent_browser, "find_free_port", lambda: 9333)
        monkeypatch.setattr(persistent_browser, "open_first_window", opened.append)

        controller = persistent_browser.PersistentChromeController.__new__(
            persistent_browser.PersistentChromeController
        )
        controller.headless = headless
        controller.viewport = None
        controller.system_profile = False

        browser_url, pid = controller._launch_chrome("/bin/chrome", tmp_path)

        assert pid == 4242
        assert opened == ([] if headless else [browser_url])
