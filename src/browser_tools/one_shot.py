"""The one-shot CDP session seam shared by every single-invocation verb.

``passthrough.send``, ``events.wait``, ``list_verbs.console_list``/
``network_list``, and ``curated.screenshot`` each open a browser-level CDP
connection, resolve a page target, attach an isolated ``Target`` session, do
their own thing over it, and detach -- for exactly one CLI invocation. That
connect/resolve/attach/detach protocol was duplicated byte-for-byte across the
four modules (RFC-01 #37/#42/#43/#50), differing only in the one line that
uses the attached session. This module is the single owner of that protocol:

- :func:`one_shot_page_session` is the seam itself: an async context manager
  that opens the connection, resolves the target, attaches, yields
  ``(cdp, session_id)`` to the caller's body, and always detaches afterward,
  best-effort.
- :func:`cli_cdp_errors` is the matching error-mapping seam: a decorator for
  each verb's public sync function that maps the seam's own failures --
  target resolution, no-page, CDP, connection, and unknown-instance errors --
  to ``LifecycleError`` (CLI exit 1). Verb-specific errors (``UsageError``,
  ``WaitTimeout``) are not touched by this decorator and pass through exactly
  as each verb already raises them.

No-page error, decided
-----------------------
Before this seam existed, ``passthrough._send_one_shot`` and
``curated._capture_screenshot`` raised ``LifecycleError("No page targets in
browser")`` directly, while ``events._wait_one_shot`` and
``list_verbs._collect_one_shot`` raised ``core.errors.NoPageError()`` (caught
one level up and mapped to ``LifecycleError``). Both already reach the same
outcome -- CLI exit 1 with a "no pages" diagnostic -- so there is no behavior
to preserve by keeping two spellings. The seam raises the vendored
``NoPageError`` (it is the specific, already-existing domain error for this
condition) and lets :func:`cli_cdp_errors` do the ``LifecycleError`` mapping,
matching how it already maps ``AmbiguousTargetError``/``TargetNotFoundError``.
"""

from __future__ import annotations

import contextlib
import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable

from .core.attach import AmbiguousTargetError, TargetNotFoundError, resolve_target
from .core.cdp_client import CDPClient, get_ws_url_async
from .core.errors import CDPError, NoPageError
from .core.registry import InstanceNotFoundError
from .endpoint import ResolvedEndpoint
from .external_connection import external_browser_connection
from .lifecycle import LifecycleError

#: Domains the run's ``CDPRuntime`` owns for the whole run (RFC-03, "Domain-
#: enable state"). A step may enable any of them - it needs them on - but MUST NOT
#: disable one: the frame manager reads ``Page`` events, so a step that turned
#: ``Page`` off would break frame selection for every later step.
#:
#: The cost of the exemption, stated because The Manual states it: a step does
#: not get a *first* enable on these. A first ``Runtime.enable`` on a session
#: replays the execution contexts that already exist and a second replays
#: nothing, so `wait --event Runtime.executionContextCreated` after any
#: Runtime-touching step reports nothing where the same command alone reports
#: the contexts already there.
#: Network stays enabled so later steps can retrieve earlier responses. Every
#: run now pays for network event delivery and metadata retention, even without
#: network-get. Chrome also retains bodies within its own limits; this does not
#: promise that a body survives eviction or a renderer change. Unlike Runtime,
#: Network does not replay past responses on enable. A later wait/network-list
#: still observes only its own window, while network-get reads the run buffer.
#: A raw Network.disable step is refused, as for Page and Runtime. Raw
#: buffer-setting commands can still affect how long Chrome retains bodies. These exemptions also apply standalone,
#: where detaching the session releases the domain and its body storage.
RUN_OWNED_DOMAINS = frozenset({"Page", "Runtime", "Network"})


@contextlib.asynccontextmanager
async def domains_enabled(
    cdp: Any, session_id: str, domains: Iterable[str], keep: Iterable[str] = ()
) -> AsyncGenerator[None]:
    """Enable each domain for the body, then disable what this body turned on.

    Domain enables are session state, and nothing in the tree used to send a
    disable. That was harmless while the session died with the invocation. A
    Step Run's session outlives every step, so an enable from step 3 is still
    in force at step 9 - and a second enable on one session is a no-op that
    replays nothing, which silently changes what a later step observes.
    Disabling on the way out restores first-enable semantics for the next step
    that wants the domain.

    It runs the same way outside a run, where the disable is one round trip of
    about a millisecond against a session that was going to be detached. One
    code path is worth more than that millisecond: a rule that only applied
    inside a run would be a second behaviour to keep correct.

    Three things are deliberately not undone here. Domains in
    :data:`RUN_OWNED_DOMAINS`, per the rule above. Domains named in ``keep``,
    which are the ones the caller turned on by hand in a raw step: an enable
    the caller typed is that step's whole output, and undoing it would make
    the step a no-op. And a domain whose enable failed, since there is
    nothing to undo.

    ``keep`` exists because CDP cannot answer the question this function
    would otherwise have to ask. A second ``Log.enable`` succeeds exactly
    like a first one, so the reply says nothing about who turned the domain
    on. Without ``keep``, a `wait --event Log.entryAdded` step after a raw
    `Log.enable` step would disable the caller's domain on its way out.

    That makes the two rules collide, and the precedence is stated rather
    than left implicit: **the caller's enable wins**. The cost is that a
    curated step after a hand-written enable does not get a first enable on
    that domain, which is the same exception the run-owned domains already
    carry. The alternative costs the caller a step that silently did nothing.

    Failures either way are suppressed. Some domains have no ``enable`` at
    all, which is why the enable path already ignored ``CDPError``; the
    disable path gets the same treatment, and also ignores a dropped
    connection. A disable that fails costs a later step its first enable,
    which the rule above already names as the known exception. Raising here
    would instead fail a step whose work had already succeeded, and from a
    ``finally``, where it would mask whatever the body raised.
    """
    mine_to_undo = RUN_OWNED_DOMAINS | frozenset(keep)
    turned_on: list[str] = []
    try:
        for domain in dict.fromkeys(domains):
            try:
                await cdp.send(method=f"{domain}.enable", session_id=session_id)
            except CDPError:
                # No enable for this domain, so nothing to undo either.
                continue
            if domain not in mine_to_undo:
                turned_on.append(domain)
        yield
    finally:
        for domain in reversed(turned_on):
            with contextlib.suppress(CDPError, ConnectionError):
                await cdp.send(method=f"{domain}.disable", session_id=session_id)


