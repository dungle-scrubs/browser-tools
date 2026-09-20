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
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import endpoint as endpoint_module
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
from .usage import UsageError

if TYPE_CHECKING:
    from collections.abc import Generator

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


#: The character set a profile name may use: the one instance names use, from
#: ``core.registry._derive_base_name`` (lowercase letters, digits, dots and
#: hyphens), with a leading and trailing character that is alphanumeric. The
#: edge restriction is what rules out ``.``, ``..`` and a hidden name, so no
#: profile can collide with the root's own ``.locks`` and ``.ephemeral``.
PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


class ProfileNameError(UsageError):
    """A profile name outside the permitted character set (CLI exit code 2)."""


def validate_profile_name(name: str) -> str:
    """Return ``name`` if it is a usable profile name, else raise.

    Checked before any filesystem work, so a rejected name never creates,
    resolves or removes a path.

    Raises:
        ProfileNameError: The name is empty or uses a character outside
            :data:`PROFILE_NAME_PATTERN` (CLI exit 2).
    """
    if not name:
        raise ProfileNameError(
            "A profile name is required. Names use lowercase letters, digits, "
            "dots and hyphens, starting and ending with a letter or digit."
        )
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise ProfileNameError(
            f"'{name}' is not a usable profile name. Names use lowercase "
            "letters, digits, dots and hyphens, starting and ending with a "
            "letter or digit -- the same characters instance names use. "
            "List the profiles that exist with: bt profile list"
        )
    return name


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
    supervisor: str | None = None


def registry_path_from_env() -> str | None:
    """Resolve the registry path override from the environment, if any."""
    return os.environ.get(REGISTRY_ENV_VAR) or None


# ---------------------------------------------------------------------------
# Extended schema (engine / profile) -- read and write at the call site
# ---------------------------------------------------------------------------


def read_engine_profile(entry: dict[str, Any]) -> tuple[str, str | None]:
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
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
    entry = reg.get(name)
    if entry is None:
        return
    entry["engine"] = engine
    entry["profile"] = profile
    core_registry._save_registry(reg, path)  # pyright: ignore[reportPrivateUsage]


def record_supervisor(
    name: str,
    *,
    pid: int,
    pid_start: str | None,
    registry_path: str | None = None,
) -> None:
    """Record which process supervises ``name``, so a missing one is visible.

    Same shape and same reason as :func:`annotate_entry`: ``core/registry.py``
    is a verbatim vendored module, so extended fields are written here through
    the registry's own atomic load/save helpers.

    Recorded at spawn rather than by the supervisor itself. A supervisor that
    died -- the state #65 found -- must read as missing, not as an instance
    that never had one.

    Idempotent and a no-op if the entry is gone (raced with stop/cleanup).
    """
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
    entry = reg.get(name)
    if entry is None:
        return
    entry["supervisor_pid"] = pid
    entry["supervisor_pid_start"] = pid_start
    core_registry._save_registry(reg, path)  # pyright: ignore[reportPrivateUsage]


def supervisor_state(entry: dict[str, Any]) -> str | None:
    """Whether ``entry``'s supervisor is still running.

    Returns "running", "missing", or None when no supervisor was ever
    recorded. None is not a fault: a headless launch gets no supervisor by
    design, and entries written before supervisors were recorded have nothing
    to check.

    Without a supervisor a live instance carries no window marking and is
    never retired from the registry when its browser closes, and nothing else
    reports that (#65).
    """
    pid = entry.get("supervisor_pid")
    if not isinstance(pid, int):
        return None
    ours = process_is_ours(pid=pid, expected_start=entry.get("supervisor_pid_start"))
    return "running" if ours else "missing"


def _entry_to_ext(name: str, entry: dict[str, Any]) -> ExtendedInstance:
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
        supervisor=supervisor_state(entry),
    )


