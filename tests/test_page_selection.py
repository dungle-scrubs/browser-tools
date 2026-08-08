"""Behavior tests for :class:`PageSelection`, the Active Page owner.

These tests exercise PageSelection through its public interface (normalize,
needs_refresh, before_call, apply_response) using a FakeClient, replacing the
old isolated unit tests that called the parse helpers directly.
"""

from __future__ import annotations

from typing import Any

from browser_tools.browser_state import BrowserState
from browser_tools.page_selection import PageSelection


def make_response(text: str) -> dict[str, Any]:
    """Build a minimal MCP text response for tests.

    Args:
        text: Text content to embed in the response.

    Returns:
        JSON-RPC response dict with a single text content item.
    """
    return {"result": {"content": [{"type": "text", "text": text}]}}


class FakeClient:
    """Records ``call_tool`` invocations and returns canned responses by name."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        """Store per-tool canned responses; default is an empty content envelope.

        Args:
            responses: Optional mapping of tool name to response dict. Tools
                without an entry return an empty content envelope.

        Returns:
            None.
        """
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any]] = dict(responses or {})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Record the call and return the configured response (or empty default).

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            Configured fake response, or an empty content envelope.
        """
        self.calls.append((name, arguments))
        return self.responses.get(name, {"result": {"content": []}})


def make_selection(**state_kwargs: Any) -> tuple[PageSelection, BrowserState, list[int]]:
    """Build a PageSelection over a fresh BrowserState with a recording save.

    Args:
        state_kwargs: Extra BrowserState fields (e.g. selected_page_id, url).

    Returns:
        The selection, its backing state, and a list that grows by one each
        time the save callable fires (so persistence can be asserted).
    """
    saves: list[int] = []
    state = BrowserState(browser_url="http://127.0.0.1:9222", **state_kwargs)
    selection = PageSelection(state, lambda: saves.append(1))
    return selection, state, saves


PAGES_RESPONSE = "## Pages\n1: about:blank\n2: https://example.com/ [selected]"


class TestApplyResponse:
    """apply_response parses tool output and mutates the active-page fields."""

    def test_tracks_selected_page_id_and_url(self) -> None:
        """A list_pages-style [selected] line sets both fields and persists."""
        selection, state, saves = make_selection()
        selection.apply_response("list_pages", {}, make_response(PAGES_RESPONSE))
        assert state.selected_page_id == 2
        assert state.selected_page_url == "https://example.com/"
        assert saves  # persistence was triggered

    def test_select_page_sets_page_id_from_params(self) -> None:
        """select_page with no [selected] in the response falls back to pageId."""
        selection, state, _ = make_selection()
        selection.apply_response("select_page", {"pageId": 5}, make_response(""))
        assert state.selected_page_id == 5

    def test_close_page_clears_when_it_matches_active(self) -> None:
        """close_page on the active tab clears both fields."""
        selection, state, _ = make_selection(selected_page_id=7)
        selection.apply_response("close_page", {"pageId": 7}, make_response(""))
        assert state.selected_page_id is None
        assert state.selected_page_url is None

    def test_close_page_leaves_selection_when_page_id_differs(self) -> None:
        """close_page on a different tab leaves the active selection alone."""
        selection, state, _ = make_selection(selected_page_id=7)
        selection.apply_response("close_page", {"pageId": 3}, make_response(""))
        assert state.selected_page_id == 7


class TestBeforeCall:
    """before_call restores the prior tab and pre-snapshots interaction tools."""

    def test_restores_by_url_then_selects_in_order(self) -> None:
        """A stored url triggers list_pages -> resolve -> select_page, in order."""
        selection, state, _ = make_selection(
            selected_page_url="https://example.com/", selected_page_id=99
        )
        client = FakeClient(
            {
                "list_pages": make_response(PAGES_RESPONSE),
                "select_page": {"result": {"content": []}},
            }
        )
        selection.before_call(client, "take_snapshot")
        assert client.calls == [
            ("list_pages", {}),
            ("select_page", {"pageId": 2}),
        ]
        assert state.selected_page_id == 2

    def test_clears_and_persists_when_url_no_longer_present(self) -> None:
        """A stored url that no longer exists clears selection and saves."""
        selection, state, saves = make_selection(
            selected_page_url="https://gone.com/", selected_page_id=2
        )
        client = FakeClient(
            {"list_pages": make_response("## Pages\n1: about:blank\n2: https://other.com/")}
        )
        selection.before_call(client, "take_snapshot")
        assert client.calls == [("list_pages", {})]
        assert state.selected_page_id is None
        assert state.selected_page_url is None
        assert saves

    def test_interaction_tool_takes_snapshot(self) -> None:
        """click is an interaction tool, so a pre-snapshot is taken."""
        selection, _, _ = make_selection()  # no stored selection -> restore skipped
        client = FakeClient({"take_snapshot": {"result": {"content": []}}})
        selection.before_call(client, "click")
        assert client.calls == [("take_snapshot", {})]

    def test_skips_restore_for_page_selecting_tools(self) -> None:
        """new_page and select_page skip restore even with a stored selection."""
        selection, _, _ = make_selection(
            selected_page_url="https://example.com/", selected_page_id=2
        )
        client = FakeClient()
        selection.before_call(client, "new_page")
        selection.before_call(client, "select_page")
        assert client.calls == []


class TestNormalize:
    """normalize rewrites wrapper arguments to the MCP server schema."""

    def test_maps_pageidx_to_pageid_for_select_page(self) -> None:
        """select_page accepts the legacy pageIdx and maps it to pageId."""
        selection, _, _ = make_selection()
        assert selection.normalize("select_page", {"pageIdx": 4}) == {"pageId": 4}

    def test_maps_pageidx_to_pageid_for_close_page(self) -> None:
        """close_page accepts the legacy pageIdx and maps it to pageId."""
        selection, _, _ = make_selection()
        assert selection.normalize("close_page", {"pageIdx": 9}) == {"pageId": 9}

    def test_leaves_other_tools_unchanged(self) -> None:
        """Tools without legacy arguments pass through untouched."""
        selection, _, _ = make_selection()
        assert selection.normalize("take_snapshot", {"verbose": True}) == {"verbose": True}

    def test_does_not_override_explicit_pageid(self) -> None:
        """An explicit pageId is kept and a stray pageIdx is not mapped over it."""
        selection, _, _ = make_selection()
        assert selection.normalize("select_page", {"pageId": 7, "pageIdx": 2}) == {
            "pageId": 7,
            "pageIdx": 2,
        }


class TestNeedsRefresh:
    """needs_refresh reports whether list_pages should be re-read after a call."""

    def test_true_for_interaction_tool(self) -> None:
        """Interaction tools (UID-based) may move the page, so refresh."""
        selection, _, _ = make_selection()
        assert selection.needs_refresh("click") is True

    def test_true_for_navigate_page(self) -> None:
        """Navigation tools refresh because they may change the active tab."""
        selection, _, _ = make_selection()
        assert selection.needs_refresh("navigate_page") is True

    def test_true_for_new_page(self) -> None:
        """new_page is navigation, so the newly-opened tab is re-read."""
        selection, _, _ = make_selection()
        assert selection.needs_refresh("new_page") is True

    def test_false_for_snapshot(self) -> None:
        """A bare snapshot does not change the page set, so no refresh."""
        selection, _, _ = make_selection()
        assert selection.needs_refresh("take_snapshot") is False
