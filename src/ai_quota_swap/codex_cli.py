"""``qswap codex <verb>``: the Codex provider's command line, and its section
in the unified ``qswap list``.

The Claude handlers in ``cli.py`` are not reused: their help, examples and
follow-up hints name Claude-only commands (``run``, ``map``, ``qswap auto``),
and the main parser carries Claude-only flags. These handlers keep the Claude
wording and layout wherever the behaviour is the same, and the same
``--json`` envelope and ``schemaVersion`` wherever the Claude equivalent has
``--json`` (``list``, ``status``, ``switch``).
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
from ai_quota_swap.printer import accent, bolded, dimmed, error, muted, warning

PROG = "qswap codex"

#: Verbs of the next milestones: refused with "not available yet".
NOT_YET = ("auto", "reset", "resets", "run")

LOGOUT_WARNING = (
    "Never run `codex logout` on a login qswap has saved: it signs the login "
    "out at OpenAI, so every saved copy of it stops working."
)

SECOND_ACCOUNT_STEPS = """\
Adding a second account:
  `codex login` signs the current login out at OpenAI before it logs in, which
  kills qswap's saved copy of it too. Log the next account in under an empty,
  throwaway Codex home instead:

    tmp="$(mktemp -d)"
    CODEX_HOME="$tmp" codex login        # log in with the other account
    CODEX_HOME="$tmp" qswap codex add
    rm -rf "$tmp"                        # deleting the file does not sign it out
"""

HELP = f"""\
usage: {PROG} <command> [args] [options]

[EXPERIMENTAL] Multi-account switcher for OpenAI Codex CLI (ChatGPT logins)

Commands:
  {PROG} help                          show this help
  {PROG} list                          list managed Codex accounts
  {PROG} status                        show the live Codex login
  {PROG} switch                        rotate to the next account
  {PROG} switch <num|email>            switch to a specific account
  {PROG} add                           save the current Codex login
  {PROG} remove <num|email>            stop managing an account
  {PROG} disable <num|email>           hold an account out of rotation
  {PROG} enable <num|email>            return a disabled account to rotation
  {PROG} threshold <num|email> <pct>   set one account's switch-away point
  {PROG} threshold                     list per-account overrides
  {PROG} priority <num|email> <tier>   set priority (higher first; default 0)
  {PROG} priority                      list account priorities
  {PROG} alias <num|email> <name>      set a short alias for an account
  {PROG} alias <num|email> --unset     remove an account's alias
  {PROG} alias                         list all aliases

Not available yet: auto, reset, run. Aliases: ls=list  rm=remove

Add --json to list, status or switch for machine-readable output.

{SECOND_ACCOUNT_STEPS}
{LOGOUT_WARNING}
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
    parser = argparse.ArgumentParser(
        prog=f"{PROG} {verb}",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON to stdout (see docs/json-output.md)",
    )


# -- verbs ------------------------------------------------------------------------


def _add(argv: list[str]) -> None:
    parser = _parser(
        "add",
        "Save the login currently in Codex's auth.json ($CODEX_HOME/auth.json, "
        "default ~/.codex/auth.json) as a managed account. Run it again for a "
        "login that is already managed to refresh its saved copy.",
        epilog=f"{SECOND_ACCOUNT_STEPS}\n{LOGOUT_WARNING}",
    )
    parser.add_argument("--slot", type=int, metavar="NUM", help="Store the account in this slot")
    parser.add_argument("--alias", metavar="NAME", help="Set a short display alias for the account")
    args = parser.parse_args(argv)
    _run(args, lambda s: s.add_account(slot=args.slot, alias=args.alias))


def _list(argv: list[str]) -> None:
    parser = _parser("list", "List managed Codex accounts with their usage.")
    _json_flag(parser)
    args = parser.parse_args(argv)
    _run(args, lambda s: s.list_accounts(json_output=args.json), as_json=args.json)


