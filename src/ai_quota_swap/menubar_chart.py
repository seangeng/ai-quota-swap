"""The menu bar's cost section: what the ledger says, and the small chart
drawn from it.

Pure: no I/O, no AppKit, no clock. ``menubar_data.read_cost`` fills a
:class:`CostSummary` off the main thread; :func:`build_chart` turns it into a
:class:`UsageChart`, the description that ``menubar_chart_view`` draws and
the outline prints. Where each part goes for a given width is here too
(:func:`frame`, :func:`layout_columns`), so the AppKit view and the demo
harness's HTML drawing of the menu lay the chart out the same way. Tested on
every platform.

The chart: seven columns, oldest left, today right, each stacked by account
at the estimated cost ``qswap usage --by account`` reports; a weekday letter
under each; the cost row's sentence above as its caption; one legend line
below with at most four accounts by their seven-day amount, the rest merged
into "other". No axis, no grid: the tallest day fills the height. Legend
names read as the account rows read (``#2 work``). When the menu shows both
providers, every Codex entry starts with ``Codex`` (``Codex #2 work``, down
to ``Codex #2`` when cut) and a Claude Code entry never has a provider
word, so even an entry cut to its number names one account.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from ai_quota_swap.labels import PROVIDER_SHORT

#: The colours accounts take, in this order (the view draws each as the
#: system colour of that name, so light and dark follow the menu). No red
#: and no pink, which read as an error; neighbours in the order look far
#: apart.
ACCOUNT_COLOURS: tuple[str, ...] = (
    "blue", "orange", "purple", "teal", "green", "brown", "indigo", "yellow",
)
#: The colour of "other": grey.
OTHER_COLOUR = -1
#: Where each provider's account #1 starts in :data:`ACCOUNT_COLOURS`:
#: Claude Code's #n takes slot n-1, Codex's #n slot n+3, so Codex #1 is
#: green. An account's colour comes from its provider and number, never its
#: rank, so a bar keeps its colour when another account overtakes it.
COLOUR_START = {"claude": 0, "codex": 4}
#: The legend names this many accounts; the rest are "other".
LEGEND_ACCOUNTS = 4
#: An account with less than this share of the week joins "other" when
#: "other" holds more than that one account.
MIN_SHARE = 0.03
OTHER = "other"
#: How ``qswap usage --by account`` names usage with no account.
UNATTRIBUTED = "unattributed"
WEEKDAY_LETTERS = ("M", "T", "W", "T", "F", "S", "S")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
#: A legend name cut with an ellipsis keeps its number and at least this
#: many characters after it (``#3 nig…``); with less room it is its number
#: alone (``#3``).
NAME_KEEP = 3
#: The legend line's width in characters, for the outline and the tests;
#: the view fits the line to its real width with the same rule.
LEGEND_LIMIT = 60


# -- what the ledger says -------------------------------------------------------------


@dataclass(frozen=True)
class CostAccount:
    """Whose usage a cost is, grouped as ``qswap usage --by account`` groups
    it: a provider's account by its ledger key (``email|org``), or usage with
    no account (``provider`` None, ``key`` ""). ``number`` is the managed
    account's number, None for one qswap does not manage; ``name`` is how
    ``qswap usage`` names it (``#2 work``, ``… (not managed)``,
    ``unattributed``), without the provider."""

    provider: str | None
    key: str
    number: int | None = None
    name: str = ""

    @property
    def ident(self) -> tuple[str | None, str]:
        return (self.provider, self.key)


@dataclass(frozen=True)
class CostDay:
    """One local day's estimated cost, by account."""

    day: date
    costs: tuple[tuple[CostAccount, float], ...] = ()