def read_instances(registry_path: str | None = None) -> list[ExtendedInstance]:
    """Read every registry entry through the extended schema (no liveness)."""
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
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
    return core_registry._instance_is_alive(  # pyright: ignore[reportPrivateUsage]
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
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
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
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
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


def profile_list(registry_path: str | None = None) -> list[dict[str, Any]]:
    """Report every named profile in the resolved root.

    Reads the root through :func:`profiles_root`, never a constant, so a
    changed ``BROWSER_TOOLS_PROFILES_DIR`` is honoured with no code change and
    the root move in #81 needs none here either.

    Needs no running instance. A root that does not exist, or holds nothing, is
    an empty list rather than a failure: no profiles is a true answer.

    Returns:
        One entry per profile, sorted by name, each with ``name``, ``path``,
        and ``held_by`` -- the live instance holding it, or None when it is
        free. A profile is held by at most one live instance.
    """
    root = profiles_root()
    if not root.is_dir():
        return []

    profiles: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda entry: entry.name):
        if child.is_symlink() or not child.is_dir():
            # A profile is a real directory the tool created under the root. A
            # symlink is not one, and listing it would name something `profile
            # delete` then refuses for leaving the root -- one rule, two verbs.
            continue
        if not PROFILE_NAME_PATTERN.fullmatch(child.name):
            # The root also holds .locks and .ephemeral, which the name pattern
            # excludes by construction: a profile name cannot begin with a dot.
            continue
        profiles.append(
            {
                "name": child.name,
                "path": str(child),
                "held_by": find_profile_holder(child.name, registry_path=registry_path),
            }
        )
    return profiles


def profile_delete(name: str, registry_path: str | None = None) -> dict[str, Any]:
    """Remove one profile directory, or refuse and say why.

    Three checks, in this order, before anything is removed:

    1. The name is validated against :data:`PROFILE_NAME_PATTERN` (exit 2).
    2. The resolved path is required to sit directly inside the resolved root
       (exit 2). Containment is decided on the resolved path, not the string,
       so a symlink planted in the root cannot carry a delete outside it.
    3. A live holder refuses the delete (exit 1), naming the instance and
       saying to stop it first. Removing a running browser's user-data-dir out
       from under it loses the session it is still writing.

    Returns:
        ``{"deleted": name, "path": str}`` for the directory that was removed.

    Raises:
        ProfileNameError: The name or its resolved path is not usable (exit 2).
        LifecycleError: No such profile, or a live instance holds it (exit 1).
    """
    validate_profile_name(name)

    root = profiles_root().resolve()
    target = (profiles_root() / name).resolve()
    if target.parent != root:
        raise ProfileNameError(
            f"'{name}' resolves to {target}, which is outside the profile root "
            f"{root}. Nothing was deleted. List the profiles that exist with: "
            "bt profile list"
        )

    if not target.is_dir():
        raise LifecycleError(
            f"No profile named '{name}' in {root}. List what is there with: "
            "bt profile list"
        )

    holder = find_profile_holder(name, registry_path=registry_path)
    if holder is not None:
        raise LifecycleError(
            f"Profile '{name}' is held by live instance '{holder}'. Stop it "
            f"first: bt stop {holder}"
        )

    shutil.rmtree(target)
    return {"deleted": name, "path": str(target)}


def instance_is_registered(name: str, registry_path: str | None = None) -> bool:
    """Whether the registry knows ``name`` as an instance.

    The one test behind the bare-token rule (RFC-01 "Instance names"): a bare
    leading token is an instance name when the registry knows it, else it is
    the verb or a ``Domain.method``.
    """
    return any(inst.name == name for inst in read_instances(registry_path=registry_path))


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


