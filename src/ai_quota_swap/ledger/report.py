"""Reports over the ledger: grouping, attribution fix-ups, prices, rendering.

Attribution at report time
    Buckets carry the account the activation log gave each event (or a
    preset one: a ``run`` profile's own history, the desktop app). One more
    rule runs here, for ``qswap run`` sessions whose history is shared into
    the default tree: Claude Code stamps each session with the account that
    started it (``session-owner``). A session started on the default login
    starts on the account the log says was live; one that started on a
    different account was started from a ``run`` profile (or after a login
    qswap had not yet observed), and a profile's login does not change, so
    the whole session goes to that account. Sessions that start before the
    first activation stay unattributed: there is no backfill.

Text output
    Every line fits ``display.WIDTH`` columns (docs/design/ux.md). The label
    column takes what the number columns leave and cuts a longer label with
    an ellipsis: account and model names lose their end, paths their start;
    a row's ``head`` and ``tail`` (a session id, "(not managed)") are kept.
    A report with no usage prints a sentence saying why, and what to do,
    instead of a table of zeros.
"""

from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ai_quota_swap.display import WIDTH, fit, wrap
from ai_quota_swap.labels import account_label
from ai_quota_swap.ledger.activations import ActivationLog, identity_key
from ai_quota_swap.ledger.pricing import (
    ANTHROPIC_SOURCE,
    FAST_SUFFIX,
    OPENAI_SOURCE,
    PRICES_FETCHED,
    PriceTable,
)
from ai_quota_swap.ledger.readers import DESKTOP_ACCOUNT
from ai_quota_swap.ledger.store import LedgerStore

GROUPINGS = ("day", "model", "account", "project", "provider", "session")
PROVIDERS = ("claude", "codex")
PROVIDER_LABELS = {"claude": "Claude Code", "codex": "Codex"}
UNATTRIBUTED = ""
#: Ends the label of an account qswap does not manage.
NOT_MANAGED = " (not managed)"

_TEXT_ROW_LIMIT = 50


@dataclass
class Totals:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0
    requests: int = 0
    cost: float = 0.0
    unpriced_tokens: int = 0

    def add(self, toks: tuple[int, ...], cost: float | None) -> None:
        self.input += toks[0]
        self.output += toks[1]
        self.cache_read += toks[2]
        self.cache_write_5m += toks[3]
        self.cache_write_1h += toks[4]
        self.reasoning += toks[5]
        self.requests += toks[6]
        if cost is None:
            self.unpriced_tokens += toks[0] + toks[1] + toks[2] + toks[3] + toks[4]
        else:
            self.cost += cost

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write_5m + self.cache_write_1h

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    def cost_or_none(self) -> float | None:
        if self.unpriced_tokens and not self.cost:
            return None
        return round(self.cost, 4)

    def to_json(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite5m": self.cache_write_5m,
            "cacheWrite1h": self.cache_write_1h,
            "reasoning": self.reasoning,
            "requests": self.requests,
            "totalTokens": self.tokens,
            "costUsd": self.cost_or_none(),
            "unpricedTokens": self.unpriced_tokens,
        }


@dataclass
class Row:
    key: object
    label: str
    totals: Totals = field(default_factory=Totals)
    extra: dict = field(default_factory=dict)
    # How the text table cuts a label too wide for its column: ``head`` and
    # ``tail`` (a start and an end of ``label``) stay whole, and the middle
    # loses its end (``cut="end"``) or, for a path, its start (``"start"``).
    head: str = ""
    tail: str = ""
    cut: str = "end"
    #: The label JSON carries when it differs from the text one: an account
    #: named by a placeholder keeps its stored address there.
    json_label: str | None = None


@dataclass
class Report:
    by: str
    since: date
    until: date
    provider: str | None
    rows: list[Row]
    total: Totals
    unpriced_models: dict[str, int]
    unattributed_tokens: int
    profile_sessions: int
    prices: PriceTable
    attribution_starts: dict[str, str | None]
    # Set by run_usage, for the text report's empty states. The directories a
    # scan looked in when it found no session file at all (home as ``~``);
    # None when it found some, or did not scan.
    searched_dirs: tuple[str, ...] | None = None
    # True when the report came from the ledger without a scan (--no-scan).
    unscanned: bool = False

    @property
    def unattributed_share(self) -> float:
        return self.unattributed_tokens / self.total.tokens if self.total.tokens else 0.0

    @property
    def empty(self) -> bool:
        """No usage at all in the report's range."""
        return not self.total.tokens and not self.total.requests


