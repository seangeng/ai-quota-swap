"""Textual-based interactive TUI for ai-quota-swap.

Entry point for ``qswap tui`` (and bare ``qswap`` in an interactive
terminal). Heavy imports (textual, rich) stay inside :func:`run` so the
plain CLI paths — ``qswap list``, cron's ``qswap auto --once`` — never pay
for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_quota_swap.switcher import ClaudeAccountSwitcher


def _codex_switcher(switcher: "ClaudeAccountSwitcher"):
    """The Codex provider beside Claude Code's, over the same data root; None
    when it cannot be built (the dashboard then shows Claude Code alone)."""
    try:
        from ai_quota_swap.codex.switcher import CodexSwitcher

        return CodexSwitcher(data_root=switcher.backup_dir, configure_logging=False)
    except Exception:
        return None


def run(switcher: "ClaudeAccountSwitcher", start: str = "dashboard") -> int:
    """Run the TUI over an existing switcher. Returns the process exit code.

    ``start="watch"`` (the ``qswap watch`` command) opens directly on the
    live watch page, stacked over the dashboard.
    """
    from ai_quota_swap.appearance import detect_terminal_background, drain_stdin
    from ai_quota_swap.tui.app import QswapApp

    # Sense the terminal background while we still own stdin in cooked mode
    # (Textual's driver starts inside app.run()). Always detect so cycling to
    # 'auto' works even when the initial theme is explicit. Both calls are
    # meant to fail safe on their own, but they're wrapped here too: a
    # detection bug must never crash the TUI launch.
    try:
        detected = detect_terminal_background()
    except Exception:
        detected = None
    app = QswapApp(switcher, codex=_codex_switcher(switcher), start=start, detected=detected)
    # Drain any late OSC reply immediately before Textual's driver starts,
    # so it isn't reissued as keystrokes once the app takes over the terminal.
    try:
        drain_stdin()
    except Exception:
        pass
    app.run()
    return app.return_code or 0