def resolve_cdp_port(
    instance: str | None,
    registry_path: str | None = None,
    endpoint: str | None = None,
) -> int:
    """Resolve the CDP port every browser-driving verb sends to.

    One resolver, two sources. With ``endpoint`` set the port comes from that
    URL and **the registry is never read or written**: an external browser has
    no registry entry, and that absence is what keeps ``stop`` and ``cleanup``
    away from the user's real profile directory (see ``endpoint``). Without it,
    ``instance`` resolves through the registry exactly as before, omittable
    when exactly one instance is registered.

    Raises:
        endpoint.EndpointUsageError: ``endpoint`` is malformed or not loopback
            (CLI exit 2).
        LifecycleError: The named instance is not registered (CLI exit 1).
    """
    if endpoint is not None:
        return endpoint_module.resolve_endpoint_port(endpoint)
    if instance is None:
        instance = resolve_single_instance(registry_path=registry_path)
    try:
        info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc
    return info.port


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

    # The name becomes a path under the profile root, so it is checked here
    # rather than only in `profile delete` (#98). Without this a launch could
    # create a profile no profile verb can name, or write outside the root.
    if profile is not None:
        validate_profile_name(profile)

    # Hold the profile's launch lock for the WHOLE sequence: stale-lock
    # cleanup, the holder lookup, the launch, the registration and the
    # annotation. Checking for a holder and then launching is not atomic, so
    # two concurrent launches could both observe no holder and race into the
    # registry (#95). A caller that waits here sees the registered winner.
    with contextlib.ExitStack() as stack:
        if profile is not None:
            stack.enter_context(profile_launch_lock(profile))
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
            # Guaranteed above: either `profile is not None` (user_data_dir is the
            # persistent per-profile dir) or `engine == "camoufox"` (user_data_dir
            # is the ephemeral tempdir). The only None-producing branch requires
            # both `profile is None` and `engine != "camoufox"`.
            assert user_data_dir is not None
            return _launch_camoufox(
                profile=profile,
                headless=headless,
                user_data_dir=user_data_dir,
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
) -> list[dict[str, Any]]:
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

    out: list[dict[str, Any]] = []
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
                # "running", "missing", or null when none was ever recorded
                # (a headless launch has no supervisor by design).
                "supervisor": ext.supervisor,
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
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
    reg.pop(name, None)
    core_registry._save_registry(reg, path)  # pyright: ignore[reportPrivateUsage]
    if reap_dir and user_data_dir and os.path.exists(user_data_dir):
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _is_under_profiles_root(user_data_dir: str) -> bool:
    """Whether a recorded path resolves inside the profile root.

    RFC-01 defines registry contents as untrusted input, so a deletion
    decision rests on the resolved path, not on a field an old, partial or
    malformed entry may be missing (#95). An entry pointing into the profile
    root is a profile whatever its ``profile`` field says.
    """
    if not user_data_dir:
        return False
    try:
        return Path(user_data_dir).resolve().is_relative_to(profiles_root().resolve())
    except OSError:
        return False


class ProfileLockBusy(LifecycleError):
    """Another process holds this profile's launch lock."""


