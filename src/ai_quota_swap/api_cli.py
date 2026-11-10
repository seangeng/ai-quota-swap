"""``qswap api org ...``, ``qswap budget ...`` and the ``usage --billed`` section.

See docs/api-accounts.md for what each command is for, and
docs/design/api-accounts.md for the decisions behind them.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_quota_swap import admin_api, api_spend
from ai_quota_swap.admin_api import AdminApiError, AdminCache
from ai_quota_swap.admin_orgs import (
    ENV_VAR,
    STORAGE_ENV,
    STORAGE_FILE,
    STORAGE_KEYCHAIN,
    OrgError,
    OrgRegistry,
    default_org_name,
    looks_like_admin_key,
    normalize_org_name,
)
from ai_quota_swap.exceptions import ProviderError
from ai_quota_swap.printer import accent, bolded, dimmed, error, muted, warning

ADMIN_KEY_WARNING = (
    "An Admin key can manage your whole organization: members, invites, workspaces "
    "and API keys (it can rename or archive keys). Anthropic offers no read-only "
    "key for usage reports. qswap sends it only with GET requests that read the "
    "key list and the usage and cost reports. Use a dedicated key with an expiry "
    "(Console > Settings > Admin keys), and remove it here when you no longer need it."
)
NOT_A_CAP = (
    "A budget is a brake inside qswap, not a cap at Anthropic: auto-switch stops "
    "using the account at its budget. For a hard cap, set a workspace spend limit "
    "in the Console."
)
_STORAGE_WORDS = {
    STORAGE_KEYCHAIN: "the macOS Keychain",
    STORAGE_FILE: "a 0600 file in the data directory",
    STORAGE_ENV: f"{ENV_VAR}, not stored",
}


def _cli():
    from ai_quota_swap import cli

    return cli


def _fail(message: str, as_json: bool = False, kind: str = "Error") -> None:
    if as_json:
        from ai_quota_swap.json_output import SCHEMA_VERSION

        print(json.dumps(
            {"schemaVersion": SCHEMA_VERSION, "error": {"type": kind, "message": message}},
            indent=2,
        ))
    else:
        error(f"Error: {message}")
    sys.exit(1)


def _api_key_infos(switcher) -> dict[str, tuple]:
    """``{num: account info}`` for the managed API-key accounts."""
    return {
        str(info[0]): info
        for info in switcher._build_accounts_info()
        if switcher._account_kind(str(info[0])) == "api_key"
    }


def _label(num: str, email: str) -> str:
    from ai_quota_swap.labels import account_label

    return account_label(num, email=email)


# -- qswap api ------------------------------------------------------------------


def api_command(argv: list[str]) -> None:
    """Handle ``qswap api org add [NAME] | list | remove NAME``."""
    prog = _cli()._prog_name()
    from ai_quota_swap.argparsing import ArgumentParser

    parser = ArgumentParser(
        prog=f"{prog} api",
        description=(
            "Register an organization's Admin API key, so API-key accounts show "
            "what Anthropic recorded for their key and usage --billed shows the "
            "organization's billed cost."
        ),
        epilog=f"""examples:
  {prog} api org add               # paste the key at the hidden prompt
  {prog} api org add acme < key.txt
  {prog} api org list

