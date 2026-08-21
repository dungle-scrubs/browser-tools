"""The ``console-list`` and ``network-list`` verbs for the merged CLI front (RFC-01 #43).

New layer-4 code, alongside ``cli.py``, ``events.py``, ``lifecycle.py``, and
``passthrough.py`` (RFC-01 #37/#42, whose structure this module follows). It
owns the two REQUIRED "CLI surface" list verbs:

- **``console-list [INSTANCE] [--target SPEC] [--url SUBSTRING] [--duration
  SECONDS]``.** Collects console messages (``Runtime.consoleAPICalled``) over
  a short attach session and renders them as a JSON list.
- **``network-list [INSTANCE] [--target SPEC] [--url SUBSTRING] [--duration
  SECONDS]``.** Collects network request/response pairs
  (``Network.requestWillBeSent`` / ``Network.responseReceived``) over a short
  attach session, correlated by ``requestId``, and renders them as a JSON
  list.

RFC-01 requires both as thin wrappers over a short attach session, replacing
the earlier point-in-time dumps (chrome-devtools-mcp's ``list_console_messages``
/ ``list_network_requests``, forwarded by default -- see ``mcp_daemon.py``)
that only see whatever had already accumulated by the moment they were
called, and so miss anything emitted between two calls. This module instead
opens an isolated ``Target`` session (mirroring ``events._wait_one_shot``'s
connect/resolve/attach plumbing), subscribes SUBSCRIBE-FIRST -- the handler is
registered before the domain-enable await and before the collection window
begins, so an event delivered while ``Domain.enable`` is in flight, or at any
point during the window, is buffered rather than lost (the same race
``events.wait_on_session`` closes for ``wait``) -- then sleeps for the window
and returns everything collected.

Reuses the #35/#36 instance-resolution helper (``lifecycle.resolve_single_instance``),
the #37/#42 ``--target``/``--url`` extraction (``events._target_slot``), and
the vendored target-selection machinery (``core.attach.resolve_target``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from . import lifecycle
from .core import registry as core_registry
from .core.attach import AmbiguousTargetError, TargetNotFoundError, resolve_target
from .core.cdp_client import CDPClient, get_ws_url
from .core.errors import CDPError, NoPageError
from .core.registry import InstanceNotFoundError
from .events import _target_slot  # pyright: ignore[reportPrivateUsage]
from .lifecycle import LifecycleError

#: The collection window's default duration in seconds. Short by design: this
#: is a snapshot-over-a-beat, not an open-ended stream (that is ``attach``'s
#: job).
DEFAULT_LIST_WINDOW_SECONDS = 2.0

#: The console verb's subscription set.
CONSOLE_EVENTS = ["Runtime.consoleAPICalled"]

#: The network verb's subscription set: one request-side and one
#: response-side event, correlated by ``requestId`` at render time.
NETWORK_EVENTS = ["Network.requestWillBeSent", "Network.responseReceived"]


# ---------------------------------------------------------------------------
# Collection: SUBSCRIBE-FIRST over a fixed window
# ---------------------------------------------------------------------------


async def collect_on_session(
    cdp: CDPClient,
    session_id: str,
    events: list[str],
    duration: float,
) -> list[dict[str, Any]]:
    """Collect every subscribed event over one CDP session for ``duration`` seconds.

    SUBSCRIBE-FIRST, the same ordering ``events.wait_on_session`` uses for
    ``wait``: every handler is registered before the domain-enable await, so
    an event delivered while ``Domain.enable`` is in flight -- or at any point
    during the sleep that follows -- lands in ``collected``, never dropped.
    ``duration`` of ``0`` (or less) skips the sleep and returns whatever
    arrived synchronously during subscription/enable.
    """
    collected: list[dict[str, Any]] = []

    def make_handler(event_name: str) -> Callable[[dict[str, Any]], None]:
        def handler(params: dict[str, Any]) -> None:
            collected.append({"method": event_name, "params": params})

        return handler

    handlers: dict[str, Callable[[dict[str, Any]], None]] = {}
    for event_name in events:
        handler = make_handler(event_name)
        handlers[event_name] = handler
        # SUBSCRIBE FIRST: registration precedes every await below.
        cdp.on(event=event_name, callback=handler, session_id=session_id)

    try:
        enabled_domains: set[str] = set()
        for event_name in events:
            domain = event_name.split(".")[0]
            if domain in enabled_domains:
                continue
            enabled_domains.add(domain)
            with contextlib.suppress(CDPError):
                # Events delivered during this await are already buffered.
                await cdp.send(method=f"{domain}.enable", session_id=session_id)

        if duration > 0:
            await asyncio.sleep(duration)

        return list(collected)
    finally:
        for event_name, handler in handlers.items():
            cdp.off(event=event_name, callback=handler)


async def _collect_one_shot(
    port: int,
    events: list[str],
    duration: float,
    target_spec: str | None,
    target_by: str | None,
) -> list[dict[str, Any]]:
    """Open an isolated attach session and collect its events for a window.

    Mirrors ``events._wait_one_shot``'s connect/resolve/attach plumbing: opens
    a browser-level connection, resolves a page target, attaches an isolated
    ``Target`` session (RFC-01 "console-list and network-list ... implemented
    as thin wrappers over a short attach session"), then delegates to
    ``collect_on_session``, detaching best-effort afterward.
    """
    browser_ws_url = get_ws_url(port=port, target_type="browser")
    async with CDPClient(ws_url=browser_ws_url) as cdp:
        targets_result = await cdp.send(method="Target.getTargets")
        target_infos: list[dict[str, Any]] = targets_result.get("targetInfos", [])

        def _target_id(t: dict[str, Any]) -> str:
            return t.get("targetId", "")

        page_targets = sorted(
            (t for t in target_infos if t.get("type") == "page"),
            key=_target_id,
        )
        if not page_targets:
            raise NoPageError()

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
            return await collect_on_session(cdp, session_id, events, duration)
        finally:
            with contextlib.suppress(Exception):
                await cdp.send(
                    method="Target.detachFromTarget",
                    params={"sessionId": session_id},
                )


def _run_collection(
    *,
    instance: str | None,
    events: list[str],
    duration: float,
    target: str | None,
    url: str | None,
    registry_path: str | None,
) -> list[dict[str, Any]]:
    """Shared resolve-instance-and-collect path for both list verbs."""
    if instance is None:
        instance = lifecycle.resolve_single_instance(registry_path=registry_path)

    try:
        info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc

    spec, target_by = _target_slot(target, url)

    try:
        return asyncio.run(_collect_one_shot(info.port, events, duration, spec, target_by))
    except (AmbiguousTargetError, TargetNotFoundError, NoPageError) as exc:
        raise LifecycleError(str(exc)) from exc
    except CDPError as exc:
        raise LifecycleError(f"CDP error {exc.code}: {exc.message}") from exc
    except ConnectionError as exc:
        raise LifecycleError(str(exc)) from exc


# ---------------------------------------------------------------------------
# console-list
# ---------------------------------------------------------------------------


def _render_console_arg(remote_object: dict[str, Any]) -> str:
    """Render one ``Runtime.RemoteObject`` argument as display text.

    Prefers the primitive ``value`` (present for strings, numbers, booleans);
    falls back to the object ``description`` Chrome supplies for non-primitive
    args (objects, errors, DOM nodes), then to ``unserializableValue``
    (``NaN``, ``Infinity``), matching what a console viewer shows.
    """
    if "value" in remote_object:
        value = remote_object["value"]
        return value if isinstance(value, str) else json.dumps(value)
    if "unserializableValue" in remote_object:
        return remote_object["unserializableValue"]
    return remote_object.get("description", "")


def _render_console_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Render one buffered ``Runtime.consoleAPICalled`` event as a console row."""
    params = item["params"]
    text = " ".join(_render_console_arg(arg) for arg in params.get("args", []))
    return {
        "type": params.get("type", "log"),
        "text": text,
        "timestamp": params.get("timestamp"),
    }


def console_list(
    *,
    instance: str | None,
    target: str | None = None,
    url: str | None = None,
    duration: float = DEFAULT_LIST_WINDOW_SECONDS,
    registry_path: str | None = None,
) -> list[dict[str, Any]]:
    """Collect console messages over a short attach window and render them.

    ``instance`` omitted resolves via ``lifecycle.resolve_single_instance``.
    Instance, target-resolution, no-page, CDP, and connection failures all
    become ``LifecycleError`` (CLI exit 1).
    """
    raw = _run_collection(
        instance=instance,
        events=CONSOLE_EVENTS,
        duration=duration,
        target=target,
        url=url,
        registry_path=registry_path,
    )
    return [_render_console_entry(item) for item in raw]


# ---------------------------------------------------------------------------
# network-list
# ---------------------------------------------------------------------------


def _render_network_entries(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Correlate buffered request/response events by ``requestId``.

    ``Network.requestWillBeSent`` opens a row; ``Network.responseReceived``
    fills in ``status``/``statusText`` on the same row if the request was seen
    in this window, or opens one itself (a response whose request fired
    before the window started still gets a row, just without request-side
    fields). Row order follows first sight of each ``requestId``.
    """
    entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in raw:
        params = item["params"]
        request_id = params.get("requestId")
        if request_id is None:
            continue

        if request_id not in entries:
            entries[request_id] = {
                "requestId": request_id,
                "method": None,
                "url": None,
                "resourceType": None,
                "status": None,
                "statusText": None,
            }
            order.append(request_id)
        entry = entries[request_id]

        if item["method"] == "Network.requestWillBeSent":
            request = params.get("request", {})
            entry["method"] = request.get("method")
            entry["url"] = request.get("url")
            entry["resourceType"] = params.get("type")
        elif item["method"] == "Network.responseReceived":
            response = params.get("response", {})
            entry["url"] = entry["url"] or response.get("url")
            entry["resourceType"] = entry["resourceType"] or params.get("type")
            entry["status"] = response.get("status")
            entry["statusText"] = response.get("statusText")

    return [entries[request_id] for request_id in order]


def network_list(
    *,
    instance: str | None,
    target: str | None = None,
    url: str | None = None,
    duration: float = DEFAULT_LIST_WINDOW_SECONDS,
    registry_path: str | None = None,
) -> list[dict[str, Any]]:
    """Collect network request/response events over a short attach window.

    ``instance`` omitted resolves via ``lifecycle.resolve_single_instance``.
    Instance, target-resolution, no-page, CDP, and connection failures all
    become ``LifecycleError`` (CLI exit 1).
    """
    raw = _run_collection(
        instance=instance,
        events=NETWORK_EVENTS,
        duration=duration,
        target=target,
        url=url,
        registry_path=registry_path,
    )
    return _render_network_entries(raw)
