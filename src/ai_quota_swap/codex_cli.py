"""``qswap codex <verb>``: the Codex provider's command line, and its section
in the unified ``qswap list``.

The Claude handlers in ``cli.py`` are mostly not reused: their help, examples
and follow-up hints name Claude-only commands (``run``, ``map``, ``qswap
auto``), and the main parser carries Claude-only flags. These handlers keep the
Claude wording and layout wherever the behaviour is the same, and the same
``--json`` envelope and ``schemaVersion`` wherever the Claude equivalent has
``--json`` (``list``, ``status``, ``switch``). ``auto`` and ``reset`` are the
exception: they run ``cli``'s own handlers under the codex provider, which
branch on it only for wording, so the engine host, the reset bookkeeping and
the output modes stay one implementation.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from ai_quota_swap import providers
from ai_quota_swap.exceptions import FeatureUnavailableError, ProviderError, ValidationError
from ai_quota_swap.json_output import error_envelope
from ai_quota_swap.models import (
    ACCOUNT_PRIORITY_MAX,
    ACCOUNT_THRESHOLD_MAX,
    ACCOUNT_THRESHOLD_MIN,
)
from ai_quota_swap.paths import get_codex_data_dir
from ai_quota_swap.printer import accent, bolded, dimmed, error, say, warning

PROG = "qswap codex"

#: Verbs of the next milestones: refused with "not available yet".
NOT_YET = ("run",)

LOGOUT_WARNING = (
    "Never run `codex logout` on a login qswap has saved: it signs the login\n"
    "out at OpenAI, and every saved copy of it stops working."
)

SECOND_ACCOUNT_STEPS = """\
Adding another account:
  qswap codex login
  It logs the account in under a private, temporary Codex home, saves it,
  and deletes that home; your current Codex login stays signed in. (Plain
  `codex login` in your normal Codex home signs the current login out at
  OpenAI first, which kills qswap's saved copy of it too.)

  By hand, if qswap codex login cannot run codex:
    tmp="$(mktemp -d)"
    CODEX_HOME="$tmp" codex login        # log in with the other account
    CODEX_HOME="$tmp" qswap codex add
    rm -rf "$tmp"                        # deleting the file does not sign out
"""

HELP = f"""\
usage: {PROG} <command> [options]

Switch between Codex (ChatGPT) logins, by hand or automatically. Experimental.

Accounts
  list                  every Codex account and its usage
  status                the live Codex login
  switch [ACCOUNT]      switch to an account, or to the next one
  add                   save the current Codex login as an account
  login                 log in another account and save it
  remove ACCOUNT        stop managing an account
  alias ACCOUNT NAME    give an account a short name
  disable, enable       hold an account out of rotation, or bring it back

Automatic switching and resets
  auto                  switch when the active account runs out
  threshold, priority   per-account switch line and order
  reset [ACCOUNT]       list reset credits, or spend one

Options
  -h, --help            this map ({PROG} help COMMAND for one command)
  --json                with list, status, switch, reset, auto, priority

ACCOUNT is a number (2), an email, or an alias. Aliases: ls=list, rm=remove.
Usage and cost: qswap usage --provider codex.

{SECOND_ACCOUNT_STEPS}
{LOGOUT_WARNING}
"""

#: Bare `qswap codex`.
BARE_HINT = f"""\
{PROG} manages several Codex (ChatGPT) logins.
  {PROG} help    the commands
  {PROG} list    your Codex accounts and their usage
"""


def _switcher(debug: bool = False):
    return providers.make_switcher("codex", debug=debug)


def _wants_json(argv: list[str]) -> bool:
    return "--json" in argv


def _fail(exc: ProviderError, as_json: bool) -> None:
    if as_json:
        print(json.dumps(error_envelope(exc), indent=2))
    else:
        error(f"Error: {exc}")
    sys.exit(1)


def _run(args: argparse.Namespace, action: Callable, *, as_json: bool = False) -> None:
    """Build the switcher, refuse a keyring-mode Codex home, run ``action``,
    and print its JSON payload; provider errors are one line (or the JSON
    error envelope) and exit 1, like every Claude command."""
    from ai_quota_swap.cli import _guard_root

    payload = None
    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        switcher.check_storage()
        payload = action(switcher)
    except ProviderError as e:
        _fail(e, as_json)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}", file=sys.stderr if as_json else sys.stdout)
        sys.exit(130)
    if as_json and payload is not None:
        print(json.dumps(payload, indent=2))


def _parser(verb: str, description: str, epilog: str | None = None) -> argparse.ArgumentParser:
    from ai_quota_swap.argparsing import ArgumentParser

    parser = ArgumentParser(
        prog=f"{PROG} {verb}",
        description=description,
        epilog=epilog,
    )
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    return parser


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true",
        help="machine-readable output (docs/json-output.md)",
    )


# -- verbs ------------------------------------------------------------------------


def _add(argv: list[str]) -> None:
    parser = _parser(
        "add",
        "Save the login Codex is using now ($CODEX_HOME/auth.json) as a managed "
        "account; for one already saved, refresh its saved copy. To add another "
        "account, use qswap codex login.",
        epilog=f"""examples:
  qswap codex add
  qswap codex add --alias work

{LOGOUT_WARNING}
""",
    )
    parser.add_argument("--slot", type=int, metavar="NUM", help="store it as this account number")
    parser.add_argument("--alias", metavar="NAME", help="give it a short name")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="replace the account already in --slot without asking")
    args = parser.parse_args(argv)
    _run(args, lambda s: s.add_account(slot=args.slot, alias=args.alias, assume_yes=args.yes))


def _list(argv: list[str]) -> None:
    parser = _parser(
        "list", "List the managed Codex accounts with their usage. Read-only.",
        epilog="""examples:
  qswap codex list
  qswap codex list --json
""",
    )
    _json_flag(parser)
    args = parser.parse_args(argv)
    _run(args, lambda s: s.list_accounts(json_output=args.json), as_json=args.json)


def _status(argv: list[str]) -> None:
    parser = _parser(
        "status", "Show the live Codex login, its usage, and whether Codex "
        "auto-switch is running. Read-only.",
        epilog="""examples:
  qswap codex status
  qswap codex status --json
""",
    )
    _json_flag(parser)
    args = parser.parse_args(argv)

    def action(s):
        payload = s.status(json_output=args.json)
        if args.json:
            return payload
        from ai_quota_swap.cli import _auto_switch_line, _auto_switch_state

        count = s.managed_count()
        if count:
            print()
            print(dimmed(f"{count} Codex account{'' if count == 1 else 's'} "
                         f"{'is' if count == 1 else 'are'} managed."))
            state = _auto_switch_state()
            if "codex" not in state["providers"]:
                state = {**state, "running": False, "providers": []}
            else:
                state = {**state, "providers": ["codex"]}
            line = _auto_switch_line(state)
            print(line.replace("Start it with: qswap auto", "Start it with: qswap codex auto"))
        return None

    _run(args, action, as_json=args.json)


def _switch(argv: list[str]) -> None:
    parser = _parser(
        "switch",
        "Make a saved Codex login the live one; without ACCOUNT, the next one. "
        "Running codex sessions keep the old account until they exit.",
        epilog="""examples:
  qswap codex switch 2
  qswap codex switch
  qswap codex switch work --json
""",
    )
    parser.add_argument("account", nargs="?", metavar="ACCOUNT",
                        help="number, email or alias; omit to rotate")
    parser.add_argument(
        "--force", action="store_true",
        help="with ACCOUNT: switch even if it needs re-login, or replace a "
        "live login qswap does not manage (lost unless saved elsewhere)",
    )
    _json_flag(parser)
    args = parser.parse_args(argv)
    if args.force and args.account is None:
        parser.error("--force can only be used with 'switch ACCOUNT'")
    if args.account is not None and not args.account:
        parser.error("'switch' requires a non-empty ACCOUNT")
    if args.account is None:
        action = lambda s: s.switch(json_output=args.json)  # noqa: E731
    else:
        def action(s):
            notes: list[str] = []
            try:
                number = s._resolve(s._require_registry(), args.account)
                live = s.current_account_number()
            except (ProviderError, ValueError):
                number = live = None
            if number is not None and number != live:
                notes = s.check_switch_target(number, force=args.force)
                if not args.json:
                    for note in notes:
                        warning(f"Warning: {note}")
            payload = s.switch_to(args.account, json_output=args.json, force=args.force)
            if isinstance(payload, dict) and notes:
                payload["warnings"] = [*notes, *payload.get("warnings", [])]
            return payload
    _run(args, action, as_json=args.json)


def _remove(argv: list[str]) -> None:
    parser = _parser(
        "remove",
        "Stop managing an account and delete qswap's saved copy of its login. "
        "The login itself stays signed in. Alias: rm.",
        epilog=f"""examples:
  qswap codex remove 2
  qswap codex remove old@example.com --yes

{LOGOUT_WARNING}
""",
    )
    parser.add_argument("account", metavar="ACCOUNT", help="number, email or alias")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    args = parser.parse_args(argv)
    if not args.account:
        parser.error("'remove' requires a non-empty ACCOUNT")
    _run(args, lambda s: s.remove_account(args.account, assume_yes=args.yes))


def _disable(argv: list[str], disabled: bool) -> None:
    verb = "disable" if disabled else "enable"
    parser = _parser(
        verb,
        "Hold an account out of rotation: a bare switch and auto-switch skip "
        "it; qswap codex switch ACCOUNT can still pick it." if disabled
        else "Return a disabled account to rotation.",
        epilog=f"""examples:
  qswap codex {verb} 2
""",
    )
    parser.add_argument("account", metavar="ACCOUNT", help="number, email or alias")
    args = parser.parse_args(argv)
    if not args.account:
        parser.error(f"'{verb}' requires a non-empty ACCOUNT")
    _run(args, lambda s: s.set_account_disabled(args.account, disabled))


def _alias(argv: list[str]) -> None:
    parser = _parser(
        "alias",
        "Set, remove or list short names for Codex accounts. A name works "
        "anywhere an account number or email does.",
        epilog="""examples:
  qswap codex alias 2 work
  qswap codex alias 2 --unset
  qswap codex alias                   # list them
""",
    )
    parser.add_argument("account", nargs="?", metavar="ACCOUNT",
                        help="number or email; omit to list the aliases")
    parser.add_argument("alias_name", nargs="?", metavar="NAME",
                        help="letters, digits, '.', '-', '_'; not only digits")
    parser.add_argument("--unset", action="store_true", help="remove the account's alias")
    args = parser.parse_args(argv)
    if args.unset and args.alias_name:
        parser.error("--unset does not take a NAME argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and not args.alias_name:
        parser.error("NAME is required (or pass --unset to remove the alias)")

    def action(s):
        from ai_quota_swap.cli import _print_aliases

        if args.account is None:
            _print_aliases(s.list_aliases(), "qswap codex alias")
            return
        if args.unset:
            num = s.unset_alias(args.account)
            say(f"{accent('Removed the alias')} of {s.label(num)}")
        else:
            num, normalized = s.set_alias(args.account, args.alias_name)
            say(f"{accent('Named')} {s.label(num)}")

    _run(args, action)


_ENGINE_NOTE = "Used only by qswap codex auto"


def _threshold(argv: list[str]) -> None:
    parser = _parser(
        "threshold",
        "Set, remove or list a Codex account's own switch line, in place of "
        "codex.threshold for that account.",
        epilog="""examples:
  qswap codex threshold 2 95
  qswap codex threshold 2 --unset
  qswap codex threshold               # list the default and each override
""",
    )
    parser.add_argument("account", nargs="?", metavar="ACCOUNT",
                        help="number, email or alias; omit to list")
    parser.add_argument(
        "threshold", nargs="?", metavar="PCT",
        help=f"percent used, {ACCOUNT_THRESHOLD_MIN:g} to {ACCOUNT_THRESHOLD_MAX:g}",
    )
    parser.add_argument("--unset", action="store_true", help="remove the account's own line")
    args = parser.parse_args(argv)
    if args.unset and args.threshold:
        parser.error("--unset does not take a PCT argument")
    if args.unset and args.account is None:
        parser.error("ACCOUNT is required with --unset")
    if args.account is not None and not args.unset and args.threshold is None:
        parser.error("PCT is required (or pass --unset to remove the override)")

    def action(s):
        if args.account is None:
            from ai_quota_swap.settings import load_engine_settings

            print(bolded("Switch lines"))
            default = load_engine_settings(s.data_root, "codex").threshold
            print(f"  {dimmed('default (codex.threshold)')}  {default:g}%")
            rows = [(n, p.threshold) for n, p in s.account_policies().items()
                    if p.threshold is not None]
            if not rows:
                print(dimmed("  No account has its own line. Set one with: "
                             "qswap codex threshold N PCT"))
            for num, value in rows:
                print(f"  {s.label(num)}  {value:g}%")
            print(dimmed(_ENGINE_NOTE))
            return
        try:
            s.set_account_threshold(args.account, None if args.unset else args.threshold)
        except ValueError as e:
            # normalize_account_threshold's message; it always names the range.
            raise ValidationError(str(e)) from None

    _run(args, action)


def _priority(argv: list[str]) -> None:
    parser = _parser(
        "priority",
        "Set or list Codex account priorities: higher tiers are used first.",
        epilog="""examples:
  qswap codex priority 2 3
  qswap codex priority 2 --unset
  qswap codex priority                # list them
""",
    )
    parser.add_argument("account", nargs="?", metavar="ACCOUNT",
                        help="number, email or alias; omit to list")
    parser.add_argument("priority", nargs="?", metavar="TIER",
                        help=f"a whole number from 0 to {ACCOUNT_PRIORITY_MAX} (default 0)")
    parser.add_argument("--unset", action="store_true", help="return to priority 0")
    _json_flag(parser)
    args = parser.parse_args(argv)
    if args.unset and args.priority is not None:
        parser.error("--unset does not take a TIER argument")
    if args.unset and args.account is None:
        parser.error("ACCOUNT is required with --unset")
    if args.account is not None and args.priority is None and not args.unset:
        parser.error("TIER is required (or pass --unset)")
    from ai_quota_swap.cli import print_priorities

    def make(debug: bool = False):
        switcher = _switcher(debug=debug)
        switcher.check_storage()
        return switcher

    print_priorities(make, args, engine_note="qswap codex auto")


def _login(argv: list[str]) -> None:
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1:]
    else:
        head, tail = argv, []
    parser = _parser(
        "login",
        "Log in another Codex account and save it, without touching the login "
        "you are on: codex login runs in a private, temporary Codex home that "
        "is deleted afterwards.",
        epilog=f"""examples:
  qswap codex login
  qswap codex login --alias work
  qswap codex login -- --device-auth   # arguments after -- go to codex login

{LOGOUT_WARNING}
""",
    )
    parser.add_argument("--slot", type=int, metavar="NUM", help="store it as this account number")
    parser.add_argument("--alias", metavar="NAME", help="give it a short name")
    args = parser.parse_args(head)
    _run(args, lambda s: s.login_new_account(tail, slot=args.slot, alias=args.alias))


def _auto(argv: list[str]) -> None:
    """``qswap codex auto``: ``cli``'s auto command over the Codex switcher."""
    from ai_quota_swap import cli

    with cli.selected_provider("codex"):
        cli._auto_command(argv)


def _reset(argv: list[str]) -> None:
    """``qswap codex reset``: ``cli``'s reset command over the Codex switcher."""
    from ai_quota_swap import cli

    with cli.selected_provider("codex"):
        cli._reset_command(argv)


_VERBS: dict[str, Callable[[list[str]], None]] = {
    "add": _add,
    "login": _login,
    "auto": _auto,
    "reset": _reset,
    "resets": _reset,
    "list": _list,
    "ls": _list,
    "status": _status,
    "switch": _switch,
    "remove": _remove,
    "rm": _remove,
    "disable": lambda argv: _disable(argv, True),
    "enable": lambda argv: _disable(argv, False),
    "alias": _alias,
    "threshold": _threshold,
    "priority": _priority,
}


def dispatch(argv: list[str]) -> None:
    """Run ``qswap codex <argv>``."""
    from ai_quota_swap.argparsing import usage_error

    if not argv:
        print(BARE_HINT, end="")
        return
    verb, rest = argv[0], argv[1:]
    if verb in ("help", "-h", "--help"):
        if verb == "help" and rest and rest[0] not in ("-h", "--help"):
            target = rest[0]
            if target in _VERBS:
                _VERBS[target](["--help"])
                return
            usage_error(f"{PROG} help", f"unknown command '{target}'; {PROG} help lists them")
        print(HELP, end="")
        return
    handler = _VERBS.get(verb)
    if handler is None:
        if verb == "usage":
            _fail(FeatureUnavailableError(
                "Codex usage is part of qswap usage: qswap usage --provider codex"
            ), _wants_json(argv))
        if verb in NOT_YET:
            _fail(FeatureUnavailableError(f"{PROG} {verb} is not available yet"), _wants_json(argv))
        usage_error(
            PROG, f"unknown command '{verb}'; {PROG} help lists them",
            as_json=_wants_json(argv),
        )
    handler(rest)


# -- the unified list -------------------------------------------------------------


def codex_accounts_managed() -> bool:
    """Whether any Codex account is managed, from the registry file alone
    (no switcher, no Codex home, no network). An unreadable registry counts,
    so the section shows its error instead of hiding it."""
    path = get_codex_data_dir() / "sequence.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    return isinstance(data, dict) and bool(data.get("accounts"))


def codex_managed_count() -> int:
    """How many Codex accounts are managed, from the registry file alone."""
    path = get_codex_data_dir() / "sequence.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    accounts = data.get("accounts") if isinstance(data, dict) else None
    return len(accounts) if isinstance(accounts, dict) else 0


def live_codex_email() -> str | None:
    """The live Codex login's email, read from ``auth.json`` alone (no
    switcher, no directory created); None when there is none or it cannot be
    read. Never raises."""
    try:
        from ai_quota_swap.codex import auth as codex_auth

        home = codex_auth.codex_home()
        login = codex_auth.try_parse_login(
            codex_auth.read_auth_bytes(codex_auth.auth_path(home))
        )
    except Exception:
        return None
    return login.email if login is not None else None


def extend_unified_list(payload, *, json_output: bool):
    """Append the Codex section to ``qswap list`` when Codex accounts exist.

    Text: a "Codex accounts:" block after the Claude output. JSON: a top-level
    ``providers.codex`` object ``{activeAccountNumber, accounts}`` whose rows
    have the Claude rows' shape; the existing top-level keys are untouched.
    No Codex accounts: nothing printed, nothing added, no network call. A Codex
    problem (a keyring-mode home, say) is shown in the section and never fails
    the Claude list.
    """
    if not codex_accounts_managed():
        return payload
    from ai_quota_swap.codex.switcher import CodexSwitcher

    section: dict = {}
    if not json_output:
        from ai_quota_swap import display

        print()
        print(bolded(display.provider_heading("codex", both=True)))
    try:
        switcher = CodexSwitcher(configure_logging=False)
        switcher.check_storage()
        if json_output:
            listed = switcher.list_payload()
            section = {
                "activeAccountNumber": listed["activeAccountNumber"],
                "accounts": listed["accounts"],
            }
        else:
            infos = switcher._accounts_info()
            switcher.print_account_rows(infos, switcher._collect_usage_entries(infos))
    except ProviderError as e:
        if json_output:
            section = {
                "activeAccountNumber": None,
                "accounts": [],
                "error": error_envelope(e)["error"],
            }
        else:
            warning(f"  Codex accounts unavailable: {e}")
    if json_output and isinstance(payload, dict):
        payload = {**payload, "providers": {"codex": section}}
    return payload


# -- reset credits: Codex wording for cli's reset command ---------------------------


def describe_credit(
    num: str, email: str, grant: dict, usage: dict | None, status: dict, now: float,
    *, threshold: float = 95.0, name: str | None = None,
) -> str:
    """What spending one Codex usage reset credit would do, before asking."""
    from ai_quota_swap import resets
    from ai_quota_swap.labels import account_label

    who = name or account_label(num, email=email)
    lines = [bolded(f"{who} · reset credit"), dimmed(f"  id {grant['id']}")]
    if grant.get("label"):
        lines.append(dimmed(f"  {grant['label']}"))
    name = resets.reset_name(grant)
    lines.append(f"  {name[:1].upper()}{name[1:]}: {resets.clears_phrase(grant)}")
    trade = resets.trade_note(grant, usage, threshold)
    if trade:
        lines.append(f"  {trade}")
    refills = []
    for key in grant.get("clears") or ():
        label = resets.WINDOW_LABELS.get(key) or key
        window = (usage or {}).get(key)
        pct = window.get("pct") if isinstance(window, dict) else None
        refills.append(f"{label} (now {pct:.0f}%)" if isinstance(pct, (int, float)) else label)
    if refills:
        lines.append(f"  Resets {', '.join(refills)} to 0%")
    end = resets.use_by(resets.parse_ts(grant.get("ends_at")), year=True)
    lines.append(f"  Expires {end}" if end else "  Does not expire")
    others = max(0, resets.total_left(status, now) - 1)
    lines.append(dimmed(
        f"  {others} other credit{'' if others == 1 else 's'} on this account · "
        "a spent credit cannot be undone"
    ))
    return "\n".join(lines)


def print_credit_used(switcher, num: str, email: str, grant: dict) -> None:
    """The success line after a spent credit, then the account's fresh usage."""
    import time

    from ai_quota_swap import resets

    cleared = ", ".join(
        resets.WINDOW_LABELS.get(k) or k for k in grant.get("clears") or ()
    ) or "usage"
    fresh = switcher.usage_entries_by_account(fetch=set()).get(num)
    status = (
        fresh.last_good.get("resets")
        if fresh is not None and isinstance(fresh.last_good, dict) else None
    )
    left = ""
    if isinstance(status, dict):
        count = resets.total_left(status, time.time())
        left = f" · {count} credit{'' if count == 1 else 's'} left"
    print(accent(f"Used a reset credit on {switcher.label(num, long=False)}: {cleared} reset{left}"))
    if fresh is not None:
        for line in switcher._entry_lines(fresh):
            print(f"     {line}")


# -- qswap usage ------------------------------------------------------------------


def observe_codex_login() -> None:
    """For ``qswap usage``: note the live Codex login in Codex's activation
    log, so a login changed outside qswap is attributed from now on.

    Nothing at all (one file check, no switcher, no Codex home read) when no
    Codex account is managed. Never raises.
    """
    try:
        if not codex_accounts_managed():
            return
        from ai_quota_swap.codex.switcher import CodexSwitcher

        CodexSwitcher(configure_logging=False).observe_activation()
    except Exception:
        pass
