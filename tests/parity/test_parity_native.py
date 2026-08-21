"""Live parity test for the native CDP snapshot engine (ticket #39).

Skips cleanly when no Playwright Chromium is available, so the default
``pytest`` run stays green offline. Run explicitly with:  uv run pytest -m parity

Two rungs are asserted here:

1. **Native self-parity, flake-free.** The native engine captures the frozen
   corpus twice; the two runs must match under the parity operator. This is the
   RFC's "two consecutive runs, flake-free" property applied to the native
   candidate.

2. **Cross-engine agreement on the shared dimensions.** Native and ARIA run
   against the *same* Chromium page, so text and UID targets are computed by
   identical JS and MUST agree. The snapshot node set is compared and its diffs
   are surfaced (roles from CDP AX vs ARIA YAML differ by vocabulary), but not
   asserted equal: the authoritative Node-vs-native gate is #41.
"""

from __future__ import annotations

import pytest
from live_chromium import PlaywrightChromiumSession, chromium_available
from parity_comparison import compare_captures, compare_corpus, corpus_matches
from parity_engines import AriaSnapshotEngine, NativeSnapshotEngine, capture_corpus

_AVAILABLE, _WHY = chromium_available()

pytestmark = [
    pytest.mark.parity,
    pytest.mark.skipif(not _AVAILABLE, reason=f"no live Chromium: {_WHY}"),
]


@pytest.fixture
def chromium_session():
    try:
        with PlaywrightChromiumSession() as session:
            # Prove the browser actually launched before yielding.
            session.navigate("about:blank")
            yield session
    except Exception as exc:  # missing browser binary, sandbox denial, etc.
        pytest.skip(f"could not launch a live Chromium: {exc}")


def test_native_engine_is_flake_free_across_two_runs(chromium_session):
    native = NativeSnapshotEngine(chromium_session)
    first = capture_corpus(native)
    second = capture_corpus(native)
    results = compare_corpus(first, second)
    broken = {pid: [d.detail for d in r.diffs] for pid, r in results.items() if not r.matched}
    assert corpus_matches(results), f"native capture not flake-free: {broken}"


def test_native_engine_captures_every_page_with_nodes(chromium_session):
    captures = capture_corpus(NativeSnapshotEngine(chromium_session))
    assert set(captures) == {"plain", "form", "iframe", "shadow", "dynamic"}
    for page_id, capture in captures.items():
        assert capture.nodes, f"no native nodes captured for {page_id}"


def test_native_matches_aria_on_shared_dimensions(chromium_session):
    """Text and UID targets agree across engines on one DOM; node set may not."""
    native = capture_corpus(NativeSnapshotEngine(chromium_session))
    aria = capture_corpus(AriaSnapshotEngine(chromium_session))
    for page_id in native:
        result = compare_captures(aria[page_id], native[page_id])
        shared = [d for d in result.diffs if d.dimension in ("text", "uid_target")]
        assert not shared, f"{page_id}: shared-dimension diffs: {[d.detail for d in shared]}"
