"""Owner of the Active Page concept.

Page IDs are not stable across MCP session restarts, so the Active Page is
tracked by URL on disk and re-resolved to a fresh ID on each call. Before this
module existed that concept was smeared across controller methods and six free
functions in ``persistent_browser``. ``PageSelection`` gives it one home with a
small interface the invoke path and the tests talk to.

The lifecycle is:

1. :meth:`normalize` rewrites wrapper arguments (``pageIdx`` -> ``pageId``)
   before the call.
2. :meth:`before_call` restores the previously selected tab, then takes a
   pre-snapshot when the upcoming tool references an element UID.
3. The caller runs the tool and, for tools that may change the page set,
   refreshes ``list_pages`` and feeds that response back through
   :meth:`apply_response`.
4. :meth:`apply_response` parses the selected id+url out of the tool response
   (and handles ``select_page`` / ``close_page``), then persists state.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .mcp_response import extract_text_items
from .tool_registry import INTERACTION_TOOLS, NAVIGATION_TOOLS, PAGE_SELECTING_TOOLS

if TYPE_CHECKING:
    from collections.abc import Callable

    from .browser_state import BrowserState

# Page-list lines look like ``2: https://example.com/ [selected]``. The selected
# marker is optional; ``SELECTED_PAGE_PATTERN`` matches only the marked line,
# while ``PAGE_LINE_PATTERN`` captures id + url (selected or not) for URL-based
# resolution and page-id enumeration.
SELECTED_PAGE_PATTERN = re.compile(r"^\s*(\d+):.*\[selected\]\s*$", re.MULTILINE)
PAGE_LINE_PATTERN = re.compile(r"^\s*(\d+):\s*(.*?)(?:\s*\[selected\])?\s*$", re.MULTILINE)


class PageSelection:
    """Track and restore the active browser tab across MCP session restarts.

    Owns the ``selected_page_id`` / ``selected_page_url`` pair on a
    :class:`BrowserState`, re-resolving by URL before each call and updating
    both fields from tool responses afterwards. State is persisted through the
    ``save`` callable, which also stamps ``last_used_at``.
    """

    def __init__(self, state: BrowserState, save: Callable[[], None]) -> None:
        """Bind this selection to a browser state and a persistence callable.

        Args:
            state: Browser state whose selected-page fields are read and
                mutated. Owned by the caller; never replaced, only updated.
            save: Callable that persists ``state`` (and stamps ``last_used_at``).
                Invoked whenever a mutation should survive the process.

        Returns:
            None.
        """
        self._state = state
        self._save = save

    def normalize(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Rewrite wrapper arguments to the MCP server's actual schema.

        ``select_page`` / ``close_page`` accept ``pageId`` upstream; the wrapper
        historically exposed ``pageIdx``. Map the legacy name when the canonical
        one is absent, and leave every other tool's arguments untouched.

        Args:
            tool_name: Tool being invoked.
            params: Original wrapper arguments.

        Returns:
            Normalized argument dictionary (a shallow copy).
        """
        normalized = dict(params)
        if tool_name in {"select_page", "close_page"} and "pageId" not in normalized:
            page_idx = normalized.pop("pageIdx", None)
            if isinstance(page_idx, int):
                normalized["pageId"] = page_idx
        return normalized

    def needs_refresh(self, tool_name: str) -> bool:
        """Decide whether a ``list_pages`` refresh is needed after the call.

        Interaction tools (UID-based clicks/fills) and navigation tools (which
        may open or change tabs) can move the selected page, so the page set is
        re-read afterwards. ``new_page`` is a navigation tool and therefore
        refreshes here, closing a latent gap where its newly-opened tab was not
        reflected in state.

        Args:
            tool_name: Tool that was invoked.

        Returns:
            True when the page list should be refreshed after the call.
        """
        return tool_name in INTERACTION_TOOLS or tool_name in NAVIGATION_TOOLS

    def before_call(self, client: Any, tool_name: str) -> None:
        """Restore the prior tab and take a pre-snapshot when required.

        Page-selection tools (``new_page``, ``select_page``) choose the
        destination tab themselves, so the restore step is skipped for them.
        For every other tool, when a selection is recorded, the target tab is
        re-resolved by URL in the current session and re-selected by its fresh
        id. Using the daemon, the MCP session persists and re-selecting the
        already-selected page is a harmless no-op.

        The upstream MCP server requires a snapshot in the same session before
        any interaction tool that references element UIDs, so one is taken
        after the restore for interaction tools.

        Args:
            client: Active MCP session or daemon client with a ``call_tool``
                method.
            tool_name: Tool about to be invoked.

        Returns:
            None.
        """
        if tool_name not in PAGE_SELECTING_TOOLS and (
            self._state.selected_page_url or self._state.selected_page_id
        ):
            if self._state.selected_page_url:
                list_response = client.call_tool("list_pages", {})
                target_id = self._resolve_page_id_by_url(
                    list_response, self._state.selected_page_url
                )
            else:
                target_id = self._state.selected_page_id

            if target_id is None:
                self._state.selected_page_id = None
                self._state.selected_page_url = None
                self._save()
            else:
                response = client.call_tool("select_page", {"pageId": target_id})
                if "error" in response:
                    self._state.selected_page_id = None
                    self._state.selected_page_url = None
                    self._save()
                else:
                    self._state.selected_page_id = target_id

        if tool_name in INTERACTION_TOOLS:
            client.call_tool("take_snapshot", {})

    def apply_response(
        self, tool_name: str, params: dict[str, Any], response: dict[str, Any]
    ) -> None:
        """Persist selected-page changes inferred from the tool response.

        A ``list_pages``-style response (returned by ``list_pages`` itself, by
        ``new_page``, or by the post-call refresh) carries a ``[selected]``
        marker that drives both fields. When the response carries no selection,
        ``select_page`` falls back to the requested ``pageId`` and
        ``close_page`` clears the fields when it closed the active tab.

        Args:
            tool_name: Tool that was invoked.
            params: Normalized tool arguments.
            response: Raw JSON-RPC response.

        Returns:
            None.
        """
        selected_page_id = self._extract_selected_page_id(response)
        selected_page_url = self._extract_selected_page_url(response)

        if selected_page_id is not None:
            self._state.selected_page_id = selected_page_id
            if selected_page_url is not None:
                self._state.selected_page_url = selected_page_url
        elif tool_name == "select_page":
            page_id = params.get("pageId")
            self._state.selected_page_id = (
                page_id if isinstance(page_id, int) else self._state.selected_page_id
            )
        elif tool_name == "close_page":
            closed_page_id = params.get("pageId")
            if self._state.selected_page_id == closed_page_id:
                self._state.selected_page_id = None
                self._state.selected_page_url = None

        self._save()

    def _extract_selected_page_id(self, response: dict[str, Any]) -> int | None:
        """Parse the selected page id from a page-list style MCP response.

        Args:
            response: Raw JSON-RPC response containing tool output.

        Returns:
            Selected page id if present, otherwise None.
        """
        texts = extract_text_items(response)
        for text in texts:
            match = SELECTED_PAGE_PATTERN.search(text)
            if match:
                return int(match.group(1))
        return None

    def _extract_selected_page_url(self, response: dict[str, Any]) -> str | None:
        """Parse the selected page URL from a page-list style MCP response.

        Args:
            response: Raw JSON-RPC response containing tool output.

        Returns:
            URL of the selected page if present, otherwise None.
        """
        texts = extract_text_items(response)
        for text in texts:
            for match in PAGE_LINE_PATTERN.finditer(text):
                line = match.group(0)
                if "[selected]" in line:
                    return match.group(2).strip()
        return None

    def _resolve_page_id_by_url(self, response: dict[str, Any], target_url: str) -> int | None:
        """Find the page ID for a given URL in a list_pages response.

        Matches exactly, or after stripping trailing slashes from both sides so
        ``https://example.com`` and ``https://example.com/`` resolve together.

        Args:
            response: Raw JSON-RPC response from list_pages.
            target_url: URL to search for.

        Returns:
            Page ID matching the URL, or None if not found.
        """
        texts = extract_text_items(response)
        for text in texts:
            for match in PAGE_LINE_PATTERN.finditer(text):
                page_id = int(match.group(1))
                page_url = match.group(2).strip()
                if page_url == target_url or page_url.rstrip("/") == target_url.rstrip("/"):
                    return page_id
        return None


__all__ = [
    "PAGE_LINE_PATTERN",
    "SELECTED_PAGE_PATTERN",
    "PageSelection",
]
