"""The ``attach`` and ``wait`` event verbs for the merged CLI front (RFC-01 #42).

New layer-4 code, alongside ``cli.py``, ``lifecycle.py``, and
``passthrough.py`` (RFC-01 #37, whose structure this module follows). It owns
the two event-observation verbs of the RFC-01 "CLI surface":

- **``attach [INSTANCE] +Domain.event [...] [--target SPEC] [--url SUBSTRING]``.**
  Streams subscribed CDP events as JSON lines. This is a thin front over the
  verbatim-vendored ``core.attach.run_attach``, which already creates an
  isolated ``Target`` session per attach and tracks its subscription set in
  call-local state. Isolation is therefore structural: two attached observers
  each run their own ``run_attach`` over their own session, so neither sees the
  other's subscriptions and a retiring observer never disturbs the other's
  stream (RFC-01: "attach subscriptions MUST be isolated per session").

- **``wait [INSTANCE] --event Domain.event [--match SUBSTRING] [--timeout SECONDS]``.**
  Blocks for one matching event using SUBSCRIBE-FIRST buffering: it registers
  the event handler, and only then begins examining, so an event that fires
  between subscription and examination is buffered in a queue rather than lost
  to the race (RFC-01 "wait design", the pattern of
  ``~/dev/chrome-agent/scripts/cdp-wait.py`` rebuilt as a verb over the core
  CDP client instead of a JSONL file tail). ``--match`` is a substring test
  against the event's JSON serialization. ``--timeout`` defaults to 30 s;
  ``--timeout 0`` means no deadline. On match: the event JSON on stdout,
  exit 0. On deadline: a timeout diagnostic on stderr, exit 1, empty stdout.

Reuses the #35/#36 instance-resolution helpers (``resolve_single_instance``)
and the vendored target-selection machinery (``core.attach.resolve_target``,
the same ``--target``/``--url`` slot ``passthrough`` fills).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from . import lifecycle
from .core import attach as core_attach
from .core import registry as core_registry
from .core.attach import AmbiguousTargetError, TargetNotFoundError, resolve_target
from .core.cdp_client import CDPClient, get_ws_url
from .core.errors import CDPError, NoPageError
from .core.registry import InstanceNotFoundError
from .lifecycle import LifecycleError
from .passthrough import UsageError

#: ``wait``'s default deadline in seconds (RFC-01 "wait design").
DEFAULT_WAIT_TIMEOUT = 30.0


class WaitTimeout(LifecycleError):
    """``wait`` reached its deadline with no matching event.

    A ``LifecycleError`` subclass so the CLI front's existing operational-error
    handler maps it to exit 1 with the diagnostic on stderr and nothing on
    stdout, exactly as RFC-01 requires for the deadline case.
    """


# ---------------------------------------------------------------------------
# Argument resolution
# ---------------------------------------------------------------------------


def resolve_attach_args(args: list[str]) -> tuple[str | None, list[str]]:
    """Split an ``attach`` positional argv into ``(instance_or_None, events)``.

    Events are the ``+Domain.event`` tokens (the leading ``+`` stripped). At
    most one bare (non-``+``) token is allowed: the instance name. The bare
    token is unambiguous here -- every subscription carries a ``+`` -- so no
    registry lookup is needed to disambiguate (unlike the passthrough line,
    whose leading token can be either). An unknown instance name surfaces later
    as an operational error from ``core.attach.run_attach``'s registry lookup,
    matching how ``passthrough`` treats an unknown instance.

    Raises ``UsageError`` when no subscription is given, when a ``+`` token is
    not ``Domain.event``-shaped, or when a second bare token appears.
    """
    instance: str | None = None
    events: list[str] = []
    for token in args:
        if token.startswith("+"):
            event = token[1:]
            if not lifecycle.looks_like_domain_method(event):
                raise UsageError(f"'{token}' is not a +Domain.event subscription")
            events.append(event)
        elif instance is None:
            instance = token
        else:
            raise UsageError(
                f"unexpected argument '{token}'; attach takes one optional INSTANCE "
                "before its +Domain.event subscriptions"
            )
    if not events:
        raise UsageError("attach requires at least one +Domain.event subscription")
    return instance, events


def _target_slot(target: str | None, url: str | None) -> tuple[str | None, str | None]:
    """Map the ``--target``/``--url`` pair to ``core.attach``'s ``(spec, by)``.

    A numeric ``--target`` selects by 1-based index, a non-numeric one by
    targetId prefix, and ``--url`` by URL substring -- the same mapping
    ``passthrough.send`` applies. ``--target`` and ``--url`` are mutually
    exclusive; the CLI front rejects the pair before calling here.
    """
    if target is not None:
        return target, ("index" if target.isdigit() else "id")
    if url is not None:
        return url, "url"
    return None, None


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


def run_attach(
    *,
    instance: str | None,
    events: list[str],
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> None:
    """Stream subscribed events as JSON lines until EOF or SIGTERM.

    Thin front over the verbatim ``core.attach.run_attach``. ``instance``
    omitted resolves via ``lifecycle.resolve_single_instance`` (fails naming
    the candidates unless exactly one instance is registered). Instance,
    target-resolution, no-page, and connection failures all become
    ``LifecycleError`` (CLI exit 1).
    """
    if not events:
        raise UsageError("attach requires at least one +Domain.event subscription")
    if instance is None:
        instance = lifecycle.resolve_single_instance(registry_path=registry_path)

    spec, target_by = _target_slot(target, url)

    try:
        asyncio.run(
            core_attach.run_attach(
                instance_name=instance,
                subscriptions=events,
                target_spec=spec,
                target_by=target_by,
                registry_path=registry_path,
            )
        )
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc
    except (AmbiguousTargetError, TargetNotFoundError, NoPageError) as exc:
        raise LifecycleError(str(exc)) from exc
    except ConnectionError as exc:
        raise LifecycleError(str(exc)) from exc


# ---------------------------------------------------------------------------
# wait -- subscribe-first buffering
# ---------------------------------------------------------------------------


async def wait_on_session(
    cdp: CDPClient,
    session_id: str,
    event: str,
    match: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Block on one CDP session for a matching event; SUBSCRIBE-FIRST.

    The ordering is the whole point (RFC-01 "wait design"): the event handler
    is registered *before* the domain-enable await and before any examination,
    so an event delivered while ``Domain.enable`` is in flight -- or at any
    moment after subscription -- lands in ``buffer`` and is drained here, never
    dropped. Examining first and subscribing second would lose exactly that
    event, which is the race this method exists to close.

    ``match`` (when given) is a substring test against the event line's JSON
    serialization. ``timeout`` of ``0`` means no deadline; any positive value
    bounds the wait. Returns the ``{"method", "params"}`` event dict on match;
    raises ``WaitTimeout`` on deadline.
    """
    buffer: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def handler(params: dict[str, Any]) -> None:
        buffer.put_nowait({"method": event, "params": params})

    # SUBSCRIBE FIRST. Registration is synchronous and precedes every await
    # below, so the handler is live before any event can be delivered.
    cdp.on(event=event, callback=handler, session_id=session_id)

    domain = event.split(".")[0]
    with contextlib.suppress(CDPError):
        # Some domains have no enable; a CDP error here must not abort the
        # wait. Events delivered during this await are already buffered.
        await cdp.send(method=f"{domain}.enable", session_id=session_id)

    deadline = None if timeout == 0 else time.monotonic() + timeout
    try:
        while True:
            if deadline is None:
                item = await buffer.get()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WaitTimeout(_timeout_message(event, match, timeout))
                try:
                    item = await asyncio.wait_for(buffer.get(), timeout=remaining)
                except TimeoutError as exc:
                    raise WaitTimeout(_timeout_message(event, match, timeout)) from exc
            if match is None or match in json.dumps(item):
                return item
    finally:
        cdp.off(event=event, callback=handler)


