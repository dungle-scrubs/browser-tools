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
    from collections.abc import AsyncGenerator, Callable

from .core.attach import AmbiguousTargetError, TargetNotFoundError, resolve_target
from .core.cdp_client import CDPClient, get_ws_url
from .core.errors import CDPError, NoPageError
from .core.registry import InstanceNotFoundError
from .lifecycle import LifecycleError


@contextlib.asynccontextmanager
async def one_shot_page_session(
    port: int,
    target_spec: str | None,
    target_by: str | None,
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
            yield cdp, session_id
        finally:
            with contextlib.suppress(Exception):
                await cdp.send(
                    method="Target.detachFromTarget",
                    params={"sessionId": session_id},
                )


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
        except (
            AmbiguousTargetError,
            TargetNotFoundError,
            NoPageError,
            InstanceNotFoundError,
        ) as exc:
            raise LifecycleError(str(exc)) from exc
        except CDPError as exc:
            raise LifecycleError(f"CDP error {exc.code}: {exc.message}") from exc
        except ConnectionError as exc:
            raise LifecycleError(str(exc)) from exc

    return wrapper
