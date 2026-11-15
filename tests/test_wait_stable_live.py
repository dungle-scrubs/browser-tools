"""wait-stable against an isolated Chrome, including documents with no root element (#190, #233)."""

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


def run_cli(endpoint: str, steps: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "browser_tools.cli", "run", str(steps), "--endpoint", endpoint],
        capture_output=True, text=True, timeout=20,
    )


def run_steps(endpoint: str, steps: Path) -> dict:
    completed = run_cli(endpoint, steps)
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


def test_a_doctype_arriving_first_does_not_start_the_quiet_period(
    curated_browser: str, tmp_path: Path
) -> None:
    # The doctype is a mutation on a document that still has no root element.
    # If that mutation started the quiet period, wait-stable would resolve
    # about STABLE_MS after the doctype, well before the root element arrives.
    doctype_at_ms = 150
    root_at_ms = 700
    restore = (
        "document.documentElement.remove();"
        "setTimeout(() => document.appendChild("
        f"document.implementation.createDocumentType('html', '', '')), {doctype_at_ms});"
        f"setTimeout(() => document.appendChild(document.createElement('html')), {root_at_ms});"
        "'removed'"
    )
    page = "data:text/html," + quote("<p>root</p>")
    steps = tmp_path / "doctype-first.steps"
    steps.write_text(
        f"Page.navigate '{json.dumps({'url': page})}'\n"
        "wait-stable\n"
        f"eval {json.dumps(restore)}\n"
        "wait-stable\n"
    )
    elapsed = stable_after_ms(run_steps(curated_browser, steps))
    # With the quiet period started by the doctype this is about
    # doctype_at_ms + STABLE_MS, 450.
    assert elapsed >= root_at_ms + STABLE_MS - 150, elapsed


def test_a_root_removed_mid_wait_ends_in_the_timeout(curated_browser: str, tmp_path: Path) -> None:
    # A mutation starts the quiet period, then the root element goes away for
    # good before the period ends. The document now has no content, so the
    # wait must not resolve on the timer the earlier mutation started.
    remove = (
        "setTimeout(() => document.body.appendChild(document.createElement('i')), 100);"
        "setTimeout(() => document.documentElement.remove(), 200);"
        "'armed'"
    )
    page = "data:text/html," + quote("<p>root</p>")
    steps = tmp_path / "root-removed.steps"
    steps.write_text(
        f"Page.navigate '{json.dumps({'url': page})}'\n"
        "wait-stable\n"
        f"eval {json.dumps(remove)}\n"
        "wait-stable --timeout-ms 1500\n"
    )
    completed = run_cli(curated_browser, steps)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "E008: wait_stable timed out after 1500ms" in completed.stdout + completed.stderr