The key is read from a hidden prompt or stdin, never from an argument, and
kept in the macOS Keychain (elsewhere, a private file in the data directory).
See docs/api-accounts.md.
""",
    )
    groups = parser.add_subparsers(dest="group", metavar="org")
    org = groups.add_parser("org", help="Registered organizations")
    sub = org.add_subparsers(dest="action", metavar="{add,list,remove}")
    p_add = sub.add_parser("add", help="Register an organization's Admin key")
    p_add.add_argument("name", nargs="?", metavar="NAME",
                       help="Name to register it under (default: from the organization's name)")
    p_add.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    p_list = sub.add_parser("list", help="List registered organizations")
    p_list.add_argument("--json", action="store_true", help="Machine-readable output")
    p_list.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    p_rm = sub.add_parser("remove", help="Unregister an organization and delete its key")
    p_rm.add_argument("name", metavar="NAME")
    p_rm.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.group != "org" or not args.action:
        parser.print_help(sys.stderr)
        sys.exit(64)

    try:
        switcher = _cli()._switcher(debug=getattr(args, "debug", False))
        _cli()._guard_root(switcher)
        if args.action == "add":
            _org_add(switcher, args.name)
        elif args.action == "list":
            _org_list(switcher, as_json=args.json)
        else:
            _org_remove(switcher, args.name)
    except OrgError as e:
        _fail(str(e), getattr(args, "json", False), "OrgError")
    except ProviderError as e:
        _fail(str(e), getattr(args, "json", False), type(e).__name__)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def read_admin_key() -> str:
    """The key from a hidden prompt on a terminal, else the first line of stdin."""
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass("Admin API key (input hidden): ").strip()
    line = sys.stdin.readline() if sys.stdin is not None else ""
    return line.strip()


def _org_add(switcher, name: str | None) -> None:
    registry = OrgRegistry(switcher.backup_dir, platform=switcher.platform)
    wanted = normalize_org_name(name) if name else None
    if wanted and registry.get(wanted) is not None:
        raise OrgError(f"'{wanted}' is already registered; remove it first (qswap api org remove {wanted})")
    print(dimmed(ADMIN_KEY_WARNING), file=sys.stderr)
    key = read_admin_key()
    if not key:
        raise OrgError("no key was given; nothing was saved")
    if not looks_like_admin_key(key):
        raise OrgError(
            "that is not an Admin API key (they start with sk-ant-admin); nothing was "
            "saved. Create one in the Console under Settings > Admin keys"
        )
    try:
        info = admin_api.organization_info(key)
    except AdminApiError as e:
        if e.kind == "auth":
            raise OrgError("Anthropic rejected the key (HTTP 401); nothing was saved") from None
        if e.kind == "forbidden":
            raise OrgError("the key may not read organization info (HTTP 403); nothing was saved") from None
        raise OrgError(f"could not check the key: {e.note} ({e}); nothing was saved") from None
    target = wanted or default_org_name(info["name"])
    org = registry.add(target, key, info["id"], info["name"])
    del key
    cache = AdminCache(switcher.backup_dir)
    cache.forget(org.name)
    shown = f" ({org.organization_name})" if org.organization_name else ""
    print(f"{accent('Registered')} organization '{org.name}'{shown}.")
    print(dimmed(f"  The Admin key is stored in {_STORAGE_WORDS.get(org.storage, org.storage)}."))
    _print_links(switcher, registry, force=True)


def _print_links(switcher, registry: OrgRegistry, *, force: bool) -> None:
    infos = _api_key_infos(switcher)
    if not infos:
        print(dimmed("  No API-key accounts are managed yet (qswap add-token sk-ant-api03-...)."))
        return
    orgs = registry.active_orgs()
    data = switcher._get_sequence_data() or {}
    keys = {num: (info[5] or "").strip() for num, info in infos.items()}
    result = api_spend.resolve_links(
        orgs, keys, data.get("accounts", {}), now=time.time(), network=True,
        cache=AdminCache(switcher.backup_dir), force=force,
    )
    persistent = {o.name for o in orgs if o.persistent}
    desired = {}
    for num in keys:
        link = result.links.get(num)
        desired[num] = link if link is not None and link.org in persistent else None
    api_spend.persist_links(switcher, desired)
    for num, info in infos.items():
        link = result.links.get(num)
        if link is not None:
            name = f'"{link.key_name}" ' if link.key_name else ""
            print(f"  Linked {_label(num, info[1])} to API key {name}({link.hint}) in '{link.org}'.")
        else:
            why = result.notes.get(num, "not linked")
            print(muted(f"  {_label(num, info[1])}: {why}; its spend is estimated locally."))


def _org_list(switcher, *, as_json: bool) -> None:
    from ai_quota_swap.json_output import SCHEMA_VERSION
    import os

    registry = OrgRegistry(switcher.backup_dir, platform=switcher.platform)
    orgs = registry.orgs()
    data = switcher._get_sequence_data() or {}
    linked: dict[str, list[str]] = {}
    for num, rec in (data.get("accounts") or {}).items():
        if isinstance(rec, dict) and isinstance(rec.get("apiOrg"), str) and rec.get("apiKeyId"):
            linked.setdefault(rec["apiOrg"], []).append(str(num))
    env_set = looks_like_admin_key(os.environ.get(ENV_VAR))
    if as_json:
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "organizations": [
                {
                    "name": o.name,
                    "organizationName": o.organization_name,
                    "organizationId": o.organization_id,
                    "storage": o.storage,
                    "added": o.added,
                    "linkedAccounts": sorted(int(n) for n in linked.get(o.name, []) if n.isdigit()),
                }
                for o in orgs
            ],
            "environmentKey": env_set,
        }, indent=2))
        return
    if not orgs:
        print(dimmed("No organization registered. Register one with: qswap api org add"))
        if env_set:
            print(dimmed(f"{ENV_VAR} is set: commands use it without storing it."))
        return
    print(bolded("Organizations (Admin API):"))
    for o in orgs:
        accounts = ", ".join(f"#{n}" for n in sorted(linked.get(o.name, []), key=int))
        tail = f"   linked: {accounts}" if accounts else "   no linked accounts"
        print(f"  {o.name}  {muted(o.organization_name or '')}  "
              f"{dimmed('key in ' + _STORAGE_WORDS.get(o.storage, o.storage))}{dimmed(tail)}")
    if env_set:
        print(dimmed(f"{ENV_VAR} is set but not used while organizations are registered."))


def _org_remove(switcher, name: str) -> None:
    registry = OrgRegistry(switcher.backup_dir, platform=switcher.platform)
    target = (name or "").strip().lower()
    known, gone = registry.remove(target)
    if not known:
        if gone:
            raise OrgError(f"no organization named '{target}' is registered")
        raise OrgError(f"no organization named '{target}' is registered, and a stored key could not be deleted")
    nums = api_spend.unlink_org(switcher, target)
    AdminCache(switcher.backup_dir).forget(target)
    print(f"{accent('Removed')} organization '{target}'.")
    if gone:
        print(dimmed("  Its stored Admin key is deleted."))
    else:
        warning(
            "  The stored Admin key could not be deleted from the Keychain; delete the "
            f"item 'ai-quota-swap' / 'admin-org-{target}' in Keychain Access."
        )
    if nums:
        print(dimmed(f"  Unlinked: {', '.join('#' + n for n in sorted(nums, key=int))}."))


# -- qswap budget ---------------------------------------------------------------


def _budget_line(reading: dict | None, budget: float | None) -> str:
    from ai_quota_swap.display import API_KEY_NO_USAGE

    if reading is None:
        reading = {"spend": {"used": None, "limit": budget}, "source": None}
    if api_spend.nothing_recorded(reading):
        # qswap list's words for the same state.
        if budget is None:
            return f"no budget · {API_KEY_NO_USAGE}"
        return f"$0 of {api_spend.budget_money(budget)} this month · {API_KEY_NO_USAGE}"
    spend = reading.get("spend") or {}
    used, pct = spend.get("used"), spend.get("pct")
    estimate = " (estimate)" if reading.get("source") == api_spend.SOURCE_LEDGER else ""
    if budget is None:
        if used is None:
            return "no budget · no spend data yet"
        return f"no budget · {api_spend.money(used)} this month{estimate}"
    if used is None:
        return f"budget {api_spend.budget_money(budget)} · no spend data yet"
    over = ", over budget" if pct is not None and pct >= 100 else ""
    line = (
        f"{api_spend.money(used)} of {api_spend.budget_money(budget)} this month "
        f"({pct:.0f}%{over}){estimate}"
    )
    today = reading.get("today")
    if today is not None:
        line += f" · {api_spend.money(today)} today"
    return line


def budget_command(argv: list[str]) -> None:
    """Handle ``qswap budget [ACCOUNT [AMOUNT|off]]``."""
    prog = _cli()._prog_name()
    from ai_quota_swap.argparsing import ArgumentParser

    parser = ArgumentParser(
        prog=f"{prog} budget",
        description=(
            "Set, remove or list a monthly spend budget (US dollars, UTC month) for "
            "an API-key account. At its budget, auto-switch stops using it."
        ),
        epilog=f"""examples:
  {prog} budget 3 500        # $500 a month for #3
  {prog} budget 3 off        # remove it
  {prog} budget              # every API-key account's budget and spend

