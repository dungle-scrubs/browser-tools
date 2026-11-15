from __future__ import annotations

import argparse
import os
import select
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from . import idle, lifecycle
from .core import registry as core_registry
from .core.utils import process_start_time
from .process_utils import process_running_state
from .spawn_environment import detached_environment

Observation = Literal["alive", "gone", "unverified"]
LeaseState = Literal["auto", "closing", "retained", "close_failed"]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start: str

    @classmethod
    def parse(cls, value: Any) -> ProcessIdentity:
        if not isinstance(value, dict):
            raise ValueError("missing process identity")
        pid, start = value.get("pid"), value.get("start")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(start, str)
            or not start
        ):
            raise ValueError("invalid process identity")
        return cls(pid, start)

    def record(self) -> dict[str, Any]:
        return {"pid": self.pid, "start": self.start}


def observe(identity: ProcessIdentity) -> Observation:
    running = process_running_state(identity.pid)
    if running is False:
        return "gone"
    if running is None:
        return "unverified"
    actual = process_start_time(identity.pid)
    if actual is None:
        return "gone" if process_running_state(identity.pid) is False else "unverified"
    return "alive" if actual == identity.start else "gone"


#: Instances this process began activity for: the registry each lives in and
#: the lease generation it had. A process touches only the instances it
#: began, and only while the lease is still the one it began on.
_begun: dict[str, tuple[str | None, str]] = {}

#: This process's own identity, cached: the start-time token does not change
#: while the process lives, and reading it costs a process query.
_identity: ProcessIdentity | None = None


def capture_owner(pid: int) -> ProcessIdentity:
    if isinstance(pid, bool) or pid <= 0:
        raise lifecycle.LifecycleError("Owner must be a positive live process PID")
    start = process_start_time(pid)
    if start is None:
        raise lifecycle.LifecycleError(f"Owner {pid} identity is unverified; nothing launched")
    identity = ProcessIdentity(pid, start)
    if observe(identity) != "alive":
        raise lifecycle.LifecycleError(f"Owner {pid} is gone or unverified; nothing launched")
    return identity


@dataclass(frozen=True)
class Lease:
    """One instance's lease: who owns it, who watches it, and its idle state.

    A lease written by the previous release carries no idle fields. It parses
    as an owner-only lease with no idle expiry, so an instance launched before
    this change is never expired by a keeper that did not know about it.
    """

    generation: str
    state: LeaseState
    owner: ProcessIdentity | None
    keeper: ProcessIdentity | None
    idle_timeout: int | None = None
    last_activity: float | None = None
    operations: tuple[ProcessIdentity, ...] = ()
    close_reason: str | None = None

    @classmethod
    def parse(cls, value: Any) -> Lease:
        if not isinstance(value, dict):
            raise ValueError("invalid lease")
        generation, state = value.get("generation"), value.get("state")
        if not isinstance(generation, str) or not generation:
            raise ValueError("missing lease generation")
        if state not in ("auto", "closing", "retained", "close_failed"):
            raise ValueError("invalid lease state")
        keeper = ProcessIdentity.parse(value["keeper"]) if value.get("keeper") is not None else None
        owner_value = value.get("owner")
        owner = None if owner_value is None else ProcessIdentity.parse(owner_value)
        return cls(
            generation,
            state,
            owner,
            keeper,
            *_idle_fields(value),
        )


def _idle_fields(
    value: dict[str, Any],
) -> tuple[int | None, float | None, tuple[ProcessIdentity, ...], str | None]:
    """Read the idle fields of a lease record, tolerating their absence."""
    idle_timeout = value.get("idle_timeout")
    if idle_timeout is not None and (
        not isinstance(idle_timeout, int) or isinstance(idle_timeout, bool) or idle_timeout <= 0
    ):
        raise ValueError("invalid idle timeout")
    last_activity = value.get("last_activity")
    if last_activity is not None and (
        not isinstance(last_activity, (int, float)) or isinstance(last_activity, bool)
    ):
        raise ValueError("invalid last activity")
    operations_value = value.get("operations", [])
    if not isinstance(operations_value, list):
        raise ValueError("invalid operations")
    operations = tuple(ProcessIdentity.parse(item) for item in operations_value)
    close_reason = value.get("close_reason")
    if close_reason is not None and not isinstance(close_reason, str):
        raise ValueError("invalid close reason")
    activity = float(last_activity) if last_activity is not None else None
    return idle_timeout, activity, operations, close_reason


