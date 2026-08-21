#!/usr/bin/env python3
"""Capture a parity baseline from a real browser.

This is the runnable entry point the parity gate needs a live browser for. It
launches this repo's ``CamoufoxSession`` (a real Chromium via Camoufox), runs
the frozen corpus through :class:`AriaSnapshotEngine` twice, checks the two
runs agree (the RFC's "two consecutive runs, flake-free" requirement), and
writes the baseline to JSON.

Phase 2 then captures the native-snapshot engine as the candidate and compares
it against this baseline with :func:`parity_comparison.compare_corpus`.

Usage:
    uv run python tests/parity/run_baseline.py [--out baseline.json]

Requires a fetched Camoufox browser (``camoufox fetch``). Without it, the
script prints why it cannot run and exits non-zero without writing anything -
it never fabricates a baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running as a plain script: put this directory on the path so the flat
# harness module names resolve the same way they do under pytest.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parity_comparison import compare_corpus, corpus_matches, corpus_to_dict
from parity_engines import AriaSnapshotEngine, capture_corpus


def browser_available() -> tuple[bool, str]:
    """Report whether a real Camoufox browser can be launched here."""
    try:
        from camoufox.sync_api import Camoufox  # noqa: F401
    except ImportError:
        return False, "camoufox is not installed (install the 'camoufox' extra)"
    return True, ""


def capture_baseline() -> dict[str, object]:
    """Launch a real browser, capture the corpus twice, and return the baseline.

    Raises:
        RuntimeError: if the two consecutive runs disagree (a flaky corpus is
            not a valid baseline).
    """
    from browser_tools.camoufox_session import CamoufoxSession

    session = CamoufoxSession()
    launched = session.call_tool("launch_browser", {"headless": True})
    if launched.get("result", {}).get("status") != "running":
        raise RuntimeError(f"browser did not launch: {launched}")
    try:
        engine = AriaSnapshotEngine(session)
        first = capture_corpus(engine)
        second = capture_corpus(engine)
    finally:
        session.call_tool("close_browser", {})

    results = compare_corpus(first, second)
    if not corpus_matches(results):
        broken = {pid: [d.detail for d in r.diffs] for pid, r in results.items() if not r.matched}
        raise RuntimeError(f"corpus is not flake-free across two runs: {broken}")
    return corpus_to_dict(first)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the parity baseline from a real browser.")
    parser.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json"),
        help="where to write the baseline JSON",
    )
    args = parser.parse_args(argv)

    ok, why = browser_available()
    if not ok:
        print(f"cannot capture baseline: {why}", file=sys.stderr)
        print("no baseline written (a baseline is never fabricated).", file=sys.stderr)
        return 2

    baseline = capture_baseline()
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(baseline, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"wrote baseline for {len(baseline)} pages to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
