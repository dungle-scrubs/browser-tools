"""One `error:` on a line, not two.

`mcp_response.make_error` prefixes `Error: ` for a daemon socket that no
longer exists: RFC-01 deleted the MCP front, and `mcp_response` survives as
the envelope shape `CDPHandler` and `curated` pass between themselves.
`cli.py` prefixes `error: ` on the way to stderr. Together they printed:

    error: Error: E002: No frame found matching 'localhost'. ...

Two prefixes, one of which is a leftover. The CLI's is the one that belongs,
because it is the one a caller greps for.
"""

from __future__ import annotations

import pytest
from doubles import HandlerSurface

from browser_tools import curated
from browser_tools.lifecycle import LifecycleError
from browser_tools.mcp_response import error_response, make_error, make_text


class Handler(HandlerSurface):
    def __init__(self, response):
        self.response = response

    def call_tool(self, name, arguments):
        return self.response

    def call_native(self, name, arguments):
        return self.response


class TestTheEnvelopePrefixIsNotCarriedIntoTheMessage:
    @pytest.mark.parametrize("funnel", [curated._tool_or_raise, curated._native_or_raise])
    def test_a_prefixed_envelope_loses_the_prefix(self, funnel):
        handler = Handler(make_error("E002: No frame found matching 'localhost'."))
        with pytest.raises(LifecycleError) as caught:
            funnel(handler, "select_frame", {})
        assert str(caught.value) == "E002: No frame found matching 'localhost'."

    @pytest.mark.parametrize("funnel", [curated._tool_or_raise, curated._native_or_raise])
    def test_the_cli_then_prints_exactly_one_prefix(self, funnel):
        handler = Handler(make_error("E002: nope"))
        with pytest.raises(LifecycleError) as caught:
            funnel(handler, "select_frame", {})
        line = f"error: {caught.value}"
        assert line == "error: E002: nope"
        assert line.lower().count("error: ") == 1

    def test_a_message_that_writes_its_own_wording_is_untouched(self):
        """`error_response` says the caller owns the text, including a prefix
        it chose. Stripping one `Error: ` still leaves that caller's own."""
        handler = Handler(error_response("E001: the port is dead"))
        with pytest.raises(LifecycleError) as caught:
            curated._tool_or_raise(handler, "anything", {})
        assert str(caught.value) == "E001: the port is dead"

    def test_the_word_error_inside_a_message_survives(self):
        """Only a leading prefix goes. A message about an error keeps it."""
        handler = Handler(make_error("the page raised Error: ReferenceError"))
        with pytest.raises(LifecycleError) as caught:
            curated._tool_or_raise(handler, "anything", {})
        assert str(caught.value) == "the page raised Error: ReferenceError"

    def test_a_success_envelope_is_returned_verbatim(self):
        handler = Handler(make_text("Error: this is the page's own text"))
        assert curated._tool_or_raise(handler, "anything", {}) == (
            "Error: this is the page's own text"
        )