@contextlib.contextmanager
def profile_launch_lock(
    profile: str,
    *,
    lock_root: str | None = None,
    timeout: float = 30.0,
) -> Generator[None]:
    """Hold an exclusive per-profile lock for the whole launch sequence.

    Profile exclusivity is a MUST: a profile is held by at most one live
    instance, and a second ``launch --profile NAME`` must fail naming the
    holder. The check and the registration were not atomic, so two concurrent
    launches could both observe no holder before either reached the registry
    (#95). Chrome's own singleton may stop the second browser from becoming
    usable, but that is not the specified behaviour and it names no holder.

    The lock spans stale-lock cleanup, the holder lookup, the launch, the
    registration and the profile annotation, so a racing caller waits and then
    sees the registered winner rather than an empty registry.

    Raises ``ProfileLockBusy`` when the lock is not acquired within ``timeout``.
    """
    root = Path(lock_root) if lock_root is not None else profiles_root() / ".locks"
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", profile)
    lock_path = root / f"{safe}.lock"

    deadline = time.monotonic() + timeout
    handle = open(lock_path, "a+")  # noqa: SIM115 - released in the finally below
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ProfileLockBusy(
                        f"Profile '{profile}' is being launched by another process. "
                        f"Waited {timeout:g}s for its launch to finish."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def retire_instance(
    *,
    instance_name: str,
    registry_path: str | None = None,
) -> bool:
    """Retire an instance whose browser has gone, keeping a bound profile.

    The lifecycle-owned replacement for the vendored ``deregister``. That
    function removes the entry and then deletes the recorded user-data dir
    unconditionally, which destroys a named profile when a headed browser is
    closed normally (#94). ``core/registry.py`` is verbatim vendored, so the
    defect is corrected here, at its call site.

    Profile-bound instances keep their directory: it is the profile's
    identity, not a throwaway session. Unbound instances have theirs reaped
    through the vendored ``_remove_session_dir``, whose retry loop is what
    makes the reap survive Chrome helpers that briefly outlive the socket.

    ``stop`` preserves a bound profile too, so with both paths preserving it
    the order they run in no longer matters and the race between them is
    closed by construction.

    Returns True when an entry was removed. Idempotent, so it stays safe to
    race with ``stop()`` and ``cleanup()``.
    """
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
    entry = reg.pop(instance_name, None)
    if entry is None:
        return False
    core_registry._save_registry(reg, path)  # pyright: ignore[reportPrivateUsage]

    _, profile = read_engine_profile(entry)
    session_dir = entry.get("user_data_dir")
    if profile is None and session_dir:
        core_registry._remove_session_dir(session_dir)  # pyright: ignore[reportPrivateUsage]
    return True


def _terminate_verified(ext: ExtendedInstance) -> None:
    """Terminate an instance's process only after verifying we own it.

    Chrome: a best-effort CDP ``Browser.close`` when the recorded port is ours,
    then a verified SIGTERM. Camoufox: a verified SIGTERM to the runner, which
    closes the browser and releases the profile on exit. A PID that is not ours
    (recycled, or a namespace alias) is never signalled.
    """
    if ext.engine == "chrome":
        claimants = core_registry._cdp_port_claimants(  # pyright: ignore[reportPrivateUsage]
            port=ext.port
        )
        if (not claimants) or (ext.user_data_dir in claimants):
            _try_cdp_browser_close(ext.port)
    if process_is_ours(pid=ext.pid, expected_start=ext.pid_start):
        terminate_process_and_wait(ext.pid, timeout=5.0)


def _try_cdp_browser_close(port: int) -> None:
    """Best-effort graceful ``Browser.close`` over CDP (never raises)."""

    async def _close() -> None:
        from .core.cdp_client import CDPClient, get_ws_url_async

        browser_ws = await get_ws_url_async(port=port, target_type="browser")
        async with CDPClient(ws_url=browser_ws) as cdp:
            await cdp.send(method="Browser.close")

    with contextlib.suppress(Exception):
        asyncio.run(_close())


def _resolve_tab_target(ext: ExtendedInstance, target_spec: str) -> str:
    """Resolve a ``--target`` spec to a complete page target ID.

    ``--target SPEC`` has one meaning across the CLI: a 1-based index into the
    page targets sorted by target ID, or a target ID prefix, read as an index
    only when every character is a digit. ``stop`` used to hand its argument
    straight to ``Target.closeTarget`` as a complete ID, so both other forms
    failed silently. Resolution goes through the shared selector so two code
    paths cannot disagree about which page ``--target 1`` names.

    Raises:
        LifecycleError: The spec matched no page, matched several, or the
            browser reported no page target at all.
    """
    from .cdp_client import resolve_page_target_id
    from .core.attach import AmbiguousTargetError, TargetNotFoundError

    try:
        target_id = resolve_page_target_id(f"http://127.0.0.1:{ext.port}", target_spec)
    except (AmbiguousTargetError, TargetNotFoundError) as exc:
        raise LifecycleError(str(exc)) from exc
    if target_id is None:
        raise LifecycleError(f"{ext.name} has no page target to close.")
    return target_id


def _close_tab(ext: ExtendedInstance, target_id: str) -> str:
    """Close one tab via CDP, leaving the browser and profile alive.

    ``target_id`` is a complete target ID, already resolved by
    :func:`_resolve_tab_target`.

    Raises:
        LifecycleError: ``Target.closeTarget`` reported ``success: false``. A
            close that did not happen is an operational failure, not a success
            envelope carrying a failure sentence.
    """

    async def _close() -> bool:
        from .core.cdp_client import CDPClient, get_ws_url_async

        browser_ws = await get_ws_url_async(port=ext.port, target_type="browser")
        async with CDPClient(ws_url=browser_ws) as cdp:
            result = await cdp.send(
                method="Target.closeTarget", params={"targetId": target_id}
            )
            return bool(result.get("success", False))

    if not asyncio.run(_close()):
        raise LifecycleError(f"Failed to close tab {target_id[:8]} in {ext.name}")
    return f"Closed tab {target_id[:8]} in {ext.name}"


def _stop_managed(
    ext: ExtendedInstance,
    registry_path: str | None,
) -> str:
    """Stop a Camoufox or profile-bound instance, preserving profile dirs.

    Profile-bound instances keep their user-data-dir across stop; unbound
    Camoufox instances have theirs reaped, mirroring the ephemeral-Chrome path.
    Closing a single tab is not this function's job; ``stop`` handles that for
    both engines before it reaches here.
    """
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

    ``target`` is a ``--target SPEC``: a 1-based index into the page targets
    sorted by target ID, or a target ID prefix, read as an index only when every
    character is a digit. A tab close takes one path for both engines and never
    reaches the vendored ``stop``, which reads the argument as a complete target
    ID and reports a refused close as a success.

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

    # A tab close is one path for both engines, and it does not reach the
    # vendored stop. That function has its own Target.closeTarget: it treats the
    # argument as a complete target ID and returns a failure sentence on
    # success: false. It is verbatim vendored, so the correction is here, at the
    # call site, by not routing tab closes through it.
    if target is not None:
        if ext.engine != "chrome":
            raise LifecycleError(
                "Closing a single tab is only supported for the chrome engine."
            )
        if not instance_is_live(ext):
            raise LifecycleError(f"{ext.name} is not live; cannot close a tab.")
        return _close_tab(ext, _resolve_tab_target(ext, target))

    if ext.engine == "camoufox" or ext.profile is not None:
        return _stop_managed(ext, registry_path)

    try:
        return core_registry.stop(
            instance_name=instance,
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
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
    preserved_removed: list[str] = []
    for name, entry in list(reg.items()):
        ext = _entry_to_ext(name, entry)
        bound = ext.profile is not None or _is_under_profiles_root(ext.user_data_dir)
        if bound and not instance_is_live(ext):
            del reg[name]
            preserved_removed.append(name)
    if preserved_removed:
        core_registry._save_registry(reg, path)  # pyright: ignore[reportPrivateUsage]

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

NAMING THE INSTANCE

  Every verb that drives a browser takes the instance ahead of the verb:

    bt web-01 snapshot
    bt web-01 frames select checkout
    bt web-01 Page.navigate '{"url": "https://..."}'

  A bare leading token is an instance name when the registry knows it, and
  the verb otherwise, so `bt frames select checkout` needs no escaping.
  INSTANCE may be omitted when exactly one instance is registered; with
  several, every verb names the candidates rather than guessing. Naming the
  instance twice is a usage error.

LIFECYCLE VERBS

  launch [--engine chrome|camoufox] [--profile NAME] [--channel NAME]
         [--headless] [--port PORT] [--fingerprint FILE] [--no-window-border]
         [-- BROWSER_ARGS]
      Launch a browser and register it. Prints the new instance as JSON.
      Vendored flags (--headless, --port, --fingerprint, --no-window-border,
      and everything after --) go straight to the launcher. Policy flags
      (--profile, --channel, --engine) are resolved to launcher parameters
      first. --engine camoufox starts an anti-detect Camoufox instance; a
      profile is held by at most one live instance at a time. launch takes
      no positional argument: the instance name is assigned by the registry,
      and any bare token before -- is a usage error (exit 2), because it
      would otherwise be handed to the browser along with every flag after
      it.

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

  profile list
      List every named profile in the profile root with its name, its path,
      and the live instance holding it (null when free). Needs no running
      instance; an empty root lists nothing.

  profile delete NAME
      Remove one profile directory, and its login state with it. This is the
      only way a profile goes away: cleanup never prunes one on age. A name
      outside the permitted character set, or one whose resolved path leaves
      the profile root, is a usage error (exit 2) and deletes nothing. A
      profile a live instance holds is refused (exit 1) naming the holder;
      stop it first.

      The profile root is $BROWSER_TOOLS_PROFILES_DIR, or
      /tmp/browser-tools-profiles.

  guide
      Print this manual.

CURATED VERBS

  snapshot [--target SPEC]
      The accessibility tree with a UID per node, for click and fill.

  click --uid UID [--target SPEC]
  fill --uid UID --text T [--target SPEC]
      Act on a node a snapshot named. A UID is valid for the document that
      produced it; after the page navigates, take a new snapshot.

  wait-idle [--timeout-ms MS] [--idle-ms MS]
  wait-stable [--timeout-ms MS] [--stable-ms MS]
      Wait for network idle, or for the DOM to stop changing.

  detect [--wait SECONDS | --no-wait]
      Run interstitial detection against the current page.

  console-list [--target SPEC | --url SUBSTRING] [--duration SECONDS]
  network-list [--target SPEC | --url SUBSTRING] [--duration SECONDS]
      Collect console messages, or network requests and responses, over a
      short attach window (default 2 seconds).

  frames list | frames select PATTERN | frames reset
      Inspect and select page frames. PATTERN is a frame URL substring.

  storage get [--key K]
      Read the selected frame's storage. --key is a frame URL pattern to
      select before reading, not a cookie or local-storage key.

  screenshot [--path FILE] [--target SPEC | --url SUBSTRING]
      Capture a full-page PNG. Without --path, the base64 data URI.

  screencast --dir DIR [--duration SECONDS] [--format FMT] [--max-frames N]
      Capture a screencast and write its frames plus a frames.json manifest
      to DIR, in one invocation. Capture ends at whichever comes first: the
      duration (default 5 seconds) or the frame cap (default 600). There is
      no separate start and stop: the frame buffer belongs to the process
      that captured it.

  window-border [on|off]
      Show, or persistently set, whether marked windows draw the colored
      border and corner badge over the page (default on). They cover the
      page's outer edge and top-left corner; 'off' removes them from every
      running browser within a second and keeps them off for later launches.
      The tab-title prefix stays either way. --no-window-border on launch
      turns off all marking for that one launch.

NEVER TAKE THE SCREEN

  A person is working on this machine. A browser window that comes to the
  front takes their keyboard focus and moves their window manager to it, and
  the next call takes it again. The tool keeps windows in the background:

  - launch opens its window in the background.
  - Target.activateTarget and Page.bringToFront are refused (exit 2). No task
    needs them.
  - Target.createTarget always opens in the background; background:false is
    refused (exit 2).
  - Input sent to a background tab (a tab that is not the selected tab of its
    window) fails (exit 1): Chrome drops it without an error.

  Input and screenshots reach the selected tab of a window even when the
  window is behind other windows or on another workspace. To work in a second
  page, open it in its own window and target it:

    bt Target.createTarget '{"url": "https://...", "newWindow": true}'
    bt Input.dispatchMouseEvent '{...}' --target <targetId>

  Or navigate the tab you already have with Page.navigate.

RAW PROTOCOL

  [INSTANCE] Domain.method '{...json params...}' [--target SPEC]
      Send any CDP method the installed browser supports straight to it and
      print the JSON result. No curated tool needs to exist for the method.
      INSTANCE may be omitted only when exactly one instance is running.
      Methods that raise the window are refused; see NEVER TAKE THE SCREEN.

  help [INSTANCE] [Domain.method]
      With a running instance, print the live CDP protocol schema read from
      that browser. Without one, print static usage. A bare leading token is
      resolved as an instance name if the registry knows it, else as a
      Domain.method.

EXTERNAL BROWSERS

  --endpoint URL
      Drive a browser this tool did not launch: one the person already has
      open and logged in. It goes on the browser-driving verbs, per
      invocation, and nothing is written to the registry.

        bt snapshot --endpoint http://127.0.0.1:9222
        bt Page.navigate '{"url": "https://..."}' --endpoint http://127.0.0.1:9222

      Start the browser yourself with --remote-debugging-port=PORT, then pass
      that port. There is no instance name, so every invocation carries the
      flag, and status does not list the browser -- status reports the
      registry, and an external browser has no entry in it. That absence is
      deliberate: stop and cleanup act on registry entries, and an external
      browser's user-data-dir is the person's real Chrome profile directory.

      launch, status, stop, cleanup, guide and profile reject --endpoint
      (exit 2), as does --endpoint beside --profile: a profile is a
      launch-time identity and --endpoint launches nothing.

      Loopback only. 127.0.0.1 and ::1 are accepted and nothing else, not
      even localhost. A CDP endpoint is unauthenticated full control of a
      logged-in browser, so a remote one is a takeover channel. Reach a
      browser on another machine by forwarding it:

        ssh -L 9222:127.0.0.1:9222 <host>

      Browser.close and Browser.crash are refused over --endpoint (exit 2):
      they would end every window and tab the person had open. Everything
      else passes, Target.closeTarget for a single tab included. The refusals
      under NEVER TAKE THE SCREEN apply unchanged.

OUTPUT AND EXIT CODES

  Machine-readable output is JSON on stdout; diagnostics go to stderr.
  Exit 0 success, 1 operational failure (browser/CDP error, timeout), 2 usage
  error.
"""
