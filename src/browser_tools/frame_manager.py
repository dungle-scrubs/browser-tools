"""Frame tree management for browser-tools.

Maintains a frame tree built from CDP Page.getFrameTree responses and kept
up-to-date via frame lifecycle events. Handles frame selection by URL pattern
(D-002) and execution context resolution.

This module is used by the daemon to manage frame state across tool calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FrameInfo:
    """Information about a single frame in the page tree."""

    frame_id: str
    url: str
    security_origin: str
    name: str
    parent_frame_id: str | None = None
    execution_context_id: int | None = None
    children: list[FrameInfo] = field(default_factory=list)
    #: The Frame Session that answers for this frame (RFC-04). ``None`` is the
    #: page session, which is what every frame was before OOPIFs.
    frame_session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a serializable dictionary.

        Returns:
            Dictionary with frame info and nested children.
        """
        result: dict[str, Any] = {
            "frameId": self.frame_id,
            "url": self.url,
            "securityOrigin": self.security_origin,
            "name": self.name,
        }
        if self.parent_frame_id:
            result["parentFrameId"] = self.parent_frame_id
        if self.frame_session_id:
            result["frameSessionId"] = self.frame_session_id
        if self.execution_context_id is not None:
            result["executionContextId"] = self.execution_context_id
        if self.children:
            result["children"] = [child.to_dict() for child in self.children]
        return result


