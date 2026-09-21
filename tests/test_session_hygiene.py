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
from doubles import FakeHandler

from browser_tools import events, list_verbs
from browser_tools.core.errors import CDPError
from browser_tools.one_shot import RUN_OWNED_DOMAINS, domains_enabled


class FakeCDP:
    """Records every method sent, and which handlers are still registered."""

    def __init__(self, no_enable: set[str] | None = None, no_disable: set[str] | None = None):
        self.calls: list[str] = []
        #: Every send and subscribe with the session it carried. Unsubscribes
        #: are absent because `CDPClient.off` takes no session. A double that
        #: drops `session_id` lets a mutation sending the whole run's traffic
        #: to the wrong session pass the suite, which is where this came from.
        self.sessions: list[tuple[str, str | None]] = []
        self.handlers: dict[str, list[Any]] = {}
        self._no_enable = no_enable or set()
        self._no_disable = no_disable or set()

    async def send(self, method: str, params: Any = None, session_id: str | None = None) -> dict:
        self.calls.append(method)
        self.sessions.append((method, session_id))
        domain = method.split(".")[0]
        if method.endswith(".enable") and domain in self._no_enable:
            raise CDPError(-32601, f"'{method}' wasn't found")
        if method.endswith(".disable") and domain in self._no_disable:
            raise CDPError(-32601, f"'{method}' wasn't found")
        return {}

    def on(self, event: str, callback: Any, session_id: str | None = None) -> None:
        self.sessions.append((f"on:{event}", session_id))
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event: str, params: Any = None) -> None:
        """Deliver an event to whoever is subscribed right now."""
        for callback in list(self.handlers.get(event, [])):
            callback(params or {})

    def off(self, event: str, callback: Any) -> None:
        # No session argument, matching `CDPClient.off`, which removes by
        # callback identity across every session.
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


class TestEveryCommandGoesToTheRunsOwnSession:
    """A browser-level connection carries several sessions at once.

    A command sent without a `session_id`, or with another target's, is
    answered by the browser rather than by the page the run attached to. It
    does not fail: `Network.enable` succeeds at browser level and subscribes
    nothing useful, so the collection returns empty and the run exits 0.

    The suite could not see this. `collect_on_session` was checked on the
    methods it sends, and the double ignored the session argument, so
    sending every command to the wrong session changed nothing a test read.
    """

    def test_a_collection_sends_and_subscribes_on_the_session_it_was_given(self):
        cdp = FakeCDP()
        asyncio.run(list_verbs.collect_on_session(cdp, "S1", list_verbs.NETWORK_EVENTS, 0))
        wrong = [entry for entry in cdp.sessions if entry[1] != "S1"]
        assert cdp.sessions and not wrong, f"these did not carry session S1: {wrong}"

    def test_a_wait_sends_and_subscribes_on_the_session_it_was_given(self):
        cdp = FakeCDP()

        async def drive():
            with pytest.raises(events.WaitTimeout):
                await events.wait_on_session(cdp, "S1", "Network.requestWillBeSent", None, 0.05)

        asyncio.run(drive())
        wrong = [entry for entry in cdp.sessions if entry[1] != "S1"]
        assert cdp.sessions and not wrong, f"these did not carry session S1: {wrong}"

    def test_the_helper_sends_on_the_session_it_was_given(self):
        cdp = FakeCDP()
        asyncio.run(_use(cdp, ["Network", "Log"]))
        assert cdp.sessions == [
            ("Network.enable", "S1"),
            ("Log.enable", "S1"),
            ("Log.disable", "S1"),
            ("Network.disable", "S1"),
        ]


