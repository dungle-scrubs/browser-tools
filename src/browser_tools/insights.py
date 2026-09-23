"""Install the locked trace engine explicitly, then analyze local files offline."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

from .lifecycle import LifecycleError
from .usage import UsageError

ADAPTER = Path(__file__).with_name("_insights")
SETUP_COMMAND = "bt insights --setup"
REINSTALL_COMMAND = "pip install --force-reinstall browser-tools"
# npm reaches the registry, so it gets the long bound. Analysis reads one local
# file; the engine costs about 130 ms for a 4000-event trace, so a run still
# going after two minutes is a stuck read, not slow work.
SETUP_TIMEOUT = 300.0
ANALYZE_TIMEOUT = 120.0
# Package data, shipped in the wheel. npm never installs or restores these, so
# a missing one is a damaged Python install and setup cannot repair it.
ADAPTER_FILES = (
    "adapter.mjs",
    "formatter_compat.js",
    "package.json",
    "package-lock.json",
    "vendor/PerformanceInsightFormatter.js",
    "vendor/PerformanceTraceFormatter.js",
    "vendor/NetworkRequestFormatter.js",
    "vendor/UnitFormatters.js",
)


def _lock_digest(directory: Path) -> str:
    return hashlib.sha256((directory / "package-lock.json").read_bytes()).hexdigest()


def _missing_adapter_files(directory: Path) -> list[str]:
    return [name for name in ADAPTER_FILES if not (directory / name).is_file()]


def _require_adapter(directory: Path) -> None:
    """Refuse before npm or Node runs when the shipped adapter is incomplete."""
    missing = _missing_adapter_files(directory)
    if missing:
        raise LifecycleError(
            f"The insights adapter in {directory} is incomplete: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing. These files ship with the "
            f"Python package, so npm cannot restore them and {SETUP_COMMAND} cannot "
            f"repair them. Reinstall the package: {REINSTALL_COMMAND}"
        )


def _dependencies_match(directory: Path) -> bool:
    """Report whether every package `npm ci --omit=dev` installs is present and pinned.

    A dev-only entry is absent by design after `--omit=dev`, and an optional
    entry may be skipped for this platform, so neither counts as a mismatch.
    """
    try:
        lock = json.loads((directory / "package-lock.json").read_text())
        for name, package in lock["packages"].items():
            if not name or not isinstance(package, dict):
                continue
            if package.get("dev") or package.get("devOptional") or package.get("link"):
                continue
            version = package.get("version")
            if version is None:
                continue
            manifest = directory / name / "package.json"
            if package.get("optional") and not manifest.exists():
                continue
            if json.loads(manifest.read_text())["version"] != version:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _ready(directory: Path) -> bool:
    try:
        if _missing_adapter_files(directory):
            return False
        return (directory / ".installed").read_text() == _lock_digest(
            directory
        ) and _dependencies_match(directory)
    except OSError:
        return False


def _stamp(directory: Path) -> None:
    staged = directory / ".installed.partial"
    staged.write_text(_lock_digest(directory))
    staged.replace(directory / ".installed")


@contextlib.contextmanager
def _locked(directory: Path, mode: int) -> Generator[None]:
    """Hold the package-local setup lock, saying so when the wait is not instant."""
    path = directory / ".setup.lock"
    try:
        # Shared holders never create the file: analysis has to work on a
        # read-only tree. A deleted lock is recreated rather than reported as
        # a raw errno, because serialization is the only thing it carries.
        handle = path.open("r" if mode == fcntl.LOCK_SH else "a")
    except FileNotFoundError:
        handle = path.open("a")
    try:
        try:
            fcntl.flock(handle, mode | fcntl.LOCK_NB)
        except OSError:
            print(f"insights: waiting for {SETUP_COMMAND} to release {path}", file=sys.stderr)
            fcntl.flock(handle, mode)
        yield
    finally:
        handle.close()


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        raise LifecycleError(
            f"Node.js 22 or newer is required. Install Node.js, then run {SETUP_COMMAND}"
        )
    return node


def setup_remedy(directory: Path | None = None) -> str:
    """The by-hand recovery command, valid shell exactly as printed.

    This string is what a caller has left when setup or analysis fails, which
    is when they are offline or the install is broken. It used to read
    `npm ci --omit=dev; then bt insights --setup`, which bash rejects.
    """
    return (
        f"cd {shlex.quote(str(ADAPTER if directory is None else directory))} "
        f"&& npm ci --omit=dev --ignore-scripts && {SETUP_COMMAND}"
    )


def setup() -> dict[str, Any]:
    """Run npm ci in the installed adapter directory, never in the caller's cwd."""
    _require_adapter(ADAPTER)
    node = _node()
    npm = shutil.which("npm")
    if npm is None:
        raise LifecycleError(f"npm is required. Install Node.js with npm, then run {SETUP_COMMAND}")
    # --ignore-scripts closes the install-time script channel. The engine
    # declares its own dependencies as "latest", which is why the lockfile is a
    # supply-chain control; a lifecycle script would walk straight past it.
    command = [npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"]
    remedy = setup_remedy()
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=True, timeout=10
        )
        if int(version.stdout.strip().lstrip("v").split(".")[0]) < 22:
            raise LifecycleError("Node.js 22 or newer is required for insights")
        with _locked(ADAPTER, fcntl.LOCK_EX):
            if not _ready(ADAPTER):
                if not (ADAPTER / ".installed").exists() and _dependencies_match(ADAPTER):
                    # Every package the lockfile names is installed at its
                    # pinned version and only the marker is gone. Re-stamping
                    # needs no registry, so offline after setup keeps working.
                    _stamp(ADAPTER)
                else:
                    (ADAPTER / ".installed").unlink(missing_ok=True)
                    result = subprocess.run(
                        command, cwd=ADAPTER, capture_output=True, text=True, timeout=SETUP_TIMEOUT
                    )
                    if result.returncode:
                        raise LifecycleError(
                            f"Insights setup failed (npm ci exit {result.returncode}). "
                            f"Setup needs network access to the npm registry and a writable "
                            f"package directory. When available, run: {remedy}\n"
                            f"{result.stderr.strip()}"
                        )
                    if not _dependencies_match(ADAPTER):
                        raise LifecycleError(
                            f"Installed dependencies do not match the lockfile. Run: {remedy}"
                        )
                    _stamp(ADAPTER)
        return {"ready": True, "path": str(ADAPTER), "lockfileSha256": _lock_digest(ADAPTER)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"Cannot set up insights: {exc}. Run: {remedy}") from exc


