"""Install the locked trace engine explicitly, then analyze local files offline."""

from __future__ import annotations

import fcntl
import hashlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .lifecycle import LifecycleError
from .usage import UsageError

ADAPTER = Path(__file__).with_name("_insights")
SETUP_COMMAND = "bt insights --setup"


def _lock_digest(directory: Path) -> str:
    return hashlib.sha256((directory / "package-lock.json").read_bytes()).hexdigest()


def _dependencies_match(directory: Path) -> bool:
    try:
        lock = json.loads((directory / "package-lock.json").read_text())
        for name, package in lock["packages"].items():
            if (
                name
                and json.loads((directory / name / "package.json").read_text())["version"]
                != package["version"]
            ):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _ready(directory: Path) -> bool:
    try:
        return (directory / ".installed").read_text() == _lock_digest(
            directory
        ) and _dependencies_match(directory)
    except OSError:
        return False


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        raise LifecycleError(
            f"Node.js 22 or newer is required. Install Node.js, then run {SETUP_COMMAND}"
        )
    return node


def setup() -> dict[str, Any]:
    """Run npm ci in the installed adapter directory, never in the caller's cwd."""
    node = _node()
    npm = shutil.which("npm")
    if npm is None:
        raise LifecycleError(f"npm is required. Install Node.js with npm, then run {SETUP_COMMAND}")
    command = [npm, "ci", "--omit=dev", "--no-audit", "--no-fund"]
    remedy = f"cd {shlex.quote(str(ADAPTER))} && npm ci --omit=dev; then {SETUP_COMMAND}"
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=True, timeout=10
        )
        if int(version.stdout.strip().lstrip("v").split(".")[0]) < 22:
            raise LifecycleError("Node.js 22 or newer is required for insights")
        with (ADAPTER / ".setup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not _ready(ADAPTER):
                (ADAPTER / ".installed").unlink(missing_ok=True)
                result = subprocess.run(
                    command, cwd=ADAPTER, capture_output=True, text=True, timeout=300
                )
                if result.returncode:
                    raise LifecycleError(
                        f"Insights setup failed (npm ci exit {result.returncode}). "
                        f"Setup needs network access to the npm registry and a writable package directory. "
                        f"When available, run: {remedy}\n{result.stderr.strip()}"
                    )
                if not _dependencies_match(ADAPTER):
                    raise LifecycleError(
                        f"Installed dependencies do not match the lockfile. Run: {remedy}"
                    )
                staged = ADAPTER / ".installed.partial"
                staged.write_text(_lock_digest(ADAPTER))
                staged.replace(ADAPTER / ".installed")
        return {"ready": True, "path": str(ADAPTER), "lockfileSha256": _lock_digest(ADAPTER)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"Cannot set up insights: {exc}. Run: {remedy}") from exc


def analyze(
    trace: str, insight: str | None, ignore_engine_mismatch: bool
) -> tuple[dict[str, Any], int]:
    """Return the adapter document and exit code without opening a browser."""
    if not _ready(ADAPTER):
        raise UsageError(f"Insights is not set up for this lockfile. Run: {SETUP_COMMAND}")
    try:
        with (ADAPTER / ".setup.lock").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            if not _ready(ADAPTER):
                raise UsageError(f"Insights is not set up. Run: {SETUP_COMMAND}")
            result = subprocess.run(
                [
                    _node(),
                    str(ADAPTER / "adapter.mjs"),
                    str(Path(trace).resolve()),
                    insight or "",
                    str(ignore_engine_mismatch).lower(),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        # The engine may log a recoverable Lantern error on stderr. Its exit
        # status and JSON document, not the presence of stderr, define success.
        document = json.loads(result.stdout)
        if (
            not isinstance(document, dict)
            or "engineRevision" not in document
            or "navigations" not in document
        ):
            raise ValueError("adapter did not return an insights document")
        if result.returncode not in (0, 1, 2):
            raise ValueError(f"adapter exited {result.returncode}")
        return document, result.returncode
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise LifecycleError(
            f"Cannot analyze trace: {exc}. Repair the runtime with {SETUP_COMMAND}"
        ) from exc
