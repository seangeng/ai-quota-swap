"""Shared render widgets: usage bars, account cards and rows, the accounts panel.

A card is the command line's account block (:class:`display.AccountBlock`)
drawn with the dashboard's bars: the header facts in ``qswap list``'s order,
the same state sentences, a held reset as a sentence. Words come from
:mod:`ai_quota_swap.display` (``header_parts``, ``window_suffix``,
``state_parts``); this module only lays them out and colours them, by the
account's own switch line (``display.severity``).

``bar_cells`` is a custom renderer rather than Textual's ``ProgressBar``: the
design needs a severity colour, a tick at the switch line, and dimming for a
stale reading.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import ListItem, Static

from ai_quota_swap import display
from ai_quota_swap.models import AccountSnapshot
from ai_quota_swap.tui import data
from ai_quota_swap.tui.theme import Palette

if TYPE_CHECKING:
    from ai_quota_swap.tui.app import QswapApp

_BAR_FILLED = "━"
_BAR_HALF = "╸"
_BAR_EMPTY = "─"
_BAR_TICK = "┃"

#: Where a card's window rows start, under its header.
BODY_INDENT = "   "
#: Window labels are at least this wide, so cards line up ("extra", "Fable").
MIN_LABEL = 5
#: Columns between a row's header and its usage summary.
GAP = "   "


# -- colours --------------------------------------------------------------------


def level_colour(level: str, palette: Palette) -> str:
    """The colour of a :func:`display.severity` level."""
    if level == "limit":
        return palette.sev_crit
    if level == "warn":
        return palette.sev_warn
    return palette.sev_ok


def _role_style(role: str, palette: Palette) -> str:
    """A ``display.state_parts`` role's style."""
    return palette.sev_warn if role == "warn" else palette.muted


# -- bars -------------------------------------------------------------------------


def bar_cells(
    pct: float | None,
    width: int,
    *,
    level: str = "ok",
    stale: bool = False,
    threshold: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """Just the bar glyphs: severity-coloured fill, track, a tick at the line."""
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=palette.track)
        return text
    frac = min(max(pct, 0.0), 100.0) / 100.0
    cells = frac * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    color = level_colour(level, palette)
    fill_style = f"{color} dim" if stale else color
    for i in range(width):
        if tick_at is not None and i == tick_at:
            text.append(_BAR_TICK, style=palette.sev_warn)
        elif i < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif i == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=palette.track)
    return text


def bar_width_for(width: int) -> int:
    """One bar width for every card at this width, so their bars line up."""
    return max(8, min(28, width - 66))


# -- header -----------------------------------------------------------------------


def _fact_style(role: str, palette: Palette) -> str:
    if role == "active":
        return f"bold {palette.foreground}"
    if role == "plan-lapsed":
        return palette.sev_warn
    return palette.muted


def account_extra(acc: AccountSnapshot) -> list[str]:
    """Facts the dashboard adds after ``qswap list``'s: an account's own
    switch line (``qswap threshold``), which its bars also tick at."""
    if acc.policy.threshold is None:
        return []
    return [f"switch at {display_pct(acc.policy.threshold)}"]


def display_pct(value: float) -> str:
    """``75%``, ``82.5%``: a line as it was set."""
    return f"{value:g}%"


