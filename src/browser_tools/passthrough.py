"""Raw CDP passthrough and live-schema help for the merged CLI front (RFC-01 #37).

New layer-4 code, alongside ``cli.py`` and ``lifecycle.py``. It owns:

- **Passthrough dispatch.** ``[INSTANCE] Domain.method '{...}' [--target SPEC]``
  sends any CDP method the installed browser supports straight to it and
  returns the raw JSON result. It opens the browser-level connection,
  resolves a page target, attaches an isolated ``Target`` session, and sends
  the call through ``core.cdp_client.CDPClient.send`` -- the same client
  ``send`` path any future curated tool would use (RFC-01, "layer 2 tools
  MUST call the same CDP client `send` path the passthrough uses").

- **Live-schema help.** ``help [INSTANCE] [Domain.method]`` reads the CDP
  protocol schema live from a running browser's ``/json/protocol`` endpoint
  (``core.protocol.discover_protocol``) when exactly one instance can be
  resolved, and prints static usage otherwise. The port is resolved by this
  module rather than delegated to ``core.protocol``'s own instance/port
  resolution, which always reads the vendored default registry path
  (``/tmp/chrome-agent/registry.json``) and ignores the
  ``BROWSER_TOOLS_REGISTRY`` override every other verb honors. Resolving the
  port here keeps ``core/protocol.py`` untouched (RFC-01: prefer the adapted
  core modules unchanged) while still respecting the registry override.

- **Instance-vs-method disambiguation.** A bare leading CLI token resolves as
  an instance name if the registry knows it, else as a ``Domain.method``
  (RFC-01, "Instance names"). ``INSTANCE`` is omittable when exactly one
  instance is registered, reusing ``lifecycle.resolve_single_instance``
  (#35/#36).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from . import lifecycle
from .core import protocol as core_protocol
from .core import registry as core_registry
from .core.attach import AmbiguousTargetError, TargetNotFoundError, resolve_target
from .core.cdp_client import CDPClient, get_ws_url
from .core.errors import CDPError
from .core.registry import InstanceNotFoundError
from .lifecycle import LifecycleError


class UsageError(Exception):
    """A malformed passthrough/help invocation (maps to CLI exit code 2)."""


# ---------------------------------------------------------------------------
# Instance-vs-method disambiguation
# ---------------------------------------------------------------------------


def is_passthrough_head(token: str, registry_path: str | None = None) -> bool:
    """Whether a bare leading CLI token should route to raw-protocol dispatch.

    True when the registry knows ``token`` as an instance name, or when it
    has the shape of a ``Domain.method`` token. The CLI front uses this to
    decide whether an unrecognized leading token is raw-protocol dispatch or
    a genuinely unknown verb (which stays an argparse usage error).
    """
    known = {inst.name for inst in lifecycle.read_instances(registry_path=registry_path)}
    if token in known:
        return True
    return lifecycle.looks_like_domain_method(token)


def resolve_passthrough_args(
    args: list[str],
    registry_path: str | None = None,
) -> tuple[str | None, str, str | None]:
    """Disambiguate the raw-protocol line's leading token.

    ``args`` is the raw-protocol argv with ``--target``/``--url`` already
    extracted (see ``extract_target_flags``). Per RFC-01 "Instance names": a
    bare leading token resolves as an instance name if the registry knows it,
    else as a ``Domain.method``. Returns
    ``(instance_or_None, method, params_json_or_None)``.

    Raises ``UsageError`` when nothing was given, when a known instance name
    is not followed by a method, or when the method position does not look
    like ``Domain.method``.
    """
    if not args:
        raise UsageError("expected Domain.method (optionally preceded by INSTANCE)")

    known = {inst.name for inst in lifecycle.read_instances(registry_path=registry_path)}
    head, tail = args[0], args[1:]

    if head in known:
        if not tail:
            raise UsageError(f"expected Domain.method after instance name '{head}'")
        method, params_json = tail[0], (tail[1] if len(tail) > 1 else None)
        if not lifecycle.looks_like_domain_method(method):
            raise UsageError(f"'{method}' does not look like Domain.method")
        return head, method, params_json

    if not lifecycle.looks_like_domain_method(head):
        raise UsageError(f"'{head}' is neither a known instance nor a Domain.method")
    params_json = tail[0] if tail else None
    return None, head, params_json


def resolve_help_args(
    args: list[str],
    registry_path: str | None = None,
) -> tuple[str | None, str | None]:
    """Disambiguate ``help [INSTANCE] [Domain.method]``.

    Same rule as the raw-protocol line: the leading token is an instance name
    if the registry knows it, else the query itself (a bare domain or a
    ``Domain.method``). Returns ``(instance_or_None, query_or_None)``.
    """
    if not args:
        return None, None
    if len(args) > 2:
        raise UsageError("help takes at most [INSTANCE] [Domain.method]")

    known = {inst.name for inst in lifecycle.read_instances(registry_path=registry_path)}
    head = args[0]
    if head in known:
        return head, (args[1] if len(args) > 1 else None)
    if len(args) > 1:
        raise UsageError(f"'{head}' is not a known instance; only a query may follow it")
    return None, head


def extract_target_flags(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """Pull ``--target SPEC`` / ``--url SUBSTRING`` out of a passthrough argv.

    They may appear anywhere in the invocation. Returns
    ``(remaining_args, target, url)``. Raises ``UsageError`` if both are
    given -- they select the same slot and cannot both be honored.
    """
    remaining: list[str] = []
    target: str | None = None
    url: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--target" and i + 1 < len(argv):
            target = argv[i + 1]
            i += 2
        elif argv[i] == "--url" and i + 1 < len(argv):
            url = argv[i + 1]
            i += 2
        else:
            remaining.append(argv[i])
            i += 1
    if target is not None and url is not None:
        raise UsageError("cannot specify both --target and --url")
    return remaining, target, url


# ---------------------------------------------------------------------------
# Passthrough dispatch
# ---------------------------------------------------------------------------


async def _send_one_shot(
    port: int,
    method: str,
    params: dict | None,
    target_spec: str | None,
    target_by: str | None,
) -> dict:
    """Open a browser-level CDP connection, resolve a target, and send.

    Same ``core.cdp_client.CDPClient.send`` path a curated tool would use
    (RFC-01 invariant). Attaches an isolated ``Target`` session for the call
    and always detaches afterward, best-effort.
    """
    browser_ws_url = get_ws_url(port=port, target_type="browser")
    async with CDPClient(ws_url=browser_ws_url) as cdp:
        targets_result = await cdp.send(method="Target.getTargets")
        page_targets = sorted(
            (t for t in targets_result.get("targetInfos", []) if t.get("type") == "page"),
            key=lambda t: t.get("targetId", ""),
        )
        if not page_targets:
            raise LifecycleError("No page targets in browser")

        target_id = resolve_target(
            page_targets=page_targets,
            target_spec=target_spec,
            target_by=target_by,
        )

        session_result = await cdp.send(
            method="Target.attachToTarget",
            params={"targetId": target_id, "flatten": True},
        )
        session_id = session_result["sessionId"]
        try:
            return await cdp.send(method=method, params=params, session_id=session_id)
        finally:
            with contextlib.suppress(Exception):
                await cdp.send(
                    method="Target.detachFromTarget",
                    params={"sessionId": session_id},
                )


def send(
    *,
    instance: str | None,
    method: str,
    params_json: str | None,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict:
    """Send one raw CDP ``Domain.method`` call and return its JSON result.

    ``instance`` omitted resolves via ``lifecycle.resolve_single_instance``
    (fails naming the candidates unless exactly one instance is registered).
    ``params_json`` must parse to a JSON object; a parse failure or a
    non-object payload is a ``UsageError`` (CLI exit 2). CDP-level and
    target-resolution failures become ``LifecycleError`` (CLI exit 1).
    """
    if instance is None:
        instance = lifecycle.resolve_single_instance(registry_path=registry_path)

    try:
        info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc

    params: dict | None = None
    if params_json is not None:
        try:
            parsed = json.loads(params_json)
        except json.JSONDecodeError as exc:
            raise UsageError(f"invalid JSON parameters: {exc}") from exc
        if not isinstance(parsed, dict):
            raise UsageError("parameters must be a JSON object")
        params = parsed

    target_by: str | None = None
    spec: str | None = None
    if target is not None:
        spec = target
        target_by = "index" if target.isdigit() else "id"
    elif url is not None:
        spec = url
        target_by = "url"

    try:
        return asyncio.run(_send_one_shot(info.port, method, params, spec, target_by))
    except (AmbiguousTargetError, TargetNotFoundError) as exc:
        raise LifecycleError(str(exc)) from exc
    except CDPError as exc:
        raise LifecycleError(f"CDP error {exc.code}: {exc.message}") from exc
    except ConnectionError as exc:
        raise LifecycleError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Live-schema help
# ---------------------------------------------------------------------------

STATIC_HELP = """\
browser-tools / bt -- CDP protocol help

