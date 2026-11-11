"""The "qswap-dark" and "qswap-light" Textual themes and shared color constants.

qswap draws on cool ink backgrounds with a single violet accent. The violet
sits apart from the green, amber and red of the usage bars, and it belongs to
neither provider. It is xterm 141, the tone printer.py uses for the command
line's accent, so both read as one tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.theme import Theme

if TYPE_CHECKING:
    from rich.text import Text

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#af87ff"  # violet (xterm 141, the command line's accent)
FOREGROUND = "#e4e6ee"  # cool off-white
MUTED = "#8a8fa3"  # secondary text
BACKGROUND = "#0f1117"  # ink
SURFACE = "#161922"
PANEL = "#1f2330"

# Usage severity ramp, light enough to read on ink.
SEV_OK = "#7fd4a6"  # green: plenty of headroom
SEV_WARN = "#f0c674"  # amber: climbing (>= 70%)
SEV_CRIT = "#f47c8c"  # red: near the limit (>= 90%)
TRACK = "#2f3445"  # unfilled bar track

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

# Light companion palette: the same roles on a cool near-white base. Text
# colours are deep enough for AA on PANEL_LIGHT, the darkest row background.
ACCENT_LIGHT = "#5b3fd1"  # violet
FOREGROUND_LIGHT = "#1c2030"
MUTED_LIGHT = "#565c70"
BACKGROUND_LIGHT = "#f7f8fc"
SURFACE_LIGHT = "#eceef5"
PANEL_LIGHT = "#dde1ec"  # most-elevated = darkest (inverted from dark)
SEV_OK_LIGHT = "#1a6b43"  # green
SEV_WARN_LIGHT = "#7a5200"  # amber
SEV_CRIT_LIGHT = "#b3263a"  # red
TRACK_LIGHT = "#c5cad9"

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
        141: palette.accent, (91, 63, 209): palette.accent,
        250: palette.muted, (86, 92, 112): palette.muted,
        1: palette.sev_crit, (179, 38, 58): palette.sev_crit,
        3: palette.sev_warn, (122, 82, 0): palette.sev_warn,
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
