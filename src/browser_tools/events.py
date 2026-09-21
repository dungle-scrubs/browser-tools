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
import selectors
import signal
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core.cdp_client import CDPClient

from . import lifecycle
from .core import attach as core_attach
from .core.attach import AmbiguousTargetError, TargetNotFoundError
from .core.errors import CDPError, NoPageError
from .core.registry import InstanceNotFoundError
from .lifecycle import LifecycleError
from .one_shot import cli_cdp_errors, domains_enabled, one_shot_page_session
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
    endpoint: str | None = None,
) -> None:
    """Stream subscribed events as JSON lines until EOF or SIGTERM.

    Thin front over the verbatim ``core.attach.run_attach``. ``instance``
    omitted resolves via ``lifecycle.resolve_single_instance`` (fails naming
    the candidates unless exactly one instance is registered). Instance,
    target-resolution, no-page, and connection failures all become
    ``LifecycleError`` (CLI exit 1).

    ``endpoint`` takes the external path instead (:func:`_attach_external`),
    because the vendored session resolves its port through the registry and
    then polls that same entry to decide when to exit. An external browser has
    no entry -- that absence is the safety property (#97) -- so the vendored
    liveness poll would read "retired" on its first tick. Correcting at the
    call site rather than in the vendored module (RFC-01, "Vendoring rules").
    """
    if not events:
        raise UsageError("attach requires at least one +Domain.event subscription")
    if endpoint is not None:
        port = lifecycle.resolve_cdp_port(None, None, endpoint)
        spec, target_by = _target_slot(target, url)
        try:
            asyncio.run(_attach_external(port, events, spec, target_by))
        except (AmbiguousTargetError, TargetNotFoundError, NoPageError) as exc:
            raise LifecycleError(str(exc)) from exc
        except ConnectionError as exc:
            raise LifecycleError(str(exc)) from exc
        return
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
        raise LifecycleError(lifecycle.instance_not_found_message(exc)) from exc
    except (AmbiguousTargetError, TargetNotFoundError, NoPageError) as exc:
        raise LifecycleError(str(exc)) from exc
    except ConnectionError as exc:
        raise LifecycleError(str(exc)) from exc


def _stdin_is_watchable() -> bool:
    """Whether this process's stdin can be registered for readability.

    ``loop.connect_read_pipe`` does not fail on a stdin the event loop cannot
    watch. It returns, then the registration fails inside a scheduled callback,
    and asyncio logs the traceback -- into the same stream the caller is
    reading events from. Measured on macOS: ``/dev/null`` is a character device
    that kqueue rejects with ``EINVAL``, and an agent running ``bt attach`` in
    the background gets exactly that stdin.

    Probing a throwaway selector of the same kind the loop uses answers the
    question before the loop is asked. A stdin that cannot be watched means no
    live ``+``/``-`` subscription channel, so the session runs on its initial
    subscriptions until a signal.
    """
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return False
    selector = selectors.DefaultSelector()
    try:
        selector.register(fd, selectors.EVENT_READ)
    except (OSError, ValueError):
        return False
    else:
        selector.unregister(fd)
        return True
    finally:
        selector.close()


