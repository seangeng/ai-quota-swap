"""Multi-account switcher for Claude Code."""

from importlib.metadata import version

__version__ = version("ai-quota-swap")

from ai_quota_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
