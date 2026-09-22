"""The parity session never launches the developer's own browser.

It used to. When the Playwright-managed Chromium was missing, the session
caught the launch failure and relaunched through the ``chrome`` channel, which
is the Google Chrome installed on the machine. Two things went wrong with that.

The comparison silently ran against a different build than the baseline it is
checked against, so the gate stayed green while measuring the wrong browser.
And on macOS, executing a browser app bundle's inner binary directly aborts
during application registration, so every fallback killed a Chrome and raised a
system crash dialog on the developer's screen, which reads as their own browser
crashing for no reason.

The ``parity`` marker is documented as "skips when no browser is available".
These tests pin that: a missing browser raises, the message names the remedy,
and no second launch is attempted with a channel.

No browser starts here. Playwright is faked.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from live_chromium import PlaywrightChromiumSession


class _FakeChromium:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def launch(self, **kwargs: Any) -> Any:
        self._calls.append(kwargs)
        raise RuntimeError("Executable doesn't exist at /nowhere/chromium_headless_shell-1217")


class _FakePlaywright:
    def __init__(self, calls: list[dict[str, Any]], stopped: list[bool]) -> None:
        self.chromium = _FakeChromium(calls)
        self._stopped = stopped

    def stop(self) -> None:
        self._stopped.append(True)


class _FakeDriver:
    def __init__(self, calls: list[dict[str, Any]], stopped: list[bool]) -> None:
        self._calls = calls
        self._stopped = stopped

    def start(self) -> _FakePlaywright:
        return _FakePlaywright(self._calls, self._stopped)


@pytest.fixture
def launch_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], list[bool]]:
    calls: list[dict[str, Any]] = []
    stopped: list[bool] = []
    # The session imports ``sync_playwright`` inside ``__enter__``, so the
    # module may not be loaded yet when this fixture runs.
    sync_api = importlib.import_module("playwright.sync_api")
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _FakeDriver(calls, stopped))
    return calls, stopped


class TestNoFallbackToTheInstalledBrowser:
    def test_a_missing_browser_raises_rather_than_substituting(self, launch_calls) -> None:
        with pytest.raises(RuntimeError):
            PlaywrightChromiumSession().__enter__()

    def test_exactly_one_launch_is_attempted(self, launch_calls) -> None:
        calls, _ = launch_calls
        with pytest.raises(RuntimeError):
            PlaywrightChromiumSession().__enter__()
        assert len(calls) == 1, f"a second launch was attempted: {calls}"

    def test_no_launch_names_a_channel(self, launch_calls) -> None:
        calls, _ = launch_calls
        with pytest.raises(RuntimeError):
            PlaywrightChromiumSession().__enter__()
        assert all("channel" not in call for call in calls), (
            "a launch named a channel, which reaches the developer's own browser"
        )

    def test_the_message_names_the_remedy(self, launch_calls) -> None:
        with pytest.raises(RuntimeError) as caught:
            PlaywrightChromiumSession().__enter__()
        assert "playwright install chromium" in str(caught.value)

    def test_the_driver_is_stopped_rather_than_leaked(self, launch_calls) -> None:
        _, stopped = launch_calls
        with pytest.raises(RuntimeError):
            PlaywrightChromiumSession().__enter__()
        assert stopped == [True], "__exit__ never runs when __enter__ raises"
