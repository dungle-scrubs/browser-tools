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
  launch route. ``--profile`` is recorded in the schema here; the persistent
  user-data-dir binding, profile exclusivity, and engine-aware liveness are
  #36's gate and are NOT enforced here. See ``launch`` for the seam.

The vendored liveness ladder, launcher, and supervisor are used unchanged.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from .core import instance_status as core_status
from .core import launcher as core_launcher
from .core import registry as core_registry
from .core.launcher import BrowserNotFoundError
from .core.registry import InstanceNotFoundError

DEFAULT_ENGINE = "chrome"
VALID_ENGINES = ("chrome", "camoufox")

#: Override the registry location from the environment (used by the CLI front
#: and tests). ``None`` keeps the vendored default (/tmp/chrome-agent/...).
REGISTRY_ENV_VAR = "BROWSER_TOOLS_REGISTRY"


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


def read_instances(registry_path: str | None = None) -> list[ExtendedInstance]:
    """Read every registry entry through the extended schema (no liveness)."""
    path = core_registry._resolve_path(registry_path)
    reg = core_registry._load_registry(path)
    out: list[ExtendedInstance] = []
    for name, entry in reg.items():
        engine, profile = read_engine_profile(entry)
        out.append(
            ExtendedInstance(
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
        )
    return out


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

    Chrome launches through the vendored launcher (fresh session dir, vendored
    liveness, supervisor, window marking). The ``engine`` and ``profile`` fields
    are written onto the entry afterward.

    Seam for #36: the camoufox engine, persistent per-profile user-data-dirs,
    profile exclusivity, and engine-aware liveness are not implemented here.
    ``--engine camoufox`` raises a clear error pointing at that work.
    """
    engine = (engine or DEFAULT_ENGINE).lower()
    if engine not in VALID_ENGINES:
        raise LifecycleError(f"Unknown engine '{engine}'. Choose one of: {', '.join(VALID_ENGINES)}")

    if engine == "camoufox":
        # #36 wires Camoufox launch + user-data-dir-hold liveness into the
        # registry. Until then the Camoufox engine is reached through the
        # launch_camoufox MCP tool, not this CLI verb.
        raise LifecycleError(
            "Launching the camoufox engine from the CLI is not wired yet (#36). "
            "Use the launch_camoufox MCP tool for anti-detect sessions."
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


def status(
    instance: str | None = None,
    registry_path: str | None = None,
) -> list[dict]:
    """Registry status enriched with liveness, page targets, engine, profile.

    Returns a JSON-ready list. With ``instance`` set, a single-element list for
    that instance (raises LifecycleError if it is unknown).
    """
    try:
        statuses = core_status.get_instance_status(
            instance_name=instance,
            registry_path=registry_path,
        )
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc

    by_name = {inst.name: inst for inst in read_instances(registry_path=registry_path)}
    out: list[dict] = []
    for st in statuses:
        ext = by_name.get(st.name)
        engine = ext.engine if ext else DEFAULT_ENGINE
        profile = ext.profile if ext else None
        out.append(
            {
                "name": st.name,
                "port": st.port,
                "alive": st.alive,
                "engine": engine,
                "profile": profile,
                "targets": [
                    {
                        "id": t.short_id,
                        "full_id": t.target_id,
                        "index": t.index,
                        "url": t.url,
                        "title": t.title,
                    }
                    for t in st.targets
                ],
            }
        )
    return out


def stop(
    instance: str | None = None,
    target: str | None = None,
    registry_path: str | None = None,
) -> str:
    """Stop a browser instance (or close one tab with ``target``).

    Delegates to the vendored registry ``stop`` (Browser.close with verified
    ownership, session-dir cleanup). Resolves an omitted instance via the
    single-instance rule.
    """
    if instance is None:
        instance = resolve_single_instance(registry_path=registry_path)
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

    Delegates to the vendored ``cleanup_sessions`` (registry cleanup plus the
    orphaned-session-dir sweep). Never touches live instances.
    """
    return core_launcher.cleanup_sessions(registry_path=registry_path)


def guide_text() -> str:
    """The bundled agent manual for the current (Phase 1) verb set."""
    return _GUIDE


_GUIDE = """\
browser-tools / bt -- registry-backed browser lifecycle

The tool tracks named browser instances in a registry. Each instance is one
running browser process, identified by a name derived from the working
directory. Liveness is process identity plus CDP port attribution, never PID
existence alone.

LIFECYCLE VERBS

  launch [--engine chrome|camoufox] [--profile NAME] [--channel NAME]
         [--headless] [--port PORT] [--fingerprint FILE] [--no-window-border]
         [-- BROWSER_ARGS]
      Launch a browser and register it. Prints the new instance as JSON.
      Vendored flags (--headless, --port, --fingerprint, --no-window-border,
      and everything after --) go straight to the launcher. Policy flags
      (--profile, --channel, --engine) are resolved to launcher parameters
      first. --engine camoufox is not wired from the CLI yet (see #36).

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

OUTPUT AND EXIT CODES

  Machine-readable output is JSON on stdout; diagnostics go to stderr.
  Exit 0 success, 1 operational failure (browser/CDP error, timeout), 2 usage
  error.
"""