@dataclass(frozen=True)
class CostSummary:
    """Today's and the last seven days' estimated cost from the ledger, both
    providers together (``qswap usage``'s totals). ``days`` holds the same
    seven days, oldest first, by account; empty when they could not be read."""

    today: float
    week: float
    today_unpriced: int = 0
    week_unpriced: int = 0
    days: tuple[CostDay, ...] = ()

    def text(self) -> str:
        """``Today $12.40 · 7 days $179.72 (estimated)``."""
        from ai_quota_swap.ledger.report import Totals, fmt_cost

        today = fmt_cost(Totals(cost=self.today, unpriced_tokens=self.today_unpriced))
        week = fmt_cost(Totals(cost=self.week, unpriced_tokens=self.week_unpriced))
        return f"Today {today} · 7 days {week} (estimated)"


# -- the chart's description -------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One account's part of a day's column. ``colour`` indexes
    :data:`ACCOUNT_COLOURS`, or is :data:`OTHER_COLOUR`."""

    key: str
    amount: float
    colour: int


@dataclass(frozen=True)
class Column:
    """One day: its weekday letter and its segments, bottom first (the
    legend's order, "other" on top). No segments: nothing that day."""

    day: date
    label: str
    segments: tuple[Segment, ...] = ()
    today: bool = False

    @property
    def total(self) -> float:
        return sum(s.amount for s in self.segments)


@dataclass(frozen=True)
class LegendEntry:
    """A dot, a name, a seven-day amount. ``head`` is how much of ``name``
    names the account's number (``#3``, ``Codex #3``), kept when the name
    is cut."""

    key: str
    name: str
    amount: float
    colour: int
    head: int = 0

    @property
    def amount_text(self) -> str:
        return whole_dollars(self.amount)


@dataclass(frozen=True)
class UsageChart:
    caption: str
    columns: tuple[Column, ...]
    legend: tuple[LegendEntry, ...]

    @property
    def peak(self) -> float:
        """The tallest day's total, which fills the chart's height."""
        return max((c.total for c in self.columns), default=0.0)

    def day_totals(self) -> list[tuple[Column, str]]:
        return [(c, _money(c.total)) for c in self.columns]

    def accessibility_label(self) -> str:
        """The caption, then each day's total: ``Today $12.40 · 7 days
        $179.72 (estimated). Thursday $20.10, …, today $12.40.``"""
        days = ", ".join(
            f"{'today' if c.today else WEEKDAY_NAMES[c.day.weekday()]} {text}"
            for c, text in self.day_totals()
        )
        return f"{self.caption}. {days}."

    def outline(self) -> str:
        """The chart in one line, for the menu's outline: each day's total,
        then the legend."""
        days = " · ".join(f"{WEEKDAY_NAMES[c.day.weekday()][:3]} {text}" for c, text in self.day_totals())
        legend = " · ".join(f"{e.name} {e.amount_text}" for e in self.legend)
        return f"chart: {days} | {legend}"

    def legend_text(self, names: Sequence[str] | None = None) -> str:
        """The legend line in characters, a dot before each entry."""
        names = [e.name for e in self.legend] if names is None else names
        return "  ".join(f"● {n} {e.amount_text}" for n, e in zip(names, self.legend))

    def fit_legend(self, fits: Callable[[list[str]], bool] | None = None) -> list[str]:
        """The legend's names, cut so the line fits: ``fits(names)`` says
        whether a line with these names fits (by default
        :data:`LEGEND_LIMIT` characters). See :func:`fit_names`. With a
        ``fits`` that always says no, every name at its shortest."""
        if fits is None:
            def fits(names: list[str]) -> bool:
                return len(self.legend_text(names)) <= LEGEND_LIMIT
        return fit_names([e.name for e in self.legend], [e.head for e in self.legend], fits)


def whole_dollars(amount: float) -> str:
    """``$120``; ``<$1`` for an amount that rounds to nothing."""
    dollars = int(amount + 0.5)
    if dollars == 0 and amount > 0:
        return "<$1"
    return f"${dollars:,}"


def _money(amount: float) -> str:
    """``$12.40``, as the caption and ``qswap usage`` write a cost."""
    from ai_quota_swap.ledger.report import Totals, fmt_cost

    return fmt_cost(Totals(cost=amount))


def _cut(name: str, width: int) -> str:
    """``name`` in ``width`` characters, ending in an ellipsis when cut (and
    never in a dot or a dash before it: ``jordan…``, not ``jordan.…``)."""
    if len(name) <= width:
        return name
    return name[: max(0, width - 1)].rstrip(" .,-_@·") + "…"


def shortest_name(name: str, head: int) -> str:
    """The shortest a legend name gets: its number (``#3``, ``Codex #3``),
    the first ``head`` characters. A name with no number (``unattributed``,
    ``… (not managed)``) keeps twice :data:`NAME_KEEP` characters
    (``unattr…``), so a short one such as "other" is never cut."""
    return name[:head] if head else _cut(name, 2 * NAME_KEEP + 1)


def name_cuts(name: str, head: int) -> list[str]:
    """Every cut of ``name`` with an ellipsis, shortest first: each keeps
    its number and at least :data:`NAME_KEEP` characters after it (twice
    as many with no number), and none ends in punctuation before the
    ellipsis. Empty when the name is too short to cut."""
    keep = head + 1 + NAME_KEEP if head else 2 * NAME_KEEP
    out: list[str] = []
    for width in range(keep + 1, len(name)):
        cut = _cut(name, width)
        if len(cut) - 1 >= keep and (not out or cut != out[-1]):
            out.append(cut)
    return out


def fit_names(
    names: Sequence[str], heads: Sequence[int], fits: Callable[[list[str]], bool]
) -> list[str]:
    """``names`` as long as the legend line has room for, so it never
    wraps. ``heads[i]`` is how much of ``names[i]`` is its number (0 for
    none); ``fits(names)`` says whether a line with these names fits.

    Every name starts at its shortest form (:func:`shortest_name`). Then,
    in legend order (the largest amount first), each grows to the longest
    form that still fits: the whole name if that fits, else its longest
    cut (:func:`name_cuts`), else it stays at its shortest. So no room is
    left that a name could have used, and a wider line never shows a
    shorter name for the first entry. When even the shortest forms do not
    fit, returns them. ``fits`` must say yes to a narrower line whenever it
    says yes to a wider one."""
    out = [shortest_name(n, h) for n, h in zip(names, heads)]
    for i, (name, head) in enumerate(zip(names, heads)):
        if out[i] == name:
            continue

        def fits_with(form: str, i: int = i) -> bool:
            return fits([*out[:i], form, *out[i + 1:]])

        if fits_with(name):
            out[i] = name
            continue
        cuts = name_cuts(name, head)
        lo, hi = 0, len(cuts)  # every cut before lo fits, none from hi on
        while lo < hi:
            mid = (lo + hi) // 2
            if fits_with(cuts[mid]):
                lo = mid + 1
            else:
                hi = mid
        if lo:
            out[i] = cuts[lo - 1]
    return out


# -- where it goes ---------------------------------------------------------------------------
#
# In points, y growing downwards from the view's top. The AppKit view and the
# HTML drawing both lay the chart out with these, each with its own fonts'
# line heights and its own insets (where the menu's titles start and end).

#: The caption sits in a band as tall as a menu row, as the text row did.
CAPTION_BAND = 24.0
BAR_TOP_GAP = 3.0
#: The bars' height: the tallest day fills it.
BAR_AREA = 44.0
LABEL_GAP = 3.0
LEGEND_GAP = 4.0
BOTTOM = 5.0
#: The whole view is never taller than this.
MAX_HEIGHT = 110.0
#: A bar is this share of its column, at most :data:`MAX_BAR_WIDTH` wide.
BAR_SHARE = 0.64
MAX_BAR_WIDTH = 40.0
#: Stacked segments are told apart by a gap left unpainted, only between
#: two neighbours both at least :data:`GAP_MIN` tall; thinner segments
#: stack with no gap, or a thin stack turns into stripes.
SEGMENT_GAP = 1.0
GAP_MIN = 3.0
#: A segment with any cost is at least this tall.
MIN_SEGMENT = 1.0
#: What a day with nothing draws on the baseline.
STUB = 1.0
#: The radius of a bar's top corners, the same on every bar (less only on
#: a bar shorter than it).
CORNER = 3.0
DOT = 7.0
DOT_GAP = 4.0
ENTRY_GAP = 10.0
#: Bar edges sit on half points, one pixel on a 2x screen, so edges and
#: gaps stay sharp.
SNAP = 0.5


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Frame:
    """Where each band of the view starts, from its top: the caption's band
    (from 0), the bars (standing on ``base``), the weekday letters, the
    legend; and the view's height."""

    bars_top: float
    base: float
    letters_top: float
    legend_top: float
    height: float


def frame(day_line: float, legend_line: float) -> Frame:
    """The view's bands for weekday letters ``day_line`` tall and a legend
    line ``legend_line`` tall."""
    bars_top = CAPTION_BAND + BAR_TOP_GAP
    base = bars_top + BAR_AREA
    letters_top = base + LABEL_GAP
    legend_top = letters_top + day_line + LEGEND_GAP
    return Frame(bars_top, base, letters_top, legend_top, legend_top + legend_line + BOTTOM)


@dataclass(frozen=True)
class SegmentBox:
    """Where one segment is painted: a plain rectangle, inside its column's
    outline."""

    segment: Segment
    box: Box


@dataclass(frozen=True)
class ColumnBox:
    """Where one day goes: its bar's left edge and width, the centre its
    weekday letter sits on, and either its segments, bottom first, with the
    one outline they are clipped to (the whole stack, its top corners
    rounded at :attr:`radius`), or the stub of a day with nothing."""

    column: Column
    x: float
    width: float
    segments: tuple[SegmentBox, ...] = ()
    stub: Box | None = None
    outline: Box | None = None

    @property
    def centre(self) -> float:
        return self.x + self.width / 2

    @property
    def radius(self) -> float:
        """The outline's top corners: :data:`CORNER`, less on a stack
        shorter than that."""
        if self.outline is None:
            return 0.0
        return max(0.0, min(CORNER, self.outline.h, self.outline.w / 2))


def _snap(value: float) -> float:
    return round(value / SNAP) * SNAP


def _stack(amounts: Sequence[float], height: float) -> list[float]:
    """Each segment's height, bottom first: its share of ``height``, at least
    :data:`MIN_SEGMENT`, the column ``height`` tall unless its segments at
    their least are taller. Boundaries sit on half points."""
    total = sum(amounts)
    heights = [max(MIN_SEGMENT, height * a / total) for a in amounts]
    over = sum(heights) - max(height, MIN_SEGMENT * len(heights))
    if over > 0:  # the room the small ones took comes off the others, pro rata
        slack = [h - MIN_SEGMENT for h in heights]
        room = sum(slack)
        heights = [h - over * s / room for h, s in zip(heights, slack)]
    out, edge, reached = [], 0.0, 0.0
    for h in heights:
        reached += h
        top = max(_snap(reached), edge + MIN_SEGMENT)
        out.append(top - edge)
        edge = top
    return out


def layout_columns(
    chart: UsageChart, *, left: float, width: float, base: float
) -> tuple[ColumnBox, ...]:
    """Where each day goes in a content area ``width`` wide from ``left``,
    the bars standing on ``base``. A bar is :data:`BAR_SHARE` of a seventh
    of the width, at most :data:`MAX_BAR_WIDTH`. The bars run edge to
    edge: the first bar's left edge is ``left``, the last bar's right edge
    ``left + width``, the others evenly spaced between, each edge on a
    device pixel (:data:`SNAP`). The tallest day is :data:`BAR_AREA` tall;
    a day with nothing is a :data:`STUB` on the baseline. A gap of
    :data:`SEGMENT_GAP` comes off the top of a segment whose upper
    neighbour is, like it, at least :data:`GAP_MIN` tall. Each column's
    segments are plain rectangles inside one outline, so every bar has the
    same corners whatever its top segment's height."""
    n = len(chart.columns)
    if not n or width <= 0:
        return ()
    bar = max(SNAP, _snap(min(MAX_BAR_WIDTH, BAR_SHARE * width / n)))
    step = (width - bar) / (n - 1) if n > 1 else 0.0
    peak = chart.peak
    out = []
    for i, column in enumerate(chart.columns):
        x = _snap(left + i * step)
        if not column.segments or peak <= 0:
            out.append(ColumnBox(column, x, bar, stub=Box(x, base - STUB, bar, STUB)))
            continue
        heights = _stack([s.amount for s in column.segments], BAR_AREA * column.total / peak)
        boxes, y, last = [], base, len(heights) - 1
        for j, (seg, h) in enumerate(zip(column.segments, heights)):
            y -= h
            shown = h
            if j < last and h >= GAP_MIN and heights[j + 1] >= GAP_MIN:
                shown = h - SEGMENT_GAP
            boxes.append(SegmentBox(seg, Box(x, y + h - shown, bar, shown)))
        outline = Box(x, y, bar, base - y)
        out.append(ColumnBox(column, x, bar, tuple(boxes), outline=outline))
    return tuple(out)


def legend_width(text_widths: Sequence[float]) -> float:
    """How wide the legend line is with entries whose texts (name and
    amount) are ``text_widths`` wide: a dot, a gap and the text each,
    :data:`ENTRY_GAP` between entries."""
    return sum(DOT + DOT_GAP + w for w in text_widths) + ENTRY_GAP * max(0, len(text_widths) - 1)


# -- building it ---------------------------------------------------------------------------


_PROVIDER_ORDER = {"claude": 0, "codex": 1}


def _identity_order(acc: CostAccount) -> tuple:
    """Managed accounts first, Claude Code's then Codex's, by number; then
    the rest by provider and key. Never by amount."""
    return (
        acc.number is None,
        _PROVIDER_ORDER.get(acc.provider or "", 2),
        acc.number if acc.number is not None else 0,
        acc.key,
    )


def account_colours(accounts: Sequence[CostAccount]) -> dict[tuple, int]:
    """Each account's colour, by its provider and number: Claude Code's #1
    is blue, #2 orange, and so on; Codex's #1 is green. Two accounts that
    would share a colour (more than four of a provider, or a number that
    wraps round) are told apart in identity order: the later one takes the
    next free colour. An account qswap does not manage, and usage with no
    account, take the first free colour. ``accounts`` is every account
    with usage in the chart's days, so which of them the legend names
    changes no account's colour."""
    n = len(ACCOUNT_COLOURS)
    used: set[int] = set()
    out: dict[tuple, int] = {}
    for acc in sorted(accounts, key=_identity_order):
        if acc.number is not None and acc.provider in COLOUR_START:
            start = (COLOUR_START[acc.provider] + acc.number - 1) % n
        else:
            start = 0
        colour = next((c for c in ((start + j) % n for j in range(n)) if c not in used), start)
        used.add(colour)
        out[acc.ident] = colour
    return out


def _segment_key(acc: CostAccount) -> str:
    if acc.provider is None:
        return UNATTRIBUTED
    if acc.number is not None:
        return f"{acc.provider}#{acc.number}"
    return f"{acc.provider}:{acc.key}"


def _legend_name(acc: CostAccount, names: Mapping[tuple[str, str], str]) -> tuple[str, int]:
    """The account's name as its row shows it (``#2 work``), and how much
    of it is the number."""
    if acc.provider is None:
        return acc.name or UNATTRIBUTED, 0
    name = acc.name
    if acc.number is not None:
        name = names.get((acc.provider, str(acc.number))) or name or f"#{acc.number}"
    head = len(f"#{acc.number}") if acc.number is not None and name.startswith(f"#{acc.number}") else 0
    return name, head


def _with_provider(acc: CostAccount, name: str, head: int) -> tuple[str, int]:
    """``Codex #1 maya@example.com``: the name with its provider in front,
    the provider kept with the number when the name is cut."""
    prefix = PROVIDER_SHORT.get(acc.provider or "", acc.provider or "") + " "
    return prefix + name, head + len(prefix) if head else 0


def build_chart(
    cost: CostSummary, *, names: Mapping[tuple[str, str], str] | None = None, both: bool = False,
) -> UsageChart | None:
    """The chart for ``cost``'s seven days, or None when there is nothing to
    draw (the days could not be read, or no usage has a price).

    ``names`` maps ``(provider, number)`` to the name the account's row
    shows (``#2 work``), which wins over the one the ledger read carried;
    without it the legend uses ``qswap usage``'s.

    ``both``: whether the menu shows both providers (``MenuInput.codex_shown``,
    which also gives Codex its own section). Then every Codex account's
    entry starts with ``Codex`` (``Codex #2 work``, ``Codex #2`` when cut)
    and a Claude Code account's has no provider word. Without it no entry
    of an account qswap manages has one."""
    names = names or {}
    days = cost.days
    if not days:
        return None
    week: dict[tuple, float] = {}
    who: dict[tuple, CostAccount] = {}
    for day in days:
        for acc, amount in day.costs:
            if amount > 0:
                week[acc.ident] = week.get(acc.ident, 0.0) + amount
                who.setdefault(acc.ident, acc)
    if not week:
        return None
    ranked = sorted(week, key=lambda k: (-week[k], _identity_order(who[k])))
    shown, rest = ranked[:LEGEND_ACCOUNTS], set(ranked[LEGEND_ACCOUNTS:])
    # A sliver too thin to see in a column joins "other" too, when there
    # is more than itself to merge; the top account always stays.
    small = [k for k in shown[1:] if week[k] < MIN_SHARE * sum(week.values())]
    if small and len(rest) + len(small) > 1:
        shown = [k for k in shown if k not in small]
        rest.update(small)
    # Over every account of the week, not only the legend's: an account
    # entering or leaving the top four moves no other account's colour.
    colours = account_colours([who[k] for k in ranked])
    # The provider word goes by provider, not by a clash of names: decided
    # on the whole name, a clash test misses two entries that read the same
    # once cut ("#1" and "#1"). An account qswap does not manage
    # ("… (not managed)") gets its provider only when another such account
    # reads the same.
    unmanaged = [
        _legend_name(who[k], names)[0] for k in ranked
        if who[k].number is None and who[k].provider is not None
    ]
    legend = []
    for k in shown:
        acc = who[k]
        name, head = _legend_name(acc, names)
        if acc.number is not None:
            if both and acc.provider == "codex":
                name, head = _with_provider(acc, name, head)
        elif acc.provider is not None and unmanaged.count(name) > 1:
            name, head = _with_provider(acc, name, head)
        legend.append(LegendEntry(_segment_key(acc), name, week[k], colours[k], head))
    if rest:
        legend.append(LegendEntry(OTHER, OTHER, sum(week[k] for k in rest), OTHER_COLOUR))
    columns = []
    for i, day in enumerate(days):
        per: dict[tuple, float] = {}
        for acc, amount in day.costs:
            if amount > 0:
                per[acc.ident] = per.get(acc.ident, 0.0) + amount
        segments = [Segment(_segment_key(who[k]), per[k], colours[k]) for k in shown if k in per]
        other = sum(v for k, v in per.items() if k in rest)
        if other > 0:
            segments.append(Segment(OTHER, other, OTHER_COLOUR))
        columns.append(Column(
            day.day, WEEKDAY_LETTERS[day.day.weekday()], tuple(segments), today=i == len(days) - 1,
        ))
    return UsageChart(cost.text(), tuple(columns), tuple(legend))