@dataclass
class FrameEvent:
    """A buffered frame lifecycle event."""

    event_type: str
    frame_id: str
    url: str | None = None
    timestamp: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a serializable dictionary.

        Returns:
            Dictionary with event info.
        """
        result: dict[str, Any] = {
            "type": self.event_type,
            "frameId": self.frame_id,
            "timestamp": self.timestamp,
        }
        if self.url:
            result["url"] = self.url
        if self.details:
            result["details"] = self.details
        return result


class FrameManager:
    """Manages the frame tree and frame selection state.

    Tracks all frames in the page, handles frame lifecycle events, and
    resolves frame selection by URL pattern.
    """

    def __init__(self) -> None:
        """Initialize an empty frame manager."""
        self._frames: dict[str, FrameInfo] = {}
        self._root_frame_id: str | None = None
        self._selected_frame_id: str | None = None
        self._selected_url_pattern: str | None = None
        self._event_buffer: list[FrameEvent] = []
        #: ``(frame session, context id) -> frame id``. Keyed by the pair
        #: because an execution context id is renderer-local: two frames in
        #: two Frame Sessions can both own context 1, and a map keyed by the
        #: id alone let the second one overwrite the first, then let a
        #: destroy for either clear the wrong frame (RFC-04, Routing). The
        #: page session is ``None``, matching ``FrameInfo.frame_session_id``.
        self._execution_contexts: dict[tuple[str | None, int], str] = {}

    @property
    def selected_frame_id(self) -> str | None:
        """Currently selected frame ID, or None for top-level."""
        return self._selected_frame_id

    @property
    def root_frame_id(self) -> str | None:
        """The root (top-level) frame ID."""
        return self._root_frame_id

    def update_from_frame_tree(self, frame_tree: dict[str, Any]) -> None:
        """Update internal state from a Page.getFrameTree response.

        Args:
            frame_tree: The 'frameTree' value from Page.getFrameTree result.
        """
        self._frames.clear()
        self._parse_frame_tree(frame_tree, parent_id=None)
        # Set root frame
        frame_data = frame_tree.get("frame", {})
        self._root_frame_id = frame_data.get("id")

    def _parse_frame_tree(
        self, tree_node: dict[str, Any], parent_id: str | None, session_id: str | None = None
    ) -> FrameInfo | None:
        """Recursively parse a frame tree node.

        ``session_id`` is the Frame Session this tree came from, carried down
        the whole subtree: a same-process child of an Out-of-Process Frame is
        answered by that frame's session, not by the page session.

        Args:
            tree_node: Frame tree node with 'frame' and optional 'childFrames'.
            parent_id: Parent frame ID.
            session_id: Owning Frame Session, or None for the page session.

        Returns:
            Parsed FrameInfo, or None if parsing fails.
        """
        frame_data = tree_node.get("frame", {})
        frame_id = frame_data.get("id", "")
        if not frame_id:
            return None

        info = FrameInfo(
            frame_id=frame_id,
            url=frame_data.get("url", ""),
            security_origin=frame_data.get("securityOrigin", ""),
            name=frame_data.get("name", ""),
            parent_frame_id=parent_id,
            frame_session_id=session_id,
        )
        self._frames[frame_id] = info

        for child_node in tree_node.get("childFrames", []):
            child = self._parse_frame_tree(child_node, parent_id=frame_id, session_id=session_id)
            if child:
                info.children.append(child)

        return info

    def get_frame_tree(self) -> dict[str, Any] | None:
        """Get the frame tree rooted at the top-level frame.

        Returns:
            Serialized frame tree, or None if no frames are tracked.
        """
        if self._root_frame_id is None:
            return None
        root = self._frames.get(self._root_frame_id)
        if root is None:
            return None
        return root.to_dict()

    def get_flat_frames(self) -> list[dict[str, Any]]:
        """Get all frames as a flat list with depth info.

        Returns:
            List of frame dictionaries with an added 'depth' field.
        """
        frames: list[dict[str, Any]] = []
        if self._root_frame_id is None:
            return frames
        self._flatten_frames(self._root_frame_id, depth=0, result=frames)
        return frames

    def _flatten_frames(self, frame_id: str, depth: int, result: list[dict[str, Any]]) -> None:
        """Recursively flatten the frame tree.

        Args:
            frame_id: Current frame ID.
            depth: Nesting depth.
            result: List to append frames to.
        """
        frame = self._frames.get(frame_id)
        if frame is None:
            return
        entry = frame.to_dict()
        entry["depth"] = depth
        # Remove children from flat representation
        entry.pop("children", None)
        result.append(entry)
        for child in frame.children:
            self._flatten_frames(child.frame_id, depth + 1, result)

    def select_frame_by_url(self, url_pattern: str) -> FrameInfo | None:
        """Select a frame by URL pattern match (D-002).

        Stores the URL pattern for re-resolution on frame navigation.

        Args:
            url_pattern: Substring to match against frame URLs (case-insensitive).

        Returns:
            Matching FrameInfo, or None if no frame matches.
        """
        self._selected_url_pattern = url_pattern
        frame = self._resolve_frame_by_url(url_pattern)
        if frame:
            self._selected_frame_id = frame.frame_id
        else:
            self._selected_frame_id = None
        return frame

    def reset_frame(self) -> None:
        """Clear frame selection and return to top-level context."""
        self._selected_frame_id = None
        self._selected_url_pattern = None

    @property
    def selection_pattern(self) -> str | None:
        """The URL pattern the selection follows, or ``None`` when unset.

        The pattern rather than the frame id is the selection's identity: it
        survives a navigation, and the id is re-resolved from it. So it is
        also the only thing worth saving and restoring.
        """
        return self._selected_url_pattern

    def restore_selection(self, pattern: str | None) -> None:
        """Put the selection back where a borrowing read found it."""
        if pattern is None:
            self.reset_frame()
        else:
            self.select_frame_by_url(pattern)

    def get_selected_frame(self) -> FrameInfo | None:
        """Get the currently selected frame, re-resolving if needed.

        Returns:
            Currently selected FrameInfo, or None if no selection or stale.
        """
        if self._selected_frame_id is None:
            return None
        frame = self._frames.get(self._selected_frame_id)
        if frame is not None:
            return frame
        # Frame ID is stale — try re-resolution via URL pattern
        if self._selected_url_pattern:
            return self.select_frame_by_url(self._selected_url_pattern)
        self._selected_frame_id = None
        return None

    def get_selected_context(self) -> tuple[str | None, int] | None:
        """Where to evaluate against the selected frame, or None.

        The pair ``(frame session, execution context id)``, because the id
        alone is no longer an address: it is unique within one renderer, and
        an Out-of-Process Frame has its own. Returning the id by itself from
        a world with several renderers is the `backendNodeId` mistake again
        (RFC-04, Routing).

        ``None`` for the frame session means the page session, which is what
        every frame was before Frame Sessions existed.
        """
        frame = self.get_selected_frame()
        if frame is None or frame.execution_context_id is None:
            return None
        return frame.frame_session_id, frame.execution_context_id

    def _reachable_frames(self) -> list[FrameInfo]:
        """The frames `frames list` shows, in the order it shows them.

        `_frames` is one representation and the `children` lists are another,
        and they can disagree: a frame whose `Page.frameNavigated` arrived
        before its `Page.frameAttached` lands in the map with no parent
        holding it. Resolving a selection against the map made such a frame
        selectable while `frames list` did not show it, so `storage get` read
        a frame the caller could not see. Both views answer from this walk
        instead, so one cannot show what the other denies.
        """
        reachable: list[FrameInfo] = []
        if self._root_frame_id is None:
            return reachable
        seen: set[str] = set()
        # Depth-first, children in order, which is the order `frames list`
        # prints. Two frames can match one pattern, so the order decides which
        # one a selection gets, and it has to be the order the caller read.
        frontier = [self._root_frame_id]
        while frontier:
            frame = self._frames.get(frontier.pop())
            if frame is None or frame.frame_id in seen:
                continue
            seen.add(frame.frame_id)
            reachable.append(frame)
            frontier.extend(child.frame_id for child in reversed(frame.children))
        return reachable

    def splice_session_tree(self, tree: dict[str, Any], session_id: str) -> bool:
        """Attach an Out-of-Process Frame's own tree under its parent.

        ``tree`` is the child's ``Page.getFrameTree`` result, rooted at the
        child frame. Its ``parentId`` names a frame in the parent session's
        tree, which is what makes the attachment point given rather than
        guessed (RFC-04, finding 3).

        Returns True when the tree was spliced, False when the parent frame is
        not in the map yet. A False is not an error: the child session can
        attach before the parent session has reported the placeholder frame,
        and the caller holds the tree and tries again. Storing it anywhere
        else would put it in the wrong place in the listing, or leave an
        island.
        """
        frame_data = tree.get("frame", {})
        frame_id = frame_data.get("id", "")
        parent_id = frame_data.get("parentId")
        if not frame_id or not parent_id:
            return False
        parent = self._frames.get(parent_id)
        if parent is None:
            return False
        # Replacing a frame this session already reported: its old subtree
        # goes with it, exactly as a re-attach does.
        self._forget_descendants(frame_id)
        self._unlink(frame_id)
        self._frames.pop(frame_id, None)

        info = self._parse_frame_tree(tree, parent_id=parent_id, session_id=session_id)
        if info is None:
            return False
        parent.children.append(info)
        self._event_buffer.append(
            FrameEvent(event_type="attached", frame_id=frame_id, url=info.url)
        )
        return True

    def forget_session(self, session_id: str) -> list[str]:
        """Drop every frame a detached Frame Session answered for.

        Returns the frame ids removed, so the caller can drop the Frame
        Sessions that hung below them in the same step. Leaving them would
        leak a session for the rest of the run.
        """
        doomed = [f.frame_id for f in self._frames.values() if f.frame_session_id == session_id]
        for frame_id in doomed:
            self._forget_descendants(frame_id)
            self._unlink(frame_id)
            self._frames.pop(frame_id, None)
            if self._selected_frame_id == frame_id:
                self._selected_frame_id = None
        self._reresolve_selection()
        return doomed

    def depth_of(self, frame_id: str) -> int:
        """How many frames sit between ``frame_id`` and the root.

        The root is 0. Used for the Frame Session depth bound, which cannot
        be applied before the splice: an ``iframe`` target's ``openerId`` is
        not set, so nothing in the attach event says how deep the frame is.
        The tree says it, once the child has been attached to its parent.

        Counts to the root or to a frame whose parent is not in the map,
        with a visited set so a cycle a bad event produced returns a number
        rather than hanging.
        """
        depth = 0
        seen: set[str] = set()
        current = self._frames.get(frame_id)
        while current is not None and current.frame_id not in seen:
            seen.add(current.frame_id)
            parent_id = current.parent_frame_id
            if not parent_id:
                break
            current = self._frames.get(parent_id)
            depth += 1
        return depth

    def session_for(self, frame_id: str) -> str | None:
        """The Frame Session that answers for a frame, or None for the page."""
        frame = self._frames.get(frame_id)
        return frame.frame_session_id if frame else None

    def _unlink(self, frame_id: str) -> None:
        """Take ``frame_id`` out of whatever parent's ``children`` holds it.

        Scans every frame rather than following ``parent_frame_id``, because
        the link this is fixing is exactly the one that may be stale.
        """
        for frame in self._frames.values():
            if any(child.frame_id == frame_id for child in frame.children):
                frame.children = [c for c in frame.children if c.frame_id != frame_id]

    def _is_descendant(self, frame_id: str, ancestor_id: str) -> bool:
        """Is ``frame_id`` at or below ``ancestor_id``?

        Walks up by ``parent_frame_id`` with a seen set, so a cycle already in
        the map answers rather than hanging.
        """
        seen: set[str] = set()
        current: str | None = frame_id
        while current and current not in seen:
            if current == ancestor_id:
                return True
            seen.add(current)
            frame = self._frames.get(current)
            current = frame.parent_frame_id if frame else None
        return False

    def _resolve_frame_by_url(self, url_pattern: str) -> FrameInfo | None:
        """Find a frame whose URL contains the pattern (case-insensitive).

        Args:
            url_pattern: Substring to match.

        Returns:
            First matching FrameInfo, or None.
        """
        pattern_lower = url_pattern.lower()
        for frame in self._reachable_frames():
            if pattern_lower in frame.url.lower():
                return frame
        return None

    # -----------------------------------------------------------------------
    # CDP Event Handlers
    # -----------------------------------------------------------------------

    def handle_frame_attached(self, params: dict[str, Any]) -> None:
        """Handle Page.frameAttached event.

        Args:
            params: CDP event parameters.
        """
        frame_id = params.get("frameId", "")
        parent_id = params.get("parentFrameId", "")
        if not frame_id:
            return

        # Two attaches the tree cannot hold, and both leave an island in the
        # map that `frames list` never shows and no walk ever collects:
        #
        #   - a parent that is not in the map, so there is nothing to hang
        #     the frame from;
        #   - a parent that is already below this frame, which would make the
        #     frame its own ancestor.
        #
        # Drop the subtree for either, because a frame the map holds is
        # reachable from the root, always. An island is worse than a drop: it
        # is unreachable and it was selectable, so `storage get` would read a
        # frame the caller had been told did not exist.
        unknown_parent = not parent_id or parent_id not in self._frames
        if unknown_parent or self._is_descendant(parent_id, frame_id) or parent_id == frame_id:
            self._forget_descendants(frame_id)
            self._unlink(frame_id)
            self._frames.pop(frame_id, None)
            if self._selected_frame_id == frame_id:
                self._selected_frame_id = None
            return

        # Re-attaching an id the map already holds replaces that frame, so
        # whatever hung below the old one is gone with it. Dropping the
        # descendants first is what keeps them from being stranded in the map:
        # nothing would hold them afterwards, `frames list` would not show
        # them, and no later walk could reach them to collect them.
        self._forget_descendants(frame_id)

        # And a frame has one parent. A re-attach under a *different* parent
        # left the old one still holding it, so `frames list` printed the
        # frame twice, once under each. Chrome re-parents a frame in ordinary
        # use, so this is not a defensive case.
        self._unlink(frame_id)

        info = FrameInfo(
            frame_id=frame_id,
            url="",
            security_origin="",
            name="",
            parent_frame_id=parent_id,
        )
        self._frames[frame_id] = info

        parent = self._frames.get(parent_id)
        if parent:
            parent.children.append(info)

        self._event_buffer.append(
            FrameEvent(
                event_type="attached",
                frame_id=frame_id,
            )
        )

    def handle_frame_detached(self, params: dict[str, Any]) -> None:
        """Handle Page.frameDetached event.

        Args:
            params: CDP event parameters.
        """
        frame_id = params.get("frameId", "")
        if not frame_id:
            return

        # Detaching a frame destroys everything inside it. Removing only this
        # frame leaves its children in the map with a `parent_frame_id` that
        # no longer resolves, which cuts them out of the tree and out of
        # `_forget_descendants`, so a later navigation cannot remove them
        # either. They stay selectable, because `_resolve_frame_by_url` walks
        # the map rather than the tree.
        self._forget_descendants(frame_id)

        frame = self._frames.pop(frame_id, None)
        if frame and frame.parent_frame_id:
            parent = self._frames.get(frame.parent_frame_id)
            if parent:
                parent.children = [c for c in parent.children if c.frame_id != frame_id]

        # Clear selection if detached frame was selected. The pattern is kept,
        # so a later navigation that brings a matching frame back re-points at
        # it; only `frames reset` and a new `frames select` clear the pattern.
        if self._selected_frame_id == frame_id:
            self._selected_frame_id = None

        self._event_buffer.append(
            FrameEvent(
                event_type="detached",
                frame_id=frame_id,
            )
        )

    def _reresolve_selection(self) -> None:
        """Re-point the selection at whatever now matches its pattern.

        The `else` matters (RFC-03, "Frame selection across steps"). Without
        it a re-resolution that finds nothing leaves the previous frame id in
        place, and the next frame-scoped read succeeds against a frame whose
        URL no longer matches the pattern the caller selected. Inside a Step
        Run the selection outlives the step, so that stale id is read by later
        steps.

        The pattern is kept, matching what a detach does. A later navigation
        that brings a matching frame back re-points the selection at it.
        """
        if not self._selected_url_pattern:
            return
        resolved = self._resolve_frame_by_url(self._selected_url_pattern)
        self._selected_frame_id = resolved.frame_id if resolved else None

    def handle_navigated_within_document(self, params: dict[str, Any]) -> None:
        """Handle Page.navigatedWithinDocument, a History API route change.

        A single-page app changes its URL through `history.pushState` rather
        than by loading a document, and Chrome reports that with its own
        event. Handling only `Page.frameNavigated` meant the frame kept the
        URL it was first loaded with: `frames list` reported the old route,
        and a selection made by pattern went on matching a URL the page had
        left. The Shopify admin and most consoles are single-page apps, so
        this is the common case rather than the exotic one.

        The document is the same one, so its child frames survive and nothing
        is forgotten here. Only the URL changed, so only the selection is
        re-resolved against it.
        """
        frame_id = params.get("frameId", "")
        url = params.get("url")
        if not frame_id or url is None:
            return

        frame = self._frames.get(frame_id)
        if frame is None:
            return
        frame.url = url

        self._reresolve_selection()
        self._event_buffer.append(FrameEvent(event_type="navigated", frame_id=frame_id, url=url))

    def handle_execution_contexts_cleared(
        self, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> None:
        """Handle Runtime.executionContextsCleared, for one session only.

        Every context that session had is gone. Keeping them would leave
        each of its frames holding a destroyed context id, which a
        frame-scoped read then evaluates against and gets an error for, or
        worse, silently skips.

        One session only, because the event says nothing about the others.
        Cleared globally, a cross-origin iframe navigating would wipe the
        main frame's context and break a read that had nothing to do with
        it (RFC-04, Routing).
        """
        for key in [k for k in self._execution_contexts if k[0] == session_id]:
            del self._execution_contexts[key]
        for frame in self._frames.values():
            if frame.frame_session_id == session_id:
                frame.execution_context_id = None

    def _forget_descendants(self, frame_id: str) -> None:
        """Drop every frame below ``frame_id``, which a navigation destroyed.

        Only the descendants: the frame itself survives its own navigation
        with a new document. Walks the flat map by parent id rather than the
        ``children`` lists, so a frame whose parent link was recorded without
        a matching child entry is still removed.
        """
        doomed: list[str] = []
        frontier = [frame_id]
        while frontier:
            parent_id = frontier.pop()
            for candidate_id, candidate in self._frames.items():
                if candidate.parent_frame_id == parent_id and candidate_id not in doomed:
                    doomed.append(candidate_id)
                    frontier.append(candidate_id)

        for doomed_id in doomed:
            self._frames.pop(doomed_id, None)
            if self._selected_frame_id == doomed_id:
                # The pattern is kept, as everywhere else: the re-resolution
                # below may find the selection again in the new document.
                self._selected_frame_id = None

    def handle_frame_navigated(self, params: dict[str, Any]) -> None:
        """Handle Page.frameNavigated event.

        Args:
            params: CDP event parameters with 'frame' object.
        """
        frame_data = params.get("frame", {})
        frame_id = frame_data.get("id", "")
        if not frame_id:
            return

        # A navigated frame gets a new document, and the old document's child
        # frames go with it. Chrome sends no `Page.frameDetached` for them, so
        # nothing else here can learn they are gone: without this, `frames
        # list` keeps reporting frames that no longer exist, and a selection
        # re-resolves onto one of them and reads a frame that is not there.
        self._forget_descendants(frame_id)

        frame = self._frames.get(frame_id)
        if frame:
            frame.url = frame_data.get("url", frame.url)
            frame.security_origin = frame_data.get("securityOrigin", frame.security_origin)
            frame.name = frame_data.get("name", frame.name)
            frame.children = []
        else:
            # Frame navigated before we saw it attached, so create it. Linking
            # it to its parent is what puts it in the tree: without that it
            # sits in `_frames` alone, invisible to `frames list` and to
            # `_reachable_frames`, which is the same orphan a detach used to
            # leave behind.
            created = FrameInfo(
                frame_id=frame_id,
                url=frame_data.get("url", ""),
                security_origin=frame_data.get("securityOrigin", ""),
                name=frame_data.get("name", ""),
                parent_frame_id=frame_data.get("parentId"),
            )
            parent = self._frames.get(created.parent_frame_id or "")
            if parent is not None:
                self._frames[frame_id] = created
                parent.children.append(created)
            elif created.parent_frame_id is None and self._root_frame_id is None:
                self._frames[frame_id] = created
                self._root_frame_id = frame_id
            else:
                # The parent was named and is not in the map, so this frame
                # arrived after its ancestor was detached. Storing it would
                # leave an island: invisible to `frames list`, selectable by
                # pattern, and never collected, because the walk that would
                # reach it goes through the parent that is gone.
                return

        self._reresolve_selection()

        self._event_buffer.append(
            FrameEvent(
                event_type="navigated",
                frame_id=frame_id,
                url=frame_data.get("url"),
            )
        )

    def handle_execution_context_created(
        self, params: dict[str, Any], session_id: str | None = None
    ) -> None:
        """Handle Runtime.executionContextCreated event.

        Maps execution contexts to their owning frames.

        Args:
            params: CDP event parameters.
            session_id: The Frame Session the event arrived on, or None for
                the page session. Part of the key, not a label: see
                :attr:`_execution_contexts`.
        """
        context = params.get("context", {})
        context_id = context.get("id")
        aux_data = context.get("auxData", {})
        frame_id = aux_data.get("frameId")

        if context_id is not None and frame_id:
            self._execution_contexts[(session_id, context_id)] = frame_id
            frame = self._frames.get(frame_id)
            if frame and aux_data.get("isDefault", False):
                frame.execution_context_id = context_id

    def handle_execution_context_destroyed(
        self, params: dict[str, Any], session_id: str | None = None
    ) -> None:
        """Handle Runtime.executionContextDestroyed event.

        Scoped to the session it arrived on. Unscoped, a child renderer
        destroying its context 1 cleared whichever frame happened to hold
        the id, which on a page with an Out-of-Process Frame is usually the
        main frame.

        Args:
            params: CDP event parameters.
            session_id: The Frame Session the event arrived on, or None for
                the page session.
        """
        context_id = params.get("executionContextId")
        if context_id is None:
            return
        frame_id = self._execution_contexts.pop((session_id, context_id), None)
        if not frame_id:
            return
        frame = self._frames.get(frame_id)
        if (
            frame
            and frame.execution_context_id == context_id
            and frame.frame_session_id == session_id
        ):
            frame.execution_context_id = None

    # -----------------------------------------------------------------------
    # Event Buffer
    # -----------------------------------------------------------------------

    def drain_events(self) -> list[dict[str, Any]]:
        """Return and clear buffered frame events.

        Returns:
            List of serialized frame events since last drain.
        """
        events = [e.to_dict() for e in self._event_buffer]
        self._event_buffer.clear()
        return events