{NOT_A_CAP}
""",
    )
    parser.add_argument("account", nargs="?", metavar="ACCOUNT",
                        help="an API-key account (number, email or alias); omit to list")
    parser.add_argument("amount", nargs="?", metavar="AMOUNT|off",
                        help="dollars per month, or 'off' to remove the budget")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (docs/json-output.md)")
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    try:
        switcher = _cli()._switcher(debug=args.debug)
        _cli()._guard_root(switcher)
        if args.amount is not None:
            value = None if args.amount.strip().lower() == "off" else args.amount
            num, email, budget, changed = api_spend.set_budget(switcher, args.account, value)
            if args.json:
                from ai_quota_swap.json_output import SCHEMA_VERSION

                print(json.dumps({
                    "schemaVersion": SCHEMA_VERSION, "number": int(num), "email": email,
                    "budget": budget, "changed": changed,
                }, indent=2))
                return
            who = _label(num, email)
            if budget is None:
                print(f"{accent('Removed')} the budget for {who}." if changed
                      else dimmed(f"{who} has no budget."))
            elif changed:
                print(f"{accent('Set')} a monthly budget of {api_spend.budget_money(budget)} for {who}.")
                print(dimmed(f"  {NOT_A_CAP}"))
            else:
                print(dimmed(f"{who} already has a budget of {api_spend.budget_money(budget)}."))
            return
        _budget_list(switcher, args.account, as_json=args.json)
    except (api_spend.BudgetError, ValueError) as e:
        _fail(str(e), args.json, "ValueError")
    except ProviderError as e:
        _fail(str(e), args.json, type(e).__name__)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _budget_list(switcher, account: str | None, *, as_json: bool) -> None:
    from ai_quota_swap.json_output import SCHEMA_VERSION, api_spend_to_json

    data = switcher._get_sequence_data() or {}
    records = data.get("accounts") or {}
    infos = _api_key_infos(switcher)
    if account is not None:
        num, email, _org = switcher.resolve_account(account)
        if num not in infos:
            raise api_spend.BudgetError(
                f"{_label(num, email)} is a subscription account; budgets apply to API-key accounts"
            )
        infos = {num: infos[num]}
    readings = api_spend.readings(switcher, infos) if infos else {}
    rows = []
    for num, info in infos.items():
        budget = api_spend.budget_from_record(records.get(num))
        rows.append((num, info, budget, readings.get(num)))
    if as_json:
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "accounts": [
                {
                    "number": int(num), "email": info[1], "budget": budget,
                    "apiSpend": api_spend_to_json(reading) if reading else None,
                }
                for num, info, budget, reading in rows
            ],
        }, indent=2))
        return
    if not rows:
        print(dimmed(
            "No API-key accounts are managed. Budgets apply to API-key accounts "
            "(qswap add-token sk-ant-api03-...)."
        ))
        return
    print(bolded("Monthly budgets for API-key accounts (UTC month):"))
    from ai_quota_swap.labels import account_label

    for num, info, budget, reading in rows:
        # "alias (email)" or the email; a placeholder by its name alone.
        who = account_label(None, alias=info[6] or None, email=info[1], long=True)
        line = _budget_line(reading, budget)
        paint = warning if reading and api_spend.over_budget(reading) else None
        text = f"  {num}: {who}  {line}"
        if paint is not None:
            paint(text)
        else:
            print(text)
        if reading:
            detail = api_spend.detail_text(reading)
            if detail:
                print(dimmed(f"     {detail}"))
    print(dimmed(NOT_A_CAP))


# -- qswap usage --billed -------------------------------------------------------

BILLED_GROUPINGS = ("day", "model", "key", "workspace")
_TOKEN_KEYS = ("input", "output", "cacheRead", "cacheWrite5m", "cacheWrite1h")


@dataclass
class BilledRow:
    key: object
    label: str
    tokens: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    cost: float | None = None
    cost_source: str | None = None  # "cost-report" | "estimate"


@dataclass
class BilledOrg:
    name: str
    organization_name: str
    organization_id: str
    rows: list[BilledRow] = field(default_factory=list)
    billed_cost: float | None = None
    error: AdminApiError | None = None
    cost_error: AdminApiError | None = None
    fetched_at: float | None = None

    @property
    def total_tokens(self) -> list[int]:
        out = [0, 0, 0, 0, 0]
        for row in self.rows:
            out = [a + b for a, b in zip(out, row.tokens)]
        return out


@dataclass
class BilledResult:
    since: date
    until: date
    starting_at: str
    ending_at: str
    by: str
    orgs: list[BilledOrg]
    notes: list[str]


def billed_grouping(by: str) -> tuple[str, str | None]:
    """``(grouping used, note)`` for a ``--by`` value: the reports group by
    day, model, API key and workspace; ``account`` reads as key."""
    if by in BILLED_GROUPINGS:
        return by, None
    if by == "account":
        return "key", None
    return "day", f"Anthropic's reports cannot group by {by}; the billed section is by day."


def _day_range(since: date, until: date) -> list[str]:
    days = []
    d = since
    while d <= until:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def billed_report(
    root: Path,
    *,
    since: date,
    until: date,
    by: str,
    platform=None,
    registry_data: dict | None = None,
    now: float | None = None,
    env: dict | None = None,
) -> BilledResult:
    """Each active organization's usage and billed cost for ``since`` to
    ``until`` as UTC days. Never raises for API trouble: an organization that
    could not be read carries its error."""
    from ai_quota_swap.ledger.pricing import PriceTable

    now = time.time() if now is None else now
    grouping, note = billed_grouping(by)
    notes = [note] if note else []
    start_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    end_dt = datetime(until.year, until.month, until.day, tzinfo=timezone.utc) + timedelta(days=1)
    tomorrow = admin_api.utc_month_bounds(now)[3]
    if end_dt > tomorrow:
        end_dt = tomorrow
    starting_at, ending_at = admin_api.iso_z(start_dt), admin_api.iso_z(end_dt)
    result = BilledResult(since, until, starting_at, ending_at, grouping, [], notes)
    registry = OrgRegistry(root, platform=platform)
    orgs = registry.active_orgs(env)
    if not orgs:
        notes.append(
            "No organization registered: add one with qswap api org add,\n"
            f"or set {ENV_VAR} for one command."
        )
        return result
    if end_dt <= start_dt:
        notes.append("The window starts after today (UTC); Anthropic has nothing to report yet.")
        return result
    if end_dt >= tomorrow:
        notes.append("Usage from the last few minutes may not be in Anthropic's reports yet.")
    cache = AdminCache(root)
    base = [
        ("starting_at", starting_at), ("ending_at", ending_at), ("bucket_width", "1d"),
    ]
    usage_groups = {
        "day": [], "model": ["model"], "key": ["api_key_id", "model", "speed"],
        "workspace": ["workspace_id"],
    }[grouping]
    cost_groups = {"day": [], "model": ["description"], "key": [], "workspace": ["workspace_id"]}[grouping]
    limit = [("limit", str(admin_api.DAY_BUCKET_LIMIT))]
    prices = PriceTable.load(root) if grouping == "key" else None
    records = (registry_data or {}).get("accounts") or {}
    for org in orgs:
        out = BilledOrg(org.name, org.organization_name, org.organization_id)
        result.orgs.append(out)
        usage = admin_api.cached_report(
            org, admin_api.USAGE_REPORT_PATH,
            base + [("group_by[]", g) for g in usage_groups] + limit,
            now=now, network=True, cache=cache,
        )
        if usage.buckets is None:
            out.error = usage.error or AdminApiError("not-read")
            continue
        out.fetched_at = usage.fetched_at
        cost = admin_api.cached_report(
            org, admin_api.COST_REPORT_PATH,
            base + [("group_by[]", g) for g in cost_groups] + limit,
            now=now, network=True, cache=cache,
        )
        try:
            rows = _usage_rows(usage.buckets, grouping, prices, since, until)
            if cost.buckets is not None:
                out.billed_cost = _apply_costs(cost.buckets, grouping, rows)
            else:
                out.cost_error = cost.error or AdminApiError("not-read")
        except AdminApiError as e:
            out.error = e
            continue
        if grouping == "key":
            _label_keys(org, rows, records, now=now, cache=cache)
        out.rows = _ordered(rows, grouping)
    return result


def _usage_rows(buckets, grouping, prices, since, until) -> dict:
    from ai_quota_swap.ledger.pricing import FAST_SUFFIX

    rows: dict[object, BilledRow] = {}
    if grouping == "day":
        for day in _day_range(since, until):
            rows[day] = BilledRow(day, day)
    for bucket in buckets:
        day = admin_api.bucket_day(bucket)
        for res in admin_api.bucket_results(bucket):
            toks = admin_api.result_tokens(res)
            if grouping == "day":
                key, label = day, day
            elif grouping == "model":
                key = res.get("model") or ""
                label = key or "(no model)"
            elif grouping == "workspace":
                key = res.get("workspace_id") or ""
                label = key or "default workspace"
            else:
                key = res.get("api_key_id") or ""
                label = key or "(no API key: Console usage)"
            row = rows.get(key)
            if row is None:
                row = rows[key] = BilledRow(key, label)
            row.tokens = [a + b for a, b in zip(row.tokens, toks)]
            if grouping == "key" and prices is not None:
                model = res.get("model") or ""
                found = prices.lookup(model + FAST_SUFFIX if res.get("speed") == "fast" else model)
                if found is not None:
                    row.cost = (row.cost or 0.0) + found[1].cost(*toks)
                    row.cost_source = "estimate"
    return rows


def _apply_costs(buckets, grouping, rows: dict) -> float:
    """Add the cost report's amounts to the rows it can split; the total."""
    total = 0.0
    for bucket in buckets:
        day = admin_api.bucket_day(bucket)
        for res in admin_api.bucket_results(bucket):
            dollars = admin_api.amount_usd(res)
            total += dollars
            if grouping == "key":
                continue
            if grouping == "day":
                key, label = day, day
            elif grouping == "model":
                model = res.get("model")
                if isinstance(model, str) and model:
                    key, label = model, model
                else:
                    kind = res.get("cost_type") or "other"
                    key = f"({str(kind).replace('_', ' ')})"
                    label = key
            else:
                key = res.get("workspace_id") or ""
                label = key or "default workspace"
            row = rows.get(key)
            if row is None:
                row = rows[key] = BilledRow(key, label)
            row.cost = (row.cost or 0.0) + dollars
            row.cost_source = "cost-report"
    return round(total, 6)


