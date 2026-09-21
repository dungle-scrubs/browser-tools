"""What a step gives back before the next one starts (RFC-03, Phase 4).

A one-shot invocation could leave the session however it liked, because the
session was detached moments later. A Step Run's session outlives every step,
so whatever step 3 leaves on is still on at step 9. Two kinds of leftover
matter, and the RFC measured the first rather than reasoning about it:

| Case                          | `executionContextCreated` delivered |
|-------------------------------|-------------------------------------|
| One session, first enable     | 2                                   |
| One session, second enable    | 0                                   |
| Fresh session, one enable     | 2                                   |

A first enable replays the state that already exists; a second replays
nothing. So a domain left enabled by step 3 silently changes what step 9
observes, and `GUIDE.txt` promises `console-list` and `network-list`
"subscribe before enabling their domain, so an event emitted during enable is
in the result" - a promise a second enable cannot keep.

The other leftover is the selected frame, which a run carries forward on
purpose. The tests at the bottom cover the one case where carrying it forward
is wrong: the pattern stopped matching, and the old frame id stayed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from browser_tools import events, list_verbs
from browser_tools.core.errors import CDPError
from browser_tools.one_shot import RUN_OWNED_DOMAINS, domains_enabled


class FakeCDP:
    """Records every method sent, and which handlers are still registered."""

    def __init__(self, no_enable: set[str] | None = None, no_disable: set[str] | None = None):
        self.calls: list[str] = []
        self.handlers: dict[str, list[Any]] = {}
        self._no_enable = no_enable or set()
        self._no_disable = no_disable or set()

    async def send(self, method: str, params: Any = None, session_id: str | None = None) -> dict:
        self.calls.append(method)
        domain = method.split(".")[0]
        if method.endswith(".enable") and domain in self._no_enable:
            raise CDPError(-32601, f"'{method}' wasn't found")
        if method.endswith(".disable") and domain in self._no_disable:
            raise CDPError(-32601, f"'{method}' wasn't found")
        return {}

    def on(self, event: str, callback: Any, session_id: str | None = None) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Any, session_id: str | None = None) -> None:
        if callback in self.handlers.get(event, []):
            self.handlers[event].remove(callback)


async def _use(cdp: FakeCDP, domains: list[str], body: Any = None) -> None:
    async with domains_enabled(cdp, "S1", domains):
        if body is not None:
            await body()


class TestAStepGivesBackTheDomainsItTurnedOn:
    def test_one_domain_is_enabled_then_disabled(self):
        cdp = FakeCDP()
        asyncio.run(_use(cdp, ["Network"]))
        assert cdp.calls == ["Network.enable", "Network.disable"]

    def test_the_disable_happens_after_the_body(self):
        cdp = FakeCDP()

        async def body():
            cdp.calls.append("<the step's work>")

        asyncio.run(_use(cdp, ["Network"], body))
        assert cdp.calls == ["Network.enable", "<the step's work>", "Network.disable"], (
            "the domain was given back before the step had finished using it"
        )

    def test_a_domain_named_twice_is_enabled_once(self):
        cdp = FakeCDP()
        asyncio.run(_use(cdp, ["Network", "Network"]))
        assert cdp.calls == ["Network.enable", "Network.disable"]

    def test_several_domains_are_undone_in_reverse(self):
        cdp = FakeCDP()
        asyncio.run(_use(cdp, ["Network", "Log"]))
        assert cdp.calls == ["Network.enable", "Log.enable", "Log.disable", "Network.disable"]

    def test_a_failing_body_still_gives_the_domain_back(self):
        cdp = FakeCDP()

        async def body():
            raise RuntimeError("the step failed")

        with pytest.raises(RuntimeError):
            asyncio.run(_use(cdp, ["Network"], body))
        assert "Network.disable" in cdp.calls, (
            "a step that failed kept the domain enabled, so the next step's "
            "enable is a no-op that replays nothing"
        )


class TestThePageAndRuntimeExemption:
    """The run's own two domains. A step may turn them on, never off."""

    @pytest.mark.parametrize("domain", sorted(RUN_OWNED_DOMAINS))
    def test_they_are_enabled_but_never_disabled(self, domain):
        cdp = FakeCDP()
        asyncio.run(_use(cdp, [domain]))
        assert cdp.calls == [f"{domain}.enable"], (
            f"a step disabled {domain}. The frame manager reads Page events, so "
            "that breaks frame selection for every step after it."
        )

    def test_the_exemption_is_exactly_page_and_runtime(self):
        assert frozenset({"Page", "Runtime"}) == RUN_OWNED_DOMAINS

    def test_an_exempt_domain_beside_an_ordinary_one(self):
        cdp = FakeCDP()
        asyncio.run(_use(cdp, ["Runtime", "Network"]))
        assert cdp.calls == ["Runtime.enable", "Network.enable", "Network.disable"]


