"""Optional read-only browser foundation. No provider import or I/O at import."""

from .config import BrowserConfig
from .session import BrowserSession
from .tools import browser_tools
from .composition import BrowserAgentComposition

__all__ = ["BrowserConfig", "BrowserSession", "browser_tools", "BrowserAgentComposition"]