def _status(argv: list[str]) -> None:
    parser = _parser("status", "Show the live Codex login and its usage.")
    _json_flag(parser)
    args = parser.parse_args(argv)
    _run(args, lambda s: s.status(json_output=args.json), as_json=args.json)


def _switch(argv: list[str]) -> None:
    parser = _parser(
        "switch",
        "Make a saved Codex login the live one. Without an account, rotate to "
        "the next one. The login being replaced is saved first, so a renewal "
        "Codex did is kept. A running background server is restarted "
        "(codex.restartDaemon); running codex sessions keep the old account "
        "until they exit.",
        epilog="""Examples:
  qswap codex switch
  qswap codex switch 2
  qswap codex switch user@example.com --json
""",
    )
    parser.add_argument("account", nargs="?", metavar="NUM|EMAIL", help="Account to switch to")
    parser.add_argument(
        "--force", action="store_true",
        help="With an account: overwrite a live login qswap does not manage "
        "(it is lost unless saved elsewhere)",
    )
    _json_flag(parser)
    args = parser.parse_args(argv)
    if args.force and args.account is None:
        parser.error("--force can only be used with 'switch <num|email>'")
    if args.account is not None and not args.account:
        parser.error("'switch' requires a non-empty NUM|EMAIL")
    if args.account is None:
        action = lambda s: s.switch(json_output=args.json)  # noqa: E731
    else:
        action = lambda s: s.switch_to(args.account, json_output=args.json, force=args.force)  # noqa: E731
    _run(args, action, as_json=args.json)


def _remove(argv: list[str]) -> None:
    parser = _parser(
        "remove",
        "Stop managing an account and delete qswap's saved copy of its login. "
        "The login itself is not signed out.",
        epilog=LOGOUT_WARNING,
    )
    parser.add_argument("account", metavar="NUM|EMAIL")
    args = parser.parse_args(argv)
    if not args.account:
        parser.error("'remove' requires a non-empty NUM|EMAIL")
    _run(args, lambda s: s.remove_account(args.account))


def _disable(argv: list[str], disabled: bool) -> None:
    verb = "disable" if disabled else "enable"
    parser = _parser(
        verb,
        "Hold an account out of rotation (bare switch skips it); it stays a "
        "valid target for switch <num|email>." if disabled
        else "Return a disabled account to rotation.",
    )
    parser.add_argument("account", metavar="NUM|EMAIL")
    args = parser.parse_args(argv)
    if not args.account:
        parser.error(f"'{verb}' requires a non-empty NUM|EMAIL")
    _run(args, lambda s: s.set_account_disabled(args.account, disabled))


