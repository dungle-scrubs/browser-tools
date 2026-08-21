"""The authoritative Phase 2 parity gate: native vs the real Node engine (#41).

This is the rung RFC-01's Testing Strategy names as the gate: the frozen corpus
run through the native CDP engine and the **real** chrome-devtools-mcp Node
engine, compared under the normative operator, matching on the full corpus for
two consecutive flake-free runs. Only then is the Node path removable (#47).

It needs two live processes -- a Playwright Chromium for the native path and a
chrome-devtools-mcp subprocess (Node + a Chrome) for the baseline -- so it is
marked ``parity`` and **skips cleanly** when either is unavailable, keeping the
default ``pytest`` run green offline. Run explicitly with:  uv run pytest -m parity

Superset semantics (``parity_comparison.corpus_covers``): the native engine reads
the raw accessibility tree and legitimately reports more detail than the Node
snapshot (AX-internal leaves, structural containers, shadow-pierced detail). The
gate therefore requires native to **cover** the Node baseline -- contain every
``(role, name, value)`` node it reports, resolve every UID identically, and
extract identical text -- while allowing native's additional nodes. A missing
baseline node, a diverging UID, or any text difference still fails, which is what
surfaced (and, via cross-frame stitching, closed) the iframe gap.
"""

from __future__ import annotations

import pytest
from live_chromium import PlaywrightChromiumSession, chromium_available
from parity_comparison import compare_corpus, corpus_covers, corpus_matches
from parity_engines import (
    NativeInteractionEngine,
    NodeEngine,
    NodeMcpSession,
    capture_corpus,
    node_engine_available,
)

_CHROMIUM_OK, _CHROMIUM_WHY = chromium_available()
_NODE_OK, _NODE_WHY = node_engine_available()

pytestmark = [
    pytest.mark.parity,
    pytest.mark.skipif(not _CHROMIUM_OK, reason=f"no live Chromium: {_CHROMIUM_WHY}"),
    pytest.mark.skipif(not _NODE_OK, reason=f"no Node engine: {_NODE_WHY}"),
]


def _capture_native_twice() -> tuple[dict, dict]:
    try:
        with PlaywrightChromiumSession() as session:
            session.navigate("about:blank")
            engine = NativeInteractionEngine(session)
            return capture_corpus(engine), capture_corpus(engine)
    except Exception as exc:  # missing browser binary, sandbox denial, etc.
        pytest.skip(f"could not launch a live Chromium: {exc}")


def _capture_node_twice() -> tuple[dict, dict]:
    try:
        with NodeMcpSession() as node:
            engine = NodeEngine(node)
            return capture_corpus(engine), capture_corpus(engine)
    except Exception as exc:  # npx/network/Chrome unavailable
        pytest.skip(f"could not launch the chrome-devtools-mcp Node engine: {exc}")


@pytest.fixture(scope="module")
def captures() -> dict[str, tuple[dict, dict]]:
    node_first, node_second = _capture_node_twice()
    native_first, native_second = _capture_native_twice()
    return {
        "node": (node_first, node_second),
        "native": (native_first, native_second),
    }


def test_gate_covers_full_corpus_two_consecutive_runs(captures) -> None:
    """The gate: native covers the Node baseline on the full corpus, twice."""
    node_runs = captures["node"]
    native_runs = captures["native"]
    for run_index in (0, 1):
        results = corpus_covers(node_runs[run_index], native_runs[run_index])
        broken = {pid: [d.detail for d in r.diffs] for pid, r in results.items() if not r.matched}
        assert corpus_matches(results), f"run {run_index + 1}: native did not cover Node baseline: {broken}"


def test_gate_covers_every_frozen_page(captures) -> None:
    """Both engines captured every frozen corpus page (no page silently dropped)."""
    expected = {"plain", "form", "iframe", "shadow", "dynamic"}
    for engine_key in ("node", "native"):
        for run in captures[engine_key]:
            assert set(run) == expected, f"{engine_key} run captured {set(run)}"


def test_native_is_flake_free_across_two_runs(captures) -> None:
    """Native self-parity (strict): the two native runs agree exactly."""
    first, second = captures["native"]
    results = compare_corpus(first, second)
    broken = {pid: [d.detail for d in r.diffs] for pid, r in results.items() if not r.matched}
    assert corpus_matches(results), f"native capture not flake-free: {broken}"


def test_native_covers_iframe_child_frame(captures) -> None:
    """Cross-frame stitching closed the iframe gap: the child frame's nodes appear.

    The pre-#41 native read missed cross-frame content, so the Node baseline's
    ``button "Framed button"`` was not covered. Stitching makes native reach across
    the frame boundary; assert the child-frame node is present in the native
    capture and that the gate covers the iframe page.
    """
    native_first = captures["native"][0]
    node_first = captures["node"][0]
    native_iframe_nodes = {(n.role, n.name) for n in native_first["iframe"].nodes}
    assert ("button", "Framed button") in native_iframe_nodes
    result = corpus_covers(node_first, native_first)["iframe"]
    assert result.matched, [d.detail for d in result.diffs]
