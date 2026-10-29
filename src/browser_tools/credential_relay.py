"""The private opchain relay: one credential, one inherited pipe, never stdout.

``bt login`` must get two secret values from 1Password into the page's form
fields without either value crossing the command line, stdout, stderr, an
environment variable, or a temp file. The mechanism is opchain's ``--value-fd``
relay (an existing documented opchain feature)::

    opchain IDENTITY --read --value-fd FD op read op://VAULT/ITEM/FIELD

opchain resolves the value privately and writes it to the inherited descriptor
``FD``; it prints nothing itself and captures its child's output privately.
The caller recipe, from opchain's own README, is followed exactly here:

- start opchain in its own process group (``process_group=0``, the
  ``Setpgid`` equivalent);
- on cancellation, and again after the process completes, ``SIGKILL`` the
  whole group *before* waiting for the inherited pipe's EOF, because the pipe
  write end may be inherited by descendants of the opchain process;
- close the descriptors on every path.

Cancellation is cooperative: the async caller passes a :class:`threading.Event`
that the blocking read checks before spawning and during every bounded wait,
so an asyncio cancellation of the awaiting coroutine becomes a real, prompt
kill of the opchain group rather than an orphaned thread.

Every failure is fail-closed with a fixed diagnostic. The read captures no
child stderr at all (``DEVNULL``) and reports one fixed sentence: child
diagnostic text is untrusted, and a fixed message needs no trust. The one
place opchain's stable failure line is used is the capability probe, which
accepts exactly one answer: exit code 1 with the ``descriptor-standard``
identifier opchain documents for the fd-0 grammar check - never any other
relay line.

The installed binary must actually have the feature: an opchain older than the
``--value-fd`` release is refused up front by that probe, and no fallback to
argv, stdout, or ``--pass-token`` exists anywhere in this module.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from .lifecycle import LifecycleError

#: Largest value accepted from the relay. A bigger one is overflow: discarded,
#: never truncated (a truncated secret is worse than a failed login).
MAX_VALUE_BYTES = 64 * 1024

#: Default bound on the whole opchain run per field.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0

#: Default bound on draining the pipe to EOF after the child completed and its
#: process group was killed. Descendants die with the group, so this is tight.
DEFAULT_READ_TIMEOUT_SECONDS = 5.0

#: Bound for the grammar-only capability probe.
PROBE_TIMEOUT_SECONDS = 10.0

#: Bound on reaping a killed relay process group. SIGKILL is immediate, so
#: this only guards a wedged kernel or zombie-holding parent.
_REAP_TIMEOUT_SECONDS = 5.0

#: How long the pipe reader's ``select`` waits before rechecking its stop
#: event; also the granularity at which a stopped reader settles.
_READER_POLL_SECONDS = 0.1

#: How long the owner waits for a stopped reader thread to end before giving
#: up on it (it is a daemon; the descriptor close ends it regardless).
_READER_STOP_JOIN_SECONDS = 1.0

#: The stable failure line new opchain binaries write on stderr, and the
#: anchored pattern opchain documents (src/op/relay.ts) for exactly this
#: purpose, deliberately distinct from any ``opchain debug:`` line.
RELAY_FAILURE_LINE = re.compile(r"^opchain: relay: ([a-z-]+(?::[a-z-]+)?)$", re.MULTILINE)

#: The one identifier that means "this binary has the relay grammar": the
#: fd-0 descriptor refusal every relay-capable opchain emits before identity,
#: config, or token resolution.
_PROBE_IDENTIFIER = "descriptor-standard"

#: The synthetic reference the capability probe sends. Grammar validation
#: only checks the ``op://`` shape, and the probe is refused at the descriptor
#: checks before identity, config, or token resolution, so this reference
#: names nothing real.
_PROBE_REFERENCE = "op://relay-support-probe/relay-support-probe/relay-support-probe"

#: Fixed diagnostic for a cancelled read, before or after spawn.
_CANCELLED_MESSAGE = "the credential relay read was cancelled; no value was used"


class RelayError(LifecycleError):
    """The relay failed. Fixed diagnostic; no child output; no value."""


class RelayMissingError(RelayError):
    """opchain is not installed or not on PATH."""


class RelayUnsupportedError(RelayError):
    """The installed opchain predates the --value-fd relay."""


@dataclass(frozen=True, repr=False)
class Secret:
    """One relayed credential value, held as raw bytes.

    ``repr`` is fixed text so the value can never ride into a log line, an
    assertion message, or an exception traceback by accident. ``text`` decodes
    strictly: a value that is not valid UTF-8 refuses rather than being
    mangled, and nothing is ever trimmed.
    """

    data: bytes

    def __repr__(self) -> str:  # Always fixed text, value excluded.
        return "<redacted relay value>"

    def text(self) -> str:
        """The value as strict UTF-8 text; byte-for-byte, no trimming."""
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError:
            raise RelayError(
                "the credential relay returned a value that is not valid UTF-8; "
                "it was discarded"
            ) from None


def find_opchain() -> str | None:
    """The opchain binary path, or None when it is not on PATH."""
    return shutil.which("opchain")


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the relay process group, tolerating an already-dead one."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Collect a killed process, bounded; a wedged child cannot hang us."""
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_REAP_TIMEOUT_SECONDS)


