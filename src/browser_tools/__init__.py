"""Browser automation over the Chrome DevTools Protocol."""

from importlib.metadata import version

from .core.cdp_client import CDPClient
from .core.errors import CDPError
from .frame_manager import FrameManager

__version__ = version("browser-tools")
__all__ = ["CDPClient", "CDPError", "FrameManager", "__version__"]
