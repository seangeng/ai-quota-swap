"""The usage report: ``qswap usage`` on a screen.

Tokens and their estimated cost at API prices, from the ledger the command
line keeps (``ledger.usage.run_usage``, rendered by ``ledger.report``): the
last 7 days by day to start with; Tab regroups by model, account, project and
provider, ``d`` switches between 7 and 30 days. The screen shows what the
ledger already holds at once, then reads new session files in a worker and
says so while it does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from ai_quota_swap.tui.theme import Palette

if TYPE_CHECKING:
    from ai_quota_swap.tui.app import QswapApp

#: The groupings Tab cycles through, in order.
GROUPINGS = ("day", "model", "account", "project", "provider")
#: The two spans ``d`` switches between.
SPANS = (7, 30)


def styled_report(text: str, palette: Palette) -> Text:
    """``ledger.report.render_text``'s plain lines, styled: the heading and
    the total bold, the column titles and footnotes muted, rows plain."""
    out = Text(no_wrap=True)
    lines = text.splitlines()
    table = any(line.startswith("  Total") for line in lines)
    seen_blank = 0
    after_total = False
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        if not table:
            out.append(line, style=palette.foreground if i == 0 else palette.muted)
            continue
        if not line.strip():
            seen_blank += 1
            continue
        if seen_blank == 0:
            out.append(line, style=f"bold {palette.foreground}")
        elif line.startswith("  Total"):
            out.append(line, style=f"bold {palette.foreground}")
            after_total = True
        elif after_total:
            out.append(line, style=palette.muted)
        elif seen_blank == 1 and lines[i - 1].strip() == "":
            out.append(line, style=palette.muted)  # the column titles
        else:
            out.append(line, style=palette.foreground)
    return out


class UsageScreen(Screen):
    """``qswap usage``: rows by day, model, account, project or provider."""

    BINDINGS = [
        Binding("tab", "next_grouping", "Regroup"),
        Binding("shift+tab", "previous_grouping", "Previous", show=False),
        Binding("d", "toggle_span", "7 / 30 days"),
        Binding("f", "rescan", "Refresh", show=False),
        Binding("escape", "back", "Back"),
        Binding("q", "app.quit", "Quit"),
    ]

    app: "QswapApp"

    def __init__(self) -> None:
        super().__init__()
        self.by = "day"
        self.days = 7
        self.scanning = False
        self._result = None
        self._generation = 0
        self._scanned: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Static("", id="usage-title")
        with VerticalScroll(id="usage-body"):
            yield Static("", id="usage-report")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "theme", lambda _t: self._render())
        self._load()

    # -- keys --------------------------------------------------------------------

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_next_grouping(self) -> None:
        self.by = GROUPINGS[(GROUPINGS.index(self.by) + 1) % len(GROUPINGS)]
        self._load()

    def action_previous_grouping(self) -> None:
        self.by = GROUPINGS[(GROUPINGS.index(self.by) - 1) % len(GROUPINGS)]
        self._load()

    def action_toggle_span(self) -> None:
        self.days = SPANS[(SPANS.index(self.days) + 1) % len(SPANS)]
        self._load()

    def action_rescan(self) -> None:
        self._scanned.discard(self.days)
        self._load()

    # -- loading ------------------------------------------------------------------

    def _load(self) -> None:
        """Show what the ledger holds now, then read new session files for
        this span once (a scan is incremental: the next one is quick)."""
        self._generation += 1
        generation = self._generation
        by, days = self.by, self.days
        scan = days not in self._scanned
        self.scanning = scan
        self._render()

        def work() -> None:
            first = self.app.read_ledger(days=days, by=by, scan=False)
            self.app.call_from_thread(self._show, generation, first, scan)
            if scan:
                result = self.app.read_ledger(days=days, by=by, scan=True)
                self.app.call_from_thread(self._show, generation, result, False, days)

        self.run_worker(work, thread=True, group="ledger", exit_on_error=False, name="usage-report")

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState

        if event.worker.name == "usage-report" and event.state is WorkerState.ERROR:
            self.scanning = False
            self._result = None
            self._error = f"Error: the usage ledger could not be read: {event.worker.error}"
            self._render()

    def _show(self, generation: int, result, scanning: bool, scanned_days: int | None = None) -> None:
        if generation != self._generation:
            return
        if scanned_days is not None:
            self._scanned.add(scanned_days)
            self.app.refresh_cost(scan=False)
        self.scanning = scanning
        if result is not None or not scanning:
            self._result = result
        self._render()

    def _render(self) -> None:
        if not self.is_mounted:
            return
        palette = Palette.from_theme(self.app.current_theme)
        title = Text(no_wrap=True)
        title.append("Usage report", style=f"bold {palette.foreground}")
        title.append(f" · last {self.days} days · by {self.by}", style=palette.muted)
        if self.scanning:
            title.append(" · reading new session files…", style=palette.accent)
        self.query_one("#usage-title", Static).update(title)
        body = self.query_one("#usage-report", Static)
        error = getattr(self, "_error", None)
        if error:
            body.update(Text(error, style=palette.sev_warn))
            self._error = None
            return
        if self._result is None:
            body.update(Text("Reading the ledger…" if self.scanning else
                             "No usage recorded yet. Use Claude Code or Codex, then press f.",
                             style=palette.muted))
            return
        from ai_quota_swap.ledger.report import render_text

        width = max(40, (body.size.width or self.app.size.width - 4))
        text = render_text(
            self._result.report, notes=self._result.notes,
            last_line=self._result.older_history, width=width,
        )
        plain = Text.from_ansi(text).plain
        body.update(styled_report(plain, palette))