class TestWaitOnlySeesWhatArrivesAfterItSubscribes:
    """Why the manual sends you to `wait-idle` after a navigation.

    `wait` is edge-triggered. It registers a handler when its own step
    starts, so an event that arrived in the gap between the navigate step and
    the wait step is already gone. The wait then times out and fails the run
    on a page that finished loading before the wait began. Keeping Page
    enabled across the run does not help: an enabled domain delivers events,
    it does not replay them.
    """

    def test_an_event_delivered_before_the_wait_is_not_seen(self):
        cdp = FakeCDP()

        async def drive():
            cdp.emit("Page.loadEventFired", {"timestamp": 1})
            with pytest.raises(events.WaitTimeout):
                await events.wait_on_session(cdp, "S1", "Page.loadEventFired", None, 0.05)

        asyncio.run(drive())

    def test_an_event_delivered_after_the_wait_subscribes_is_seen(self):
        cdp = FakeCDP()

        async def drive():
            task = asyncio.ensure_future(
                events.wait_on_session(cdp, "S1", "Page.loadEventFired", None, 1.0)
            )
            while "Page.loadEventFired" not in cdp.handlers:
                await asyncio.sleep(0)
            cdp.emit("Page.loadEventFired", {"timestamp": 1})
            return await task

        assert asyncio.run(drive()) == {
            "method": "Page.loadEventFired",
            "params": {"timestamp": 1},
        }


class TestAHandWrittenEnableIsNotUndone:
    """Rule 1 and rule 3 collide, and the precedence is stated.

    Rule 1 says a step gives back every domain it turned on. Rule 3 says a
    domain the caller enabled by hand in a raw step is that step's whole
    output and is not undone. A raw `Network.enable` step followed by a
    `network-list` step satisfies both descriptions, and CDP cannot tell
    them apart: a second `Network.enable` succeeds exactly like a first, so
    the reply says nothing about who turned the domain on.

    The caller's enable wins. `keep` carries the domains a raw step named,
    and the helper leaves them alone on the way out. The cost is that the
    curated step does not get a first enable on that domain, which is the
    same exception `Page` and `Runtime` already carry.
    """

    def test_a_kept_domain_is_enabled_and_left_on(self):
        cdp = FakeCDP()

        async def drive():
            async with domains_enabled(cdp, "S1", ["Network"], keep={"Network"}):
                pass

        asyncio.run(drive())
        assert cdp.calls == ["Network.enable"]

    def test_a_domain_the_caller_did_not_name_is_still_given_back(self):
        cdp = FakeCDP()

        async def drive():
            async with domains_enabled(cdp, "S1", ["Network", "Log"], keep={"Network"}):
                pass

        asyncio.run(drive())
        assert cdp.calls == ["Network.enable", "Log.enable", "Log.disable"]

    def test_keeping_a_domain_the_step_never_enables_changes_nothing(self):
        cdp = FakeCDP()

        async def drive():
            async with domains_enabled(cdp, "S1", ["Log"], keep={"Network"}):
                pass

        asyncio.run(drive())
        assert cdp.calls == ["Log.enable", "Log.disable"]

    def test_a_collection_after_a_hand_written_enable_leaves_it_on(self):
        cdp = FakeCDP()

        async def drive():
            await cdp.send("Network.enable", session_id="S1")
            await list_verbs.collect_on_session(
                cdp, "S1", list_verbs.NETWORK_EVENTS, 0, frozenset({"Network"})
            )

        asyncio.run(drive())
        assert "Network.disable" not in cdp.calls, (
            "the collection disabled the domain the caller's own step turned "
            "on, which makes that step a no-op"
        )

    def test_a_wait_after_a_hand_written_enable_leaves_it_on(self):
        cdp = FakeCDP()

        async def drive():
            await cdp.send("Network.enable", session_id="S1")
            with pytest.raises(events.WaitTimeout):
                await events.wait_on_session(
                    cdp, "S1", "Network.requestWillBeSent", None, 0.05, frozenset({"Network"})
                )

        asyncio.run(drive())
        assert "Network.disable" not in cdp.calls


