"""Multi-account switcher for Claude Code.

``ClaudeAccountSwitcher`` and ``__version__`` are looked up on first use rather
than here, so that a small command (``qswap key-helper``, which Claude Code
runs on every key refresh) does not load the switcher, the usage client and
everything they import, nor ``importlib.metadata`` (with ``email`` and
``zipfile``) just to learn the version, by importing the package.
"""

__all__ = ["ClaudeAccountSwitcher", "__version__"]


def __getattr__(name: str):
    if name == "ClaudeAccountSwitcher":
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        return ClaudeAccountSwitcher
    if name == "__version__":
        from importlib.metadata import version

        value = version("ai-quota-swap")
        globals()["__version__"] = value  # looked up once
        return value
    raise AttributeError(f"module 'ai_quota_swap' has no attribute {name!r}")
