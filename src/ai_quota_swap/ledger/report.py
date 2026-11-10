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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

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

    @property
    def unattributed_share(self) -> float:
        return self.unattributed_tokens / self.total.tokens if self.total.tokens else 0.0


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
        self.by_key: dict[str, dict] = {}
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
                    key,
                    {
                        "number": int(num) if str(num).isdigit() else None,
                        "email": acct["email"],
                        "alias": acct.get("alias") or "",
                        "provider": provider,
                    },
                )
                if provider == "claude" and acct.get("uuid"):
                    self.by_owner[f"{acct['uuid']}|{org}"] = key
        self.former_numbers: dict[str, int] = {}
        for log in logs.values():
            self.former_numbers.update(log.numbers())

    def owner_key(self, owner: str | None) -> str | None:
        return self.by_owner.get(owner) if owner else None

    def describe(self, key: str) -> tuple[str, dict | None]:
        if key == UNATTRIBUTED:
            return "unattributed", None
        if key == DESKTOP_ACCOUNT:
            return "Claude desktop app (its own login)", None
        email, _, _org = key.partition("|")
        info = self.by_key.get(key)
        if info is not None:
            num, alias = info["number"], info["alias"]
            label = f"Account-{num}" if num is not None else "Account"
            label += f" {alias} ({email})" if alias else f" ({email})"
            return label, {"number": num, "email": email}
        num = self.former_numbers.get(key)
        info = {"number": None, "email": email}
        if num is not None:
            info["formerNumber"] = num
        return f"{email} (not managed by qswap)", info


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
    for prov, hour, account, model, project, session, *toks in raw:
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
            gkey = account
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
    for row in rows:
        _label(row, by, accounts)
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


def _short_path(path: str) -> str:
    if not path:
        return "(no project)"
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _label(row: Row, by: str, accounts: Accounts) -> None:
    if by == "day":
        row.label = row.key.strftime("%a %Y-%m-%d")  # type: ignore[union-attr]
    elif by == "model":
        model = str(row.key)
        row.label = (
            model[: -len(FAST_SUFFIX)] + " (fast)" if model.endswith(FAST_SUFFIX) else model
        )
    elif by == "account":
        row.label, info = accounts.describe(str(row.key))
        row.extra["account"] = info
    elif by == "project":
        row.label = _short_path(str(row.key))
    elif by == "provider":
        row.label = PROVIDER_LABELS.get(str(row.key), str(row.key))
    else:
        prov, session = row.key  # type: ignore[misc]
        row.extra["provider"] = prov
        row.label = f"{session[:8] or '(none)'}  {_short_path(row.extra.get('project', ''))}"


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
        entry = {"key": key, "label": row.label, **row.totals.to_json()}
        if report.by == "account":
            entry["key"] = row.key or None
            entry["account"] = row.extra.get("account")
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


def render_text(report: Report, *, notes: list[str] | None = None) -> str:
    from ai_quota_swap.printer import bolded, dimmed, muted

    lines: list[str] = []
    span = (
        report.since.isoformat()
        if report.since == report.until
        else f"{report.since.isoformat()} to {report.until.isoformat()}"
    )
    who = PROVIDER_LABELS.get(report.provider or "", "Claude Code and Codex")
    lines.append(bolded(f"Token usage, {span}, {who}, by {report.by}"))
    lines.append("")
    rows = report.rows
    hidden = 0
    if report.by != "day" and len(rows) > _TEXT_ROW_LIMIT:
        hidden = len(rows) - _TEXT_ROW_LIMIT
        rows = rows[:_TEXT_ROW_LIMIT]
    labels = [r.label for r in rows] + ["Total", _BY_TITLES[report.by]]
    width = min(max(len(s) for s in labels), 56)
    heads = ("Input", "Output", "Cache read", "Cache write", "Est. cost")
    widths = (9, 9, 12, 13, 12)

    def line(label: str, cells: tuple[str, ...]) -> str:
        if len(label) > width:
            label = "…" + label[-(width - 1):]
        return "  " + label.ljust(width) + "".join(c.rjust(w) for c, w in zip(cells, widths))

    lines.append(dimmed(line(_BY_TITLES[report.by], heads)))
    for r in rows:
        t = r.totals
        cells = (
            fmt_tokens(t.input), fmt_tokens(t.output), fmt_tokens(t.cache_read),
            fmt_tokens(t.cache_write), fmt_cost(t),
        )
        text = line(r.label, cells)
        lines.append(muted(text) if not t.tokens else text)
    if hidden:
        lines.append(dimmed(f"  … {hidden} more (--json lists all)"))
    t = report.total
    lines.append(
        bolded(
            line(
                "Total",
                (
                    fmt_tokens(t.input), fmt_tokens(t.output), fmt_tokens(t.cache_read),
                    fmt_tokens(t.cache_write), fmt_cost(t),
                ),
            )
        )
    )
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
    lines.extend(dimmed(f) for f in footer)
    return "\n".join(lines)