async def _attach_external(
    port: int,
    subscriptions: list[str],
    target_spec: str | None,
    target_by: str | None,
) -> None:
    """Stream events from an external browser until EOF, SIGTERM, or a drop.

    The same wire protocol the vendored session speaks: one ``{"status":
    "ready"}`` line, then one JSON line per event, with ``+Domain.event`` and
    ``-Domain.event`` on stdin adding and removing subscriptions live. It runs
    over :func:`one_shot_page_session`, which already owns connect / resolve /
    attach / detach, so only the subscription and shutdown halves live here.
    The ready line carries ``endpoint`` (the port being driven) where the
    registry path carries ``target``, because there is no instance to name.

    The one behavioral difference from the registry path is the exit
    condition. The vendored session polls the registry entry and exits when the
    instance is retired. There is no entry here, so the session ends on stdin
    EOF, on SIGTERM or SIGINT, or when the browser drops the connection.
    """
    async with one_shot_page_session(
        port, target_spec, target_by, external=True
    ) as (cdp, session_id):
        enabled_domains: set[str] = set()
        handlers: dict[str, Any] = {}

        async def subscribe(event_name: str) -> None:
            domain = event_name.split(".")[0]
            if domain not in enabled_domains:
                with contextlib.suppress(CDPError):
                    await cdp.send(method=f"{domain}.enable", session_id=session_id)
                enabled_domains.add(domain)

            def handler(params: dict[str, Any]) -> None:
                print(json.dumps({"method": event_name, "params": params}), flush=True)

            handlers[event_name] = handler
            cdp.on(event=event_name, callback=handler, session_id=session_id)

        def unsubscribe(event_name: str) -> None:
            handler = handlers.pop(event_name, None)
            if handler is not None:
                cdp.off(event=event_name, callback=handler)

        for event_name in subscriptions:
            await subscribe(event_name)

        # Signal handlers go up before the ready line: a supervisor that reads
        # "ready" and immediately sends SIGTERM must get the graceful path.
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, OSError):
                loop.add_signal_handler(sig, shutdown.set)

        print(
            json.dumps({"status": "ready", "sessionId": session_id[:16], "endpoint": port}),
            flush=True,
        )

        async def stdin_loop() -> None:
            if not _stdin_is_watchable():
                await shutdown.wait()
                return
            reader = asyncio.StreamReader()
            await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
            )
            with contextlib.suppress(EOFError, asyncio.CancelledError):
                while not shutdown.is_set():
                    raw = await reader.readline()
                    if not raw:
                        return
                    line = raw.decode().strip()
                    if line.startswith("+"):
                        await subscribe(line[1:])
                    elif line.startswith("-"):
                        unsubscribe(line[1:])
                    elif line:
                        print(json.dumps({"warning": f"Unknown command: {line}"}), flush=True)

        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(stdin_loop()),
            asyncio.create_task(shutdown.wait()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


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

    # The cleanup scope opens here, not after the enable. `suppress` catches
    # `CDPError` and nothing else, so a dropped connection during the enable
    # leaves this function by an exception. Outside a Step Run the process
    # ends and the leaked subscription with it; on a shared session it stays
    # registered and keeps pushing into a queue nobody reads.
    try:
        # Enabled for this wait and disabled after it, so the next step that
        # wants this domain still gets a first enable (RFC-03, "Domain-enable
        # state"). Events delivered during the enable are already buffered.
        async with domains_enabled(cdp, session_id, [event.split(".")[0]]):
            deadline = None if timeout == 0 else time.monotonic() + timeout
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


#: Headroom over a wait's own deadline, so the outer bound never fires first
#: and turns the wait's own `WaitTimeout` into something else.
_WAIT_GRACE_SECONDS = 5.0


@cli_cdp_errors
def wait(
    *,
    instance: str | None,
    event: str,
    match: str | None = None,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    target: str | None = None,
    url: str | None = None,
    registry_path: str | None = None,
    endpoint: str | None = None,
    handler: Any | None = None,
) -> dict[str, Any]:
    """Block for one matching event and return its JSON dict.

    ``instance`` omitted resolves via ``lifecycle.resolve_single_instance``.
    On the deadline, raises ``WaitTimeout`` (CLI exit 1, diagnostic on stderr,
    empty stdout) -- untouched by ``@cli_cdp_errors``, which only maps the
    seam's own failures. Target-resolution, no-page, CDP, and connection
    failures become ``LifecycleError`` (CLI exit 1), via ``@cli_cdp_errors``.
    """
    if handler is not None:
        # A Step Run already holds the session. `timeout=0` means no deadline
        # here, so the submitted wait gets none either; the run's own
        # `--timeout` is what bounds an otherwise unbounded step.
        cdp, session_id = handler.require_session()
        return handler.submit(
            wait_on_session(cdp, session_id, event, match, timeout),
            timeout=None if timeout == 0 else timeout + _WAIT_GRACE_SECONDS,
        )

    port = lifecycle.resolve_cdp_port(instance, registry_path, endpoint)

    spec, target_by = _target_slot(target, url)

    async def _wait() -> dict[str, Any]:
        async with one_shot_page_session(
            port, spec, target_by, external=endpoint is not None
        ) as (cdp, session_id):
            return await wait_on_session(cdp, session_id, event, match, timeout)

    return asyncio.run(_wait())