class TestADomainThatCannotBeTurnedOnOrOff:
    """Some domains have no enable, and a step must not fail over it."""

    def test_a_missing_enable_is_not_an_error(self):
        cdp = FakeCDP(no_enable={"Fake"})
        asyncio.run(_use(cdp, ["Fake"]))
        assert cdp.calls == ["Fake.enable"]

    def test_and_nothing_tries_to_disable_it(self):
        cdp = FakeCDP(no_enable={"Fake"})
        asyncio.run(_use(cdp, ["Fake"]))
        assert "Fake.disable" not in cdp.calls, (
            "a domain that has no enable was sent a disable, which is one "
            "wasted round trip per step for a call that cannot work"
        )

    def test_a_missing_disable_does_not_fail_the_step(self):
        cdp = FakeCDP(no_disable={"Network"})
        asyncio.run(_use(cdp, ["Network"]))
        assert cdp.calls == ["Network.enable", "Network.disable"]

    def test_a_dropped_connection_during_cleanup_does_not_mask_the_failure(self):
        """The disable runs in a `finally`. Raising there would replace the
        exception the step actually failed with."""
        cdp = FakeCDP()
        sent: list[str] = []

        async def send(method, params=None, session_id=None):
            sent.append(method)
            if method.endswith(".disable"):
                raise ConnectionError("websocket closed")
            return {}

        cdp.send = send  # type: ignore[method-assign]

        async def body():
            raise RuntimeError("the real failure")

        with pytest.raises(RuntimeError, match="the real failure"):
            asyncio.run(_use(cdp, ["Network"], body))
        assert "Network.disable" in sent


class TestTheVerbsThatEnableDomains:
    """The rule reaches the verbs, not only the helper."""

    def test_wait_gives_back_the_event_s_domain(self):
        cdp = FakeCDP()

        async def drive():
            task = asyncio.ensure_future(
                events.wait_on_session(cdp, "S1", "Network.requestWillBeSent", None, 0.05)
            )
            with pytest.raises(events.WaitTimeout):
                await task

        asyncio.run(drive())
        assert cdp.calls == ["Network.enable", "Network.disable"]
        assert cdp.handlers.get("Network.requestWillBeSent") == [], (
            "the wait left its handler registered, so it keeps pushing into a "
            "queue nobody reads for the rest of the run"
        )

    def test_wait_does_not_disable_runtime(self):
        cdp = FakeCDP()

        async def drive():
            with pytest.raises(events.WaitTimeout):
                await events.wait_on_session(
                    cdp, "S1", "Runtime.executionContextCreated", None, 0.05
                )

        asyncio.run(drive())
        assert cdp.calls == ["Runtime.enable"]

    def test_console_list_gives_runtime_back_without_disabling_it(self):
        cdp = FakeCDP()
        asyncio.run(list_verbs.collect_on_session(cdp, "S1", list_verbs.CONSOLE_EVENTS, 0))
        assert "Runtime.disable" not in cdp.calls
        assert all(event in cdp.handlers for event in list_verbs.CONSOLE_EVENTS)
        assert all(cdp.handlers[event] == [] for event in list_verbs.CONSOLE_EVENTS), (
            "a collection left its handlers registered"
        )

    def test_network_list_gives_network_back(self):
        cdp = FakeCDP()
        asyncio.run(list_verbs.collect_on_session(cdp, "S1", list_verbs.NETWORK_EVENTS, 0))
        assert "Network.enable" in cdp.calls
        assert "Network.disable" in cdp.calls, (
            "a second network-list in one run would get a no-op enable and so "
            "lose the events that fire during it"
        )

    def test_two_collections_in_one_session_each_get_a_first_enable(self):
        """The property the whole rule exists for."""
        cdp = FakeCDP()
        asyncio.run(list_verbs.collect_on_session(cdp, "S1", list_verbs.NETWORK_EVENTS, 0))
        asyncio.run(list_verbs.collect_on_session(cdp, "S1", list_verbs.NETWORK_EVENTS, 0))
        assert cdp.calls == [
            "Network.enable",
            "Network.disable",
            "Network.enable",
            "Network.disable",
        ]