def header_text(
    block: display.AccountBlock,
    width: int,
    palette: Palette,
    *,
    extra: Sequence[str] = (),
    drop: int = 0,
) -> Text:
    """One header line within ``width`` columns. The email gives way first
    (``display.header_parts``), then the organization; ``drop`` leaves out
    that many facts from the end beyond those (the plan before the usage
    figures a row keeps), and a name that still does not fit is cut."""
    extra_plain = "".join(f" · {e}" for e in extra)
    parts = display.header_parts(block, width=max(10, width - len(extra_plain)), indent="")
    facts = list(parts.facts)
    keep = [f for f in facts if f[1] in ("active", "disabled")]
    droppable = [f for f in facts if f[1] not in ("active", "disabled")]
    if drop:
        droppable = droppable[: max(0, len(droppable) - drop)]
        facts = [f for f in facts if f in keep or f in droppable]
    extras = list(extra)
    if drop > len(parts.facts):
        extras = []
    text = Text(no_wrap=True, overflow="ellipsis")
    if parts.number:
        text.append(parts.number, style=f"bold {palette.foreground}")
        text.append(" ")
    text.append(parts.name, style=f"bold {palette.accent}" if parts.alias else palette.foreground)
    if parts.email:
        text.append(f" ({parts.email})", style=palette.muted)
    for fact, role in facts:
        text.append(" · ", style=palette.track)
        text.append(fact, style=_fact_style(role, palette))
    for fact in extras:
        text.append(" · ", style=palette.track)
        text.append(fact, style=palette.muted)
    if cell_len(text.plain) > width:
        text.truncate(max(1, width), overflow="ellipsis")
    return text


# -- window rows ------------------------------------------------------------------