def _describe(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device"
    return "not a regular file"


def _trace_path(trace: str) -> Path:
    """Resolve --trace, refusing a path Node could only block on."""
    if not trace.strip():
        raise UsageError("insights --trace needs a file path; the value given is empty")
    path = Path(trace).expanduser().resolve()
    try:
        mode = path.stat().st_mode
    except OSError:
        # A missing or unreadable file is the adapter's to report, with the
        # engine's own message. Only a path that exists and cannot be read to
        # the end is refused here.
        return path
    if not stat.S_ISREG(mode):
        raise UsageError(
            f"insights --trace must name a regular trace file. {path} is {_describe(mode)}. "
            f"Analysis reads the whole file, so it would never finish."
        )
    return path


def analyze(
    trace: str, insight: str | None, ignore_engine_mismatch: bool
) -> tuple[dict[str, Any], int]:
    """Return the adapter document and exit code without opening a browser."""
    _require_adapter(ADAPTER)
    path = _trace_path(trace)
    if not _ready(ADAPTER):
        raise UsageError(
            f"Insights is not set up for this lockfile. Run: {SETUP_COMMAND}. "
            f"Setup needs network access to the npm registry; analysis afterwards does not."
        )
    node_stderr = ""
    try:
        with _locked(ADAPTER, fcntl.LOCK_SH):
            if not _ready(ADAPTER):
                raise UsageError(f"Insights is not set up. Run: {SETUP_COMMAND}")
            result = subprocess.run(
                [
                    _node(),
                    str(ADAPTER / "adapter.mjs"),
                    str(path),
                    insight or "",
                    str(ignore_engine_mismatch).lower(),
                ],
                capture_output=True,
                text=True,
                timeout=ANALYZE_TIMEOUT,
            )
        # The engine may log a recoverable Lantern error on stderr. Its exit
        # status and JSON document, not the presence of stderr, define success.
        # Once the run has failed, that stderr is the only account of why.
        node_stderr = result.stderr.strip()
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
    except subprocess.TimeoutExpired as exc:
        raise LifecycleError(
            f"Trace analysis timed out after {ANALYZE_TIMEOUT:.0f}s on {path}. "
            f"Analysis reads one local file and normally takes under a second."
        ) from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = f"\n{node_stderr[-2000:]}" if node_stderr else ""
        raise LifecycleError(
            f"Cannot analyze trace: {exc}. Repair the runtime with {SETUP_COMMAND}{detail}"
        ) from exc
