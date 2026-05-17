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
        result = {
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
        self._execution_contexts: dict[int, str] = {}  # context_id -> frame_id

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
        self, tree_node: dict[str, Any], parent_id: str | None
    ) -> FrameInfo | None:
        """Recursively parse a frame tree node.

        Args:
            tree_node: Frame tree node with 'frame' and optional 'childFrames'.
            parent_id: Parent frame ID.

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
        )
        self._frames[frame_id] = info

        for child_node in tree_node.get("childFrames", []):
            child = self._parse_frame_tree(child_node, parent_id=frame_id)
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

    def get_selected_execution_context_id(self) -> int | None:
        """Get the execution context ID for the selected frame.

        Returns:
            Execution context ID, or None if no selection or no context mapped.
        """
        frame = self.get_selected_frame()
        if frame is None:
            return None
        return frame.execution_context_id

    def _resolve_frame_by_url(self, url_pattern: str) -> FrameInfo | None:
        """Find a frame whose URL contains the pattern (case-insensitive).

        Args:
            url_pattern: Substring to match.

        Returns:
            First matching FrameInfo, or None.
        """
        pattern_lower = url_pattern.lower()
        for frame in self._frames.values():
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

        info = FrameInfo(
            frame_id=frame_id,
            url="",
            security_origin="",
            name="",
            parent_frame_id=parent_id,
        )
        self._frames[frame_id] = info

        # Add as child of parent
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

        frame = self._frames.pop(frame_id, None)
        if frame and frame.parent_frame_id:
            parent = self._frames.get(frame.parent_frame_id)
            if parent:
                parent.children = [c for c in parent.children if c.frame_id != frame_id]

        # Clear selection if detached frame was selected
        if self._selected_frame_id == frame_id:
            self._selected_frame_id = None

        self._event_buffer.append(
            FrameEvent(
                event_type="detached",
                frame_id=frame_id,
            )
        )

    def handle_frame_navigated(self, params: dict[str, Any]) -> None:
        """Handle Page.frameNavigated event.

        Args:
            params: CDP event parameters with 'frame' object.
        """
        frame_data = params.get("frame", {})
        frame_id = frame_data.get("id", "")
        if not frame_id:
            return

        frame = self._frames.get(frame_id)
        if frame:
            frame.url = frame_data.get("url", frame.url)
            frame.security_origin = frame_data.get("securityOrigin", frame.security_origin)
            frame.name = frame_data.get("name", frame.name)
        else:
            # Frame navigated before we saw attached — create it
            self._frames[frame_id] = FrameInfo(
                frame_id=frame_id,
                url=frame_data.get("url", ""),
                security_origin=frame_data.get("securityOrigin", ""),
                name=frame_data.get("name", ""),
                parent_frame_id=frame_data.get("parentId"),
            )

        # Re-resolve frame selection if URL pattern is active
        if self._selected_url_pattern:
            resolved = self._resolve_frame_by_url(self._selected_url_pattern)
            if resolved:
                self._selected_frame_id = resolved.frame_id

        self._event_buffer.append(
            FrameEvent(
                event_type="navigated",
                frame_id=frame_id,
                url=frame_data.get("url"),
            )
        )

    def handle_execution_context_created(self, params: dict[str, Any]) -> None:
        """Handle Runtime.executionContextCreated event.

        Maps execution contexts to their owning frames.

        Args:
            params: CDP event parameters.
        """
        context = params.get("context", {})
        context_id = context.get("id")
        aux_data = context.get("auxData", {})
        frame_id = aux_data.get("frameId")

        if context_id is not None and frame_id:
            self._execution_contexts[context_id] = frame_id
            frame = self._frames.get(frame_id)
            if frame and aux_data.get("isDefault", False):
                frame.execution_context_id = context_id

    def handle_execution_context_destroyed(self, params: dict[str, Any]) -> None:
        """Handle Runtime.executionContextDestroyed event.

        Args:
            params: CDP event parameters.
        """
        context_id = params.get("executionContextId")
        if context_id is not None:
            frame_id = self._execution_contexts.pop(context_id, None)
            if frame_id:
                frame = self._frames.get(frame_id)
                if frame and frame.execution_context_id == context_id:
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