class _PipeReader:
    """A background reader that drains the relay pipe, bounded in bytes.

    Reading concurrently with the child is what makes the size bound real:
    a value larger than the pipe buffer would otherwise block the child's
    write forever, and a caller that only reads after ``wait`` could never
    observe the overflow, only a deadline. The reader stops early on
    overflow; the caller then kills the group, which unblocks or kills the
    child. EOF is only trusted after the process group is dead, so
    :meth:`finish` is called then, with its own bound.

    Every read is gated by a ``select`` with a short timeout and a stop
    event, so no reader can sit blocked in ``os.read`` forever when a
    descendant that escaped the process group still holds the write end:
    :meth:`stop` ends the thread within one poll interval. The thread never
    closes the descriptor; its single owner (:func:`read_reference`) does.
    """

    def __init__(self, fd: int, max_bytes: int) -> None:
        self._fd = fd
        self._max_bytes = max_bytes
        self._stop = threading.Event()
        self._chunks: list[bytes] = []
        self._total = 0
        self._overflow = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], _READER_POLL_SECONDS)
                if not ready:
                    continue
                chunk = os.read(self._fd, 32 * 1024)
            except (OSError, ValueError, InterruptedError) as exc:
                # Includes the owner closing the fd after giving up on us.
                self._error = exc
                return
            if not chunk:
                return  # EOF, only trusted once the group is dead.
            self._total += len(chunk)
            if self._total > self._max_bytes:
                self._overflow = True
                return
            self._chunks.append(chunk)

    def overflowed(self) -> bool:
        return self._overflow

    def finish(self, timeout: float) -> bytes:
        """Wait for the reader to end, then return the bytes or refuse."""
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._stop.set()
            raise RelayError(
                "the credential relay's descriptor did not close in time; "
                "the value was discarded"
            )
        if self._error is not None:
            raise RelayError(
                "the credential relay's descriptor could not be read; "
                "the value was discarded"
            )
        if self._overflow:
            raise RelayError(
                f"the credential value exceeded the relay's {self._max_bytes}-byte "
                "bound and was discarded"
            )
        return b"".join(self._chunks)

    def stop(self) -> None:
        """End the reader, bounded; partial output is never recovered."""
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(_READER_STOP_JOIN_SECONDS)


def _read_stderr_bounded(proc: subprocess.Popen[bytes], timeout: float) -> str:
    """Read the child's captured stderr to EOF, bounded, never surfaced raw.

    Used by the capability probe only, whose stderr holds at most one
    anchored failure line; this stays in memory just long enough to match it.
    """
    if proc.stderr is None:
        return ""
    fd = proc.stderr.fileno()
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if not ready:
            continue
        chunk = os.read(fd, 16 * 1024)
        if not chunk:
            return b"".join(chunks).decode("utf-8", "replace")
        chunks.append(chunk)
        if sum(len(c) for c in chunks) > 64 * 1024:
            return b"".join(chunks).decode("utf-8", "replace")


def relay_failure_identifier(stderr_text: str) -> str | None:
    """The stable relay identifier in ``stderr_text``, or None.

    Only a line matching opchain's anchored ``opchain: relay: <identifier>``
    shape counts; any other text in the capture is untrusted and dropped.
    """
    match = RELAY_FAILURE_LINE.search(stderr_text)
    return match.group(1) if match else None