class TestTheSelectedFrameAfterANavigation:
    """A stale frame id read by a later step reports success on the wrong frame."""

    def _manager(self):
        from browser_tools.frame_manager import FrameManager

        manager = FrameManager()
        manager.handle_frame_navigated(
            {"frame": {"id": "ROOT", "url": "https://shop.test/", "securityOrigin": "x"}}
        )
        manager.handle_frame_attached({"frameId": "CHECKOUT", "parentFrameId": "ROOT"})
        manager.handle_frame_navigated(
            {
                "frame": {
                    "id": "CHECKOUT",
                    "url": "https://pay.test/checkout",
                    "securityOrigin": "y",
                    "parentId": "ROOT",
                }
            }
        )
        return manager

    def test_a_selection_follows_its_pattern_across_a_navigation(self):
        """The selected frame leaves the pattern and another frame matches it.

        Not two frames matching at once: `_resolve_frame_by_url` takes the
        first match, so with both matching it would keep the original and the
        test would prove nothing about following.
        """
        manager = self._manager()
        assert manager.select_frame_by_url("checkout") is not None

        manager.handle_frame_attached({"frameId": "CHECKOUT2", "parentFrameId": "ROOT"})
        manager.handle_frame_navigated(
            {
                "frame": {
                    "id": "CHECKOUT2",
                    "url": "https://pay.test/checkout/step-2",
                    "parentId": "ROOT",
                }
            }
        )
        # The originally selected frame leaves the pattern.
        manager.handle_frame_navigated(
            {"frame": {"id": "CHECKOUT", "url": "https://pay.test/done", "parentId": "ROOT"}}
        )

        selected = manager.get_selected_frame()
        assert selected is not None, "the selection was cleared where a frame still matched"
        assert selected.frame_id == "CHECKOUT2", (
            "the selection did not follow its pattern, so a later step reads "
            "the frame that was there before the navigation"
        )

    def test_a_pattern_that_stops_matching_clears_the_selection(self):
        manager = self._manager()
        assert manager.select_frame_by_url("checkout") is not None
        # Everything navigates away from the pattern.
        manager.handle_frame_navigated(
            {"frame": {"id": "CHECKOUT", "url": "https://pay.test/done", "parentId": "ROOT"}}
        )
        assert manager.get_selected_frame() is None, (
            "the stale frame id survived a re-resolution that found nothing, so "
            "the next frame-scoped step reads a frame whose URL no longer "
            "matches the pattern the caller selected, and reports success"
        )

    def test_the_pattern_survives_the_clear(self):
        """The caller selected by pattern, so a matching frame coming back
        re-points the selection at it."""
        manager = self._manager()
        manager.select_frame_by_url("checkout")
        manager.handle_frame_navigated(
            {"frame": {"id": "CHECKOUT", "url": "https://pay.test/done", "parentId": "ROOT"}}
        )
        assert manager.get_selected_frame() is None
        manager.handle_frame_navigated(
            {"frame": {"id": "CHECKOUT", "url": "https://pay.test/checkout", "parentId": "ROOT"}}
        )
        selected = manager.get_selected_frame()
        assert selected is not None, (
            "the pattern was dropped along with the id, so a frame that matches "
            "again does not come back. Only `frames reset` clears the pattern."
        )
        assert selected.frame_id == "CHECKOUT"

    def test_reset_clears_the_pattern_for_good(self):
        manager = self._manager()
        manager.select_frame_by_url("checkout")
        manager.reset_frame()
        manager.handle_frame_navigated(
            {"frame": {"id": "CHECKOUT", "url": "https://pay.test/checkout", "parentId": "ROOT"}}
        )
        assert manager.get_selected_frame() is None, (
            "a navigation re-selected a frame after `frames reset`"
        )


