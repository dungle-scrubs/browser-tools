"""wait-stable against an isolated Chrome, including a document with no root yet (#190)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from pathlib import Path

#: The root element returns this long after wait-stable starts.
ROOT_DELAY_MS = 400
#: wait-stable's default quiet period.
STABLE_MS = 300


def run_steps(endpoint: str, steps: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "browser_tools.cli", "run", str(steps), "--endpoint", endpoint],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


def stable_after_ms(result: dict) -> int:
    message = result["steps"][-1]["result"]["result"]
    match = re.search(r"stable after (\d+)ms", message)
    assert match, message
    return int(match[1])


def test_waits_for_a_root_element_before_counting_quiet_time(
    curated_browser: str, tmp_path: Path
) -> None:
    # A committed document with no root element is what wait-stable meets
    # right after a slow Page.navigate. Removing the root reproduces that
    # state on demand; the timer puts it back after ROOT_DELAY_MS.
    restore = (
        "document.documentElement.remove();"
        f"setTimeout(() => document.appendChild(document.createElement('html')), {ROOT_DELAY_MS});"
        "'removed'"
    )
    page = "data:text/html," + quote("<p>root</p>")
    steps = tmp_path / "no-root.steps"
    steps.write_text(
        f"Page.navigate '{json.dumps({'url': page})}'\n"
        "wait-stable\n"
        f"eval {json.dumps(restore)}\n"
        "wait-stable\n"
    )
    elapsed = stable_after_ms(run_steps(curated_browser, steps))
    # The restore timer starts in the eval step, before wait-stable does, so
    # allow for the gap between steps. Without the root-element gate this is
    # STABLE_MS, about 300.
    assert elapsed >= ROOT_DELAY_MS + STABLE_MS - 150, elapsed


def test_a_quiet_document_is_stable_after_the_quiet_period(
    curated_browser: str, tmp_path: Path
) -> None:
    page = "data:text/html," + quote("<p>quiet</p>")
    steps = tmp_path / "quiet.steps"
    steps.write_text(f"Page.navigate '{json.dumps({'url': page})}'\nwait-stable\n")
    elapsed = stable_after_ms(run_steps(curated_browser, steps))
    assert STABLE_MS <= elapsed < STABLE_MS + 1000, elapsed