def _label_keys(org, rows: dict, records: dict, *, now: float, cache: AdminCache) -> None:
    listing = admin_api.api_key_list(org, now=now, network=True, cache=cache)
    names = {k.get("id"): k.get("name") for k in (listing.keys or []) if isinstance(k, dict)}
    accounts = {
        rec.get("apiKeyId"): num for num, rec in records.items()
        if isinstance(rec, dict) and rec.get("apiKeyId") and rec.get("apiOrg") == org.name
    }
    for key, row in rows.items():
        if not key:
            continue
        name = names.get(key) or key
        num = accounts.get(key)
        row.label = f"{name} (#{num})" if num else name


def _ordered(rows: dict, grouping: str) -> list[BilledRow]:
    if grouping == "day":
        return sorted(rows.values(), key=lambda r: str(r.key))
    return sorted(rows.values(), key=lambda r: (-(r.cost or 0.0), -sum(r.tokens), str(r.key)))


def org_error_line(org: BilledOrg) -> str:
    """One line for an organization that could not be read."""
    e = org.error
    if e is None:
        return ""
    if e.kind == "auth":
        fix = (f"set {ENV_VAR} to a valid Admin key" if org.name == "env" else
               f"replace it: qswap api org remove {org.name}, then qswap api org add {org.name}")
        return f"{org.name}: Anthropic rejected the Admin key (HTTP 401); {fix}."
    if e.kind == "forbidden":
        return f"{org.name}: the Admin key may not read usage reports (HTTP 403)."
    if e.kind == "rate-limited":
        return f"{org.name}: the Admin API is rate limiting requests; try again in a few minutes."
    return f"{org.name}: could not read Anthropic's reports: {e.note}."


