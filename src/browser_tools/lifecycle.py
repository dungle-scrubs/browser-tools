"""Registry-backed browser lifecycle for the merged CLI front (RFC-01 Phase 1).

This is layer-2/3 code that sits between the CLI front (``cli.py``) and the
vendored core (``browser_tools.core``). It owns:

- The **extended registry schema**. The vendored registry entry carries six
  fields (``port``, ``pid``, ``browser_version``, ``user_data_dir``,
  ``launched``, ``pid_start``). Phase 1 adds two: ``engine`` ("chrome" or
  "camoufox") and ``profile`` (str or null). An entry written by the vendored
  code -- lacking both -- reads as ``engine="chrome"``, ``profile=null`` so a
  registry written before this change stays readable. The two fields are
  written by re-opening the registry after the vendored ``register`` /
  ``launch_browser`` stored the entry, never by modifying the verbatim
  ``registry.py`` (RFC-01, "Vendoring rules": adaptation at call sites only).

- Policy-flag resolution: ``--channel`` -> Chrome binary path, ``--engine`` ->
  launch route. ``--profile`` binds a persistent per-profile user-data-dir.

- Engine-aware liveness (#36). Chrome liveness is the vendored ladder (process
  identity + CDP port attribution). Camoufox exposes no Chrome debugging port,
  so its liveness is process identity + user-data-dir hold
  (``pid_holds_user_data_dir``). The registry entry's ``engine`` field selects
  the path.

- Profile exclusivity (#36). A profile is held by at most one live instance.
  ``launch --profile NAME`` cleans a stale singleton lock, then fails if a live
  instance already holds the profile -- never a second browser on the same dir.
  A profile's user-data-dir persists across ``stop``; only unbound/ephemeral
  instances have their dir reaped.

- Camoufox launch (#36). ``launch --engine camoufox`` starts the detached
  ``camoufox_runner`` host process, registers it with ``engine="camoufox"``, and
  reports it live via the user-data-dir hold. The in-process ``CamoufoxSession``
  MCP tools are untouched.

- Registry corruption is not retirement (#36). An unparseable registry reads as
  ``unknown``: ``status`` reports it, ``stop`` refuses to signal, and
  ``cleanup`` deletes nothing and quarantines the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .core import instance_status as core_status
from .core import launcher as core_launcher
from .core import registry as core_registry
from .core.launcher import BrowserNotFoundError
from .core.registry import InstanceNotFoundError
from .core.utils import process_is_ours, process_start_time
from .process_utils import (
    clean_stale_singleton_lock,
    pid_holds_user_data_dir,
    terminate_process_and_wait,
)

DEFAULT_ENGINE = "chrome"
VALID_ENGINES = ("chrome", "camoufox")

#: Override the registry location from the environment (used by the CLI front
#: and tests). ``None`` keeps the vendored default (/tmp/chrome-agent/...).
REGISTRY_ENV_VAR = "BROWSER_TOOLS_REGISTRY"

#: Root for persistent per-profile user-data-dirs and ephemeral Camoufox
#: session dirs. It is deliberately OUTSIDE the vendored session root
#: (/tmp/chrome-agent) so the launch-time orphan sweep never reaps a profile.
PROFILES_ENV_VAR = "BROWSER_TOOLS_PROFILES_DIR"
DEFAULT_PROFILES_ROOT = "/tmp/browser-tools-profiles"


def profiles_root() -> Path:
    """Resolve the root directory that holds persistent profile dirs."""
    return Path(os.environ.get(PROFILES_ENV_VAR) or DEFAULT_PROFILES_ROOT)


def profile_user_data_dir(profile: str) -> Path:
    """Resolve the persistent user-data-dir bound to a named profile.

    The directory persists across ``stop``: it is the profile's identity, not a
    throwaway session. One profile maps to exactly one directory, which is what
    makes profile exclusivity a check the registry can answer.
    """
    return profiles_root() / profile


class LifecycleError(Exception):
    """An operational lifecycle failure (maps to CLI exit code 1)."""


@dataclass
class ExtendedInstance:
    """A registry entry read through the extended (engine/profile) schema."""

    name: str
    port: int
    pid: int
    browser_version: str
    user_data_dir: str
    launched: str | None
    pid_start: str | None
    engine: str
    profile: str | None


def registry_path_from_env() -> str | None:
    """Resolve the registry path override from the environment, if any."""
    return os.environ.get(REGISTRY_ENV_VAR) or None


# ---------------------------------------------------------------------------
# Extended schema (engine / profile) -- read and write at the call site
# ---------------------------------------------------------------------------


def read_engine_profile(entry: dict) -> tuple[str, str | None]:
    """Read (engine, profile) from a raw registry entry with defaults.

    An entry missing the fields (written by the vendored code) reads as
    ("chrome", None). A stored ``engine`` of None or "" also defaults to
    "chrome" so a half-written entry never yields an empty engine.
    """
    engine = entry.get("engine") or DEFAULT_ENGINE
    profile = entry.get("profile")
    return engine, profile


def annotate_entry(
    name: str,
    *,
    engine: str,
    profile: str | None,
    registry_path: str | None = None,
) -> None:
    """Write the extended (engine/profile) fields onto a stored registry entry.

    Runs after the vendored ``register`` created the entry. Uses the registry's
    own atomic load/save helpers so the file layout stays identical; this is a
    call-site augmentation, not a modification of the verbatim registry module.
    Idempotent and a no-op if the entry is gone (raced with stop/cleanup).
    """
    path = core_registry._resolve_path(registry_path)
    reg = core_registry._load_registry(path)
    entry = reg.get(name)
    if entry is None:
        return
    entry["engine"] = engine
    entry["profile"] = profile
    core_registry._save_registry(reg, path)


def _entry_to_ext(name: str, entry: dict) -> ExtendedInstance:
    """Build an ``ExtendedInstance`` from a raw registry entry."""
    engine, profile = read_engine_profile(entry)
    return ExtendedInstance(
        name=name,
        port=entry.get("port", 0),
        pid=entry.get("pid", 0),
        browser_version=entry.get("browser_version", ""),
        user_data_dir=entry.get("user_data_dir", ""),
        launched=entry.get("launched"),
        pid_start=entry.get("pid_start"),
        engine=engine,
        profile=profile,
    )


def read_instances(registry_path: str | None = None) -> list[ExtendedInstance]:
    """Read every registry entry through the extended schema (no liveness)."""
    path = core_registry._resolve_path(registry_path)
    reg = core_registry._load_registry(path)
    return [_entry_to_ext(name, entry) for name, entry in reg.items()]


# ---------------------------------------------------------------------------
# Engine-aware liveness
# ---------------------------------------------------------------------------


def _camoufox_is_alive(pid: int, pid_start: str | None, user_data_dir: str) -> bool:
    """Liveness for a Camoufox instance: process identity + user-data-dir hold.

    Camoufox exposes no Chrome debugging port, so the vendored port-attribution
    ladder does not apply. Instead the recorded PID must be a live process of
    ours (start-time token guards PID reuse) AND its command line must still
    reference the recorded user-data-dir (``pid_holds_user_data_dir``). The two
    together mean a reused PID after reboot or namespace change never reads as a
    false "alive": either the start-time token mismatches, or the recycled
    process does not hold this profile dir.
    """
    if not user_data_dir:
        return False
    if not process_is_ours(pid=pid, expected_start=pid_start):
        return False
    return pid_holds_user_data_dir(pid, Path(user_data_dir))


def instance_is_live(inst: ExtendedInstance) -> bool:
    """Whether an instance is live, dispatching on its ``engine`` field.

    Chrome: the vendored ladder (process identity + CDP port attribution),
    unchanged. Camoufox: process identity + user-data-dir hold.
    """
    if inst.engine == "camoufox":
        return _camoufox_is_alive(inst.pid, inst.pid_start, inst.user_data_dir)
    return core_registry._instance_is_alive(
        inst.pid,
        inst.port,
        pid_start=inst.pid_start,
        user_data_dir=inst.user_data_dir,
    )


# ---------------------------------------------------------------------------
# Profile exclusivity
# ---------------------------------------------------------------------------


def find_profile_holder(
    profile: str,
    registry_path: str | None = None,
) -> str | None:
    """Name the live instance holding ``profile``, or None if it is free.

    A profile is held by at most one live instance. This is the check
    ``launch --profile`` runs to refuse a second browser on the same
    user-data-dir. Liveness is engine-aware, so a dead holder does not block a
    relaunch.
    """
    for inst in read_instances(registry_path=registry_path):
        if inst.profile == profile and instance_is_live(inst):
            return inst.name
    return None


# ---------------------------------------------------------------------------
# Registry corruption (unknown vs retired)
# ---------------------------------------------------------------------------


def registry_is_parseable(registry_path: str | None = None) -> bool:
    """Whether the registry file parses to a JSON object.

    A missing file is parseable (it reads as an empty registry). An
    unparseable file is the ``unknown`` state: nothing may be signalled or
    deleted on its basis (RFC-01, "Registry corruption is not retirement").
    """
    path = core_registry._resolve_path(registry_path)
    if not os.path.exists(path):
        return True
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict)


def _quarantine_registry(registry_path: str | None = None) -> str | None:
    """Move a corrupt registry file aside so a later run starts clean.

    Returns the quarantine path, or None when nothing was moved.
    """
    path = core_registry._resolve_path(registry_path)
    if not os.path.exists(path):
        return None
    quarantine = f"{path}.corrupt-{int(time.time())}"
    try:
        os.rename(path, quarantine)
    except OSError:
        return None
    return quarantine


# ---------------------------------------------------------------------------
# Policy-flag resolution
# ---------------------------------------------------------------------------


def _channel_candidates(channel: str) -> list[str]:
    """Platform-specific Chrome binary paths for a named release channel."""
    channel = channel.lower()
    if sys.platform == "darwin":
        by_channel = {
            "stable": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
            "beta": ["/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"],
            "dev": ["/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev"],
            "canary": [
                "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
            ],
        }
        return by_channel.get(channel, [])
    if sys.platform == "linux":
        by_channel = {
            "stable": ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"],
            "beta": ["/usr/bin/google-chrome-beta"],
            "dev": ["/usr/bin/google-chrome-unstable"],
            "canary": ["/usr/bin/google-chrome-canary"],
        }
        return by_channel.get(channel, [])
    return []


def resolve_channel_binary(channel: str | None) -> str | None:
    """Resolve a ``--channel`` value to an installed Chrome binary path.

    Returns None when ``channel`` is None (let the launcher auto-detect).
    Raises LifecycleError when a channel was named but no matching binary is
    installed, listing the paths searched.
    """
    if not channel:
        return None
    candidates = _channel_candidates(channel)
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    searched = "\n  ".join(candidates) if candidates else "(no known paths for this platform)"
    raise LifecycleError(
        f"Chrome '{channel}' channel not found. Searched:\n  {searched}"
    )


# ---------------------------------------------------------------------------
# Instance-name resolution (leading [INSTANCE] grammar)
# ---------------------------------------------------------------------------


def resolve_single_instance(registry_path: str | None = None) -> str:
    """Resolve the implied instance when none was named on the command line.

    Per RFC-01 "CLI surface": INSTANCE may be omitted only when exactly one
    instance is registered. Zero or many registered instances is an error that
    names the candidates rather than guessing.
    """
    names = [inst.name for inst in read_instances(registry_path=registry_path)]
    if len(names) == 1:
        return names[0]
    if not names:
        raise LifecycleError("No browser instances are registered. Launch one with: bt launch")
    listing = ", ".join(sorted(names))
    raise LifecycleError(
        f"Multiple instances are running; name one explicitly. Available: {listing}"
    )


def looks_like_domain_method(token: str) -> bool:
    """Whether a bare token reads as a ``Domain.method`` rather than a name.

    The disambiguation rule (RFC-01 "Instance names"): a bare token is an
    instance name if the registry knows it, else a ``Domain.method``. This
    helper reports the shape test only; callers combine it with a registry
    lookup. The raw-protocol passthrough verb that consumes it lands in a later
    ticket, so this is exposed for that seam.
    """
    return "." in token and token[:1].isalpha()


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


def launch(
    *,
    engine: str = DEFAULT_ENGINE,
    profile: str | None = None,
    channel: str | None = None,
    headless: bool = False,
    port: int | None = None,
    fingerprint: str | None = None,
    window_border: bool = True,
    browser_args: list[str] | None = None,
    registry_path: str | None = None,
) -> ExtendedInstance:
    """Launch a browser instance and record it with the extended schema.

    Chrome launches through the vendored launcher; Camoufox through the detached
    ``camoufox_runner`` host process. When ``--profile`` is given the instance is
    bound to a persistent user-data-dir and the launch enforces exclusivity: a
    stale singleton lock from a dead process is cleaned first, then a live holder
    of the profile fails the launch (exit 1) naming the holder -- never a second
    browser on the same dir, never a steal.
    """
    engine = (engine or DEFAULT_ENGINE).lower()
    if engine not in VALID_ENGINES:
        raise LifecycleError(f"Unknown engine '{engine}'. Choose one of: {', '.join(VALID_ENGINES)}")

    # Resolve the user-data-dir. Profile-bound instances get the persistent
    # per-profile dir; an unbound Camoufox instance still needs a dir for its
    # liveness hold, so it gets a throwaway one that stop/cleanup reaps.
    if profile is not None:
        user_data_dir = profile_user_data_dir(profile)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        # Clean stale singleton locks (dead holder) BEFORE the exclusivity check
        # so a crashed previous run does not wedge the profile forever.
        clean_stale_singleton_lock(user_data_dir)
        holder = find_profile_holder(profile, registry_path=registry_path)
        if holder is not None:
            raise LifecycleError(
                f"Profile '{profile}' is already held by live instance '{holder}'. "
                f"Stop it first, or launch a different profile."
            )
    elif engine == "camoufox":
        base = profiles_root() / ".ephemeral"
        base.mkdir(parents=True, exist_ok=True)
        user_data_dir = Path(tempfile.mkdtemp(prefix="camoufox-", dir=str(base)))
    else:
        user_data_dir = None

    if engine == "camoufox":
        return _launch_camoufox(
            profile=profile,
            headless=headless,
            user_data_dir=user_data_dir,  # never None on this path
            registry_path=registry_path,
        )

    binary = resolve_channel_binary(channel)

    try:
        info = asyncio.run(
            core_launcher.launch_browser(
                port_override=port,
                fingerprint=fingerprint,
                headless=headless,
                working_dir=os.getcwd(),
                registry_path=registry_path,
                extra_args=browser_args or None,
                window_border=window_border,
                binary=binary,
                user_data_dir=str(user_data_dir) if user_data_dir is not None else None,
            )
        )
    except BrowserNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc
    except (RuntimeError, TimeoutError, OSError) as exc:
        raise LifecycleError(f"Launch failed: {exc}") from exc

    annotate_entry(
        info.name,
        engine=engine,
        profile=profile,
        registry_path=registry_path,
    )

    return ExtendedInstance(
        name=info.name,
        port=info.port,
        pid=info.pid,
        browser_version=info.browser_version,
        user_data_dir=info.user_data_dir,
        launched=None,
        pid_start=info.pid_start,
        engine=engine,
        profile=profile,
    )


#: Seconds to wait for the detached Camoufox runner to report readiness.
_CAMOUFOX_READY_TIMEOUT = 45.0


def _spawn_camoufox_process(
    user_data_dir: Path,
    headless: bool,
) -> tuple[int, str | None]:
    """Spawn the detached Camoufox host process and wait for readiness.

    Returns ``(pid, pid_start)``. The ``--user-data-dir=<dir>`` flag is carried
    on the runner's own argv so ``pid_holds_user_data_dir`` attributes the hold
    to this PID. Raises LifecycleError if the runner dies or never signals
    readiness. Isolated behind a seam so tests substitute it without launching a
    real browser.
    """
    from . import camoufox_runner

    ready_file = user_data_dir / camoufox_runner.READY_SENTINEL
    if ready_file.exists():
        ready_file.unlink()

    args = [
        sys.executable,
        "-m",
        "browser_tools.camoufox_runner",
        f"--user-data-dir={user_data_dir}",
    ]
    if headless:
        args.append("--headless")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    pid_start = process_start_time(pid=proc.pid)

    deadline = time.monotonic() + _CAMOUFOX_READY_TIMEOUT
    while time.monotonic() < deadline:
        if ready_file.exists():
            return proc.pid, pid_start
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise LifecycleError(
                f"Camoufox runner exited before readiness (code {proc.returncode}). "
                f"stderr: {stderr[:500]}"
            )
        time.sleep(0.2)

    proc.kill()
    raise LifecycleError("Camoufox did not become ready within the timeout")


def _launch_camoufox(
    *,
    profile: str | None,
    headless: bool,
    user_data_dir: Path,
    registry_path: str | None,
) -> ExtendedInstance:
    """Launch Camoufox as a detached instance and register it (engine=camoufox).

    Liveness is process identity + user-data-dir hold, so the recorded PID is
    the long-lived runner (carrying ``--user-data-dir=`` on its argv), not a
    transient launcher. The vendored ``register`` allocates a name/port as for
    any instance; the port is unused by Camoufox but keeps the schema uniform.
    """
    pid, pid_start = _spawn_camoufox_process(user_data_dir, headless)

    info = core_registry.register(
        working_dir=os.getcwd(),
        pid=pid,
        browser_version="camoufox",
        user_data_dir=str(user_data_dir),
        registry_path=registry_path,
        pid_start=pid_start,
    )
    annotate_entry(
        info.name,
        engine="camoufox",
        profile=profile,
        registry_path=registry_path,
    )
    return ExtendedInstance(
        name=info.name,
        port=info.port,
        pid=info.pid,
        browser_version=info.browser_version,
        user_data_dir=str(user_data_dir),
        launched=None,
        pid_start=pid_start,
        engine="camoufox",
        profile=profile,
    )


def status(
    instance: str | None = None,
    registry_path: str | None = None,
) -> list[dict]:
    """Registry status enriched with liveness, page targets, engine, profile.

    Liveness is engine-aware (Chrome via the vendored ladder, Camoufox via the
    user-data-dir hold). Page targets are enumerated only for live Chrome
    instances -- Camoufox has no Chrome debugging port.

    Corruption: an unparseable registry is the ``unknown`` state. It is reported
    as a single row ``{"status": "unknown", ...}`` rather than being misread as
    "no instances" (RFC-01, "Registry corruption is not retirement").
    """
    if not registry_is_parseable(registry_path):
        return [
            {
                "status": "unknown",
                "detail": "registry file is unparseable; instance state is unknown",
            }
        ]

    instances = read_instances(registry_path=registry_path)
    if instance is not None:
        instances = [inst for inst in instances if inst.name == instance]
        if not instances:
            available = [i.name for i in read_instances(registry_path=registry_path)]
            raise LifecycleError(
                str(InstanceNotFoundError(name=instance, available=available))
            )

    out: list[dict] = []
    for ext in instances:
        alive = instance_is_live(ext)
        targets = (
            core_status.query_targets(port=ext.port)
            if alive and ext.engine == "chrome"
            else []
        )
        out.append(
            {
                "name": ext.name,
                "port": ext.port,
                "alive": alive,
                "engine": ext.engine,
                "profile": ext.profile,
                "targets": [
                    {
                        "id": t.short_id,
                        "full_id": t.target_id,
                        "index": t.index,
                        "url": t.url,
                        "title": t.title,
                    }
                    for t in targets
                ],
            }
        )
    return out


def _remove_entry(
    name: str,
    registry_path: str | None,
    *,
    reap_dir: bool,
    user_data_dir: str,
) -> None:
    """Drop a registry entry, reaping its user-data-dir only when told to.

    Profile-bound instances keep their dir (it is the profile's identity);
    unbound/ephemeral instances have it reaped.
    """
    path = core_registry._resolve_path(registry_path)
    reg = core_registry._load_registry(path)
    reg.pop(name, None)
    core_registry._save_registry(reg, path)
    if reap_dir and user_data_dir and os.path.exists(user_data_dir):
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _terminate_verified(ext: ExtendedInstance) -> None:
    """Terminate an instance's process only after verifying we own it.

    Chrome: a best-effort CDP ``Browser.close`` when the recorded port is ours,
    then a verified SIGTERM. Camoufox: a verified SIGTERM to the runner, which
    closes the browser and releases the profile on exit. A PID that is not ours
    (recycled, or a namespace alias) is never signalled.
    """
    if ext.engine == "chrome":
        claimants = core_registry._cdp_port_claimants(port=ext.port)
        if (not claimants) or (ext.user_data_dir in claimants):
            _try_cdp_browser_close(ext.port)
    if process_is_ours(pid=ext.pid, expected_start=ext.pid_start):
        terminate_process_and_wait(ext.pid, timeout=5.0)


def _try_cdp_browser_close(port: int) -> None:
    """Best-effort graceful ``Browser.close`` over CDP (never raises)."""

    async def _close() -> None:
        from .core.cdp_client import CDPClient, get_ws_url

        browser_ws = get_ws_url(port=port, target_type="browser")
        async with CDPClient(ws_url=browser_ws) as cdp:
            await cdp.send(method="Browser.close")

    with contextlib.suppress(Exception):
        asyncio.run(_close())


def _close_tab(ext: ExtendedInstance, target: str) -> str:
    """Close a single tab via CDP, leaving the browser and profile alive."""

    async def _close() -> bool:
        from .core.cdp_client import CDPClient, get_ws_url

        browser_ws = get_ws_url(port=ext.port, target_type="browser")
        async with CDPClient(ws_url=browser_ws) as cdp:
            result = await cdp.send(
                method="Target.closeTarget", params={"targetId": target}
            )
            return bool(result.get("success", False))

    if asyncio.run(_close()):
        return f"Closed tab {target[:8]} in {ext.name}"
    return f"Failed to close tab {target[:8]} in {ext.name}"


def _stop_managed(
    ext: ExtendedInstance,
    target: str | None,
    registry_path: str | None,
) -> str:
    """Stop a Camoufox or profile-bound instance, preserving profile dirs.

    Profile-bound instances keep their user-data-dir across stop; unbound
    Camoufox instances have theirs reaped, mirroring the ephemeral-Chrome path.
    """
    if target is not None:
        if ext.engine != "chrome":
            raise LifecycleError(
                "Closing a single tab is only supported for the chrome engine."
            )
        if not instance_is_live(ext):
            raise LifecycleError(f"{ext.name} is not live; cannot close a tab.")
        return _close_tab(ext, target)

    alive = instance_is_live(ext)
    if alive:
        _terminate_verified(ext)

    preserve = ext.profile is not None
    _remove_entry(
        ext.name,
        registry_path,
        reap_dir=not preserve,
        user_data_dir=ext.user_data_dir,
    )

    verb = "Stopped" if alive else "cleaned up (was not live)"
    if ext.profile is not None:
        return f"{verb} {ext.name} (profile '{ext.profile}' preserved at {ext.user_data_dir})"
    return f"{verb} {ext.name}"


def stop(
    instance: str | None = None,
    target: str | None = None,
    registry_path: str | None = None,
) -> str:
    """Stop a browser instance (or close one tab with ``target``).

    Ephemeral Chrome delegates to the vendored registry ``stop`` (Browser.close
    with verified ownership, session-dir cleanup). Camoufox and profile-bound
    instances take an engine-aware path that preserves a profile's user-data-dir.

    Corruption: on an unparseable registry (``unknown``) this refuses to signal
    anything (RFC-01).
    """
    if not registry_is_parseable(registry_path):
        raise LifecycleError(
            "Registry file is unparseable (state unknown); refusing to signal "
            "or delete anything. Run 'cleanup' to quarantine the corrupt file."
        )

    if instance is None:
        instance = resolve_single_instance(registry_path=registry_path)

    by_name = {inst.name: inst for inst in read_instances(registry_path=registry_path)}
    ext = by_name.get(instance)
    if ext is None:
        raise LifecycleError(
            str(InstanceNotFoundError(name=instance, available=list(by_name)))
        )

    if ext.engine == "camoufox" or ext.profile is not None:
        return _stop_managed(ext, target, registry_path)

    try:
        return core_registry.stop(
            instance_name=instance,
            target_id=target,
            registry_path=registry_path,
        )
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc


def cleanup(registry_path: str | None = None) -> list[str]:
    """Remove stale registry entries and their session directories.

    Dead profile-bound instances are deregistered but keep their user-data-dir;
    everything else (ephemeral Chrome, unbound Camoufox, orphaned session dirs)
    is reaped by the vendored ``cleanup_sessions``. Live instances are never
    touched.

    Corruption: on an unparseable registry (``unknown``) nothing is deleted and
    the corrupt file is quarantined (RFC-01, "cleanup ... MAY move the corrupt
    file aside").
    """
    if not registry_is_parseable(registry_path):
        _quarantine_registry(registry_path)
        return []

    # Deregister dead PROFILE-BOUND instances first, keeping their dirs. Removing
    # the entry hides them from the vendored sweep, which would otherwise rmtree
    # the profile dir. Their dirs live outside the vendored session root, so the
    # orphan sweep never touches them either.
    path = core_registry._resolve_path(registry_path)
    reg = core_registry._load_registry(path)
    preserved_removed: list[str] = []
    for name, entry in list(reg.items()):
        ext = _entry_to_ext(name, entry)
        if ext.profile is not None and not instance_is_live(ext):
            del reg[name]
            preserved_removed.append(name)
    if preserved_removed:
        core_registry._save_registry(reg, path)

    removed = core_launcher.cleanup_sessions(registry_path=registry_path)
    return preserved_removed + removed


def guide_text() -> str:
    """The bundled agent manual for the current (Phase 1) verb set."""
    return _GUIDE


_GUIDE = """\
browser-tools / bt -- registry-backed browser lifecycle

The tool tracks named browser instances in a registry. Each instance is one
running browser process, identified by a name derived from the working
directory. Liveness is engine-aware: Chrome is process identity plus CDP port
attribution; Camoufox is process identity plus user-data-dir hold. Never PID
existence alone.

LIFECYCLE VERBS

  launch [--engine chrome|camoufox] [--profile NAME] [--channel NAME]
         [--headless] [--port PORT] [--fingerprint FILE] [--no-window-border]
         [-- BROWSER_ARGS]
      Launch a browser and register it. Prints the new instance as JSON.
      Vendored flags (--headless, --port, --fingerprint, --no-window-border,
      and everything after --) go straight to the launcher. Policy flags
      (--profile, --channel, --engine) are resolved to launcher parameters
      first. --engine camoufox starts an anti-detect Camoufox instance; a
      profile is held by at most one live instance at a time.

  status [INSTANCE]
      Show every registered instance with liveness, engine, profile, and page
      targets. With INSTANCE, only that one. JSON on stdout.

  stop [INSTANCE] [--target SPEC]
      Stop a browser (Browser.close, then session-dir cleanup), or close one
      tab with --target. INSTANCE may be omitted only when exactly one instance
      is running.

  cleanup
      Remove stale registry entries and orphaned session directories. Live
      instances are never touched.

  guide
      Print this manual.

RAW PROTOCOL

  [INSTANCE] Domain.method '{...json params...}' [--target SPEC]
      Send any CDP method the installed browser supports straight to it and
      print the JSON result. No curated tool needs to exist for the method.
      INSTANCE may be omitted only when exactly one instance is running.

  help [INSTANCE] [Domain.method]
      With a running instance, print the live CDP protocol schema read from
      that browser. Without one, print static usage. A bare leading token is
      resolved as an instance name if the registry knows it, else as a
      Domain.method.

OUTPUT AND EXIT CODES

  Machine-readable output is JSON on stdout; diagnostics go to stderr.
  Exit 0 success, 1 operational failure (browser/CDP error, timeout), 2 usage
  error.
"""
