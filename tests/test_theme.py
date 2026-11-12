"""Tests for the Palette value object and the light/dark themes."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_quota_swap import printer
from ai_quota_swap.tui import theme
from ai_quota_swap.tui.theme import QSWAP_DARK, QSWAP_LIGHT, Palette


def test_dark_palette_matches_constants():
    p = Palette.DARK
    assert (p.accent, p.foreground, p.muted, p.sev_ok, p.sev_warn, p.sev_crit, p.track) == (
        theme.ACCENT,
        theme.FOREGROUND,
        theme.MUTED,
        theme.SEV_OK,
        theme.SEV_WARN,
        theme.SEV_CRIT,
        theme.TRACK,
    )


# qswap's palette, as the colour spec gives it. Changing a value is a design
# decision: change it here as well, and keep the contrast tests below passing.
_PALETTE = {
    "ACCENT": "#af87ff",
    "FOREGROUND": "#e4e6ee",
    "MUTED": "#8a8fa3",
    "BACKGROUND": "#0f1117",
    "SURFACE": "#161922",
    "PANEL": "#1f2330",
    "SEV_OK": "#7fd4a6",
    "SEV_WARN": "#f0c674",
    "SEV_CRIT": "#f47c8c",
    "TRACK": "#2f3445",
    "ACCENT_LIGHT": "#5b3fd1",
    "FOREGROUND_LIGHT": "#1c2030",
    "MUTED_LIGHT": "#565c70",
    "BACKGROUND_LIGHT": "#f7f8fc",
    "SURFACE_LIGHT": "#eceef5",
    "PANEL_LIGHT": "#dde1ec",
    "SEV_OK_LIGHT": "#1a6b43",
    "SEV_WARN_LIGHT": "#7a5200",
    "SEV_CRIT_LIGHT": "#b3263a",
    "TRACK_LIGHT": "#c5cad9",
}

# The palette of the project qswap was forked from. None of it may come back.
_UPSTREAM = {
    "#d7875f", "#e8e4de", "#8a8a8a", "#141414", "#1e1e1e", "#262626", "#87af87",
    "#d7af5f", "#d75f5f", "#3a3a3a", "#954c2a", "#2b2723", "#635d55", "#faf7f2",
    "#efeae1", "#e2dbcf", "#3d6b3d", "#795911", "#ad3128", "#cec7ba",
}


# The demo harness's own warm colours from before qswap had its palette: the
# CLI window's light frame (render.py), the recordings' stage backdrop
# (web/app_index.html and the played pages) and the shots index.
_OLD_HARNESS = {
    "#fbfaf7", "#ecebe7", "#d9d7d1", "#6d6c68", "#8a8984", "#242422",
    "#26211d", "#0d0d0e", "#fffaf2", "#e9e3d8", "#e2dfd8",
}


def test_palette_is_qswaps_own():
    assert {name: getattr(theme, name) for name in _PALETTE} == _PALETTE
    assert not set(_PALETTE.values()) & (_UPSTREAM | _OLD_HARNESS)


def test_no_upstream_colour_in_the_code_or_the_demo_scripts():
    # The constants above are the one source of truth: no widget, stylesheet
    # or demo script may draw with an upstream value, or an old harness one,
    # of its own.
    root = Path(__file__).resolve().parents[1]
    files = [
        path
        for folder in ("src/ai_quota_swap", "scripts/demo")
        for path in sorted((root / folder).rglob("*"))
        if path.suffix in {".py", ".tcss", ".html", ".css", ".js"}
    ]
    assert files
    found = [
        f"{path.relative_to(root)}: {value}"
        for path in files
        for value in sorted(_UPSTREAM | _OLD_HARNESS)
        if value in path.read_text(encoding="utf-8").lower()
    ]
    assert found == []


def test_from_theme_reads_theme_object_including_track():
    p = Palette.from_theme(QSWAP_LIGHT)
    assert p.accent == theme.ACCENT_LIGHT
    assert p.sev_crit == theme.SEV_CRIT_LIGHT
    assert p.track == theme.TRACK_LIGHT  # from Theme.variables["track"], not app cache


def test_severity_ramp_and_none():
    p = Palette.DARK
    assert p.severity(None) == p.muted
    assert p.severity(95.0) == p.sev_crit
    assert p.severity(75.0) == p.sev_warn
    assert p.severity(10.0) == p.sev_ok


def test_both_themes_expose_track_variable():
    assert QSWAP_DARK.variables["track"] == theme.TRACK
    assert QSWAP_LIGHT.variables["track"] == theme.TRACK_LIGHT
    assert QSWAP_LIGHT.dark is False


def _contrast(hex_a: str, hex_b: str) -> float:
    def lum(h: str) -> float:
        r, g, b = (int(h[i:i+2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    la, lb = sorted((lum(hex_a), lum(hex_b)))
    return (lb + 0.05) / (la + 0.05)


# The roles as the constants name them; a theme's constant is ROLE + suffix
# ("" for qswap-dark, "_LIGHT" for qswap-light).
_THEMES = pytest.mark.parametrize("suffix", ["", "_LIGHT"], ids=["dark", "light"])
_TEXT = ("ACCENT", "FOREGROUND", "MUTED", "SEV_OK", "SEV_WARN", "SEV_CRIT")
_ROWS = ("BACKGROUND", "SURFACE", "PANEL")
_FILLS = ("SEV_OK", "SEV_WARN", "SEV_CRIT")


def _colour(role: str, suffix: str) -> str:
    return getattr(theme, role + suffix)


@_THEMES
def test_text_meets_AA_on_all_backgrounds(suffix):
    # Accent and severity colors render as PERCENTAGE TEXT on highlighted
    # ($surface) and flash ($panel) rows, not just the base background, so
    # every text color must clear the 4.5:1 text bar against all three.
    for role in _TEXT:
        for row in _ROWS:
            ratio = _contrast(_colour(role, suffix), _colour(row, suffix))
            assert ratio >= 4.5, f"{role}{suffix} on {row}{suffix}: {ratio:.2f}"


@_THEMES
def test_background_on_accent_meets_AA(suffix):
    # The LIVE badge and a focused dialog button draw $background text on a
    # $primary block.
    ratio = _contrast(_colour("BACKGROUND", suffix), _colour("ACCENT", suffix))
    assert ratio >= 4.5, f"BACKGROUND{suffix} on ACCENT{suffix}: {ratio:.2f}"


@_THEMES
def test_bar_fills_stand_out_from_the_track_and_the_row(suffix):
    # A bar's fill ends where its track starts, on any row background: the
    # fill needs the 3:1 non-text bar against both.
    for fill in _FILLS:
        for against in ("TRACK", *_ROWS):
            ratio = _contrast(_colour(fill, suffix), _colour(against, suffix))
            assert ratio >= 3.0, f"{fill}{suffix} against {against}{suffix}: {ratio:.2f}"


def _printed_colour(code: str):
    """The colour Rich reads from one of printer's escapes."""
    from rich.text import Text

    (span,) = Text.from_ansi(f"{code}x\033[0m").spans
    return span.style.color