def window_row_text(
    w: display.WindowRow,
    *,
    width: int,
    label_width: int,
    bar_width: int,
    stale: bool,
    palette: Palette,
    indent: str = BODY_INDENT,
) -> Text:
    """``   5h     ━━━━━━━━──────┃──  46%  resets 16:55 · in 2h 35m``.

    The suffix is ``display.window_suffix``; when it does not fit, the
    countdown goes and the clock stays (a row ahead of pace keeps it too),
    then the rest is cut with an ellipsis."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(indent)
    text.append(f"{w.label:<{label_width}} ", style=palette.muted)
    if w.text is not None:
        # A login row: its words start where a window's bar does.
        text.append(w.text, style=f"{palette.muted} dim" if stale else palette.muted)
        return _fit(text, width)
    level = display.severity(w.pct, w.line)
    text.append(bar_cells(
        w.pct, bar_width, level=level, stale=stale, threshold=w.line, palette=palette,
    ))
    pct = f" {w.pct:3.0f}%" if isinstance(w.pct, (int, float)) else "  n/a"
    colour = level_colour(level, palette) if isinstance(w.pct, (int, float)) else palette.muted
    text.append(pct, style=f"{colour} dim" if stale else colour)
    room = width - cell_len(text.plain) - 2
    suffix = display.window_suffix(w)
    if len(suffix) > room:
        suffix = display.window_suffix(w, countdown=False)
    if suffix:
        text.append("  ")
        style = f"{palette.muted} dim" if stale else palette.muted
        if level == "limit" and suffix.endswith("limit reached"):
            head = suffix[: -len("limit reached")]
            text.append(head, style=style)
            text.append("limit reached", style=palette.sev_crit)
        else:
            text.append(suffix, style=style)
    return _fit(text, width)


def _fit(text: Text, width: int) -> Text:
    if cell_len(text.plain) > width:
        text.truncate(max(1, width), overflow="ellipsis")
    return text


def state_texts(
    block: display.AccountBlock,
    width: int,
    palette: Palette,
    *,
    indent: str = BODY_INDENT,
    roles: Sequence[str] | None = None,
) -> list[Text]:
    """The block's sentences (``display.state_parts``), wrapped to ``width``
    as the command line wraps them: a command is never split."""
    out: list[Text] = []
    for sentence, role in display.state_parts(block):
        if roles is not None and role not in roles:
            continue
        for line in display.wrap(sentence, width, indent):
            out.append(Text(line, style=_role_style(role, palette), no_wrap=True))
    return out


# -- a card ------------------------------------------------------------------------


@dataclass(frozen=True)
class CardLine:
    """One line of a card, and whether a crowded panel may leave it out."""

    text: Text
    optional: bool = False


def card_lines(
    block: display.AccountBlock,
    width: int,
    palette: Palette,
    *,
    label_width: int = MIN_LABEL,
    bar_width: int | None = None,
    extra: Sequence[str] = (),
) -> list[CardLine]:
    """An account in full: the header, a bar per window, then its sentences.

    An account that needs re-login says so first and shows what it last read
    dimmed, as ``qswap list`` does. The held-reset sentence is optional: a
    crowded dashboard drops it before anything else.
    """
    bar_width = bar_width or bar_width_for(width)
    label_width = max(label_width, *(len(w.label) for w in block.windows)) if block.windows else label_width
    out = [CardLine(header_text(block, width, palette, extra=extra))]
    stale = block.stale_s is not None or block.state == "needs-relogin"

    def windows() -> list[CardLine]:
        return [
            CardLine(window_row_text(
                w, width=width, label_width=label_width, bar_width=bar_width,
                stale=stale, palette=palette,
            ))
            for w in block.windows
        ]

    if block.state == "needs-relogin":
        out.extend(CardLine(t) for t in state_texts(block, width, palette))
        out.extend(windows())
        return out
    out.extend(windows())
    for sentence, role in display.state_parts(block):
        if role == "muted":
            # The held resets: whole facts give way rather than wrap.
            sentence = fit_facts(sentence, width - len(BODY_INDENT))
        for line in display.wrap(sentence, width, BODY_INDENT):
            out.append(CardLine(
                Text(line, style=_role_style(role, palette), no_wrap=True),
                optional=role == "muted",
            ))
    return out


def fit_facts(text: str, room: int) -> str:
    """``a · b · c`` within ``room`` columns: the last facts go first, and
    the first is cut with an ellipsis only when it alone does not fit."""
    facts = text.split(" · ")
    while len(facts) > 1 and len(" · ".join(facts)) > room:
        facts.pop()
    return display.fit(" · ".join(facts), room)


def card_text(block: display.AccountBlock, width: int, palette: Palette, **kwargs) -> Text:
    """:func:`card_lines` as one renderable."""
    return Text("\n").join(line.text for line in card_lines(block, width, palette, **kwargs))


# -- a one-line row ------------------------------------------------------------------


def _login_part(block: display.AccountBlock) -> tuple[str, str] | None:
    """The login row (``login  expires Nov 14 03:00 · in 5d 12h``) in a few
    words for a one-line row: ``login expires in 5d 12h``."""
    row = next((w for w in block.windows if w.label == "login" and w.text), None)
    if row is None:
        return None
    if row.text.startswith("expired"):
        return ("login expired", "warn-text")
    when = row.text.rsplit(" · ", 1)[-1]
    return (f"login expires {when}" if when.startswith("in ") else "login expires soon", "dim")


def summary_parts(
    block: display.AccountBlock, *, over_budget: bool = False, login: bool = True,
) -> list[tuple[str, str]]:
    """A row's usage at a glance, as ``(text, role)``: ``5h 12% · 7d 21%``
    with each figure's severity, or the state in ``qswap list``'s words, then
    ``login expires in 5d 12h`` while the saved login is due (``login``).
    Roles: ``ok``/``warn``/``limit`` (a figure), ``sep``, ``dim``, ``warn-text``."""
    out = _summary_core(block, over_budget=over_budget)
    extra = _login_part(block) if login and block.state != "needs-relogin" else None
    if extra is not None:
        out.extend([(" · ", "sep"), extra])
    return out


def _summary_core(block: display.AccountBlock, *, over_budget: bool) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if block.state == "needs-relogin":
        return [(display.NEEDS_RELOGIN, "warn-text")]
    if block.state == "unavailable":
        retrying = block.error not in ("store-unmirrored", "invalid_client", "setup-token")
        return [(display.usage_unavailable(block.error, retry_in_s=block.retry_in_s,
                                           retrying=retrying), "dim")]
    if block.state == "api-key":
        return [(display.api_key_summary(block), "warn-text" if over_budget else "dim")]
    if block.state == "not-read":
        return [(display.NOT_READ_YET, "dim")]
    if block.state == "note":
        note = block.notes[0] if block.notes else ""
        return [(note.split(": ", 1)[0], "dim")]
    stale = block.stale_s is not None
    limit = False
    for w in block.windows:
        if w.text is not None or w.label == "extra":
            continue
        if out:
            out.append((" · ", "sep"))
        if isinstance(w.pct, (int, float)):
            level = display.severity(w.pct, w.line)
            limit = limit or level == "limit"
            out.append((f"{w.label} {w.pct:.0f}%", "dim" if stale else level))
        else:
            out.append((f"{w.label} n/a", "dim"))
    if limit:
        out.extend([(" · ", "sep"), ("limit reached", "limit")])
    if stale:
        out.extend([(" · ", "sep"), (display.as_of(block.stale_s), "dim")])
    if not out:
        out.append((display.NOT_READ_YET, "dim"))
    return out


def _summary_style(role: str, palette: Palette) -> str:
    if role in ("ok", "warn", "limit"):
        return level_colour(role, palette)
    if role == "warn-text":
        return palette.sev_warn
    if role == "sep":
        return palette.track
    return palette.muted


def summary_text(parts: list[tuple[str, str]], palette: Palette) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    for part, role in parts:
        text.append(part, style=_summary_style(role, palette))
    return text


def row_text(
    block: display.AccountBlock,
    width: int,
    palette: Palette,
    *,
    column: int | None = None,
    extra: Sequence[str] = (),
    over_budget: bool = False,
) -> Text:
    """One account on one line: its header, then its usage at ``column``.

    Nothing wraps: the email gives way first, then the organization and the
    plan; the figures are kept. ``column`` lines the summaries of several rows
    up (``None``: right after the header)."""
    summary = summary_text(summary_parts(block, over_budget=over_budget), palette)
    if cell_len(summary.plain) > width // 2:
        # The login note goes before the header is cut to nothing.
        summary = summary_text(summary_parts(block, over_budget=over_budget, login=False), palette)
    room = max(12, width - cell_len(summary.plain) - len(GAP))
    start = min(column, room) if column is not None else room
    head = Text()
    for drop in range(0, 6):
        head = header_text(block, start, palette, extra=extra, drop=drop)
        if cell_len(head.plain) <= start and not head.plain.endswith("…"):
            break
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append_text(head)
    pad = (start if column is not None else cell_len(head.plain)) - cell_len(head.plain)
    line.append(" " * max(0, pad) + GAP)
    line.append_text(summary)
    return _fit(line, width)


def row_header_width(block: display.AccountBlock, width: int, palette: Palette,
                     extra: Sequence[str] = ()) -> int:
    """How wide a row's header is with nothing cut."""
    return cell_len(header_text(block, width, palette, extra=extra).plain)


