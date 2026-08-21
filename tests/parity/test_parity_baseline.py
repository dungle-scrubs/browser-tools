"""Live parity-gate test. Skips cleanly when no browser is available.

This is the rung that needs a real browser. It launches Camoufox, captures the
frozen corpus twice through the ARIA-snapshot engine, and asserts the two runs
agree under the parity operator - the RFC's "two consecutive runs, flake-free"
requirement. Without a fetched Camoufox browser it skips, so the default
``pytest`` run stays green offline.

Run explicitly with:  uv run pytest -m parity
"""

from __future__ import annotations

import pytest
from parity_comparison import compare_corpus, corpus_matches
from parity_engines import AriaSnapshotEngine, capture_corpus

try:
    from camoufox.sync_api import Camoufox  # noqa: F401

    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

pytestmark = [
    pytest.mark.parity,
    pytest.mark.skipif(not CAMOUFOX_AVAILABLE, reason="camoufox not installed; no live browser"),
]


@pytest.fixture(scope="module")
def live_engine():
    from browser_tools.camoufox_session import CamoufoxSession

    session = CamoufoxSession()
    launched = session.call_tool("launch_browser", {"headless": True})
    if launched.get("result", {}).get("status") != "running":
        pytest.skip(f"could not launch a live browser: {launched}")
    yield AriaSnapshotEngine(session)
    session.call_tool("close_browser", {})


def test_corpus_is_flake_free_across_two_runs(live_engine):
    """The gate predicate: the same corpus matches itself on two consecutive runs."""
    first = capture_corpus(live_engine)
    second = capture_corpus(live_engine)
    results = compare_corpus(first, second)
    broken = {pid: [d.detail for d in r.diffs] for pid, r in results.items() if not r.matched}
    assert corpus_matches(results), f"corpus not flake-free: {broken}"


def test_corpus_captures_every_page(live_engine):
    captures = capture_corpus(live_engine)
    assert set(captures) == {"plain", "form", "iframe", "shadow", "dynamic"}
    # Every page must yield at least one accessibility node.
    for page_id, capture in captures.items():
        assert capture.nodes, f"no nodes captured for {page_id}"