def _alias(argv: list[str]) -> None:
    parser = _parser(
        "alias",
        "Set, remove, or list a short display alias for an account. Once set, "
        "the alias can be used anywhere an account number or email is accepted "
        "(switch, remove, disable, enable, threshold, priority).",
        epilog="""Examples:
  qswap codex alias 2 work
  qswap codex alias user@example.com work
  qswap codex alias 2 --unset
  qswap codex alias                   # list all aliases
""",
    )
    parser.add_argument("account", nargs="?", metavar="NUM|EMAIL",
                        help="Account to alias (number or email). Omit to list aliases.")
    parser.add_argument("alias_name", nargs="?", metavar="NAME",
                        help="Alias to set (letters, digits, ., -, _; not purely numeric).")
    parser.add_argument("--unset", action="store_true", help="Remove the account's alias")
    args = parser.parse_args(argv)
    if args.unset and args.alias_name:
        parser.error("--unset does not take a NAME argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and not args.alias_name:
        parser.error("NAME is required (or pass --unset to remove the alias)")

    def action(s):
        if args.account is None:
            rows = s.list_aliases()
            if not rows:
                print(dimmed("No aliases set"))
                return
            print(bolded("Aliases:"))
            for num, name, email in rows:
                print(f"  {num}: {name} {muted(f'({email})')}")
            return
        if args.unset:
            num = s.unset_alias(args.account)
            print(f"{accent('Removed alias')} for Account {num}")
        else:
            num, normalized = s.set_alias(args.account, args.alias_name)
            print(f"{accent('Set alias')} '{normalized}' for Account {num}")

    _run(args, action)


_ENGINE_NOTE = "Used by qswap codex auto, which is not available yet."


def _threshold(argv: list[str]) -> None:
    parser = _parser(
        "threshold",
        "Set, remove, or list a per-account switch-away threshold for Codex "
        "accounts.",
        epilog=f"""Examples:
  qswap codex threshold 2 95
  qswap codex threshold 2 --unset
  qswap codex threshold               # list overrides

Valid range: {ACCOUNT_THRESHOLD_MIN:g}-{ACCOUNT_THRESHOLD_MAX:g} (percent used).
{_ENGINE_NOTE}
""",
    )
    parser.add_argument("account", nargs="?", metavar="NUM|EMAIL")
    parser.add_argument(
        "threshold", nargs="?", metavar="PCT",
        help=f"Percent used at which to switch away "
        f"({ACCOUNT_THRESHOLD_MIN:g}-{ACCOUNT_THRESHOLD_MAX:g}).",
    )
    parser.add_argument("--unset", action="store_true", help="Remove the account's override")
    args = parser.parse_args(argv)
    if args.unset and args.threshold:
        parser.error("--unset does not take a PCT argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and args.threshold is None:
        parser.error("PCT is required (or pass --unset to remove the override)")

    def action(s):
        if args.account is None:
            print(bolded("Switch-away thresholds:"))
            rows = [(n, p.threshold) for n, p in s.account_policies().items()
                    if p.threshold is not None]
            if not rows:
                print(dimmed("  No per-account overrides set"))
            for num, value in rows:
                print(f"  Account-{num}  {value:g}%")
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
        "Set or list account priority tiers. Higher numbers are used first.",
        epilog=_ENGINE_NOTE,
    )
    parser.add_argument("account", nargs="?", metavar="NUM|EMAIL|ALIAS")
    parser.add_argument("priority", nargs="?", metavar="TIER",
                        help=f"Integer from 0 to {ACCOUNT_PRIORITY_MAX} (default 0)")
    parser.add_argument("--unset", action="store_true", help="Return to priority 0")
    args = parser.parse_args(argv)
    if args.unset and args.priority is not None:
        parser.error("--unset does not take a TIER argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL|ALIAS is required with --unset")
    if args.account is not None and args.priority is None and not args.unset:
        parser.error("TIER is required (or pass --unset)")

    def action(s):
        if args.account is None:
            print(bolded("Account priorities (higher first; default 0):"))
            for num, policy in s.account_policies().items():
                print(f"  Account-{num}  {s.account_email(num)}  {policy.priority}")
            print(dimmed(_ENGINE_NOTE))
            return
        try:
            s.set_account_priority(args.account, None if args.unset else args.priority)
        except ValueError as e:
            raise ValidationError(str(e)) from None

    _run(args, action)


_VERBS: dict[str, Callable[[list[str]], None]] = {
    "add": _add,
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
    if not argv:
        print(f"usage: {PROG} <command> [args] [options]", file=sys.stderr)
        print(f"{PROG}: error: no command given — try '{PROG} help'", file=sys.stderr)
        sys.exit(2)
    verb, rest = argv[0], argv[1:]
    if verb in ("help", "-h", "--help"):
        print(HELP, end="")
        return
    handler = _VERBS.get(verb)
    if handler is None:
        if verb == "usage":
            message = (
                f"{PROG} usage is not a Codex command: run qswap usage --provider codex"
            )
        elif verb in NOT_YET:
            message = f"{PROG} {verb} is not available yet"
        else:
            message = f"{PROG} {verb} is not a Codex command (try '{PROG} help')"
        _fail(FeatureUnavailableError(message), _wants_json(argv))
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
        print()
        print(bolded("Codex accounts:"))
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