def billed_to_json(result: BilledResult) -> dict:
    orgs = []
    for org in result.orgs:
        entry: dict = {
            "name": org.name,
            "organizationName": org.organization_name,
            "organizationId": org.organization_id or None,
            "rows": [
                {
                    "key": row.key if row.key != "" else None,
                    "label": row.label,
                    **dict(zip(_TOKEN_KEYS, row.tokens)),
                    "costUsd": None if row.cost is None else round(row.cost, 4),
                    "costSource": row.cost_source,
                }
                for row in org.rows
            ],
            "total": {
                **dict(zip(_TOKEN_KEYS, org.total_tokens)),
                "billedCostUsd": None if org.billed_cost is None else round(org.billed_cost, 4),
            },
            "error": None if org.error is None else {
                "type": org.error.kind, "message": org_error_line(org),
            },
        }
        if org.cost_error is not None:
            entry["costError"] = {"type": org.cost_error.kind, "message": org.cost_error.note}
        if org.fetched_at is not None:
            entry["fetchedAt"] = admin_api.iso_z(datetime.fromtimestamp(org.fetched_at, tz=timezone.utc))
        orgs.append(entry)
    return {
        "since": result.since.isoformat(),
        "until": result.until.isoformat(),
        "timezone": "UTC",
        "startingAt": result.starting_at,
        "endingAt": result.ending_at,
        "by": result.by,
        "organizations": orgs,
        "notes": result.notes,
    }