class TestTheFramesOfThePageYouLeft:
    """Chrome sends no `Page.frameDetached` when a parent navigates.

    Measured with `bt attach +Page.frameDetached +Page.frameNavigated` across
    a navigation: only `frameNavigated` arrives. So nothing here can learn
    from an event that the old document's child frames are gone, and before
    this they stayed in the tree - `frames list` reported frames that no
    longer existed, and a selection re-resolved onto one and read it.
    """

    @staticmethod
    def _urls(manager) -> str:
        """Every frame the manager would list, as one searchable string."""
        return " ".join(frame.get("url", "") for frame in manager.get_flat_frames())

    def _page_with_a_checkout_frame(self):
        """Bootstrapped from `Page.getFrameTree`, the way the runtime does it."""
        from browser_tools.frame_manager import FrameManager

        manager = FrameManager()
        manager.update_from_frame_tree(
            {
                "frame": {"id": "ROOT", "url": "https://shop.test/"},
                "childFrames": [
                    {
                        "frame": {
                            "id": "CHECKOUT",
                            "url": "https://pay.test/checkout",
                            "parentId": "ROOT",
                        }
                    }
                ],
            }
        )
        return manager

    def test_a_navigation_forgets_the_old_documents_frames(self):
        manager = self._page_with_a_checkout_frame()
        manager.handle_frame_navigated({"frame": {"id": "ROOT", "url": "https://shop.test/done"}})
        listed = self._urls(manager)
        assert "checkout" not in listed.lower(), (
            f"a frame from the page we navigated away from is still listed: {listed}"
        )

    def test_the_navigated_frame_itself_survives(self):
        manager = self._page_with_a_checkout_frame()
        manager.handle_frame_navigated({"frame": {"id": "ROOT", "url": "https://shop.test/done"}})
        assert "done" in self._urls(manager)

    def test_a_grandchild_goes_too(self):
        """Checked against what can still be selected, not against the listing.

        `get_flat_frames` walks the `children` lists, so a frame orphaned by
        pruning only its parent drops out of the listing while staying in the
        flat map. `_resolve_frame_by_url` iterates that map directly, so an
        orphan is still selectable: the listing agreeing is not enough.
        """
        manager = self._page_with_a_checkout_frame()
        manager.handle_frame_attached({"frameId": "WIDGET", "parentFrameId": "CHECKOUT"})
        manager.handle_frame_navigated(
            {"frame": {"id": "WIDGET", "url": "https://pay.test/widget", "parentId": "CHECKOUT"}}
        )
        manager.handle_frame_navigated({"frame": {"id": "ROOT", "url": "https://shop.test/done"}})

        assert manager.select_frame_by_url("widget") is None, (
            "a frame two levels inside the page we navigated away from can "
            "still be selected, and a read against it would report success"
        )
        assert "widget" not in self._urls(manager).lower()

    def test_a_child_navigating_forgets_its_own_children_only(self):
        manager = self._page_with_a_checkout_frame()
        manager.handle_frame_attached({"frameId": "WIDGET", "parentFrameId": "CHECKOUT"})
        manager.handle_frame_navigated(
            {"frame": {"id": "WIDGET", "url": "https://pay.test/widget", "parentId": "CHECKOUT"}}
        )
        manager.handle_frame_navigated(
            {"frame": {"id": "CHECKOUT", "url": "https://pay.test/checkout/2", "parentId": "ROOT"}}
        )
        assert manager.select_frame_by_url("widget") is None, (
            "the navigating frame kept a child from its old document"
        )
        listed = self._urls(manager)
        assert "checkout/2" in listed, "the frame that navigated was itself forgotten"
        assert "shop.test" in listed, "an unrelated frame was forgotten"

    def test_a_selection_inside_the_forgotten_subtree_is_cleared(self):
        manager = self._page_with_a_checkout_frame()
        assert manager.select_frame_by_url("checkout") is not None
        manager.handle_frame_navigated({"frame": {"id": "ROOT", "url": "https://shop.test/done"}})
        assert manager.get_selected_frame() is None, (
            "the selection still points into the page we navigated away from"
        )