class TestTheRunRemembersWhichDomainsTheCallerEnabled:
    """`keep` is only as good as what fills it.

    The handler carries the set, because it is the one thing every step of a
    run shares. A raw step tells it what it just sent; a curated step reads
    it on the way into the helper.
    """

    def _runtime(self):
        from browser_tools.cdp_handler import CDPRuntime

        return CDPRuntime.__new__(CDPRuntime)

    def test_it_starts_empty(self):
        assert self._runtime().caller_enabled == frozenset()

    def test_an_enable_is_recorded_under_its_domain(self):
        runtime = self._runtime()
        runtime.record_caller_enable("Network.enable")
        assert runtime.caller_enabled == frozenset({"Network"})

    def test_two_enables_accumulate(self):
        runtime = self._runtime()
        runtime.record_caller_enable("Network.enable")
        runtime.record_caller_enable("Log.enable")
        assert runtime.caller_enabled == frozenset({"Network", "Log"})

    @pytest.mark.parametrize(
        "method",
        ["Network.getCookies", "Page.navigate", "Runtime.evaluate", "enable", "Network.enabled"],
    )
    def test_anything_that_is_not_an_enable_is_ignored(self, method):
        runtime = self._runtime()
        runtime.record_caller_enable(method)
        assert runtime.caller_enabled == frozenset()

    def test_two_runtimes_do_not_share_the_set(self):
        """The class-level default is a `frozenset`, and recording rebinds it.

        A mutable default here would make one run's hand-written enable
        survive into every other run built the same way.
        """
        first, second = self._runtime(), self._runtime()
        first.record_caller_enable("Network.enable")
        assert second.caller_enabled == frozenset()

    def test_a_raw_step_records_what_it_sent(self):
        """The step run is the only thing that knows a raw step happened."""
        from browser_tools import step_run
        from browser_tools.step_list import Step

        handler = FakeHandler()
        step = Step(
            number=1,
            line=1,
            text="Network.enable {}",
            argv=["Network.enable", "{}"],
            args=None,
            method="Network.enable",
            params={},
        )
        step_run.execute([step], handler)
        assert handler.recorded_enables == ["Network.enable"]
        assert handler.caller_enabled == frozenset({"Network"})

    def test_a_curated_step_records_nothing(self):
        """Only a method the caller typed counts. A curated verb enabling a
        domain through the helper gives it back, which is rule 1."""
        import argparse

        from browser_tools import step_run
        from browser_tools.step_list import Step

        handler = FakeHandler()
        args = argparse.Namespace(
            command="snapshot", instance=None, target=None, url=None, endpoint=None
        )
        step = Step(number=1, line=1, text="snapshot", argv=["snapshot"], args=args)
        step_run.execute([step], handler)
        assert handler.recorded_enables == []


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


class TestAReadBorrowsTheSelectionAndGivesItBack:
    """`storage get --key` used to move the run's selection and leave it there.

    The key names the frame for that one read. In a one-shot invocation the
    difference does not show, because the selection dies with the process. In
    a Step Run the selection outlives the step, so step 4 reading
    `--key checkout` silently re-pointed every later frame-scoped step at the
    checkout frame. `GUIDE.txt` says only `frames reset` and `frames select`
    change the pattern, and that sentence was false.
    """

    def _manager(self):
        from browser_tools.frame_manager import FrameManager

        frames = FrameManager()
        frames.update_from_frame_tree(
            {
                "frame": {"id": "R", "url": "https://x.test/"},
                "childFrames": [
                    {"frame": {"id": "C", "url": "https://x.test/checkout"}},
                    {"frame": {"id": "A", "url": "https://x.test/account"}},
                ],
            }
        )
        return frames

    def _handler(self, frames):
        from browser_tools.cdp_handler import CDPHandler, CDPRuntime

        runtime = CDPRuntime.__new__(CDPRuntime)
        runtime._frame_manager = frames
        handler = CDPHandler.__new__(CDPHandler)
        handler._rt = runtime
        return handler

    def test_the_pattern_comes_back(self):
        frames = self._manager()
        frames.select_frame_by_url("account")
        with self._handler(frames).borrowed_frame_selection():
            frames.select_frame_by_url("checkout")
            assert frames.get_selected_frame().frame_id == "C"
        assert frames.selection_pattern == "account"
        assert frames.get_selected_frame().frame_id == "A"

    def test_no_selection_comes_back_as_no_selection(self):
        frames = self._manager()
        with self._handler(frames).borrowed_frame_selection():
            frames.select_frame_by_url("checkout")
        assert frames.selection_pattern is None
        assert frames.get_selected_frame() is None

    def test_a_failing_read_still_gives_it_back(self):
        """The path that matters: the read raises and the run carries on."""
        frames = self._manager()
        frames.select_frame_by_url("account")
        handler = self._handler(frames)
        with pytest.raises(ValueError), handler.borrowed_frame_selection():
            frames.select_frame_by_url("checkout")
            raise ValueError("the read failed")
        assert frames.selection_pattern == "account"

    def test_it_survives_a_handler_with_no_frame_manager(self):
        """A handler whose connection never came up has none."""
        handler = self._handler(None)
        with handler.borrowed_frame_selection():
            pass

    def test_storage_get_with_a_key_leaves_the_selection_alone(self):
        """The whole point, at the verb."""
        from browser_tools import curated

        frames = self._manager()
        frames.select_frame_by_url("account")
        handler = self._handler(frames)
        calls: list[tuple[str, dict]] = []

        def call_tool(name, arguments):
            from browser_tools.mcp_response import make_text

            calls.append((name, arguments))
            if name == "select_frame":
                frames.select_frame_by_url(arguments["url_pattern"])
            return make_text("Cookies (0):")

        handler.call_tool = call_tool
        curated.storage_get(instance=None, key="checkout", handler=handler)

        assert [name for name, _ in calls] == ["select_frame", "get_frame_storage"]
        assert frames.selection_pattern == "account", (
            "the read kept the frame it borrowed, so every later step in the "
            "run reads that frame instead of the one the caller selected"
        )

    def test_storage_get_without_a_key_selects_nothing(self):
        from browser_tools import curated

        frames = self._manager()
        frames.select_frame_by_url("account")
        handler = self._handler(frames)
        calls: list[str] = []

        def call_tool(name, arguments):
            from browser_tools.mcp_response import make_text

            calls.append(name)
            return make_text("Cookies (0):")

        handler.call_tool = call_tool
        curated.storage_get(instance=None, handler=handler)

        assert calls == ["get_frame_storage"]
        assert frames.selection_pattern == "account"


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