@contextlib.asynccontextmanager
async def one_shot_page_session(
    port: int | ResolvedEndpoint,
    target_spec: str | None,
    target_by: str | None,
    *,
    external: bool = False,
) -> AsyncGenerator[tuple[CDPClient, str]]:
    """Connect, resolve a page target, attach, yield the session, detach.

    Opens the browser-level ``core.cdp_client.CDPClient`` connection, lists
    targets, sorts the page targets deterministically by target ID, resolves
    one against ``target_spec``/``target_by`` (``core.attach.resolve_target``
    -- ``AmbiguousTargetError``/``TargetNotFoundError`` propagate unchanged),
    attaches an isolated flattened ``Target`` session, and yields
    ``(cdp, session_id)`` to the body. The session is always detached in a
    ``finally``, best-effort, whether the body returns or raises.

    Raises ``NoPageError`` when the browser has no page targets at all (see
    the module docstring for why this is the one no-page spelling now).

    ``external`` says the port came from ``--endpoint`` rather than the
    registry. A failed connection then also reports who holds the port and what
    profile directory they hold, because there is no registry entry to compare
    against and no ``bt status`` row to look the browser up in (#97).
    """
    async with contextlib.AsyncExitStack() as stack:
        if isinstance(port, ResolvedEndpoint) and port.websocket_urls:
            cdp = await stack.enter_async_context(external_browser_connection(port))
        else:
            number = port.port if isinstance(port, ResolvedEndpoint) else port
            try:
                browser_ws_url = await get_ws_url_async(port=number, target_type="browser")
            except ConnectionError as exc:
                raise ConnectionError(
                    connection_failure_message(port=number, cause=exc.__cause__, external=external)
                ) from exc
            cdp = await stack.enter_async_context(CDPClient(ws_url=browser_ws_url))
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
            yield cdp, session_id
        finally:
            with contextlib.suppress(Exception):
                await cdp.send(
                    method="Target.detachFromTarget",
                    params={"sessionId": session_id},
                )


def connection_failure_message(
    *, port: int, cause: BaseException | None, external: bool = False
) -> str:
    """Spell out why the DevTools HTTP endpoint on ``port`` did not answer.

    The vendored ``core.cdp_client.get_ws_url`` raises one ``ConnectionError``
    ("No browser listening ... chrome-agent launch") for every HTTP failure,
    chaining the real one as its cause. Two different failures hide behind it,
    and they have different remedies:

    - refused / unreachable: nothing is bound to the port. The browser never
      started or has exited; launch one.
    - timeout: the port is bound, but Chrome serves ``/json*`` from its UI
      thread, so a browser whose UI thread is busy or hung accepts the TCP
      connection and never answers. Launching another browser does not fix
      that; ``bt status`` (a plain TCP probe) still reports it alive.

    Call-site adaptation of the verbatim core (RFC-01, "Vendoring rules"): the
    core text and program name stay untouched in ``core/``.
    """
    if _is_timeout(cause):
        message = (
            f"Browser on port {port} is bound but did not answer the DevTools HTTP "
            f"endpoint before the timeout (busy or hung browser UI thread). "
            f"Check it with: bt status"
        )
    else:
        message = f"No browser listening on port {port}. Start one with: bt launch"
    if not external:
        return message
    from .endpoint import describe_endpoint

    return (
        f"--endpoint on port {port} did not answer. "
        f"Start the browser with --remote-debugging-port={port}.\n"
        f"{describe_endpoint(port)}"
    )


def _is_timeout(exc: BaseException | None) -> bool:
    """True for a socket timeout, bare or wrapped as ``URLError.reason``."""
    if exc is None:
        return False
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


def cli_cdp_errors[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    """Map the seam's shared failures to ``LifecycleError`` (CLI exit 1).

    Catches ``AmbiguousTargetError``, ``TargetNotFoundError``, ``NoPageError``,
    ``InstanceNotFoundError`` (the same unknown-instance error every verb's
    own ``core_registry.lookup``/``resolve_single_instance`` call already
    converts, folded in here so the verb bodies no longer need their own
    try/except around it), ``CDPError``, and ``ConnectionError``. Every other
    exception -- notably ``passthrough.UsageError`` and ``events.WaitTimeout``
    -- passes through untouched, exactly as each verb already raises it.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return fn(*args, **kwargs)
        except InstanceNotFoundError as exc:
            # Adapted here, not in the verbatim core: the vendored text names
            # upstream's program. See lifecycle.instance_not_found_message.
            from .lifecycle import instance_not_found_message

            raise LifecycleError(instance_not_found_message(exc)) from exc
        except (
            AmbiguousTargetError,
            TargetNotFoundError,
            NoPageError,
        ) as exc:
            raise LifecycleError(str(exc)) from exc
        except CDPError as exc:
            raise LifecycleError(f"CDP error {exc.code}: {exc.message}") from exc
        except ConnectionError as exc:
            raise LifecycleError(str(exc)) from exc

    return wrapper
