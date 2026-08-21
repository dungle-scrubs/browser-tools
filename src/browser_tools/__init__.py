"""Browser Tools automation package.

The public names below are re-exported lazily (PEP 562). Importing the package
-- or any daemonless CLI submodule such as ``browser_tools.cli`` -- MUST NOT
drag in the optional MCP front and its legacy session stack
(``persistent_browser``, ``camoufox_session``, and the daemon supervisor they
pull in). Those modules load only when one of the re-exported names is actually
accessed, so the CLI verb path stays free of the daemon machinery (RFC-01
Phase 3, "the MCP front is truly optional"). Submodule imports
(``from browser_tools import browser_session``) resolve directly and are
unaffected by this lazy layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

# Re-exported name -> submodule that defines it. Kept out of module import time
# so the daemon/session stack loads on first access, not on ``import
# browser_tools``.
_LAZY_EXPORTS = {
    "ActiveAttachConfig": ".browser_state",
    "BrowserState": ".browser_state",
    "ProjectBrowserConfig": ".browser_state",
    "CamoufoxSession": ".camoufox_session",
    "CDPClient": ".cdp_client",
    "CDPError": ".cdp_client",
    "FrameManager": ".frame_manager",
    "PersistentChromeController": ".persistent_browser",
}


def __getattr__(name: str) -> Any:
    """Resolve a re-exported name from its submodule on first access (PEP 562)."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    """Expose the lazy re-exports to ``dir()`` and tab completion."""
    return sorted(__all__)
