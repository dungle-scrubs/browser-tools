"""Browser Tools automation package."""

from __future__ import annotations

from .browser_state import ActiveAttachConfig, BrowserState, ProjectBrowserConfig
from .camoufox_session import CamoufoxSession
from .cdp_client import CDPClient, CDPError
from .frame_manager import FrameManager
from .persistent_browser import PersistentChromeController

__all__ = [
    "ActiveAttachConfig",
    "BrowserState",
    "CDPClient",
    "CDPError",
    "CamoufoxSession",
    "FrameManager",
    "PersistentChromeController",
    "ProjectBrowserConfig",
    "__version__",
]
__version__ = "0.1.0"
