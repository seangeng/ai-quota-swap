"""Multi-account switcher for Claude Code.

``ClaudeAccountSwitcher`` is imported on first use rather than here, so that a
small command (``qswap key-helper``, which Claude Code runs on every key
refresh) does not load the switcher, the usage client and everything they
import just by importing the package.
"""

from importlib.metadata import version

__version__ = version("ai-quota-swap")

__all__ = ["ClaudeAccountSwitcher", "__version__"]


def __getattr__(name: str):
    if name == "ClaudeAccountSwitcher":
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        return ClaudeAccountSwitcher
    raise AttributeError(f"module 'ai_quota_swap' has no attribute {name!r}")