def test_command_line_accent_is_the_dashboard_accent():
    from rich.color import Color

    assert Color.from_ansi(141).get_truecolor().hex == "#af87ff"
    dark = printer._PALETTES["dark"]["accent"]
    assert dark == "\033[38;5;141m"
    assert _printed_colour(dark).get_truecolor().hex == theme.ACCENT


def test_light_command_line_colours_are_the_light_palette():
    expected = {
        "accent": theme.ACCENT_LIGHT,
        "muted": theme.MUTED_LIGHT,
        "red": theme.SEV_CRIT_LIGHT,
        "yellow": theme.SEV_WARN_LIGHT,
    }
    light = printer._PALETTES["light"]
    assert {key: _printed_colour(code).triplet.hex for key, code in light.items()} == expected


# What each printer colour stands for in the dashboard.
_PRINTER_ROLE = {"accent": "accent", "muted": "muted", "red": "sev_crit", "yellow": "sev_warn"}


@pytest.mark.parametrize("table", ["dark", "light"])
@pytest.mark.parametrize("target", [QSWAP_DARK, QSWAP_LIGHT], ids=["dark", "light"])
def test_from_printer_maps_every_printer_colour(table, target):
    palette = Palette.from_theme(target)
    codes = printer._PALETTES[table]
    assert set(codes) == set(_PRINTER_ROLE), "a printer colour from_printer does not know"
    for key, code in codes.items():
        styles = [str(s.style) for s in theme.from_printer(f"{code}x\033[0m", palette).spans]
        assert styles == [getattr(palette, _PRINTER_ROLE[key])], f"{table} {key}"


@pytest.mark.parametrize("table", ["dark", "light"])
@pytest.mark.parametrize("target", [QSWAP_DARK, QSWAP_LIGHT], ids=["dark", "light"])
def test_from_printer_maps_dim_to_muted_and_keeps_bold(table, target, monkeypatch):
    # printer.dimmed sets no colour, only dim; bold_accent sets bold and the
    # accent. Both must come out in the theme's colours.
    monkeypatch.setattr(printer, "_theme", table)
    palette = Palette.from_theme(target)
    with printer.force_color():
        dimmed = printer.dimmed("x")
        bold_accent = printer.bold_accent("x")
    assert [str(s.style) for s in theme.from_printer(dimmed, palette).spans] == [palette.muted]
    assert [str(s.style) for s in theme.from_printer(bold_accent, palette).spans] == [
        f"bold {palette.accent}"
    ]
