"""Browser Tools automation package.

The public names below are re-exported lazily (PEP 562), so importing the
package does not pull in ``websockets`` and the CDP client with it. Importing
the package, or any CLI submodule such as ``browser_tools.cli``, loads only
what that path needs.

The MCP front and the legacy session stack this lazy layer used to keep out of
the CLI path no longer exist (#92). Submodule imports
(``from browser_tools import lifecycle``) resolve directly and are unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cdp_client import CDPClient, CDPError
    from .frame_manager import FrameManager

__all__ = [
    "CDPClient",
    "CDPError",
    "FrameManager",
    "__version__",
]
__version__ = "0.1.0"

# Re-exported name -> submodule that defines it. Kept out of module import time
# so ``websockets`` loads on first access, not on ``import browser_tools``.
_LAZY_EXPORTS = {
    "CDPClient": ".cdp_client",
    "CDPError": ".cdp_client",
    "FrameManager": ".frame_manager",
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