def load_registry(registry_path: str | None) -> tuple[str, dict[str, Any]]:
    """Read a parseable registry or refuse a lease operation with unknown state.

    Args:
        registry_path: The registry to read, or None for the default one.

    Returns:
        The resolved registry file and its parsed contents.

    Raises:
        LifecycleError: The registry cannot be parsed.
    """
    if not lifecycle.registry_is_parseable(registry_path):
        raise lifecycle.LifecycleError("Registry state unknown; lease operation refused")
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    return path, core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]


def save_registry(path: str, registry: dict[str, Any]) -> None:
    """Write a registry back to disk with the core's own atomic save.

    Args:
        path: The resolved registry file.
        registry: The whole registry to write.
    """
    core_registry._save_registry(registry, path)  # pyright: ignore[reportPrivateUsage]


def status(value: Any) -> dict[str, Any]:
    """Report one lease for ``bt status``.

    The idle fields are what makes expiry visible: the configured timeout,
    when the instance was last used, when it will be closed, how long that is
    from now, and how many running commands are protecting it. A lease with
    no owner reports ``"none"`` rather than an observation of a process that
    was never there.

    Args:
        value: The registry entry's raw ``lease`` record.

    Returns:
        The lease block of the status output. A record that does not parse
        reports ``unverified`` with a missing keeper.
    """
    try:
        lease = Lease.parse(value)
    except ValueError:
        return {"state": "unverified", "owner": "unverified", "keeper": "missing"}
    keeper = observe(lease.keeper) if lease.keeper else "gone"
    # A retained lease has no keeper enforcing a deadline, so it reports none.
    deadline = (
        None
        if lease.state == "retained"
        else idle.deadline_of(lease.last_activity, lease.idle_timeout)
    )
    remaining = None if deadline is None else max(0, int(deadline - time.time()))
    report = {
        "state": lease.state,
        "generation": lease.generation,
        "owner": observe(lease.owner) if lease.owner is not None else "none",
        "keeper": "running" if keeper == "alive" else "missing",
        "keeper_observation": keeper,
        "mode": value.get("mode"),
        "idle_timeout": lease.idle_timeout,
        "last_activity": None if lease.last_activity is None else idle.iso_utc(lease.last_activity),
        "idle_deadline": None if deadline is None else idle.iso_utc(deadline),
        "idle_remaining": remaining,
        "active_operations": len(lease.operations),
    }
    if lease.close_reason:
        report["close_reason"] = lease.close_reason
    report.update({key: value[key] for key in ("note", "close_error") if key in value})
    return report


def summary(name: str, registry_path: str | None) -> dict[str, Any]:
    """Report one instance's idle timeout and deadline, for launch output.

    Args:
        name: The instance to report.
        registry_path: The registry that holds it.

    Returns:
        ``idle_timeout`` and ``idle_deadline``, or an empty mapping when the
        instance has no readable lease.

    Raises:
        LifecycleError: The registry cannot be parsed.
    """
    _, registry = load_registry(registry_path)
    entry = registry.get(name)
    value = entry.get("lease") if entry else None
    try:
        lease = Lease.parse(value)
    except ValueError:
        return {}
    deadline = idle.deadline_of(lease.last_activity, lease.idle_timeout)
    return {
        "idle_timeout": lease.idle_timeout,
        "idle_deadline": None if deadline is None else idle.iso_utc(deadline),
    }


def retain(name: str, note: str | None, registry_path: str | None) -> dict[str, Any]:
    with lifecycle.registry_lock(registry_path):
        path, registry = load_registry(registry_path)
        entry = registry.get(name)
        if entry is None:
            raise lifecycle.LifecycleError(f"No instance named '{name}'")
        value = entry.get("lease")
        if value is None:
            return {"name": name, "retained": True, "mode": "manual"}
        try:
            lease = Lease.parse(value)
        except ValueError as exc:
            raise lifecycle.LifecycleError(f"Lease state unknown for {name}") from exc
        if lease.state in ("closing", "close_failed"):
            raise lifecycle.LifecycleError(
                f"Cannot retain {name}: close already claimed ({lease.state}); use bt stop"
            )
        value["state"] = "retained"
        if note is not None:
            value["note"] = note
        save_registry(path, registry)
        return {
            "name": name,
            "retained": True,
            "mode": value.get("mode"),
            **({"note": value["note"]} if "note" in value else {}),
        }