def _timeout_message(event: str, match: str | None, timeout: float) -> str:
    """Build the deadline diagnostic (RFC-01: timeout error on stderr)."""
    suffix = f" matching '{match}'" if match is not None else ""
    return f"timeout: no {event} event{suffix} within {timeout}s"


async def _wait_one_shot(
    port: int,
    event: str,
    match: str | None,
    timeout: float,
    target_spec: str | None,
    target_by: str | None,
) -> dict[str, Any]:
    """Open a browser-level connection, resolve a target, and wait on it.

    Mirrors ``passthrough._send_one_shot``'s connect/resolve/attach plumbing so
    ``wait`` opens its own isolated ``Target`` session (RFC-01 "wait design":
    it MUST open an attach session), then delegates to ``wait_on_session`` for
    the subscribe-first loop and always detaches afterward, best-effort.
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
            return await wait_on_session(cdp, session_id, event, match, timeout)
        finally:
            with contextlib.suppress(Exception):
                await cdp.send(
                    method="Target.detachFromTarget",
                    params={"sessionId": session_id},
                )


def wait(
    *,
    instance: str | None,
    event: str,
    match: str | None = None,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Block for one matching event and return its JSON dict.

    ``instance`` omitted resolves via ``lifecycle.resolve_single_instance``.
    On the deadline, raises ``WaitTimeout`` (CLI exit 1, diagnostic on stderr,
    empty stdout). Target-resolution, no-page, CDP, and connection failures
    become ``LifecycleError`` (CLI exit 1).
    """
    if instance is None:
        instance = lifecycle.resolve_single_instance(registry_path=registry_path)

    try:
        info = core_registry.lookup(instance_name=instance, registry_path=registry_path)
    except InstanceNotFoundError as exc:
        raise LifecycleError(str(exc)) from exc

    spec, target_by = _target_slot(target, url)

    try:
        return asyncio.run(_wait_one_shot(info.port, event, match, timeout, spec, target_by))
    except (AmbiguousTargetError, TargetNotFoundError, NoPageError) as exc:
        raise LifecycleError(str(exc)) from exc
    except CDPError as exc:
        raise LifecycleError(f"CDP error {exc.code}: {exc.message}") from exc
    except ConnectionError as exc:
        raise LifecycleError(str(exc)) from exc