def day_bounds(since: date, until: date) -> tuple[int, int]:
    """UTC epoch hours covering local ``since`` 00:00 to ``until`` 24:00."""
    start = datetime(since.year, since.month, since.day).astimezone()
    end = datetime(until.year, until.month, until.day).astimezone() + timedelta(days=1)
    return int(start.timestamp() // 3600), int(-(-end.timestamp() // 3600))


def _local_day(hour: int) -> date:
    return datetime.fromtimestamp(hour * 3600).date()


class Accounts:
    """Labels for account keys, from the provider registries."""

    def __init__(self, registries: dict[str, dict | None], logs: dict[str, ActivationLog]):
        # (provider, account key) → number, email, alias. Claude and Codex
        # each number their accounts from 1, so a key is looked up in its own
        # provider's registry only.
        self.by_key: dict[tuple[str, str], dict] = {}
        self.by_owner: dict[str, str] = {}
        for provider, reg in registries.items():
            accounts = (reg or {}).get("accounts") or {}
            if not isinstance(accounts, dict):
                continue
            for num, acct in accounts.items():
                if not isinstance(acct, dict) or not acct.get("email"):
                    continue
                org = acct.get("organizationUuid") or ""
                key = identity_key(acct["email"], org)
                self.by_key.setdefault(
                    (provider, key),
                    {
                        "number": int(num) if str(num).isdigit() else None,
                        "email": acct["email"],
                        "alias": acct.get("alias") or "",
                    },
                )
                if provider == "claude" and acct.get("uuid"):
                    self.by_owner[f"{acct['uuid']}|{org}"] = key
        self.former_numbers: dict[str, dict[str, int]] = {
            provider: log.numbers() for provider, log in logs.items()
        }

    def owner_key(self, owner: str | None) -> str | None:
        return self.by_owner.get(owner) if owner else None

    def describe(
        self, key: str, provider: str | None = "claude", *, show_provider: bool = False,
        stored: bool = False,
    ) -> tuple[str, dict | None, str]:
        """``(label, JSON info, tail)`` for an account key in ``provider``'s
        usage. ``show_provider`` prefixes the provider's name (``Claude #2
        work``), for a report holding both providers' usage. ``tail`` is the
        end of the label a narrow table must keep (``" (not managed)"``)."""
        if key == UNATTRIBUTED:
            return "unattributed", None, ""
        if key == DESKTOP_ACCOUNT:
            return "Claude desktop app", None, ""
        email, _, _org = key.partition("|")
        prefix = provider if show_provider else None
        info = self.by_key.get((provider or "", key))
        if info is not None:
            num = info["number"]
            label = account_label(num, alias=info["alias"] or None, email=email,
                                  provider=prefix, stored=stored)
            return label, {"number": num, "email": email}, ""
        found: dict = {"number": None, "email": email}
        num = self.former_numbers.get(provider or "", {}).get(key)
        if num is not None:
            found["formerNumber"] = num
        label = account_label(None, email=email, provider=prefix, stored=stored)
        return label + NOT_MANAGED, found, NOT_MANAGED


def _profile_sessions(
    store: LedgerStore, accounts: Accounts, log: ActivationLog
) -> dict[str, str]:
    """``{session: account}`` for Claude sessions started on a login other than
    the default one: ``qswap run`` sessions whose history went to the shared
    tree."""
    out: dict[str, str] = {}
    if not len(log):
        return out
    for session, (owner, first_ts) in store.sessions("claude").items():
        if first_ts is None:
            continue
        owner_key = accounts.owner_key(owner)
        if owner_key is None:
            continue
        start = log.account_at(first_ts)
        if start != UNATTRIBUTED and start != owner_key:
            out[session] = owner_key
    return out


def build_report(
    store: LedgerStore,
    *,
    since: date,
    until: date,
    by: str = "day",
    provider: str | None = None,
    registries: dict[str, dict | None] | None = None,
    logs: dict[str, ActivationLog] | None = None,
    prices: PriceTable | None = None,
) -> Report:
    if by not in GROUPINGS:
        raise ValueError(f"unknown grouping: {by}")
    logs = logs or {}
    prices = prices or PriceTable()
    accounts = Accounts(registries or {}, logs)
    claude_log = logs.get("claude") or ActivationLog([])
    overrides = _profile_sessions(store, accounts, claude_log)
    since_hour, until_hour = day_bounds(since, until)
    raw = store.query_buckets(since_hour=since_hour, until_hour=until_hour, provider=provider)

    groups: dict[object, Row] = {}
    total = Totals()
    unpriced: dict[str, int] = {}
    unattributed = 0
    used_profile_sessions: set[str] = set()
    seen_providers: set[str] = set()
    for prov, hour, account, model, project, session, *toks in raw:
        seen_providers.add(prov)
        toks = tuple(toks)
        if prov == "claude" and session in overrides and account not in (DESKTOP_ACCOUNT,):
            account = overrides[session]
            used_profile_sessions.add(session)
        found = prices.lookup(model)
        cost = None
        if found is not None:
            cost = found[1].cost(*toks[:5])
        else:
            unpriced[model] = unpriced.get(model, 0) + sum(toks[:5])
        if account == UNATTRIBUTED:
            unattributed += sum(toks[:5])
        if by == "day":
            gkey: object = _local_day(hour)
        elif by == "model":
            gkey = model
        elif by == "account":
            # One row per provider's account: the two number their accounts
            # separately. Unattributed usage is one row whatever its provider.
            gkey = (None, account) if account == UNATTRIBUTED else (prov, account)
        elif by == "project":
            gkey = project
        elif by == "provider":
            gkey = prov
        else:
            gkey = (prov, session)
        row = groups.get(gkey)
        if row is None:
            row = groups[gkey] = Row(key=gkey, label="")
            if by == "session":
                row.extra["project"] = project
        row.totals.add(toks, cost)
        total.add(toks, cost)

    if by == "day":
        d = since
        while d <= until:
            groups.setdefault(d, Row(key=d, label=""))
            d += timedelta(days=1)
        rows = sorted(groups.values(), key=lambda r: r.key)  # type: ignore[arg-type,return-value]
    else:
        rows = sorted(
            groups.values(),
            key=lambda r: (-(r.totals.cost or 0.0), -r.totals.tokens, str(r.key)),
        )
    both = len(seen_providers) > 1
    for row in rows:
        _label(row, by, accounts, show_provider=both)
    return Report(
        by=by,
        since=since,
        until=until,
        provider=provider,
        rows=rows,
        total=total,
        unpriced_models=dict(sorted(unpriced.items(), key=lambda x: -x[1])),
        unattributed_tokens=unattributed,
        profile_sessions=len(used_profile_sessions),
        prices=prices,
        attribution_starts={
            p: _iso_or_none(log.first_ts) for p, log in logs.items()
        },
    )


def _iso_or_none(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def home_path(path: str | Path) -> str:
    """``path`` with the home directory spelled ``~``."""
    path = str(path)
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _short_path(path: str) -> str:
    return home_path(path) if path else "(no project)"


def _label(row: Row, by: str, accounts: Accounts, *, show_provider: bool = False) -> None:
    if by == "day":
        row.label = row.key.strftime("%a %Y-%m-%d")  # type: ignore[union-attr]
    elif by == "model":
        model = str(row.key)
        row.label = (
            model[: -len(FAST_SUFFIX)] + " (fast)" if model.endswith(FAST_SUFFIX) else model
        )
    elif by == "account":
        prov, key = row.key  # type: ignore[misc]
        row.label, info, row.tail = accounts.describe(key, prov, show_provider=show_provider)
        stored = accounts.describe(key, prov, show_provider=show_provider, stored=True)[0]
        row.json_label = stored if stored != row.label else None
        row.extra["account"] = info
        row.extra["provider"] = prov
    elif by == "project":
        row.label = _short_path(str(row.key))
        row.cut = "start"
    elif by == "provider":
        row.label = PROVIDER_LABELS.get(str(row.key), str(row.key))
    else:
        prov, session = row.key  # type: ignore[misc]
        row.extra["provider"] = prov
        row.head = f"{session[:8] or '(none)':<8}  "
        row.label = row.head + _short_path(row.extra.get("project", ""))
        row.cut = "start"


# -- JSON --------------------------------------------------------------------


def report_to_json(report: Report, scan: dict | None = None) -> dict:
    from ai_quota_swap.json_output import SCHEMA_VERSION

    rows = []
    for row in report.rows:
        if report.by == "day":
            key: object = row.key.isoformat()  # type: ignore[union-attr]
        elif report.by == "session":
            key = row.key[1]  # type: ignore[index]
        else:
            key = row.key
        entry = {"key": key, "label": row.json_label or row.label, **row.totals.to_json()}
        if report.by == "account":
            entry["key"] = row.key[1] or None  # type: ignore[index]
            entry["account"] = row.extra.get("account")
            entry["provider"] = row.extra.get("provider")
        elif report.by == "session":
            entry["provider"] = row.extra.get("provider")
            entry["project"] = row.extra.get("project")
        rows.append(entry)
    out = {
        "schemaVersion": SCHEMA_VERSION,
        "by": report.by,
        "since": report.since.isoformat(),
        "until": report.until.isoformat(),
        "timezone": datetime.now().astimezone().strftime("%z"),
        "provider": report.provider,
        "rows": rows,
        "total": report.total.to_json(),
        "estimate": True,
        "unpricedModels": [
            {"model": m, "tokens": t} for m, t in report.unpriced_models.items()
        ],
        "unattributedTokens": report.unattributed_tokens,
        "unattributedShare": round(report.unattributed_share, 4),
        "attributionStarts": report.attribution_starts,
        "pricing": {
            "fetched": PRICES_FETCHED,
            "sources": [ANTHROPIC_SOURCE, OPENAI_SOURCE],
            "override": str(report.prices.override_path)
            if report.prices.overridden and report.prices.override_path
            else None,
            "warnings": report.prices.warnings,
        },
    }
    if scan is not None:
        out["scan"] = scan
    return out


# -- text --------------------------------------------------------------------


def fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            v = n / div
            return f"{v:.0f}{suffix}" if v >= 100 else f"{v:.1f}{suffix}"
    return str(n)  # pragma: no cover


def fmt_cost(totals: Totals) -> str:
    if totals.unpriced_tokens and not totals.cost:
        return "n/a"
    s = f"${totals.cost:,.2f}"
    return s + "+" if totals.unpriced_tokens else s


_BY_TITLES = {
    "day": "Day",
    "model": "Model",
    "account": "Account",
    "project": "Project",
    "provider": "Provider",
    "session": "Session",
}


_HEADS = ("Input", "Output", "Cache read", "Cache write", "Est. cost")
_GAP = 2  # columns between two cells
_INDENT = "  "


def fit_label(row: Row, width: int) -> str:
    """``row.label`` cut to ``width`` columns with an ellipsis, keeping its
    ``head`` and ``tail`` whole when there is room for anything between."""
    label = row.label
    if len(label) <= width:
        return label
    head, tail = row.head, row.tail
    middle = label[len(head): len(label) - len(tail)]
    room = width - len(head) - len(tail)
    if room < 2:
        return fit(label, width)
    if row.cut == "start":
        kept = middle[-(room - 1):]
        # Start at a directory boundary when one is in reach: "…/checkout-api".
        cut = min((i for i in (kept.find("/"), kept.find("\\")) if i > 0), default=-1)
        if 0 < cut < len(kept) - 1:
            kept = kept[cut:]
        return head + "…" + kept + tail
    return head + fit(middle, room) + tail


def _cells(t: Totals) -> tuple[str, ...]:
    return (
        fmt_tokens(t.input), fmt_tokens(t.output), fmt_tokens(t.cache_read),
        fmt_tokens(t.cache_write), fmt_cost(t),
    )


def _span_words(report: Report) -> str:
    """``on 2026-11-10`` / ``between 2026-11-04 and 2026-11-10``."""
    if report.since == report.until:
        return f"on {report.since.isoformat()}"
    return f"between {report.since.isoformat()} and {report.until.isoformat()}"


def _wider_days(report: Report) -> int | None:
    """A ``--days`` worth suggesting after an empty range."""
    span = (report.until - report.since).days + 1
    return next((n for n in (30, 90, 365) if n > span), None)


def _or_list(items: tuple[str, ...]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


def empty_lines(report: Report) -> list[str]:
    """Why ``report`` has no usage, and what to do, as sentences (unwrapped)."""
    command = "qswap usage" + (f" --provider {report.provider}" if report.provider else "")
    if report.searched_dirs is not None:
        tools = PROVIDER_LABELS.get(report.provider or "", "Claude Code or Codex")
        return [
            f"No {tools} session files were found in {_or_list(report.searched_dirs)}.",
            f"Use {tools}, then run {command} again.",
        ]
    scope = f"{PROVIDER_LABELS[report.provider]} " if report.provider in PROVIDER_LABELS else ""
    if report.unscanned:
        return [
            f"No {scope}usage {_span_words(report)} in the ledger.",
            "Run the command again without --no-scan to read new session files.",
        ]
    lines = [f"No {scope}usage {_span_words(report)}."]
    days = _wider_days(report)
    if days is not None:
        by = f" --by {report.by}" if report.by != "day" else ""
        lines.append(f"To look further back: {command} --days {days}{by}")
    return lines


def render_text(
    report: Report,
    *,
    notes: list[str] | None = None,
    last_line: str | None = None,
    width: int = WIDTH,
) -> str:
    """The report as text, every line at most ``width`` columns.
    ``last_line`` (such as what the scan left out) ends it, muted. A report
    with no usage is a sentence saying why, and the next step."""
    from ai_quota_swap.printer import bolded, dimmed, muted

    def wrapped(text: str) -> list[str]:
        out: list[str] = []
        for s in wrap(text, width):
            # A word longer than the line (a deep path) is broken rather
            # than let through.
            out.extend([s] if len(s) <= width else textwrap.wrap(s, width))
        return out

    lines: list[str] = []
    if report.empty:
        for text in empty_lines(report):
            lines.extend(wrapped(text))
        tail = [f"Pricing override: {w}" for w in report.prices.warnings] + list(notes or [])
        for text in tail:
            lines.extend(dimmed(s) for s in wrapped(text))
        if last_line:
            lines.extend(muted(s) for s in wrapped(last_line))
        return "\n".join(lines)

    span = (
        report.since.isoformat()
        if report.since == report.until
        else f"{report.since.isoformat()} to {report.until.isoformat()}"
    )
    who = PROVIDER_LABELS.get(report.provider or "", "Claude Code and Codex")
    lines.extend(bolded(s) for s in wrapped(f"Token usage, {span}, {who}, by {report.by}"))
    lines.append("")
    rows = report.rows
    hidden = 0
    if report.by != "day" and len(rows) > _TEXT_ROW_LIMIT:
        hidden = len(rows) - _TEXT_ROW_LIMIT
        rows = rows[:_TEXT_ROW_LIMIT]
    body = [(r, _cells(r.totals)) for r in rows]
    total_cells = _cells(report.total)
    widths = [
        _GAP + max([len(_HEADS[i]), len(total_cells[i])] + [len(c[i]) for _r, c in body])
        for i in range(len(_HEADS))
    ]
    # The session view's label is two columns: the id and the project.
    title = "Session   Project" if report.by == "session" else _BY_TITLES[report.by]
    room = width - len(_INDENT) - sum(widths)
    label_width = min(max(len(s) for s in [r.label for r in rows] + ["Total", title]), room)

    def line(label: str, cells: tuple[str, ...]) -> str:
        return (
            _INDENT + label.ljust(label_width)
            + "".join(c.rjust(w) for c, w in zip(cells, widths))
        )

    lines.append(dimmed(line(fit(title, label_width), _HEADS)))
    for r, cells in body:
        text = line(fit_label(r, label_width), cells)
        lines.append(muted(text) if not r.totals.tokens else text)
    if hidden:
        lines.append(dimmed(f"{_INDENT}… {hidden} more (--json lists all)"))
    lines.append(bolded(line("Total", total_cells)))
    lines.append("")
    footer = [
        f"Costs are estimates at API list prices (Anthropic and OpenAI pricing pages, "
        f"{PRICES_FETCHED}). Subscription usage is not billed this way."
    ]
    if report.unpriced_models:
        names = ", ".join(
            f"{m} ({fmt_tokens(n)} tokens)" for m, n in report.unpriced_models.items()
        )
        footer.append(f"No price for: {names}. '+' marks a cost that leaves them out.")
    if report.by == "account" or report.unattributed_share >= 0.005:
        if report.unattributed_tokens:
            footer.append(
                f"Unattributed: {report.unattributed_share:.0%} of tokens, used before "
                "qswap recorded which account was live (see docs/usage.md)."
            )
    for w in report.prices.warnings:
        footer.append(f"Pricing override: {w}")
    footer.extend(notes or [])
    for text in footer:
        lines.extend(dimmed(s) for s in wrapped(text))
    if last_line:
        lines.extend(muted(s) for s in wrapped(last_line))
    return "\n".join(lines)