No running browser instance is available to answer this query, so this is
static usage rather than the live protocol schema read from a browser.

  help [INSTANCE] [Domain.method]
      With a running instance, prints the live CDP protocol schema fetched
      from that browser: every domain, one domain's commands and events, or
      one method's full parameter and return signature. INSTANCE may be
      omitted when exactly one instance is running.

  [INSTANCE] Domain.method '{...json params...}' [--target SPEC]
      Send any CDP method the installed browser supports straight to it and
      print the JSON result to stdout. No curated tool is required to exist
      for the method. INSTANCE may be omitted when exactly one instance is
      running; with several running, name one explicitly.

Launch a browser first: bt launch
"""


def _resolve_help_port(instance: str | None, registry_path: str | None) -> int | None:
    """Resolve the port to query for live help, or None for static usage."""
    if instance is not None:
        try:
            info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
        except InstanceNotFoundError as exc:
            raise LifecycleError(str(exc)) from exc
        return info.port

    # No instance named: only auto-resolve when exactly one instance is
    # actually live (engine-aware). Zero or several live instances means
    # there is nothing unambiguous to query, so this falls back to static
    # usage rather than guessing.
    instances = lifecycle.read_instances(registry_path=registry_path)
    live = [i for i in instances if lifecycle.instance_is_live(i)]
    if len(live) == 1:
        return live[0].port
    return None


def run_help(
    instance: str | None,
    query: str | None,
    registry_path: str | None = None,
) -> None:
    """Print live-schema help from a running instance, else static usage.

    Raises ``UsageError`` for an unresolvable ``Domain``/``Domain.method``
    query against a schema that was successfully fetched.
    """
    port = _resolve_help_port(instance, registry_path)
    if port is None:
        print(STATIC_HELP, end="")
        return
    try:
        core_protocol.discover_protocol(port=port, query=query)
    except ConnectionError:
        print(STATIC_HELP, end="")
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
