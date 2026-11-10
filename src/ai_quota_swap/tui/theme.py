"""The "qswap-dark" Textual theme and shared color constants.

A subtle modern dark theme: neutral charcoal backgrounds in the VS Code
register, one warm terracotta accent (the same xterm-173 tone printer.py has
always used for the CLI — a deliberate nod to Claude Code's orange, used
sparingly), and desaturated severity colors so usage bars read calmly on a
dark background. Deliberately *not* a wholesale copy of any other tool's
palette.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.theme import Theme

if TYPE_CHECKING:
    from rich.text import Text

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#d7875f"  # warm terracotta (xterm 173)
FOREGROUND = "#e8e4de"  # soft, slightly warm off-white
MUTED = "#8a8a8a"  # secondary text
BACKGROUND = "#141414"
SURFACE = "#1e1e1e"
PANEL = "#262626"

# Usage severity ramp (desaturated for dark backgrounds).
SEV_OK = "#87af87"  # calm green: plenty of headroom
SEV_WARN = "#d7af5f"  # amber: climbing (>= 70%)
SEV_CRIT = "#d75f5f"  # soft red: near the limit (>= 90%)
TRACK = "#3a3a3a"  # unfilled bar track

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class Palette:
    """Resolved colors for Rich renderables, keyed to a Textual theme.

    Rich renderables bake color into styles at render time, so they can't read
    Textual's ``$variables`` the way the .tcss layer does. A Palette carries the
    active theme's colors so the same render code paints correctly in either
    theme. Resolve from the Theme object (never App.theme_variables, which lags
    the deferred CSS refresh)."""

    accent: str
    foreground: str
    muted: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    track: str

    DARK: ClassVar["Palette"]

    def severity(self, pct: float | None) -> str:
        if pct is None:
            return self.muted
        if pct >= CRIT_PCT:
            return self.sev_crit
        if pct >= WARN_PCT:
            return self.sev_warn
        return self.sev_ok

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            sev_ok=theme.success,
            sev_warn=theme.warning,
            sev_crit=theme.error,
            track=theme.variables.get("track", TRACK),
        )


QSWAP_DARK = Theme(
    name="qswap-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables={
        # Footer keys pick up the accent instead of the default blue.
        "footer-key-foreground": ACCENT,
        "block-cursor-background": PANEL,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "none",
        "track": TRACK,
    },
)

# Light companion palette (same intent, tuned for a warm near-white base).
ACCENT_LIGHT = "#954c2a"  # burnt sienna — deepened for AA on panel (worst-case row bg)
FOREGROUND_LIGHT = "#2b2723"
MUTED_LIGHT = "#635d55"
BACKGROUND_LIGHT = "#faf7f2"
SURFACE_LIGHT = "#efeae1"
PANEL_LIGHT = "#e2dbcf"  # most-elevated = darkest (inverted from dark)
SEV_OK_LIGHT = "#3d6b3d"  # forest green — deepened for AA on panel
SEV_WARN_LIGHT = "#795911"  # deep ochre — deepened for AA on panel
SEV_CRIT_LIGHT = "#ad3128"  # brick red — deepened for AA on panel
TRACK_LIGHT = "#cec7ba"

QSWAP_LIGHT = Theme(
    name="qswap-light",
    primary=ACCENT_LIGHT,
    secondary=MUTED_LIGHT,
    accent=ACCENT_LIGHT,
    foreground=FOREGROUND_LIGHT,
    background=BACKGROUND_LIGHT,
    surface=SURFACE_LIGHT,
    panel=PANEL_LIGHT,
    success=SEV_OK_LIGHT,
    warning=SEV_WARN_LIGHT,
    error=SEV_CRIT_LIGHT,
    dark=False,
    variables={
        "footer-key-foreground": ACCENT_LIGHT,
        "block-cursor-background": PANEL_LIGHT,
        "block-cursor-foreground": FOREGROUND_LIGHT,
        "block-cursor-text-style": "none",
        "track": TRACK_LIGHT,
    },
)

Palette.DARK = Palette(
    accent=ACCENT, foreground=FOREGROUND, muted=MUTED,
    sev_ok=SEV_OK, sev_warn=SEV_WARN, sev_crit=SEV_CRIT, track=TRACK,
)


def from_printer(line: str, palette: Palette) -> "Text":
    """A line the command line styled (``printer``'s ANSI) in this theme's
    colours: its accent, muted, yellow and red become the palette's, so a
    log line or an action's output reads like the rest of the screen."""
    from rich.style import Style
    from rich.text import Text

    text = Text.from_ansi(line)
    roles = {
        173: palette.accent, (149, 76, 42): palette.accent,
        250: palette.muted, (99, 93, 85): palette.muted,
        1: palette.sev_crit, (173, 49, 40): palette.sev_crit,
        3: palette.sev_warn, (121, 89, 17): palette.sev_warn,
    }
    spans = []
    for span in text.spans:
        style = span.style if isinstance(span.style, Style) else Style.parse(str(span.style))
        color = style.color
        new = None
        if color is not None:
            key = tuple(color.triplet) if color.number is None and color.triplet else color.number
            new = roles.get(key)
        elif style.dim:
            new = palette.muted
        if new is not None:
            style = Style(color=new, bold=style.bold)
        spans.append(type(span)(span.start, span.end, style))
    text.spans = spans
    return text
