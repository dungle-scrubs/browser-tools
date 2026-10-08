from __future__ import annotations

import argparse
import select
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from . import lifecycle
from .core import registry as core_registry
from .core.utils import process_start_time
from .process_utils import process_running_state

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
    generation: str
    state: LeaseState
    owner: ProcessIdentity
    keeper: ProcessIdentity | None

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
        return cls(generation, state, ProcessIdentity.parse(value.get("owner")), keeper)


def load_registry(registry_path: str | None) -> tuple[str, dict[str, Any]]:
    """Read a parseable registry or refuse a lease operation with unknown state."""
    if not lifecycle.registry_is_parseable(registry_path):
        raise lifecycle.LifecycleError("Registry state unknown; lease operation refused")
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    return path, core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]


def _save(path: str, registry: dict[str, Any]) -> None:
    core_registry._save_registry(registry, path)  # pyright: ignore[reportPrivateUsage]


def status(value: Any) -> dict[str, Any]:
    try:
        lease = Lease.parse(value)
    except ValueError:
        return {"state": "unverified", "owner": "unverified", "keeper": "missing"}
    keeper = observe(lease.keeper) if lease.keeper else "gone"
    return {
        "state": lease.state,
        "generation": lease.generation,
        "owner": observe(lease.owner),
        "keeper": "running" if keeper == "alive" else "missing",
        "keeper_observation": keeper,
        "mode": value.get("mode"),
        **{key: value[key] for key in ("note", "close_error") if key in value},
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
        _save(path, registry)
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


def establish(name: str, owner: ProcessIdentity, headless: bool, registry_path: str | None) -> None:
    generation = uuid.uuid4().hex
    with lifecycle.registry_lock(registry_path):
        path, registry = load_registry(registry_path)
        registry[name]["lease"] = {
            "state": "auto",
            "generation": generation,
            "owner": owner.record(),
            "keeper": None,
            "mode": "headless" if headless else "headed",
        }
        _save(path, registry)
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "browser_tools.task_lease", name, generation, path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
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
            _save(path, current)
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                threading.Thread(target=_reap_failed_keeper, args=(proc,), daemon=True).start()
            _, current = load_registry(registry_path)
            current[name]["lease"]["state"] = "closing"
            _save(path, current)
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
                _save(path, registry)


def watch_once(name: str, generation: str, registry_path: str | None) -> bool:
    """Observe one owner; return whether this keeper should continue watching."""
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
        if observe(lease.owner) != "gone":
            return True
        entry["lease"]["state"] = "closing"
        _save(path, registry)
    try:
        lifecycle.stop(name, registry_path=registry_path, _lease_generation=generation)
    except lifecycle.LifecycleError as exc:
        record_failure(name, generation, str(exc), registry_path)
    return False


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