def render_billed(result: BilledResult) -> str:
    from ai_quota_swap.ledger.report import fmt_tokens

    lines: list[str] = []
    span = (result.since.isoformat() if result.since == result.until
            else f"{result.since.isoformat()} to {result.until.isoformat()}")
    lines.append(bolded(f"Billed by Anthropic, {span} (UTC days), by {result.by}"))
    if not result.orgs:
        lines.extend(dimmed(n) for n in result.notes)
        return "\n".join(lines)
    heads = ("Input", "Output", "Cache read", "Cache write",
             "Est. cost" if result.by == "key" else "Cost")
    widths = (9, 9, 12, 13, 12)
    title = {"day": "Day", "model": "Model", "key": "API key", "workspace": "Workspace"}[result.by]
    for org in result.orgs:
        lines.append("")
        shown = f" ({org.organization_name})" if org.organization_name else ""
        lines.append(f"{org.name}{shown}")
        if org.error is not None:
            lines.append(muted(f"  {org_error_line(org)}"))
            continue
        if not any(sum(r.tokens) or r.cost for r in org.rows):
            lines.append(muted("  No usage reported for this window yet (Anthropic's reports lag by a few minutes)."))
            continue
        labels = [r.label for r in org.rows] + ["Total", title]
        width = min(max(len(s) for s in labels), 48)

        def line(label: str, cells: tuple[str, ...]) -> str:
            if len(label) > width:
                label = "…" + label[-(width - 1):]
            return "  " + label.ljust(width) + "".join(c.rjust(w) for c, w in zip(cells, widths))

        def cost_cell(value: float | None) -> str:
            return "n/a" if value is None else api_spend.money(value)

        lines.append(dimmed(line(title, heads)))
        for row in org.rows:
            t = row.tokens
            lines.append(line(row.label, (
                fmt_tokens(t[0]), fmt_tokens(t[1]), fmt_tokens(t[2]),
                fmt_tokens(t[3] + t[4]), cost_cell(row.cost),
            )))
        t = org.total_tokens
        lines.append(bolded(line("Total", (
            fmt_tokens(t[0]), fmt_tokens(t[1]), fmt_tokens(t[2]),
            fmt_tokens(t[3] + t[4]), cost_cell(org.billed_cost),
        ))))
        if result.by == "key":
            lines.append(dimmed(
                "  Per-key cost is estimated from tokens at list prices (the cost report has "
                "no per-key split); the total is Anthropic's cost report."
            ))
        if org.cost_error is not None:
            lines.append(muted(f"  Cost report not read: {org.cost_error.note}."))
    lines.append("")
    footer = ["Costs are Anthropic's cost report, in US dollars; tokens are its usage report."]
    footer.extend(result.notes)
    lines.extend(dimmed(f) for f in footer)
    return "\n".join(lines)