class TestTheTwoViewsOfTheFrameTreeAgree:
    """`_frames` is one representation, the `children` lists are another.

    `frames list` and the frame tree walk the children. A selection used to
    walk the map. So a frame in the map that no parent held was invisible to
    the caller and selectable anyway, and `storage get` would read a frame
    `frames list` said did not exist. That is the orphan the detach fix was
    about, reached by two other routes.

    Each test below drives an event sequence and then asserts the same
    invariant, rather than asserting the particular ids. A view that can
    disagree with the other has somewhere to hide otherwise: the surviving
    mutation that started this changed which frames a navigation forgets, and
    every id-by-id test still passed while the tree and the list disagreed
    about a frame's URL.
    """

    def _manager(self):
        from browser_tools.frame_manager import FrameManager

        frames = FrameManager()
        frames.update_from_frame_tree(
            {
                "frame": {"id": "R", "url": "https://x.test/"},
                "childFrames": [
                    {
                        "frame": {"id": "C", "url": "https://x.test/checkout"},
                        "childFrames": [
                            {"frame": {"id": "G", "url": "https://x.test/pay"}}
                        ],
                    }
                ],
            }
        )
        return frames

    @staticmethod
    def _assert_one_set_of_frames(frames):
        """The tree, the flat list, and what a selection can reach: one set."""
        walked: list[str] = []

        def walk(node):
            walked.append(node["frameId"])
            for child in node.get("children", []):
                walk(child)

        tree = frames.get_frame_tree()
        if tree is not None:
            walk(tree)
        listed = [entry["frameId"] for entry in frames.get_flat_frames()]

        assert walked == listed, (
            f"the tree shows {walked} and `frames list` shows {listed}"
        )
        assert len(set(walked)) == len(walked), f"a frame appears twice: {walked}"

        urls = {entry["frameId"]: entry["url"] for entry in frames.get_flat_frames()}
        for frame_id, url in urls.items():
            if not url:
                continue
            resolved = frames._resolve_frame_by_url(url)
            assert resolved is not None, f"{frame_id} is listed but cannot be selected"
        stranded = sorted(set(frames._frames) - set(walked))
        assert not stranded, (
            f"{stranded} sit in the frame map with nothing holding them. They "
            "are invisible to `frames list` and they are never collected, "
            "because the walk that would reach them goes through the parent "
            "that no longer exists."
        )

    def test_a_pattern_two_frames_match_selects_the_first_one_listed(self):
        """`frames list` order is the selection order, and the map's is not.

        Two frames can match one pattern, so the order decides which the
        caller gets, and it has to be the order the caller read. The two
        orders do diverge: re-attaching a frame moves it to the end of the
        map while its position among its parent's children is unchanged. This
        sequence is what a re-parent looks like, and under it the map says A
        and the listing says B.
        """
        from browser_tools.frame_manager import FrameManager

        frames = FrameManager()
        frames.update_from_frame_tree({"frame": {"id": "R", "url": "https://x.test/"}})
        for frame_id, url in (("A", "pay/a"), ("B", "pay/b")):
            frames.handle_frame_attached({"frameId": frame_id, "parentFrameId": "R"})
            frames.handle_frame_navigated(
                {"frame": {"id": frame_id, "url": f"https://x.test/{url}", "parentId": "R"}}
            )
        frames.handle_frame_attached({"frameId": "A", "parentFrameId": "R"})
        frames.handle_frame_navigated(
            {"frame": {"id": "A", "url": "https://x.test/pay/a2", "parentId": "R"}}
        )

        listed = [entry["frameId"] for entry in frames.get_flat_frames()]
        assert listed == ["R", "B", "A"], listed
        assert list(frames._frames) == ["R", "A", "B"], (
            "this test needs the two orders to differ, and they no longer do"
        )
        assert [f.frame_id for f in frames._reachable_frames()] == listed
        assert frames.select_frame_by_url("pay").frame_id == "B", (
            "the selection resolved against the frame map, so it picked a "
            "frame the caller's `frames list` showed second"
        )

    def test_the_starting_tree_agrees_with_itself(self):
        self._assert_one_set_of_frames(self._manager())

    def test_after_a_child_navigates(self):
        """The sequence that outlived every id-by-id test."""
        frames = self._manager()
        frames.handle_frame_navigated(
            {"frame": {"id": "C", "url": "https://x.test/checkout/2", "parentId": "R"}}
        )
        self._assert_one_set_of_frames(frames)
        listed = {entry["frameId"]: entry["url"] for entry in frames.get_flat_frames()}
        assert listed["C"] == "https://x.test/checkout/2"
        assert "G" not in listed, "the old document's child survived the navigation"

    def test_after_the_root_navigates(self):
        frames = self._manager()
        frames.handle_frame_navigated({"frame": {"id": "R", "url": "https://x.test/done"}})
        self._assert_one_set_of_frames(frames)

    def test_after_a_frame_navigates_before_it_attaches(self):
        """Chrome can deliver frameNavigated for a frame we never saw attach."""
        frames = self._manager()
        frames.handle_frame_navigated(
            {"frame": {"id": "N", "url": "https://x.test/new", "parentId": "R"}}
        )
        self._assert_one_set_of_frames(frames)
        listed = [entry["frameId"] for entry in frames.get_flat_frames()]
        assert "N" in listed, (
            "the frame was in the map and not in the tree, so `frames select "
            "new` found what `frames list` denied"
        )

    def test_after_the_same_frame_attaches_twice(self):
        frames = self._manager()
        frames.handle_frame_attached({"frameId": "C", "parentFrameId": "R"})
        self._assert_one_set_of_frames(frames)

    def test_a_re_parented_frame_has_one_parent(self):
        """The old parent kept holding it, so `frames list` printed it twice.

        Chrome re-parents a frame in ordinary use, so this is not a defensive
        case. The duplicate also nested the frame under itself in the tree.
        """
        frames = self._manager()
        frames.handle_frame_attached({"frameId": "N", "parentFrameId": "R"})
        frames.handle_frame_attached({"frameId": "N", "parentFrameId": "C"})
        self._assert_one_set_of_frames(frames)
        listed = [entry["frameId"] for entry in frames.get_flat_frames()]
        assert listed.count("N") == 1, listed

    def test_an_attach_that_would_make_a_frame_its_own_ancestor_drops_it(self):
        """The tree cannot hold a cycle, and an island is worse than a drop.

        Keeping the pieces would leave frames in the map that `frames list`
        never shows and no walk ever collects, and `storage get` would read
        one of them if a pattern matched.
        """
        frames = self._manager()
        frames.handle_frame_attached({"frameId": "C", "parentFrameId": "G"})
        self._assert_one_set_of_frames(frames)
        listed = [entry["frameId"] for entry in frames.get_flat_frames()]
        assert listed == ["R"], listed

    def test_a_cycle_already_in_the_map_does_not_hang_the_ancestor_walk(self):
        frames = self._manager()
        frames._frames["C"].parent_frame_id = "G"
        frames._frames["G"].parent_frame_id = "C"
        assert frames._is_descendant("G", "R") is False
        assert frames._is_descendant("C", "C") is True

    def test_after_a_detach_and_a_navigation(self):
        frames = self._manager()
        frames.handle_frame_detached({"frameId": "C", "reason": "remove"})
        frames.handle_frame_navigated({"frame": {"id": "R", "url": "https://x.test/done"}})
        self._assert_one_set_of_frames(frames)

    def test_after_an_attach_then_a_navigation_of_the_parent(self):
        frames = self._manager()
        frames.handle_frame_attached({"frameId": "N", "parentFrameId": "C"})
        frames.handle_frame_navigated(
            {"frame": {"id": "C", "url": "https://x.test/checkout/2", "parentId": "R"}}
        )
        self._assert_one_set_of_frames(frames)
        assert "N" not in [entry["frameId"] for entry in frames.get_flat_frames()]