# -- the accounts panel ----------------------------------------------------------


@dataclass
class _Piece:
    """Lines of the panel and how a crowded panel treats them. ``compact`` is
    the one-line form a crowded panel may use instead."""

    lines: list[Text]
    kind: str  # "heading" | "card" | "card-extra" | "row" | "note" | "blank" | "more"
    section: int = 0
    compact: list[Text] | None = None


class AccountsPanel(Static):
    """The dashboard's monitor: each provider's accounts under its heading,
    the active one in full and the others on one line each, in slot order.

    ``max_rows`` (set by the screen) bounds its height: past it, held-reset
    sentences go first, then the last rows of the longest section fold into
    ``… N more · w shows every account``. ``provider`` and ``active_only``
    make it the auto view's card for one provider's active account.
    """

    def __init__(
        self,
        *,
        provider: str | None = None,
        active_only: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.provider = provider
        self.active_only = active_only
        self.max_rows: int | None = None

    def on_mount(self) -> None:
        self.watch(self.app, "fleet", lambda _f: self.refresh(layout=True))
        self.watch(self.app, "theme", lambda _t: self.refresh(layout=True))

    def set_max_rows(self, rows: int | None) -> None:
        if rows != self.max_rows:
            self.max_rows = rows
            self.refresh(layout=True)

    def render(self) -> Text:
        app: QswapApp = self.app  # type: ignore[assignment]
        palette = Palette.from_theme(app.current_theme)
        fleet = app.fleet
        width = max(20, (self.size.width or 80))
        if fleet is None:
            return Text("loading…", style=palette.muted)
        try:
            pieces = self._pieces(fleet, width, palette, app)
        except Exception as e:  # a render must never take the app down
            return Text(f"Could not draw the accounts: {e}", style=palette.sev_warn)
        lines = fit_pieces(pieces, self.max_rows, palette)
        return Text("\n").join(lines) if lines else Text("")

    def _pieces(self, fleet: data.Fleet, width: int, palette: Palette, app) -> list[_Piece]:
        if self.active_only:
            view = fleet.view(self.provider or "claude")
            if view is None:
                return []
            acc = view.account(view.active_number) if view.active_number else None
            if acc is None:
                return [_Piece(sentences(_no_active_lines(view), width, palette.muted), "note")]
            block = data.block_for(view, acc, threshold=app.session_threshold(view.provider))
            return _card_pieces(block, width, palette, extra=account_extra(acc),
                                label_width=label_width_for(fleet))
        if not fleet.managed:
            lines = first_run_lines(fleet)
            return [_Piece(sentences(lines[:1], width, palette.foreground)
                           + sentences(lines[1:], width, palette.muted), "note")]
        return panel_pieces(fleet, width, palette, app)


def sentences(texts: Sequence[str], width: int, style: str) -> list[Text]:
    """Sentences wrapped to ``width`` as the command line wraps them: a
    command after "run: " or "with: " is never split."""
    return [Text(line, style=style, no_wrap=True)
            for text in texts for line in display.wrap(text, width)]


def label_width_for(fleet: data.Fleet) -> int:
    """One window-label width for every card on screen."""
    labels = [MIN_LABEL]
    for _v, acc in fleet.rows():
        last = acc.usage.last_good if isinstance(acc.usage.last_good, dict) else {}
        for w in last.get("scoped") or ():
            if isinstance(w, dict) and "name" in w:
                labels.append(len(str(w["name"])))
    return max(labels)


def _no_active_lines(view: data.ProviderView) -> list[str]:
    if not view.accounts:
        return display.nothing_managed_lines(
            view.provider, live_email=view.live_email, cli_found=view.cli_found,
        )
    if view.live_email:
        return [f"{view.name} is logged in as {view.live_email}, which qswap does not manage. "
                f"Add it with: {display.ADD_COMMANDS.get(view.provider, 'qswap add')}"]
    return [f"No {view.name} account is active."]


def first_run_lines(fleet: data.Fleet) -> list[str]:
    """Nothing managed by either provider: why, and the next step for each
    login this machine has (``cli._first_run``)."""
    claude = fleet.view("claude")
    out = display.nothing_managed_lines(
        "claude",
        live_email=claude.live_email if claude else None,
        cli_found=claude.cli_found if claude else None,
    )
    codex = fleet.view("codex")
    if codex is not None and codex.live_email:
        out.extend(display.nothing_managed_lines(
            "codex", live_email=codex.live_email, sentence=False,
        ))
    return out


def _card_pieces(block, width, palette, *, extra=(), label_width=MIN_LABEL, section=0,
                 compact: Text | None = None) -> list[_Piece]:
    lines = card_lines(block, width, palette, extra=extra, label_width=label_width)
    pieces = [_Piece([ln.text for ln in lines if not ln.optional], "card", section,
                     compact=[compact] if compact is not None else None)]
    optional = [ln.text for ln in lines if ln.optional]
    if optional:
        pieces.append(_Piece(optional, "card-extra", section))
    return pieces


def panel_pieces(fleet: data.Fleet, width: int, palette: Palette, app) -> list[_Piece]:
    pieces: list[_Piece] = []
    sectioned = fleet.sectioned
    labels = label_width_for(fleet)
    blocks = {
        (view.provider, acc.number): data.block_for(
            view, acc, threshold=app.session_threshold(view.provider) if app else None,
        )
        for view, acc in fleet.rows()
    }
    # One summary column for every one-line row: as far right as the widest
    # header needs, as long as the widest set of figures still fits after it.
    rows = [(blocks[(v.provider, a.number)], a) for v, a in fleet.rows()]
    widest = max((row_header_width(b, width, palette, account_extra(a)) for b, a in rows), default=0)
    figures = max(
        (cell_len(summary_text(summary_parts(b, login=False), palette).plain)
         for b, _a in rows if b.state == "ok"),
        default=0,
    )
    column = max(16, min(widest, width - figures - len(GAP)))
    for section, view in enumerate(fleet.views):
        if not view.accounts and not view.live_email and not view.error:
            continue
        if not view.accounts and not sectioned and not view.error:
            # Accounts in one provider and a live, unmanaged login in the
            # other: the sentence ``qswap list`` prints for it.
            pieces.append(_Piece([Text("")], "blank", section))
            pieces.append(_Piece(sentences(display.nothing_managed_lines(
                view.provider, live_email=view.live_email, sentence=False,
            ), width, palette.muted), "note", section))
            continue
        if pieces:
            pieces.append(_Piece([Text("")], "blank", section))
        if sectioned:
            pieces.append(_Piece(
                [Text(display.provider_heading(view.provider, both=True),
                      style=f"bold {palette.foreground}")],
                "heading", section,
            ))
        if view.error:
            pieces.append(_Piece(sentences([f"{view.name} accounts unavailable: {view.error}"],
                                           width, palette.sev_warn), "note", section))
            continue
        for acc in view.accounts:
            block = blocks[(view.provider, acc.number)]
            row = row_text(
                block, width, palette, column=column, extra=account_extra(acc),
                over_budget=data.over_budget(acc),
            )
            if acc.is_active:
                pieces.extend(_card_pieces(
                    block, width, palette, extra=account_extra(acc),
                    label_width=labels, section=section, compact=row,
                ))
            elif block.state == "needs-relogin":
                # The way out matters more than one line: the header, then
                # the sentence with its command, as `qswap list` says it.
                lines = [header_text(block, width, palette, extra=account_extra(acc))]
                lines.extend(state_texts(block, width, palette, roles=("warn",)))
                pieces.append(_Piece(lines, "row", section, compact=[row]))
            else:
                pieces.append(_Piece([row], "row", section))
        notes = tuple(getattr(getattr(view, "snapshot", None), "key_notes", ()) or ())
        if notes:
            # The key helper line names an account that cannot give a key, or
            # run profiles kept a stale settings copy: the repair, in words.
            pieces.append(_Piece(sentences([" ".join(n.strip() for n in notes)],
                                           width, palette.sev_warn), "note", section))
    return pieces


def fit_pieces(pieces: list[_Piece], max_rows: int | None, palette: Palette) -> list[Text]:
    """The panel's lines within ``max_rows``. A crowded panel gives up, in
    order: held-reset sentences, the blank line between sections, the second
    line of a row that needs re-login, then the bars of the active cards
    (last provider first), and last folds the final rows of the longest
    section into one ``… N more · w shows every account`` line."""

    def size(ps: list[_Piece]) -> int:
        return sum(len(p.lines) for p in ps)

    def flat(ps: list[_Piece]) -> list[Text]:
        return [line for p in ps for line in p.lines]

    if max_rows is None or size(pieces) <= max_rows:
        return flat(pieces)
    for kind in ("card-extra", "blank"):
        pieces = [p for p in pieces if p.kind != kind]
        if size(pieces) <= max_rows:
            return flat(pieces)
    for kind in ("row", "card"):
        for p in reversed(pieces):
            if p.kind == kind and p.compact is not None and len(p.lines) > 1:
                p.lines = p.compact
                if size(pieces) <= max_rows:
                    return flat(pieces)
    folded: dict[int, int] = {}
    while size(pieces) > max_rows:
        sections: dict[int, list[int]] = {}
        for i, p in enumerate(pieces):
            if p.kind == "row":
                sections.setdefault(p.section, []).append(i)
        if not sections:
            break
        section = max(sections, key=lambda s: len(sections[s]))
        idx = sections[section][-1]
        pieces.pop(idx)
        folded[section] = folded.get(section, 0) + 1
        more = _Piece([Text(f"… {folded[section]} more · w shows every account",
                            style=palette.muted)], "more", section)
        marker = next(
            (i for i, p in enumerate(pieces) if p.kind == "more" and p.section == section), None
        )
        if marker is not None:
            pieces[marker] = more
        else:
            pieces.insert(idx, more)
    lines = flat(pieces)
    if len(lines) > max_rows:
        lines = lines[: max(0, max_rows - 1)] + [
            Text("… w shows every account", style=palette.muted)
        ]
    return lines


# -- list items -----------------------------------------------------------------------


class AccountCard(Static):
    """One account in full (the switch picker and the watch page)."""

    def __init__(self, view: data.ProviderView, acc: AccountSnapshot, *, label_width: int = MIN_LABEL) -> None:
        super().__init__()
        self._view = view
        self._acc = acc
        self._label_width = label_width

    def set_account(self, view: data.ProviderView, acc: AccountSnapshot, *, label_width: int = MIN_LABEL) -> None:
        self._view = view
        self._acc = acc
        self._label_width = label_width
        self.refresh(layout=True)

    def render(self) -> Text:
        palette = Palette.from_theme(self.app.current_theme)
        width = max(20, self.size.width or 80)
        try:
            app = self.app
            threshold = app.session_threshold(self._view.provider) if hasattr(app, "session_threshold") else None
            block = data.block_for(self._view, self._acc, threshold=threshold)
            return card_text(
                block, width, palette, extra=account_extra(self._acc),
                label_width=self._label_width,
            )
        except Exception as e:  # never take the app down over one card
            return Text(f"#{self._acc.number}: could not draw this account ({e})",
                        style=palette.sev_warn)


class AccountItem(ListItem):
    """A ListView row holding an :class:`AccountCard`; knows its account."""

    def __init__(self, view: data.ProviderView, acc: AccountSnapshot, *, label_width: int = MIN_LABEL) -> None:
        super().__init__(AccountCard(view, acc, label_width=label_width))
        self.provider = view.provider
        self.number = acc.number
        self.email = acc.email

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.number)

    def set_account(self, view: data.ProviderView, acc: AccountSnapshot, *, label_width: int = MIN_LABEL) -> None:
        self.provider = view.provider
        self.number = acc.number
        self.email = acc.email
        self.query_one(AccountCard).set_account(view, acc, label_width=label_width)


class HeadingItem(ListItem):
    """A provider heading inside a list; the cursor passes over it."""

    def __init__(self, text: str) -> None:
        label = Static(text, markup=False)
        label.add_class("list-heading")
        super().__init__(label, disabled=True)
        self.add_class("heading-item")


class MenuItem(ListItem):
    """One menu row: a label plus an action id the screen dispatches on."""

    def __init__(self, label: str, action_id: str, *, muted: bool = False, heading: bool = False) -> None:
        item = Static(label, markup=False)
        if muted:
            item.add_class("menu-item-muted")
        if heading:
            item.add_class("menu-heading")
        super().__init__(item, disabled=heading)
        self.action_id = action_id

