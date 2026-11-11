"""The menu bar's cost section: what the ledger says, and the small chart
drawn from it.

Pure: no I/O, no AppKit, no clock. ``menubar_data.read_cost`` fills a
:class:`CostSummary` off the main thread; :func:`build_chart` turns it into a
:class:`UsageChart`, the description that ``menubar_chart_view`` draws and
the outline prints. Tested on every platform.

The chart: seven columns, oldest left, today right, each stacked by account
at the estimated cost ``qswap usage --by account`` reports; a weekday letter
under each; the cost row's sentence above as its caption; one legend line
below with at most four accounts by their seven-day amount, the rest merged
into "other". No axis, no grid: the tallest day fills the height. Legend
names read as the account rows read (``#2 work``); two that would read the
same get their provider (``Claude #2 work``, ``Codex #2 work``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from ai_quota_swap.labels import PROVIDER_SHORT

#: The colours accounts take, in this order (the view draws each as the
#: system colour of that name, so light and dark follow the menu). Never
#: red, which reads as an error.
ACCOUNT_COLOURS: tuple[str, ...] = (
    "blue", "teal", "indigo", "orange", "pink", "purple", "yellow", "green",
)
#: The colour of "other": grey.
OTHER_COLOUR = -1
#: Where each provider's account #1 starts in :data:`ACCOUNT_COLOURS`.
#: An account's colour comes from its provider and number, never its rank,
#: so a bar keeps its colour when another account overtakes it. Codex starts
#: at pink so the usual few accounts of each provider never meet.
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
#: A legend name keeps its number and this many characters after it before
#: it is cut to the number alone (``#3 nig…``).
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
    names the account's number (``#3``, ``Claude #3``), kept when the name
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
        :data:`LEGEND_LIMIT` characters). See :func:`fit_names`."""
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


def fit_names(
    names: Sequence[str], heads: Sequence[int], fits: Callable[[list[str]], bool]
) -> list[str]:
    """``names`` cut with an ellipsis until ``fits(names)``, so the legend
    line never wraps. ``heads[i]`` is how much of ``names[i]`` is its
    number (0 for none). Every name longer than a common length is cut to
    it, the longest length that fits, so the longest names give way first;
    a name keeps at least its number and :data:`NAME_KEEP` characters
    (``#3 nig…``; one with no number keeps twice as many, ``unattr…``).
    When even that does not fit, names are cut to their number alone
    (``#3``), from the end of the legend (the smallest amounts) back. A
    short name such as "other" is never cut. Returns the shortest it can
    when nothing more can give way. ``fits`` must say yes to shorter names
    whenever it says yes to longer ones."""
    if fits(list(names)):
        return list(names)
    floors = [h + 1 + NAME_KEEP + 1 if h else 2 * NAME_KEEP + 1 for h in heads]
    bare: set[int] = set()

    def cut_to(length: int) -> list[str]:
        return [
            names[i][: heads[i]] if i in bare else _cut(names[i], max(floors[i], length))
            for i in range(len(names))
        ]

    numbered = [i for i in reversed(range(len(names))) if heads[i]]
    while True:
        lo, hi = 0, max((len(n) for n in names), default=0)
        if fits(cut_to(lo)):
            while lo < hi:  # the longest common length that fits
                mid = (lo + hi + 1) // 2
                if fits(cut_to(mid)):
                    lo = mid
                else:
                    hi = mid - 1
            return cut_to(lo)
        if not numbered:
            return cut_to(0)
        bare.add(numbered.pop(0))


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
    is blue, #2 teal, and so on; Codex's #1 is pink. Two accounts that
    would share a colour (more than four of a provider, or a number that
    wraps round) are told apart in identity order: the later one takes the
    next free colour. An account qswap does not manage, and usage with no
    account, take the first free colour."""
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
    """``Claude #1 maya@example.com``: for two entries that would otherwise
    read the same, one per provider."""
    prefix = PROVIDER_SHORT.get(acc.provider or "", acc.provider or "") + " "
    return prefix + name, head + len(prefix) if head else 0


def build_chart(
    cost: CostSummary, *, names: Mapping[tuple[str, str], str] | None = None
) -> UsageChart | None:
    """The chart for ``cost``'s seven days, or None when there is nothing to
    draw (the days could not be read, or no usage has a price).

    ``names`` maps ``(provider, number)`` to the name the account's row
    shows (``#2 work``), which wins over the one the ledger read carried;
    without it the legend uses ``qswap usage``'s."""
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
    colours = account_colours([who[k] for k in shown])
    named = {k: _legend_name(who[k], names) for k in shown}
    seen: dict[str, int] = {}
    for name, _head in named.values():
        seen[name] = seen.get(name, 0) + 1
    legend = []
    for k in shown:
        name, head = named[k]
        if seen[name] > 1 and who[k].provider is not None:
            name, head = _with_provider(who[k], name, head)
        legend.append(LegendEntry(_segment_key(who[k]), name, week[k], colours[k], head))
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
