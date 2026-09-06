"""The handler table owns routing and invocation lifetime policy."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HandlerTool:
    method: str
    navigation: bool = False
    requires_capture: bool = False


TOOLS: dict[str, HandlerTool] = {
    "list_frames": HandlerTool("_handle_list_frames"),
    "select_frame": HandlerTool("_handle_select_frame"),
    "reset_frame": HandlerTool("_handle_reset_frame"),
    "get_frame_events": HandlerTool("_handle_get_frame_events"),
    "get_frame_storage": HandlerTool("_handle_get_frame_storage"),
    "ax_find": HandlerTool("_handle_ax_find"),
    "ax_node": HandlerTool("_handle_ax_node"),
    "export_pdf": HandlerTool("_handle_export_pdf"),
    "screenshot_element": HandlerTool("_handle_screenshot_element"),
    "screencast_start": HandlerTool("_handle_screencast_start", requires_capture=True),
    "screencast_stop": HandlerTool("_handle_screencast_stop", requires_capture=True),
    "wait_idle": HandlerTool("_handle_wait_idle"),
    "wait_stable": HandlerTool("_handle_wait_stable"),
    "get_text": HandlerTool("_handle_get_text"),
    "get_html": HandlerTool("_handle_get_html"),
    "get_attr": HandlerTool("_handle_get_attr"),
    "element_exists": HandlerTool("_handle_element_exists"),
    "element_visible": HandlerTool("_handle_element_visible"),
}