def _reap_failed_keeper(proc: subprocess.Popen[str]) -> None:
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def spawn_keeper(argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Start the keeper process.

    A seam, so a test can replace the keeper without touching
    ``subprocess`` itself: replacing the whole spawn is what a test that
    wants a different keeper does, and it leaves every other process the
    test starts alone.

    ``subprocess.Popen`` itself is looked up when the keeper starts, not
    when this module was imported. A test that watches how a detached
    process is started -- the environment it is given, the session it leaves
    behind -- has to see the keeper's spawn too, or it proves nothing about
    the keeper: the keeper is the process that outlives the command, so its
    start is the thing under test.
    """
    return subprocess.Popen(argv, **kwargs)


def establish(
    name: str,
    owner: ProcessIdentity | None,
    headless: bool,
    registry_path: str | None,
    idle_timeout: int = idle.DEFAULT_IDLE_TIMEOUT,
) -> None:
    """Give ``name`` a lease and start its keeper.

    Every managed launch gets one, owner or not: the owner is an extra
    cleanup rule, not what makes the lease exist. The keeper is the process
    that enforces the idle timeout, so a launch that cannot start one has no
    expiry at all, and that is a launch failure (exit 1) rather than a
    browser nobody will ever close.

    Args:
        name: The instance the launch just registered.
        owner: The process whose exit closes the instance, or None.
        headless: Whether the browser was launched headless.
        registry_path: The registry that holds the instance.
        idle_timeout: Seconds without activity before the keeper closes it.

    Raises:
        LifecycleError: The keeper did not start. The new browser was
            stopped, or the message says that stopping it failed too.
    """
    generation = uuid.uuid4().hex
    with lifecycle.registry_lock(registry_path):
        path, registry = load_registry(registry_path)
        registry[name]["lease"] = {
            "state": "auto",
            "generation": generation,
            "owner": None if owner is None else owner.record(),
            "keeper": None,
            "mode": "headless" if headless else "headed",
            "idle_timeout": idle_timeout,
            "last_activity": time.time(),
            "operations": [],
        }
        save_registry(path, registry)
        proc: subprocess.Popen[str] | None = None
        try:
            proc = spawn_keeper(
                [sys.executable, "-m", "browser_tools.task_lease", name, generation, path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=detached_environment(os.environ.copy()),
                start_new_session=True,
            )
            if (
                proc.stdout is None
                or not select.select([proc.stdout], [], [], 5.0)[0]
                or proc.stdout.readline().strip() != "ready"
            ):
                raise RuntimeError("keeper did not become ready")
            start = process_start_time(proc.pid)
            if start is None or observe(ProcessIdentity(proc.pid, start)) != "alive":
                raise RuntimeError("keeper identity is unverified")
            _, current = load_registry(registry_path)
            current[name]["lease"]["keeper"] = ProcessIdentity(proc.pid, start).record()
            save_registry(path, current)
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                threading.Thread(target=_reap_failed_keeper, args=(proc,), daemon=True).start()
            _, current = load_registry(registry_path)
            current[name]["lease"]["state"] = "closing"
            save_registry(path, current)
            try:
                lifecycle.stop(name, registry_path=registry_path, _lease_generation=generation)
            except lifecycle.LifecycleError as cleanup_error:
                record_failure(name, generation, str(cleanup_error), registry_path)
                raise lifecycle.LifecycleError(
                    f"Keeper setup failed: {exc}; browser cleanup failed: {cleanup_error}"
                ) from exc
            raise lifecycle.LifecycleError(
                f"Keeper setup failed: {exc}; new browser stopped"
            ) from exc
        finally:
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()


def record_failure(name: str, generation: str, error: str, registry_path: str | None) -> None:
    with lifecycle.registry_lock(registry_path):
        path, registry = load_registry(registry_path)
        entry = registry.get(name)
        if entry is not None and isinstance(entry.get("lease"), dict):
            value = entry["lease"]
            if value.get("generation") == generation and value.get("state") == "closing":
                value.update(state="close_failed", close_error=error)
                save_registry(path, registry)


def configure(name: str, idle_timeout: int, registry_path: str | None) -> dict[str, Any]:
    """Set one instance's idle timeout and restart its countdown.

    The whole read-modify-write happens under the registry lock, and so does
    the keeper's claim of ``closing``. That is what makes a race between an
    extension and an expiry have exactly one truthful outcome: either the
    extension lands first and the keeper sees a fresh deadline, or the keeper
    claims the close first and the extension is refused. An extension is
    never reported as done while the browser is closing.

    Args:
        name: The instance to configure.
        idle_timeout: The new timeout in seconds.
        registry_path: The registry that holds the instance.

    Returns:
        ``name``, ``idle_timeout``, ``idle_deadline`` and ``state``.

    Raises:
        LifecycleError: No such instance, no lease, a lease nothing enforces
            a deadline for (retained, or written by an earlier release), or
            a close that has already been claimed (CLI exit 1).
    """
    with lifecycle.registry_lock(registry_path):
        path, registry = load_registry(registry_path)
        entry = registry.get(name)
        if entry is None:
            raise lifecycle.LifecycleError(f"No instance named '{name}'")
        value = entry.get("lease")
        if not isinstance(value, dict):
            raise lifecycle.LifecycleError(
                f"{name} is not a managed lease; nothing to configure"
            )
        try:
            lease = Lease.parse(value)
        except ValueError as exc:
            raise lifecycle.LifecycleError(f"Lease state unknown for {name}") from exc
        if lease.state in ("closing", "close_failed"):
            raise lifecycle.LifecycleError(
                f"Cannot configure {name}: close already claimed ({lease.state}); use bt stop"
            )
        if lease.state == "retained":
            # A retained instance has no keeper watching it, so a deadline
            # reported here would be one that nothing enforces.
            raise lifecycle.LifecycleError(
                f"Cannot configure {name}: it is retained and does not idle-expire"
            )
        if lease.idle_timeout is None:
            # A lease from an earlier release is watched by that release's
            # keeper, which knows nothing about idle expiry.
            raise lifecycle.LifecycleError(
                f"Cannot configure {name}: it was launched by an earlier release and "
                "does not idle-expire; stop it and launch it again"
            )
        value["idle_timeout"] = idle_timeout
        value["last_activity"] = time.time()
        save_registry(path, registry)
        deadline = idle.deadline_of(value["last_activity"], idle_timeout)
        return {
            "name": name,
            "idle_timeout": idle_timeout,
            "idle_deadline": None if deadline is None else idle.iso_utc(deadline),
            "state": lease.state,
        }


def _browser_observation(entry: dict[str, Any]) -> Observation:
    """Whether the instance's own browser process is still there."""
    pid, start = entry.get("pid"), entry.get("pid_start")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(start, str) or not start:
        return "unverified"
    try:
        return observe(ProcessIdentity(pid, start))
    except ValueError:
        return "unverified"


def _browser_is_live(name: str, entry: dict[str, Any]) -> bool:
    """Whether the browser still runs although its recorded process is gone.

    A wrapper install records the wrapper's PID, which exits while the real
    browser keeps the debug port. The registry's own liveness ladder knows
    that case, so the keeper asks it before it stops watching.
    """
    instance = lifecycle._entry_to_ext(name, entry)  # pyright: ignore[reportPrivateUsage]
    return lifecycle.instance_is_live(instance)


def watch_once(name: str, generation: str, registry_path: str | None) -> bool:
    """Run one keeper tick; return whether this keeper should keep watching.

    The tick is one read-modify-write under the registry lock, in this order:

    1. the entry, the lease or the generation is gone, or the lease is not
       ``auto`` (a retained instance has no keeper, as before) -- stop;
    2. the browser is gone -- stop without writing, so the supervisor or
       ``bt cleanup`` retires the entry;
    3. the owner is gone -- claim ``closing`` with reason ``owner`` and stop;
    4. prune operations whose process is gone, refreshing the countdown when
       any were pruned (a crashed command loses its protection);
    5. any operation left -- keep watching;
    6. the deadline has passed -- claim ``closing`` with reason ``idle`` and
       stop.

    Args:
        name: The instance this keeper watches.
        generation: The lease generation this keeper was started for.
        registry_path: The registry that holds the instance.

    Returns:
        True while the keeper should keep watching.

    Raises:
        LifecycleError: The registry lock timed out or the registry cannot
            be parsed; the caller decides whether to try again.
    """
    with lifecycle.registry_lock(registry_path):
        path, registry = load_registry(registry_path)
        entry = registry.get(name)
        if entry is None:
            return False
        try:
            lease = Lease.parse(entry.get("lease"))
        except ValueError:
            return False
        if lease.generation != generation or lease.state != "auto":
            return False
        if _browser_observation(entry) == "gone" and not _browser_is_live(name, entry):
            return False
        if lease.owner is not None and observe(lease.owner) == "gone":
            entry["lease"]["state"] = "closing"
            entry["lease"]["close_reason"] = "owner"
            save_registry(path, registry)
            claimed = True
        else:
            claimed = False
            remaining, pruned = _prune_operations(lease.operations)
            last_activity = lease.last_activity
            if pruned:
                # A crashed command loses its protection, and the countdown
                # starts again from now: the same fresh start a command that
                # finished normally gives.
                last_activity = time.time()
                entry["lease"]["operations"] = [operation.record() for operation in remaining]
                entry["lease"]["last_activity"] = last_activity
                save_registry(path, registry)
            if remaining:
                return True
            deadline = idle.deadline_of(last_activity, lease.idle_timeout)
            if deadline is None or time.time() < deadline:
                return True
            entry["lease"]["state"] = "closing"
            entry["lease"]["close_reason"] = "idle"
            save_registry(path, registry)
            claimed = True
    if claimed:
        _close(name, generation, registry_path)
    return False


def _prune_operations(
    operations: tuple[ProcessIdentity, ...],
) -> tuple[list[ProcessIdentity], bool]:
    """Split operations into the ones still running and whether any were pruned.

    An operation whose process cannot be verified keeps its protection for
    this tick: pruning an unverified identity would close a browser that a
    live command is still using.
    """
    remaining: list[ProcessIdentity] = []
    pruned = False
    for operation in operations:
        if observe(operation) == "gone":
            pruned = True
            continue
        remaining.append(operation)
    return remaining, pruned


def process_identity() -> ProcessIdentity | None:
    """Return this process's identity.

    The identity is the PID plus its process-start token, so a recycled PID
    is never mistaken for this process. It is cached once it is known. A
    read that fails is not cached, so a later call tries again.

    Returns:
        The identity, or None when the start token cannot be read.
    """
    global _identity
    if _identity is None:
        start = process_start_time(os.getpid())
        if not start:
            return None
        _identity = ProcessIdentity(os.getpid(), start)
    return _identity


def begin(name: str, registry_path: str | None) -> None:
    """Record that this process is about to drive the instance ``name``.

    Activity is the lifetime of a ``bt`` process that resolved a registered
    instance and is about to drive it. Idempotent per process and instance.
    Nothing happens for an entry without a lease, for a lease that is not
    ``auto``, for an unknown registry, or when the registry lock cannot be
    taken: the command still runs, it simply does not count as activity.

    Args:
        name: The registered instance this process is about to drive.
        registry_path: The registry that holds it.
    """
    if name in _begun:
        return
    identity = process_identity()
    if identity is None:
        return
    try:
        with lifecycle.registry_lock(registry_path):
            path, registry = load_registry(registry_path)
            value = (registry.get(name) or {}).get("lease")
            if not isinstance(value, dict):
                return
            try:
                lease = Lease.parse(value)
            except ValueError:
                return
            if lease.state != "auto":
                return
            records = [operation.record() for operation in lease.operations]
            if identity.record() not in records:
                value["operations"] = [*records, identity.record()]
                save_registry(path, registry)
    except (lifecycle.LifecycleError, OSError):
        return
    _begun[name] = (registry_path, lease.generation)


def finish() -> None:
    """Release this process's protection and refresh the idle countdown.

    Runs at the end of every command, successful or not. Only this process's
    own identity is removed, so overlapping commands each keep their own
    protection. The countdown starts again from the moment the work ended.
    A lease with another generation is a replacement instance that reused
    the name, and is left alone.
    """
    begun = dict(_begun)
    _begun.clear()
    if not begun:
        return
    identity = process_identity()
    if identity is None:
        return
    for name, (registry_path, generation) in begun.items():
        try:
            with lifecycle.registry_lock(registry_path):
                path, registry = load_registry(registry_path)
                value = (registry.get(name) or {}).get("lease")
                if not isinstance(value, dict):
                    continue
                try:
                    lease = Lease.parse(value)
                except ValueError:
                    continue
                if lease.generation != generation:
                    continue
                value["operations"] = [
                    operation.record() for operation in lease.operations if operation != identity
                ]
                value["last_activity"] = time.time()
                save_registry(path, registry)
        except (lifecycle.LifecycleError, OSError):
            continue


def _close(name: str, generation: str, registry_path: str | None) -> None:
    """Stop the instance after a close was claimed, recording a failed close."""
    try:
        lifecycle.stop(name, registry_path=registry_path, _lease_generation=generation)
    except lifecycle.LifecycleError as exc:
        record_failure(name, generation, str(exc), registry_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("generation")
    parser.add_argument("registry")
    args = parser.parse_args()
    print("ready", flush=True)
    while True:
        try:
            if not watch_once(args.name, args.generation, args.registry):
                return
        except lifecycle.RegistryLockTimeout:
            pass
        except (lifecycle.LifecycleError, OSError):
            return
        time.sleep(0.25)


if __name__ == "__main__":
    main()