class TestAStepCannotTurnOffWhatTheRunOwns:
    """The RFC's "a caller undoes their own enable" rule does not cover this.

    `Page.disable` is not undoing an enable the caller wrote. It turns off a
    domain the run owns, and the run never learns anything again. Measured
    before the refusal, against a real browser, with a run whose step 2 was
    `Page.disable '{}'`:

    - step 3 navigated to a page with no iframe;
    - step 5 `frames list` reported the page the run had left, iframe and all;
    - step 6 `storage get` read a frame that no longer existed and succeeded;
    - the run exited 0.

    Nothing failed, so nothing told the caller. That is the failure the rest
    of this module exists to prevent, reachable in one line.
    """

    @pytest.mark.parametrize("domain", sorted(RUN_OWNED_DOMAINS))
    def test_disabling_a_run_owned_domain_is_refused(self, domain, monkeypatch):
        from browser_tools import step_list

        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        with pytest.raises(step_list.StepListError) as exc:
            step_list.validate(f"snapshot\n{domain}.disable\n")
        assert "line 2" in str(exc.value)
        assert "cannot be a step" in str(exc.value)

    @pytest.mark.parametrize("domain", sorted(RUN_OWNED_DOMAINS))
    def test_the_refusal_names_a_remedy_that_works(self, domain, monkeypatch):
        """RFC-01: every refusal names a remedy, and the remedy must work."""
        from browser_tools import step_list

        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        with pytest.raises(step_list.StepListError) as exc:
            step_list.validate(f"{domain}.disable\n")
        assert "on its own, outside a run" in str(exc.value)

    def test_nothing_runs_when_a_step_list_contains_one(self, monkeypatch, tmp_path):
        from browser_tools import curated, step_list, step_run

        def refuse(*args, **kwargs):
            raise AssertionError("the run connected before refusing Page.disable")

        monkeypatch.setattr(curated, "run_session", refuse)
        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        source = tmp_path / "steps.txt"
        source.write_text("snapshot\nPage.disable\nsnapshot\n")
        with pytest.raises(step_list.StepListError):
            step_run.run(instance=None, source=str(source), registry_path=None)

    @pytest.mark.parametrize("domain", sorted(RUN_OWNED_DOMAINS))
    def test_enabling_one_by_hand_is_still_allowed(self, domain, monkeypatch):
        """Only the disable is refused. A redundant enable harms nothing."""
        from browser_tools import step_list

        monkeypatch.setattr("browser_tools.lifecycle.read_instances", lambda registry_path=None: [])
        steps = step_list.validate(f"{domain}.enable\n")
        assert steps[0].as_method()[0] == f"{domain}.enable"

    def test_another_domain_may_be_disabled_by_hand(self):
        """A domain the caller turned on is the caller's to turn off."""
        from browser_tools import step_list

        steps = step_list.validate("Network.disable\n")
        assert steps[0].as_method()[0] == "Network.disable"