def ensure_relay_supported(
    binary: str, identity: str, timeout: float = PROBE_TIMEOUT_SECONDS
) -> None:
    """Refuse an opchain that predates the ``--value-fd`` relay.

    The probe asks for fd 0, which every relay-capable binary refuses at its
    descriptor checks -- *before* identity resolution, config load, or token
    resolution -- writing exactly one ``opchain: relay: descriptor-standard``
    line and exiting 1. That exact answer, and only that answer, is the
    support signal: a different relay line, a different exit code, or any
    other output means the binary lacks the feature. No credential is
    touched either way. The probe child runs in its own process group and is
    killed and reaped on every path, timeout included.
    """
    argv = [
        binary,
        identity,
        "--read",
        "--value-fd",
        "0",
        "op",
        "read",
        _PROBE_REFERENCE,
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            process_group=0,
        )
    except FileNotFoundError as exc:
        raise RelayMissingError(
            "opchain is not installed or not on PATH. bt login reads credentials "
            "only through opchain's --value-fd relay; install opchain and retry"
        ) from exc
    except OSError:
        # An executable-format-invalid binary (ENOEXEC), a permission failure,
        # a missing interpreter: every other spawn OSError is one operational
        # fact -- opchain could not be started -- and its OSError text (which
        # can carry the provider's path) is never echoed.
        raise RelayError(
            "opchain could not be started; the credential relay requires a "
            "working opchain binary"
        ) from None
    try:
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RelayError("the opchain relay probe did not answer in time")
                try:
                    returncode = proc.wait(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # Interrupts must kill descendants too, before reading stderr.
            _kill_group(proc)
            _reap(proc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError("the opchain relay probe did not answer in time")
        stderr_text = _read_stderr_bounded(proc, remaining)
    finally:
        if proc.stderr is not None:
            with contextlib.suppress(OSError):
                proc.stderr.close()
    if returncode != 1 or relay_failure_identifier(stderr_text) != _PROBE_IDENTIFIER:
        raise RelayUnsupportedError(
            "the installed opchain does not support the --value-fd private relay. "
            "Update opchain to a release that documents --value-fd; bt login will "
            "not fall back to command lines, stdout, or environment variables"
        )


def read_reference(
    *,
    binary: str,
    identity: str,
    vault: str,
    item: str,
    field: str,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    cancel: threading.Event | None = None,
) -> Secret:
    """Read one ``op://VAULT/ITEM/FIELD`` through the private relay.

    The argv list is built here, never from caller-supplied free text; the
    reference's parts come from the validated policy. ``cancel`` is a
    cooperative cancellation signal, checked before the child spawns and
    during every bounded wait; when it fires the opchain group is killed and
    reaped and all partial output is discarded. Returns the value as bytes on
    successful child completion only; every other path raises
    :class:`RelayError` with a fixed diagnostic. The child's own stderr is
    never read: it is untrusted text, and the failure message is fixed.
    """
    if field not in ("username", "password"):
        raise RelayError("the credential relay only reads username and password")
    reference = f"op://{vault}/{item}/{field}"
    if cancel is not None and cancel.is_set():
        raise RelayError(_CANCELLED_MESSAGE)
    read_fd, write_fd = os.pipe()
    argv = [
        binary,
        identity,
        "--read",
        "--value-fd",
        str(write_fd),
        "op",
        "read",
        reference,
    ]
    proc: subprocess.Popen[bytes] | None = None
    reader = _PipeReader(read_fd, MAX_VALUE_BYTES)
    try:
        try:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(write_fd,),
                    process_group=0,
                )
            except FileNotFoundError as exc:
                raise RelayMissingError(
                    "opchain is not installed or not on PATH. bt login reads "
                    "credentials only through opchain's --value-fd relay; install "
                    "opchain and retry"
                ) from exc
            except OSError:
                # Same normalization as the probe: a spawn failure other than
                # a missing binary is one fixed operational fact, its OSError
                # text never echoed, its cause suppressed.
                raise RelayError(
                    "opchain could not be started; no value was used"
                ) from None
        finally:
            # The parent's copy is closed whether or not the spawn worked; the
            # child holds its own inherited copy.
            os.close(write_fd)
        reader.start()
        deadline = time.monotonic() + command_timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise RelayError(_CANCELLED_MESSAGE)
            if reader.overflowed():
                raise RelayError(
                    f"the credential value exceeded the relay's {MAX_VALUE_BYTES}-byte "
                    "bound and was discarded"
                )
            try:
                returncode = proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise RelayError(
                        "the credential relay did not finish within its deadline; "
                        "no value was used"
                    ) from None
        # The child completed. Kill its group again before trusting the pipe's
        # EOF: a descendant may still hold the write end open.
        _kill_group(proc)
        if cancel is not None and cancel.is_set():
            raise RelayError(_CANCELLED_MESSAGE)
        data = reader.finish(read_timeout)
        if returncode != 0:
            raise RelayError("the credential relay refused or failed to read the value")
        return Secret(data)
    except BaseException:
        # Cancellation, timeout, overflow, any failure: the group dies and is
        # reaped, and every partial byte dies with it.
        if proc is not None:
            _kill_group(proc)
            _reap(proc)
        raise
    finally:
        reader.stop()
        with contextlib.suppress(OSError):
            os.close(read_fd)


__all__ = [
    "MAX_VALUE_BYTES",
    "RelayError",
    "RelayMissingError",
    "RelayUnsupportedError",
    "Secret",
    "ensure_relay_supported",
    "find_opchain",
    "read_reference",
    "relay_failure_identifier",
]
