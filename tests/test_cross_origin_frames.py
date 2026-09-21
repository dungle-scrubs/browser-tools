"""What `frames select` says when the frame is in another process (#127).

A cross-origin iframe runs in its own renderer process under Chrome's site
isolation, with its own CDP `iframe` target. `Page.getFrameTree` on the page
session does not contain it and its lifecycle events do not arrive there, so
the frame manager cannot see it at all.

The old message sent the caller to `frames list`, which will never contain
the frame they are looking for. Reproduced against headless Chrome with two
loopback origins: a parent on `127.0.0.1` embedding a child on `localhost`.

    frames list        only the parent
    snapshot           `Iframe` with nothing under it
    frames select      E002, pointing at that listing
    --target <iframe>  refused; only page targets are attachable

So there is no workaround to name, and the message says that rather than
implying one. Telling the two cases apart costs one `Target.getTargets` on
the failure path, and a browser that will not answer it leaves the caller
with the plain message rather than an error about the diagnosis.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from browser_tools.cdp_handler import CDPHandler, CDPRuntime
from browser_tools.frame_manager import FrameManager


class FakeClient:
    """Answers `Target.getTargets`, and nothing else."""

    connected = True

    def __init__(self, targets: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self.targets = targets or []
        self.error = error
        self.sent: list[str] = []

    async def send(self, method: str, params: Any = None, session_id: str | None = None):
        self.sent.append(method)
        if self.error is not None:
            raise self.error
        return {"targetInfos": self.targets}


def _handler(client, frames=None):
    runtime = CDPRuntime.__new__(CDPRuntime)
    runtime._cdp_client = client
    runtime._frame_manager = frames
    handler = CDPHandler.__new__(CDPHandler)
    handler._rt = runtime
    return handler


def _page_with_one_frame():
    frames = FrameManager()
    frames.update_from_frame_tree({"frame": {"id": "R", "url": "http://127.0.0.1:8127/"}})
    return frames


def _select(handler, pattern):
    result = asyncio.run(handler._handle_select_frame({"url_pattern": pattern}))
    return result["result"]["content"][0]["text"]


OOPIF = {"type": "iframe", "url": "http://localhost:8128/", "targetId": "T1"}
PAGE = {"type": "page", "url": "http://127.0.0.1:8127/", "targetId": "T0"}


class TestTheMessageNamesTheRealCause:
    def test_a_matching_iframe_target_is_named(self):
        handler = _handler(FakeClient([PAGE, OOPIF]), _page_with_one_frame())
        message = _select(handler, "localhost")
        assert "http://localhost:8128/" in message
        assert "cross-origin iframe" in message
        assert "issues/127" in message

    def test_it_does_not_send_the_caller_to_frames_list(self):
        """The listing will never contain the frame, so naming it is a trap."""
        handler = _handler(FakeClient([PAGE, OOPIF]), _page_with_one_frame())
        assert "Run 'frames list'" not in _select(handler, "localhost")

    def test_it_says_target_does_not_reach_it_either(self):
        """Measured: only page targets are attachable, so there is no flag."""
        message = _select(_handler(FakeClient([PAGE, OOPIF]), _page_with_one_frame()), "localhost")
        assert "--target does not reach it" in message

    def test_the_match_is_case_insensitive_like_the_selection(self):
        handler = _handler(FakeClient([PAGE, OOPIF]), _page_with_one_frame())
        assert "cross-origin iframe" in _select(handler, "LOCALHOST")


class TestThePlainMessageIsKeptForThePlainCase:
    def test_a_pattern_no_iframe_matches_gets_the_old_message(self):
        """A typo is still a typo, and `frames list` is still the answer."""
        handler = _handler(FakeClient([PAGE, OOPIF]), _page_with_one_frame())
        message = _select(handler, "nowhere")
        assert "Run 'frames list' to see available frames." in message
        assert "cross-origin" not in message

    def test_a_page_with_no_iframe_targets_gets_the_old_message(self):
        handler = _handler(FakeClient([PAGE]), _page_with_one_frame())
        assert "Run 'frames list'" in _select(handler, "localhost")

    def test_a_page_target_is_not_mistaken_for_an_iframe(self):
        """A second tab whose URL matches is not the caller's missing frame."""
        other = {"type": "page", "url": "http://localhost:8128/", "targetId": "T2"}
        handler = _handler(FakeClient([PAGE, other]), _page_with_one_frame())
        assert "cross-origin" not in _select(handler, "localhost")


class TestTheDiagnosisNeverBecomesTheFailure:
    """The caller asked to select a frame, not to be told why the diagnosis
    could not run. A browser that will not answer `Target.getTargets` leaves
    the plain message, which is still true."""

    @pytest.mark.parametrize(
        "error",
        [ConnectionError("gone"), TimeoutError(), OSError("broken pipe")],
    )
    def test_a_failing_probe_falls_back(self, error):
        handler = _handler(FakeClient(error=error), _page_with_one_frame())
        assert "Run 'frames list'" in _select(handler, "localhost")

    def test_a_cdp_error_falls_back(self):
        from browser_tools.cdp_handler import _get_cdp_error_class

        error = _get_cdp_error_class()(-32601, "'Target.getTargets' wasn't found")
        handler = _handler(FakeClient(error=error), _page_with_one_frame())
        assert "Run 'frames list'" in _select(handler, "localhost")

    def test_no_client_falls_back(self):
        handler = _handler(None, _page_with_one_frame())
        assert "Run 'frames list'" in _select(handler, "localhost")

    def test_a_disconnected_client_is_not_asked(self):
        client = FakeClient([OOPIF])
        client.connected = False
        handler = _handler(client, _page_with_one_frame())
        assert "Run 'frames list'" in _select(handler, "localhost")
        assert client.sent == [], "the probe ran against a client that is gone"


class TestTheProbeRunsOnlyWhenItIsNeeded:
    def test_a_successful_selection_does_not_ask_the_browser(self):
        """One extra round trip on the failure path, none on the happy one."""
        client = FakeClient([PAGE, OOPIF])
        handler = _handler(client, _page_with_one_frame())
        message = _select(handler, "127.0.0.1")
        assert "Selected frame: R" in message
        assert client.sent == []
