"""Command-line interface for AI Quota Swap."""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import math
import os
import sys
from collections.abc import Iterator

from ai_quota_swap import __version__, paths, printer, providers
from ai_quota_swap.argparsing import ArgumentParser
from ai_quota_swap.exceptions import ProviderError
from ai_quota_swap.json_output import error_envelope
from ai_quota_swap.models import (
    ACCOUNT_PRIORITY_MAX,
    ACCOUNT_THRESHOLD_MAX,
    ACCOUNT_THRESHOLD_MIN,
)
from ai_quota_swap.printer import (
    accent,
    bolded,
    dimmed,
    error,
    force_utf8_output,
    muted,
    warning,
    yellowed,
)
from ai_quota_swap.settings import (
    AUTOSWITCH_STRATEGIES,
    load_ui_settings,
    load_update_settings,
)
from ai_quota_swap.switcher import ClaudeAccountSwitcher

# The provider the command line selected (`qswap codex <verb>`). Only `main`
# sets it, for the length of one dispatch, and always resets it, so nothing
# carries over into the next in-process call; tests use `selected_provider`.
_provider: contextvars.ContextVar[str] = contextvars.ContextVar(
    "qswap_provider", default=providers.DEFAULT_PROVIDER
)


@contextlib.contextmanager
def selected_provider(provider_id: str) -> Iterator[None]:
    """Select ``provider_id`` for every switcher built inside the block."""
    token = _provider.set(provider_id)
    try:
        yield
    finally:
        _provider.reset(token)


def _switcher(debug: bool = False):
    """The switcher for the selected provider; every command builds its own here.

    Claude Code's switcher is built through this module's own
    ``ClaudeAccountSwitcher`` name rather than ``providers.make_switcher``, so
    tests that patch ``ai_quota_swap.cli.ClaudeAccountSwitcher`` still
    intercept the construction.
    """
    provider_id = _provider.get()
    if provider_id == providers.DEFAULT_PROVIDER:
        return ClaudeAccountSwitcher(debug=debug)
    return providers.make_switcher(provider_id, debug=debug)


def _prog_name() -> str:
    """The command name to show in usage/help.

    argparse otherwise defaults to ``os.path.basename(sys.argv[0])``, which for
    an installed entry-point shim renders as an ugly absolute path (e.g.
    ``python.exe C:\\Users\\me\\.local\\bin\\qswap``). We strip that down to the
    bare command the user typed (``qswap`` / ``ai-quota-swap``), falling back to
    ``qswap`` for ``python -m ai_quota_swap`` and odd launchers.
    """
    name = os.path.basename(sys.argv[0] or "")
    for ext in (".exe", ".pyw", ".py"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not name or name in {"__main__", "python", "python3", "py"}:
        return "qswap"
    return name


# Memorable subcommand aliases → the long-standing flags they expand to. Lets
# users type `qswap list`, `qswap status`, `qswap add`, etc. instead of `--list`
# / `--status` / `--add-account`, which all still work. `switch` is special-cased
# below (a bare `switch` rotates; `switch <target>` jumps to one account) and
# `run`/`auto` keep their own pre-dispatch parsers, so none of those are listed here.
_SUBCOMMAND_FLAGS = {
    "help": "--help",
    "list": "--list",
    "ls": "--list",
    "status": "--status",
    "add": "--add-account",
    "add-token": "--add-token",
    "remove": "--remove-account",
    "rm": "--remove-account",
    "disable": "--disable-account",
    "enable": "--enable-account",
    "export": "--export",
    "import": "--import",
    "import-usage": "--import-usage",
    "purge": "--purge",
    "upgrade": "--upgrade",
    "update": "--upgrade",
    "tui": "--tui",
    "watch": "--watch",
    "menubar": "--menubar",
}


def _translate_subcommand(argv: list[str]) -> list[str]:
    """Rewrite a leading memorable subcommand into the equivalent flag argv.

    ``argv`` is the args after the program name. The rewrite only fires when the
    first token is a recognized verb (which never starts with '-'), so the
    established ``--flag`` interface — and every existing test that drives it —
    is left untouched. Tokens after the verb pass through verbatim, so flags
    like ``--json``, ``--strategy``, ``--slot``, and ``--force`` keep combining
    exactly as before (e.g. ``qswap switch --strategy best``, ``qswap list --json``).
    """
    if not argv:
        return argv

    verb, rest = argv[0], argv[1:]

    if verb == "switch":
        # Bare `switch` rotates; `switch <num|email>` jumps to that account.
        if rest and not rest[0].startswith("-"):
            return ["--switch-to", *rest]
        return ["--switch", *rest]

    flag = _SUBCOMMAND_FLAGS.get(verb)
    if flag is not None:
        return [flag, *rest]

    return argv


def _run_command(argv: list[str]) -> None:
    """Handle `qswap run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built: a positional subcommand
    can't coexist with main()'s mutually-exclusive flag group, and this keeps
    the existing parser untouched. Limitation: `run` must be the
    first argument (`qswap --debug run 2` is not supported; use
    `qswap run 2 --debug`).

    On POSIX this execs claude and never returns; on Windows it exits with
    claude's return code. Either way the post-dispatch update check in
    main() is unreachable, which is intended.
    """
    # Everything after the first `--` is forwarded to claude verbatim.
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = ArgumentParser(
        prog=f"{_prog_name()} run",
        usage="qswap run [ACCOUNT] [options] [-- CLAUDE_ARGS]",
        description=(
            "Run Claude Code as a saved account in this terminal only; the "
            "active login and other terminals keep theirs. Experimental."
        ),
        epilog="""examples:
  qswap run 2
  qswap run work -- --resume      # arguments after -- go to claude
  qswap run                       # the account mapped to this directory
""",
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="ACCOUNT",
        help="number, email or alias; omit for this directory's account (qswap map)",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "do not share settings, CLAUDE.md, skills, commands, agents and "
            "rules from ~/.claude with the session"
        ),
    )
    parser.add_argument(
        "--share-history",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "share conversation history with ~/.claude (the default; "
            "run.shareHistory sets it for every launch)"
        ),
    )
    parser.add_argument(
        "--require-session",
        action="store_true",
        help="refuse when the account is already the active login",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log debug detail",
    )
    args = parser.parse_args(head)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)

        from ai_quota_swap.session import SessionManager

        manager = SessionManager(switcher)

        # share_history goes through as given: None (neither flag) leaves it
        # to run.shareHistory, which SessionManager.run resolves.
        if args.account is not None:
            manager.run(
                args.account,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
                require_session=args.require_session,
            )
            return  # only reachable in tests where exec/exit is mocked

        # No account given: resolve from the current directory's mapping.
        slot, email = switcher.slot_for_directory(os.getcwd())
        if slot is not None:
            manager.run(
                slot,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
                require_session=args.require_session,
            )
            return  # only reachable in tests
        if email is not None:
            warning(
                f"Mapped account {email} no longer exists — "
                "launching the default account."
            )
        else:
            print(
                dimmed(
                    f"No account mapped for {os.getcwd()} — "
                    "launching the default account."
                )
            )
        manager.exec_default(tail)
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _settings_root(switcher):
    """Where ``settings.json`` lives: the data root, shared by every provider.

    Claude Code's switcher keeps its data in the data root itself
    (``backup_dir``); Codex's keeps its own one level down and names the
    shared root ``data_root``.
    """
    from pathlib import Path

    root = getattr(switcher, "data_root", None)
    return root if isinstance(root, Path) else switcher.backup_dir


def _guard_root(switcher: ClaudeAccountSwitcher) -> None:
    """Refuse to run as root outside a container (shared by run/map/unmap)."""
    if sys.platform != "win32":
        if os.geteuid() == 0 and not switcher._is_running_in_container():
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)


def _map_command(argv: list[str]) -> None:
    """Handle `qswap map [NUM|EMAIL] [PATH]`.

    With no NUM|EMAIL, lists all mappings. Otherwise maps PATH (default: the
    current directory) to the given account. Pre-dispatched before the main
    parser for the same reason as `run` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = ArgumentParser(
        prog="qswap map",
        description=(
            "Pick the account qswap run starts in a directory. With no "
            "arguments, list the mappings."
        ),
        epilog="""examples:
  qswap map 2 ~/work/client-app
  qswap map work                      # the current directory
  qswap map                           # list the mappings
""",
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="ACCOUNT",
        help="number, email or alias; omit to list the mappings",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="the directory (default: the current one)",
    )
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            switcher.list_mappings()
            return

        from ai_quota_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        account_num, email, org_uuid = switcher.resolve_account(args.account)
        target = args.path or os.getcwd()
        if not os.path.isdir(target):
            warning(f"Warning: {target} is not an existing directory (mapping it anyway)")
        previous = store.get(target)
        store.set(target, email, org_uuid)

        shown = normalize_path(target)
        name = switcher.label(account_num, email)
        if previous and previous.get("email") != email:
            prev_email = previous.get("email")
            print(
                f"{accent('Mapped')} {shown} → {name} "
                f"{muted(f'(was {prev_email})')}"
            )
        else:
            print(f"{accent('Mapped')} {shown} → {name}")
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unmap_command(argv: list[str]) -> None:
    """Handle `qswap unmap [PATH]` — remove a directory→account mapping."""
    parser = ArgumentParser(
        prog="qswap unmap",
        description="Remove a directory's account mapping.",
        epilog="""examples:
  qswap unmap                         # the current directory
  qswap unmap ~/work/client-app
""",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="the directory (default: the current one)",
    )
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)

        from ai_quota_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        target = args.path or os.getcwd()
        shown = normalize_path(target)
        if store.remove(target):
            print(f"{accent('Unmapped')} {shown}")
        else:
            print(dimmed(f"No mapping for {shown}"))
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unclaimed_command(argv: list[str]) -> None:
    """Handle `qswap unclaimed [--purge ID]` — inspect or drop a stash row.

    The stash holds credential bytes a switch or a consume gate could not
    attribute to a slot. Rows normally clear themselves (the next gate pass
    adopts or retires them), but two states need a human: a row whose bytes
    are unreadable until a keychain is unlocked or a mode is fixed, and one
    whose metadata was lost, which no pass can ever adopt. ``--json`` lists
    only bare ids, so without this there is nothing to look at and nothing to
    drop short of hand-editing the manifest.
    """
    parser = ArgumentParser(
        prog=f"{_prog_name()} unclaimed",
        description=(
            "List saved logins that no account claims (kept when a switch "
            "could not tell whose they were), or delete one. A deleted one is "
            "gone; the way back is /login in Claude Code, then qswap add."
        ),
        epilog="""examples:
  qswap unclaimed
  qswap unclaimed --purge ID
""",
    )
    parser.add_argument(
        "--purge",
        metavar="ID",
        help="delete this saved login",
    )
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        entries = switcher.list_unclaimed_credentials()

        if args.purge:
            if args.purge not in entries:
                error(f"Error: no unclaimed saved login {args.purge}; qswap unclaimed lists them")
                sys.exit(1)
            switcher._store._remove_unclaimed_credential(args.purge)
            print(f"{accent('Deleted')} {args.purge}")
            return

        if not entries:
            print(dimmed("No unclaimed saved logins; nothing to do."))
            return
        print(bolded("Unclaimed saved logins"))
        for entry_id, meta in sorted(entries.items()):
            slot = meta.get("configSlot")
            reason = meta.get("reason") or "no record of where it came from"
            where = f"from slot {slot}" if slot else "slot unknown"
            print(f"  {entry_id}  {muted(where)}  {dimmed(reason)}")
        print(dimmed("Delete one with: qswap unclaimed --purge ID"))
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _swap_command(argv: list[str]) -> None:
    """Handle `qswap swap NUM|EMAIL|ALIAS NUM|EMAIL|ALIAS`.

    Exchanges the two accounts' slot numbers (list order and numeric
    targets). Pre-dispatched before the main parser for the same reason as
    `alias` (the main parser's required mutually-exclusive group can't hold
    a positional subcommand).
    """
    parser = ArgumentParser(
        prog=f"{_prog_name()} swap",
        description=(
            "Exchange two accounts' numbers. Aliases, saved logins and session "
            "history move with each account."
        ),
        epilog="""examples:
  qswap swap 1 2
  qswap swap work 1
""",
    )
    parser.add_argument("first", metavar="ACCOUNT", help="one account (number, email or alias)")
    parser.add_argument("second", metavar="ACCOUNT", help="the other account")
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        num_a, num_b = switcher.swap_accounts(args.first, args.second)
        print(f"{accent('Swapped')} slots {num_a} and {num_b}:")
        for num in sorted((num_a, num_b), key=int):
            print(f"  {switcher.label(num)}")
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _move_command(argv: list[str]) -> None:
    """Handle `qswap move NUM|EMAIL|ALIAS SLOT`.

    Assigns an account to a specific slot number. If the slot is empty the
    account is relocated there (its old slot is freed); if it is occupied the
    two accounts trade places. `swap a b` is exactly `move a <b's slot>`.
    Pre-dispatched before the main parser for the same reason as `alias`.
    """
    parser = ArgumentParser(
        prog=f"{_prog_name()} move",
        description=(
            "Give an account another number (slot). A free slot moves it "
            "there; a taken one swaps the two accounts."
        ),
        epilog="""examples:
  qswap move 3 1                  # swaps with #1 if slot 1 is taken
  qswap move work 1
  qswap move user@example.com 6
""",
    )
    parser.add_argument("account", metavar="ACCOUNT", help="number, email or alias")
    parser.add_argument("slot", metavar="SLOT", help="the number to give it")
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        num_src, num_target, swapped = switcher.move_account(args.account, args.slot)
        if num_src == num_target:
            print(dimmed(f"{switcher.label(num_target)} is already in slot {num_target}."))
        elif swapped:
            print(f"{accent('Swapped')} slots {num_src} and {num_target}:")
            for num in sorted((num_src, num_target), key=int):
                print(f"  {switcher.label(num)}")
        else:
            print(f"{accent('Moved')} {switcher.label(num_target)} from slot {num_src} → {num_target}")
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _alias_command(argv: list[str]) -> None:
    """Handle `qswap alias [NUM|EMAIL] [NAME] [--unset]`.

    With no arguments, lists all aliases. Otherwise sets (or, with --unset,
    removes) the alias for the given account. Pre-dispatched before the main
    parser for the same reason as `map` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = ArgumentParser(
        prog="qswap alias",
        description=(
            "Set, remove or list short names for accounts. A name works "
            "anywhere an account number or email does."
        ),
        epilog="""examples:
  qswap alias 2 work
  qswap alias 2 --unset
  qswap alias                         # list them
""",
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="ACCOUNT",
        help="number or email; omit to list the aliases",
    )
    parser.add_argument(
        "alias_name",
        nargs="?",
        metavar="NAME",
        help="letters, digits, '.', '-', '_'; not only digits",
    )
    parser.add_argument("--unset", action="store_true", help="remove the account's alias")
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    if args.unset and args.alias_name:
        parser.error("--unset does not take a NAME argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and not args.alias_name:
        parser.error("NAME is required (or pass --unset to remove the alias)")

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            _print_aliases(switcher.list_aliases(), "qswap alias")
            return

        if args.unset:
            account_num = switcher.unset_alias(args.account)
            printer.say(f"{accent('Removed the alias')} of {switcher.label(account_num)}")
        else:
            account_num, normalized = switcher.set_alias(args.account, args.alias_name)
            printer.say(f"{accent('Named')} {switcher.label(account_num)}")
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _print_aliases(rows: list, command: str) -> None:
    """The alias list (``qswap alias``, ``qswap codex alias``)."""
    from ai_quota_swap.labels import account_label

    if not rows:
        print(dimmed(f"No aliases set. Name an account with: {command} N NAME"))
        return
    print(bolded("Aliases"))
    for num, alias_name, email in rows:
        print(f"  {account_label(num, alias=accent(alias_name), email=email, long=True)}")


def _threshold_command(argv: list[str]) -> None:
    """Handle `qswap threshold [NUM|EMAIL|ALIAS] [PCT] [--unset]`.

    With no arguments, prints the global `autoswitch.threshold` and every
    per-account override. Otherwise sets (or, with --unset, removes) one
    account's own switch-away threshold. Pre-dispatched before the main parser
    for the same reason as `alias`: the main parser's required
    mutually-exclusive group cannot hold a positional subcommand.

    The value is validated inside `set_account_threshold`, before any file is
    touched, so a rejected value leaves `sequence.json` byte-identical.
    """
    parser = ArgumentParser(
        prog="qswap threshold",
        description=(
            "Set, remove or list an account's own switch line: the percent "
            "used at which auto-switch moves off it, in place of "
            "autoswitch.threshold for that account."
        ),
        epilog="""examples:
  qswap threshold 2 85
  qswap threshold 2 --unset           # back to autoswitch.threshold
  qswap threshold                     # list the default and each override
""",
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="ACCOUNT",
        help="number, email or alias; omit to list",
    )
    parser.add_argument(
        "threshold",
        nargs="?",
        metavar="PCT",
        help=f"percent used, {ACCOUNT_THRESHOLD_MIN:g} to {ACCOUNT_THRESHOLD_MAX:g}",
    )
    parser.add_argument(
        "--unset", action="store_true", help="remove the account's own line"
    )
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    if args.unset and args.threshold:
        parser.error("--unset does not take a PCT argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and args.threshold is None:
        parser.error("PCT is required (or pass --unset to remove the override)")

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            _print_threshold_table(switcher)
            return

        switcher.set_account_threshold(
            args.account, None if args.unset else args.threshold
        )
    except ValueError as e:
        # normalize_account_threshold's message; it always names the range.
        error(f"Error: {e}")
        sys.exit(1)
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _print_threshold_table(switcher: ClaudeAccountSwitcher) -> None:
    """Print the global switch-away threshold and every per-account override.

    The global line is printed unconditionally: an operator reading a list of
    overrides needs to know what the accounts *without* one are using.
    """
    from ai_quota_swap.settings import load_settings

    default = load_settings(switcher.backup_dir).threshold
    print(bolded("Switch lines"))
    print(f"  {dimmed('default (autoswitch.threshold)')}  {default:g}%")

    rows = sorted(
        (
            (num, policy.threshold)
            for num, policy in switcher.account_policies().items()
            if policy.threshold is not None
        ),
        key=lambda row: (not row[0].isdigit(), int(row[0]) if row[0].isdigit() else 0),
    )
    if not rows:
        print(dimmed("  No account has its own line. Set one with: qswap threshold N PCT"))
        return
    for num, value in rows:
        print(f"  {switcher.label(num)}  {value:g}%")


def _priority_command(argv: list[str]) -> None:
    """Set, clear, or inspect tiers without enabling the strategy implicitly."""
    parser = ArgumentParser(
        prog="qswap priority",
        description=(
            "Set or list account priorities for the priority-consume-first "
            "strategy: higher tiers are used first; within a tier, the account "
            "whose weekly window resets soonest."
        ),
        epilog="""examples:
  qswap priority 2 5
  qswap priority 2 --unset            # back to 0
  qswap priority                      # list them
""",
    )
    parser.add_argument("account", nargs="?", metavar="ACCOUNT",
                        help="number, email or alias; omit to list")
    parser.add_argument(
        "priority", nargs="?", metavar="TIER",
        help=f"a whole number from 0 to {ACCOUNT_PRIORITY_MAX} (default 0)",
    )
    parser.add_argument("--unset", action="store_true", help="return to priority 0")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (docs/json-output.md)")
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)
    if args.unset and args.priority is not None:
        parser.error("--unset does not take a TIER argument")
    if args.unset and args.account is None:
        parser.error("ACCOUNT is required with --unset")
    if args.account is not None and args.priority is None and not args.unset:
        parser.error("TIER is required (or pass --unset)")
    print_priorities(_switcher, args, engine_note="qswap auto --strategy priority-consume-first")


def print_priorities(make_switcher, args, *, engine_note: str) -> None:
    """``priority`` for either provider: list, set or clear; ``--json`` too."""
    from ai_quota_swap.json_output import SCHEMA_VERSION

    try:
        switcher = make_switcher(debug=args.debug)
        _guard_root(switcher)
        if args.account is None:
            policies = switcher.account_policies()
            if args.json:
                print(json.dumps({
                    "schemaVersion": SCHEMA_VERSION,
                    "accounts": [
                        {"number": int(num), "email": switcher.account_email(num),
                         "priority": policy.priority}
                        for num, policy in policies.items()
                    ],
                }, indent=2))
                return
            print(bolded("Priorities (higher first; default 0)"))
            for num, policy in policies.items():
                print(f"  {switcher.label(num)}  {policy.priority}")
            print(dimmed(f"Used only by {engine_note}"))
            return
        import contextlib
        import io

        quiet = io.StringIO() if args.json else None
        with contextlib.redirect_stdout(quiet) if quiet is not None else contextlib.nullcontext():
            switcher.set_account_priority(args.account, None if args.unset else args.priority)
        if args.json:
            num, email, _org = switcher.resolve_account(args.account)
            print(json.dumps({
                "schemaVersion": SCHEMA_VERSION,
                "account": {"number": int(num), "email": email},
                "priority": switcher.account_policies()[num].priority,
            }, indent=2))
    except (ValueError, ProviderError) as e:
        if args.json:
            from ai_quota_swap.exceptions import ValidationError

            exc = e if isinstance(e, ProviderError) else ValidationError(str(e))
            print(json.dumps(error_envelope(exc), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


_CODEX_RESET_DESCRIPTION = (
    "List Codex reset credits, or spend one on an account. A credit resets "
    "the account's usage windows to 0% and is used up when spent."
)

_CODEX_RESET_EPILOG = """examples:
  qswap codex reset               # every account's credits and the auto plan
  qswap codex reset 2             # spend one on #2 (asks first)
  qswap codex reset 2 --yes

Automatic use (qswap codex auto) follows resets.auto, as for Claude Code:
qswap config set resets.auto off | active | on-limit | last-resort
"""

_RESET_DESCRIPTION = (
    "List usage resets, or spend one on an account. A reset refills the "
    "windows it clears to 0%; the weekly reset day stays where it was."
)

_RESET_EPILOG = """examples:
  qswap reset                     # every account's resets and the auto plan
  qswap reset 2                   # spend one on #2 (asks first)
  qswap reset 2 --grant ID --yes

Kinds: a Fable reset clears Fable usage only; a five-hour reset the 5-hour
window; a weekly reset all weekly usage; a full reset everything. Automatic
use (qswap auto, the dashboard, the menu bar) follows resets.auto:
qswap config set resets.auto off | active | on-limit | last-resort
"""


def _reset_command(argv: list[str]) -> None:
    """Handle `qswap reset [ACCOUNT] [--grant ID] [--yes] [--json]`.

    Bare: list every account's usage-reset grants, the auto policy, and what
    the engine would spend right now. With an account: spend one reset there
    (asks first unless --yes; off a terminal, --yes is required). Exit 1 when
    nothing was spent; every failure under --json is the error envelope.

    Under the codex prefix (`qswap codex reset`) the same flow lists and
    spends Codex reset credits; only the wording differs.
    """
    import time as _time

    codex = _provider.get() == "codex"
    parser = ArgumentParser(
        prog="qswap codex reset" if codex else "qswap reset",
        description=_CODEX_RESET_DESCRIPTION if codex else _RESET_DESCRIPTION,
        epilog=_CODEX_RESET_EPILOG if codex else _RESET_EPILOG,
    )
    parser.add_argument("account", nargs="?", metavar="ACCOUNT",
                        help="number, email or alias; omit to list")
    parser.add_argument(
        "--grant", metavar="ID",
        help=("which credit to spend (default: the one expiring first)" if codex
              else "which reset to spend (default: the best usable one)"),
    )
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (docs/json-output.md)")
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    from ai_quota_swap import display, prompts, resets
    from ai_quota_swap.autoswitch import (
        STATE_FILENAME,
        _read_state_file,
        record_reset_claim,
        reset_request_id,
    )
    from ai_quota_swap.exceptions import ValidationError
    from ai_quota_swap.json_output import SCHEMA_VERSION

    # What the messages call one reset, and the command that lists them.
    noun = "reset credit" if codex else "reset"
    command = "qswap codex reset" if codex else "qswap reset"
    in_flight = False
    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        if codex:
            # A keyring-mode home or nothing managed: one line, as every codex verb.
            switcher.require_accounts()
        rs = switcher.reset_settings()
        if not rs.enabled:
            raise ValidationError(
                "usage resets are off; turn them on with: "
                "qswap config set resets.enabled true"
            )
        if args.account is None:
            _print_resets(switcher, as_json=args.json)
            return

        num, email, _org = switcher.resolve_account(args.account)
        name = switcher.label(num, long=False)
        entry = switcher.refresh_account_usage(num)
        last_good = entry.last_good if entry is not None else None
        status = last_good.get("resets") if isinstance(last_good, dict) else None
        now = _time.time()
        if status is None:
            why = entry.last_error if entry is not None else None
            if why == "invalid_grant" or (entry is not None and entry.sentinel == "re-login needed"):
                step = display.relogin_step("codex" if codex else "claude")
                raise ValidationError(
                    f"{name} {display.NEEDS_RELOGIN}; nothing was spent.\n"
                    f"{step[:1].upper()}{step[1:]}"
                )
            if why:
                raise ValidationError(
                    f"{name}: its {noun}s could not be read ({display.error_label(why)}); "
                    "try again later"
                )
            raise ValidationError(f"{name} reports no {noun}s; nothing to spend")
        if not status.get("eligible"):
            reason = status.get("ineligible_reason") or "unknown"
            raise ValidationError(f"{name} has no {noun}s (not eligible: {reason})")
        grants = status.get("grants") or []
        if args.grant:
            grant = next((g for g in grants if g["id"] == args.grant), None)
            if grant is None:
                raise ValidationError(f"{name} has no {noun} '{args.grant}' ({command} lists them)")
        else:
            # One that clears a window at the line first, the narrowest
            # first: with only Fable at its limit, a Fable reset before a
            # weekly one.
            usable = resets.manual_order(status, last_good, rs.threshold, now)
            if not usable:
                held = resets.unused_grants(status, now)
                if not held:
                    raise ValidationError(f"{name} holds no {noun}s; nothing to spend")
                reasons = "; ".join(
                    f"{resets.reset_name(g)}: "
                    f"{display.grant_state(resets.grant_unusable_reason(g, now))}"
                    for g in held
                )
                raise ValidationError(f"{name} has no {noun} it can use right now ({reasons})")
            grant = usable[0]
        unusable = resets.grant_unusable_reason(grant, now)
        if unusable:
            raise ValidationError(
                f"that {noun} cannot be used right now ({display.grant_state(unusable)})"
            )

        reset_name = resets.reset_name(grant)
        if not args.yes:
            if not prompts.interactive():
                raise prompts.needs_confirmation(command)
            if codex:
                from ai_quota_swap import codex_cli

                print(codex_cli.describe_credit(
                    num, email, grant, last_good, status, now, threshold=rs.threshold,
                    name=switcher.label(num),
                ))
                question = "Use this reset credit?"
            else:
                print(_describe_reset(
                    num, email, grant, last_good, status, now, threshold=rs.threshold,
                    name=switcher.label(num),
                ))
                question = f"Use this {reset_name}?"
            if not prompts.confirm(question, command=command):
                print(dimmed("Kept; nothing was spent."))
                return

        state = _read_state_file(switcher.backup_dir / STATE_FILENAME)
        claims = state.get("resetClaims") if isinstance(state.get("resetClaims"), dict) else {}
        request_id = reset_request_id(claims.get(num), grant["id"], switcher)
        base = {
            "at": _time.time(),
            "grantId": grant["id"],
            "requestId": request_id,
            "reason": "manual",
            "auto": False,
        }
        record_reset_claim(switcher.backup_dir, num, {**base, "result": "pending", "settled": False})
        in_flight = True
        result = switcher.claim_usage_reset(num, grant["id"], request_id)
        in_flight = False
        record_reset_claim(
            switcher.backup_dir, num,
            {**base, "result": result.result, "settled": result.settled,
             "detail": result.reason or result.detail},
        )
        if args.json:
            print(json.dumps({
                "schemaVersion": SCHEMA_VERSION,
                "account": {"number": int(num), "email": email},
                "grantId": grant["id"],
                "result": result.to_json(),
            }))
        elif result.ok and codex:
            from ai_quota_swap import codex_cli

            codex_cli.print_credit_used(switcher, num, email, grant)
        elif result.ok:
            left = f" · {result.resets_left} left on this grant" if result.resets_left is not None else ""
            cleared = ", ".join(
                resets.WINDOW_LABELS.get(k) or k for k in result.cleared
            ) or resets.grant_kind(grant)
            print(accent(f"Used a {reset_name} on {name}: {cleared} refilled{left}"))
            fresh = switcher.usage_entries_by_account(fetch=set()).get(num)
            if fresh is not None:
                block = display.block_from_entry(
                    num, fresh, provider="claude",
                    now=_time.time(), stale_after_s=float("inf"),
                )
                for line in display.render_block(block)[1:]:
                    print(line)
        else:
            detail = (result.reason or result.detail or "").replace("_", " ")
            error(
                f"{'Credit' if codex else 'Reset'} not used: {result.result.replace('_', ' ')}"
                + (f" ({detail})" if detail else "")
            )
        if not result.ok:
            sys.exit(1)
    except ProviderError as e:
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        if in_flight:
            # The POST may have reached the server; the pending record keeps
            # its request id, so a retry (`qswap reset N` or the engine)
            # cannot spend a second one.
            print(f"\n{yellowed(f'Interrupted mid-claim; the {noun} may have been used. {command} shows where things stand.')}")
        else:
            print(f"\n{dimmed('Cancelled; nothing was spent.')}")
        sys.exit(130)


def _describe_reset(
    num: str, email: str, grant: dict, usage: dict | None, status: dict, now: float,
    *, threshold: float = 95.0, name: str | None = None,
) -> str:
    """What spending ``grant`` would do, in the terms Claude Code uses, and
    what it trades when it is broader than the limit reached. ``name`` is
    the account's label (``#2 work (maya@example.com)``)."""
    from ai_quota_swap import resets
    from ai_quota_swap.labels import account_label
    from ai_quota_swap.oauth import reset_clock_string
    from datetime import datetime, timezone

    reset_name = resets.reset_name(grant)
    who = name or account_label(num, email=email)
    lines = [bolded(f"{who} · {reset_name}"), dimmed(f"  id {grant['id']}")]
    if grant.get("label"):
        lines.append(dimmed(f"  {grant['label']}"))
    phrase = resets.clears_phrase(grant)
    lines.append(f"  {phrase[:1].upper()}{phrase[1:]}")
    trade = resets.trade_note(grant, usage, threshold)
    if trade:
        lines.append(yellowed(f"  {trade}"))
    refills = []
    for key in grant.get("clears") or ():
        label = resets.WINDOW_LABELS.get(key) or key
        pct = (grant.get("percent_used") or {}).get(key)
        refills.append(f"{label} (now {pct:.0f}%)" if isinstance(pct, (int, float)) else label)
    if refills:
        lines.append(f"  Refills {', '.join(refills)} to 0%")
    if grant.get("gives_back_weekly"):
        share = next(
            (
                s.get("share_of_weekly")
                for s in (usage or {}).get("scoped") or ()
                if isinstance(s, dict)
                and any(
                    (resets.WINDOW_LABELS.get(k) or "").lower() == str(s.get("name", "")).lower()
                    for k in grant.get("clears") or ()
                )
            ),
            None,
        )
        given = f" (about {share:.0f} pts)" if isinstance(share, (int, float)) else ""
        lines.append(f"  Also gives back what it used of the weekly window{given}")
    week = resets.parse_ts(status.get("weekly_resets_at"))
    if week is not None:
        try:
            clock = reset_clock_string(
                datetime.fromtimestamp(week, tz=timezone.utc),
                datetime.fromtimestamp(now, tz=timezone.utc),
            )
        except (ValueError, OverflowError, OSError):
            clock = None
        if clock:
            lines.append(f"  Your weekly reset stays {clock}")
    left = max(0, grant["resets_left"] - 1)
    end = resets.use_by(resets.parse_ts(grant.get("ends_at")), year=True)
    tail = f" · use by {end}" if end else ""
    lines.append(dimmed(f"  {left} left on this grant after{tail} · a used reset cannot be undone"))
    return "\n".join(lines)


def _print_resets(switcher: ClaudeAccountSwitcher, *, as_json: bool) -> None:
    """Every account's grants, the auto policy, and the plan right now."""
    import time as _time

    from ai_quota_swap import resets
    from ai_quota_swap.autoswitch import (
        MAX_AUTO_RESETS_PER_DAY,
        STATE_FILENAME,
        _read_state_file,
        preview_reset_plan,
        reset_claims_today,
    )
    from ai_quota_swap.settings import load_engine_settings

    codex = _provider.get() == "codex"
    entries = switcher.usage_entries_by_account()
    # Readings taken before reset grants were read (an older qswap, or
    # resets.enabled off at the time) carry no status: read those now.
    missing = {
        num for num, entry in entries.items()
        if entry.sentinel is None
        and isinstance(entry.last_good, dict)
        and "resets" not in entry.last_good
        and switcher.account_kind_for(num) != "api_key"
    }
    if missing:
        entries = switcher.refresh_usage(missing)
    now = _time.time()
    rs = switcher.reset_settings()
    settings = load_engine_settings(_settings_root(switcher), _provider.get())
    plan = preview_reset_plan(switcher, settings, entries, now=now)
    # A broad reset kept only because a per-model limit is all that is
    # reached: what auto would do if resets.weeklyForModelLimit allowed it.
    kept = None
    if plan is None and rs.auto != "off" and not rs.weekly_for_model_limit:
        from dataclasses import replace

        kept = resets.kept_by_scope(None, preview_reset_plan(
            switcher, settings, entries, now=now,
            reset_settings=replace(rs, weekly_for_model_limit=True),
        ))
    state = _read_state_file(switcher.backup_dir / STATE_FILENAME)
    used_today = reset_claims_today(state, now)
    current = switcher.current_account_number()

    if as_json:
        from ai_quota_swap.json_output import SCHEMA_VERSION

        accounts = []
        for num, entry in entries.items():
            status = entry.last_good.get("resets") if isinstance(entry.last_good, dict) else None
            accounts.append({
                "number": int(num),
                "email": switcher.account_email(num),
                "active": num == current,
                "resets": resets.status_to_json(status, now) if status else None,
                "error": None if status else (entry.last_error or entry.sentinel or "no reading"),
            })
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "auto": {
                "mode": rs.auto,
                "threshold": rs.threshold,
                "minHoursLeft": rs.min_hours_left,
                "expiryDays": rs.expiry_days,
                "expiryThreshold": rs.expiry_threshold,
                "weeklyForModelLimit": rs.weekly_for_model_limit,
                "usedToday": used_today,
                "maxPerDay": MAX_AUTO_RESETS_PER_DAY,
            },
            "plan": plan.to_json() if plan else None,
            "accounts": accounts,
        }))
        return

    from ai_quota_swap import display

    noun = "reset credit" if codex else "reset"
    command = "qswap codex reset" if codex else "qswap reset"
    set_aside = switcher.set_aside_accounts()
    mode = {
        "off": f"off · only {command} N spends one",
        "active": f"active · when the account in use reaches {rs.threshold:g}% on a window "
                  "its reset clears",
        "last-resort": f"last-resort · when the active account reaches {rs.threshold:g}% "
                       "and no switch helps",
        "on-limit": f"on-limit · when any account reaches {rs.threshold:g}%",
    }[rs.auto]
    print(bolded("Reset credits" if codex else "Usage resets"))
    for line in display.wrap(f"Auto: {mode}", subsequent="  "):
        print(dimmed(line))
    print()
    for num, entry in entries.items():
        header = display.AccountBlock(
            number=num, email=switcher.account_email(num), alias=switcher.alias_for(num),
            active=num == current,
        )
        print(display.header_line(header))
        status = entry.last_good.get("resets") if isinstance(entry.last_good, dict) else None
        if num in set_aside:
            print(f"     {dimmed('└')} {yellowed(display.NEEDS_RELOGIN)} "
                  f"{dimmed('(' + display.set_aside_reason(set_aside[num]) + ')')}")
            continue
        if status is None:
            if switcher.account_kind_for(num) == "api_key" or entry.sentinel == "api key":
                note = f"API key; no {noun}s"
            elif entry.last_error == "invalid_grant" or entry.sentinel == "re-login needed":
                note = display.NEEDS_RELOGIN
            elif entry.last_error:
                note = f"{noun}s could not be read ({display.error_label(entry.last_error)})"
            elif entry.sentinel:
                note = (display.NEEDS_RELOGIN if entry.sentinel == "re-login needed"
                        else display.error_label(entry.sentinel))
            else:
                note = f"no {noun}s reported"
            print(f"     {dimmed('└')} {dimmed(note)}")
            continue
        grants = resets.unused_grants(status, now)
        if not status.get("eligible") or not grants:
            reason = status.get("ineligible_reason")
            note = (f"not eligible ({reason})" if not status.get("eligible") and reason
                    else f"no {noun}s held")
            print(f"     {dimmed('└')} {dimmed(note)}")
            continue
        # Grouped by kind, narrowest first; each says what it clears.
        grants = sorted(enumerate(grants), key=lambda item: (
            resets.SCOPE_BREADTH[resets.grant_scope(item[1])], resets.grant_kind(item[1]), item[0],
        ))
        grants = [g for _, g in grants]
        for j, g in enumerate(grants):
            last = j == len(grants) - 1
            branch = dimmed("└" if last else "├")
            cont = " " if last else dimmed("│")
            n = g["resets_left"]
            what = (f"{n} reset credit{'' if n == 1 else 's'}" if codex
                    else f"{n} {resets._count_label(g)} reset{'' if n == 1 else 's'}")
            first = f"{what} · {resets.clears_phrase(g)}"
            if g["id"] == status.get("next_grant_id"):
                first += " · next"
            facts = [display.grant_state(resets.grant_unusable_reason(g, now))]
            end = resets.use_by(resets.parse_ts(g.get("ends_at")))
            if end:
                facts.append(f"use by {end}")
            facts.append(f"id {g['id']}")
            print(f"     {branch} {muted(display.fit(first, 72))}")
            print(f"     {cont} {dimmed(display.fit(' · '.join(facts), 72))}")
    print()
    if rs.auto != "off":
        if plan is not None:
            what = "a reset credit" if codex else f"a {plan.noun}"
            when = ""
            if plan.hours_left:
                when = f", {display.age_text(plan.hours_left * 3600)} before it refills on its own"
            for line in display.wrap(
                f"Auto would now use {what} on {switcher.label(plan.account, long=False)} "
                f"({plan.window} at {plan.pct:.0f}%{when})."
            ):
                print(accent(line))
        else:
            print(dimmed(f"Auto would not use a {noun} right now."))
            if kept is not None:
                for line in display.wrap(resets.kept_note(kept)):
                    print(dimmed(line))
        print(dimmed(
            f"Spent automatically in the last 24h: {used_today} of {MAX_AUTO_RESETS_PER_DAY}."
        ))


def _usage_date(value: str):
    """argparse type for YYYY-MM-DD (a local date)."""
    from datetime import date

    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value!r}")


def _usage_command(argv: list[str]) -> None:
    """Handle `qswap usage [--days N | --since DATE [--until DATE]] [--by ...]`.

    Pre-dispatched before the main parser, like `reset`. Reads the session
    files Claude Code and Codex write, brings the ledger up to date, and
    reports tokens and an estimated API cost. No network calls.
    """
    from ai_quota_swap.ledger.report import GROUPINGS, PROVIDERS

    parser = ArgumentParser(
        prog=f"{_prog_name()} usage",
        description=(
            "Tokens used and their estimated cost at API prices, read from the "
            "session files Claude Code and Codex write. Nothing is sent anywhere; "
            "subscription usage is not billed per token."
        ),
        epilog="""examples:
  qswap usage                        # the last 7 days, one row per day
  qswap usage --days 30 --by model
  qswap usage --by account --json

docs/usage.md explains the numbers. --billed adds what Anthropic billed a
registered organization (qswap api org add); it is the only network use.
""",
    )
    span = parser.add_mutually_exclusive_group()
    span.add_argument(
        "--days", type=int, metavar="N", help="the last N days, today included (default 7)"
    )
    span.add_argument(
        "--since", type=_usage_date, metavar="DATE", help="first day (YYYY-MM-DD, local time)"
    )
    parser.add_argument(
        "--until", type=_usage_date, metavar="DATE",
        help="last day, included (with --since; default today)",
    )
    parser.add_argument(
        "--by", choices=(*GROUPINGS, "key", "workspace"), default="day", metavar="GROUP",
        help="rows by day (default), model, account, project, provider or "
             "session; key and workspace need --billed",
    )
    parser.add_argument("--provider", choices=PROVIDERS, help="only this CLI's usage")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (docs/json-output.md)")
    parser.add_argument(
        "--billed", action="store_true",
        help="add each registered organization's billed usage (Admin API)",
    )
    scan = parser.add_mutually_exclusive_group()
    scan.add_argument(
        "--no-scan", action="store_true",
        help="report from what was read before, without reading new files",
    )
    scan.add_argument(
        "--rescan", action="store_true",
        help="read every session file again from scratch (slow)",
    )
    scan.add_argument(
        "--all", action="store_true", dest="scan_all",
        help="read every session file, not only those the dates need",
    )
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    if args.until is not None and args.since is None:
        parser.error("--until needs --since")
    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    if args.by in ("key", "workspace") and not args.billed:
        parser.error(f"--by {args.by} needs --billed (the local ledger has no {args.by}s)")
    # The ledger's own grouping for the Admin API's key/workspace views.
    ledger_by = {"key": "account", "workspace": "day"}.get(args.by, args.by)

    import sqlite3

    from ai_quota_swap.ledger.report import render_text, report_to_json
    from ai_quota_swap.ledger.usage import resolve_range, run_usage

    since, until = resolve_range(days=args.days, since=args.since, until=args.until)
    if until < since:
        parser.error("--until is before --since")

    def fail(kind: str, message: str) -> None:
        if args.json:
            from ai_quota_swap.json_output import SCHEMA_VERSION

            print(json.dumps(
                {"schemaVersion": SCHEMA_VERSION, "error": {"type": kind, "message": message}},
                indent=2,
            ))
        else:
            error(f"Error: {message}")
        sys.exit(1)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        # Note the live login first, so usage from a login made outside
        # qswap since its last run is attributed from now on. Codex's too,
        # when any Codex account is managed (a file check otherwise).
        switcher.observe_activation()
        from ai_quota_swap import codex_cli

        codex_cli.observe_codex_login()
        try:
            registry = switcher._get_sequence_data()
        except ProviderError:
            registry = None  # labels fall back to emails; the report still runs
        result = run_usage(
            switcher.backup_dir,
            since=since,
            until=until,
            by=ledger_by,
            provider=args.provider,
            scan=not args.no_scan,
            rescan=args.rescan,
            scan_all=args.scan_all,
            registry=registry,
            progress=not args.json,
        )
    except ProviderError as e:
        fail(type(e).__name__, str(e))
        return
    except (sqlite3.Error, OSError) as e:
        fail(type(e).__name__, f"the usage ledger could not be read or written: {e}")
        return
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Scan interrupted. What was read so far is saved; run qswap usage again to continue.')}",
            file=sys.stderr,
        )
        sys.exit(130)

    billed = None
    if args.billed:
        from ai_quota_swap import api_cli

        # Never raises for API trouble: each organization carries its error.
        billed = api_cli.billed_report(
            switcher.backup_dir, since=since, until=until, by=args.by,
            platform=switcher.platform, registry_data=registry,
        )

    if args.json:
        payload = report_to_json(
            result.report, scan=result.scan.to_json() if result.scan else None
        )
        if result.notes:
            payload["notes"] = result.notes
        if billed is not None:
            payload["billed"] = api_cli.billed_to_json(billed)
        print(json.dumps(payload, indent=2))
        return
    print(render_text(result.report, notes=result.notes, last_line=result.older_history))
    if billed is not None:
        print()
        print(api_cli.render_billed(billed))


_CODEX_AUTO_DESCRIPTION = (
    "Switch Codex accounts when the active one runs out, and spend reset "
    "credits under resets.auto. A switch restarts Codex's background server, "
    "which interrupts work in progress, so the default line is 99% "
    "(codex.threshold)."
)

_AUTO_DESCRIPTION = (
    "Switch accounts before the active one runs out. Runs until Ctrl-C; with "
    "--once it checks once and exits 0 switched, 1 error, 2 nothing to do, "
    "3 blocked (for cron)."
)

_AUTO_EPILOG = """examples:
  qswap auto                       # switch at 90% used (autoswitch.threshold)
  qswap auto --once; echo $?       # one check; the outcome is the exit code
  qswap auto --dry-run --verbose   # say what it would do, switch nothing

Defaults are settings (qswap config); flags override them for this run.
"""

_CODEX_AUTO_EPILOG = """examples:
  qswap codex auto                 # switch at 99% used (codex.threshold)
  qswap codex auto --once; echo $? # 0 switched, 1 error, 2 nothing, 3 blocked
  qswap codex auto --dry-run       # say what it would do, switch nothing

A saved login whose token expired (usage unknown until used) is never picked;
switch to it by hand and Codex renews it.
"""


def _auto_command(argv: list[str]) -> None:
    """Handle `qswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built, like `run` (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted). Usage errors exit 64.

    Under the codex prefix (`qswap codex auto`) the same engine runs over the
    Codex switcher, with Codex's own threshold (`codex.threshold`) and its own
    wording; everything else is shared.
    """
    import signal
    import time as _time

    codex = _provider.get() == "codex"
    parser = ArgumentParser(
        prog="qswap codex auto" if codex else "qswap auto",
        description=_CODEX_AUTO_DESCRIPTION if codex else _AUTO_DESCRIPTION,
        epilog=_CODEX_AUTO_EPILOG if codex else _AUTO_EPILOG,
    )
    parser.add_argument("--once", action="store_true",
                        help="check once, maybe switch, and exit (exit code = outcome)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what it would do; never switch or spend a reset")
    parser.add_argument("--verbose", action="store_true",
                        help="show every account on every check, not only when it matters")
    parser.add_argument("--json", action="store_true",
                        help="one JSON event per line (docs/json-output.md)")
    parser.add_argument("--interval", type=float, metavar="SECONDS",
                        help="seconds between checks (at least 15; default 60)")
    parser.add_argument(
        "--threshold", type=float, metavar="PCT",
        help="switch line for the 5h window, or every window without --weekly-threshold "
             "(50-99.9; default " + ("codex.threshold, 99)" if codex else "90)"),
    )
    parser.add_argument(
        "--weekly-threshold", type=float, metavar="PCT",
        help="switch line for the 7d and per-model windows (50-99.9)",
    )
    parser.add_argument("--cooldown", type=float, metavar="SECONDS",
                        help="least time between switches before a limit (default 300)")
    parser.add_argument(
        "--model", metavar="NAMES",
        help=("also switch when one of these limits is reached: names as qswap codex "
              "list shows them, comma-separated, or 'all'") if codex else (
            "also switch on these per-model limits (Fable, Opus, ...; "
            "comma-separated, or 'all')"
        ),
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        # Codex manages ChatGPT logins only; accepted there, not advertised.
        help=argparse.SUPPRESS if codex else (
            "allow switching onto API-key accounts as a last resort (they bill "
            "per token)"
        ),
    )
    parser.add_argument(
        "--strategy", choices=AUTOSWITCH_STRATEGIES, default=None, metavar="NAME",
        help="best: the account with the most quota left (default); weekly-first: "
             "at the line, the one whose weekly window resets soonest; "
             "consume-first: use up the soonest-resetting account first; "
             "priority-consume-first: qswap priority's order first",
    )
    parser.add_argument("--debug", action="store_true", help="log debug detail")
    args = parser.parse_args(argv)

    from ai_quota_swap.autoswitch import (
        AutoSwitchEngine,
        AutoSwitchEvent,
        pct_label,
        write_heartbeat,
    )
    from ai_quota_swap.settings import load_engine_settings, merged_with_cli

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        print(json.dumps(event.to_json()), flush=True)

    human_log = None
    if not args.json:
        from ai_quota_swap.autolog import AutoLog

        human_log = AutoLog(
            lambda line: print(line, flush=True),
            verbose=args.verbose,
            provider="codex" if codex else "claude",
        )

    try:
        switcher = _switcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if codex:
            # One clear line instead of an engine with nothing to watch.
            switcher.require_accounts()
        settings = merged_with_cli(
            load_engine_settings(_settings_root(switcher), _provider.get()), args
        )
        if not args.json:
            # Out-of-range flags are clamped (settings._clamped); say so rather
            # than run silently on another value.
            for flag, given, used in (
                ("--threshold", args.threshold, settings.threshold),
                ("--weekly-threshold", args.weekly_threshold, settings.weekly_threshold),
                ("--interval", args.interval, settings.interval_seconds),
                ("--cooldown", args.cooldown, settings.cooldown_seconds),
            ):
                if given is not None and used is not None and given != used:
                    unit = "s" if flag in ("--interval", "--cooldown") else "%"
                    warning(
                        f"Warning: {flag} {given:g} is out of range; using "
                        f"{pct_label(used)}{unit}"
                    )
        engine = AutoSwitchEngine(
            switcher,
            settings,
            jsonl_emit if args.json else human_log,
            dry_run=args.dry_run,
        )

        if args.once:
            outcome = engine.tick()
            if human_log is not None:
                human_log.flush()
            write_heartbeat(
                switcher.backup_dir, mode="once", dry_run=args.dry_run,
                last_tick_at=_time.time(), next_tick_at=None,
            )
            sys.exit(outcome.value)

        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        if human_log is not None:
            from ai_quota_swap.autolog import banner

            print(dimmed(banner(settings, dry_run=args.dry_run,
                                provider="codex" if codex else "claude")))
        code = engine.run_loop()
        if human_log is not None:
            human_log.flush()
        sys.exit(code)
    except ProviderError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        if human_log is not None:
            human_log.flush()
        print(
            f"\n{dimmed('Auto-switch stopped.')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)


def _config_command(argv: list[str]) -> None:
    """Handle `qswap config [list|get KEY|set KEY VALUE|unset KEY|path]`.

    Pre-dispatched before the main parser is built, like `run` and `auto`
    (same limitation: `config` must be the first argument). Edits
    settings.json in the backup root with strict validation — unlike loading,
    which forgivingly clamps — so a typo'd key or out-of-range value errors
    loudly here instead of silently degrading at `qswap auto` time.
    """
    from ai_quota_swap.settings import (
        SETTING_SPECS,
        effective_settings,
        format_setting_value,
        set_setting,
        setting_spec,
        settings_path,
        unset_setting,
    )

    import textwrap

    key_lines = textwrap.fill(
        ", ".join(SETTING_SPECS), width=76, initial_indent="  ", subsequent_indent="  ",
        break_on_hyphens=False,
    )
    parser = ArgumentParser(
        prog="qswap config",
        description=(
            "Show or change qswap's settings (settings.json in the data "
            "directory). Bare qswap config lists every setting and its value."
        ),
        epilog=f"""examples:
  qswap config                              # every setting and its value
  qswap config set autoswitch.threshold 85
  qswap config unset autoswitch.threshold   # back to the default

settings (README, Configuration, describes each):
{key_lines}
""",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output, with list or get (docs/json-output.md)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log debug detail",
    )
    sub = parser.add_subparsers(dest="action", metavar="{list,get,set,unset,path}")

    p_list = sub.add_parser(
        "list", help="every setting and its value (the default)",
        description="Every setting and its value; (default) marks one not set.",
    )
    p_get = sub.add_parser(
        "get", help="one setting's value",
        description="Print one setting's value.",
        epilog="examples:\n  qswap config get autoswitch.threshold\n",
    )
    p_get.add_argument("key", metavar="KEY", help="a setting, such as autoswitch.threshold")
    for p in (p_list, p_get):
        # SUPPRESS: without it the subparser's False default would clobber a
        # pre-verb `qswap config --json` in the shared namespace.
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="machine-readable output",
        )
    p_set = sub.add_parser(
        "set", help="change one setting",
        description="Check and save one setting.",
        epilog="examples:\n  qswap config set autoswitch.threshold 85\n",
    )
    p_set.add_argument("key", metavar="KEY", help="a setting, such as autoswitch.threshold")
    p_set.add_argument("value", metavar="VALUE", help="its new value")
    p_unset = sub.add_parser(
        "unset", help="return one setting to its default",
        description="Remove one setting, so its default applies again.",
        epilog="examples:\n  qswap config unset autoswitch.threshold\n",
    )
    p_unset.add_argument("key", metavar="KEY", help="a setting")
    sub.add_parser(
        "path", help="where settings.json is",
        description="Print where settings.json is (in the data directory).",
    )

    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    action = args.action or "list"
    if json_mode and action not in ("list", "get"):
        parser.error("--json can only be used with list or get")

    try:
        switcher = _switcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)
        root = switcher.backup_dir

        if action == "path":
            print(settings_path(root))
        elif action == "list":
            rows = effective_settings(root)
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "path": str(settings_path(root)),
                    "settings": [
                        {"key": spec.dotted, "value": value, "isSet": is_set}
                        for spec, value, is_set in rows
                    ],
                }
                print(json.dumps(payload, indent=2))
            else:
                key_w = max(len(spec.dotted) for spec, _, _ in rows)
                val_w = max(len(format_setting_value(v)) for _, v, _ in rows)
                for spec, value, is_set in rows:
                    line = f"{spec.dotted:<{key_w}}  {format_setting_value(value):<{val_w}}"
                    print(line if is_set else f"{line}  {dimmed('(default)')}")
        elif action == "get":
            spec = setting_spec(args.key)
            value, is_set = next(
                (v, s) for sp, v, s in effective_settings(root) if sp is spec
            )
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "key": spec.dotted,
                    "value": value,
                    "isSet": is_set,
                }
                print(json.dumps(payload, indent=2))
            else:
                print(format_setting_value(value))
        elif action == "set":
            value = set_setting(root, args.key, args.value)
            print(f"{args.key} = {format_setting_value(value)}")
        elif action == "unset":
            if unset_setting(root, args.key):
                default = setting_spec(args.key).default
                print(f"{args.key} unset (default: {format_setting_value(default)})")
            else:
                print(muted(f"{args.key} is not set; it already uses its default"), file=sys.stderr)
    except ProviderError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)


def _use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the CLI over a TLS-trust nicety.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def _menubar_settings_path():
    from ai_quota_swap.paths import get_backup_root

    return get_backup_root() / "menubar_settings.json"


def _set_menubar_auto_switch(enabled: bool) -> bool:
    """Set the menu bar's auto-switch toggle and return its previous value."""
    from ai_quota_swap.menubar import MenuBarSettings

    path = _menubar_settings_path()
    settings = MenuBarSettings.load(path)
    previous = settings.auto_switch_enabled
    settings.auto_switch_enabled = enabled
    settings.save(path)
    return previous


def _print_menubar_auto_switch(*, suggest_flag: bool) -> None:
    """Say whether the menu bar will switch accounts or only show usage.

    The toggle defaults to off and lives only in the menu, so a service
    installed from a terminal can run for weeks as a display while its owner
    believes it is switching (it only shows when an account hits its limit).
    """
    from ai_quota_swap.menubar import MenuBarSettings

    if MenuBarSettings.load(_menubar_settings_path()).auto_switch_enabled:
        print("  auto-switch: on")
        return
    hint = (
        "turn it on from the menu, or re-run with --auto-switch"
        if suggest_flag
        else "turn it on from the menu, or run `qswap menubar --auto-switch`"
    )
    warning(
        f"  auto-switch: off -- the menu bar shows usage but will not switch "
        f"accounts; {hint}",
        file=sys.stderr,
    )


def _menubar_extra_missing() -> str | None:
    """Why the menu bar cannot run here: the optional extra is not installed.

    Checked before anything touches launchd. A worker that exits on ``import
    rumps`` would give launchd a service that crash-loops at every login, which
    is strictly worse than the import error a foreground run shows.
    """
    import importlib.util

    if importlib.util.find_spec("rumps") is not None:
        return None
    from ai_quota_swap.update_check import MENUBAR_INSTALL_SPEC, _detect_install_method

    method = _detect_install_method()
    if method == "uv":
        remedy = f"uv tool install --force {MENUBAR_INSTALL_SPEC}"
    elif method == "pipx":
        remedy = f"pipx install --force {MENUBAR_INSTALL_SPEC}"
    else:
        remedy = f"pip install {MENUBAR_INSTALL_SPEC}"
    return f"The menu bar needs the 'menubar' extra. Install it with: {remedy}"


def _menubar_foreground(switcher, service_pid: int | None, *, worker: bool) -> int:
    """Run the menu bar in this process: launchd's job, or ``--foreground`` from a shell.

    A second instance beside the service would draw a second icon over the same
    accounts, so a shell run refuses while the service has a pid. launchd's own
    job is that pid, which is what ``worker`` says.
    """
    if not worker and service_pid is not None:
        error(
            f"The menu bar is already running as a service (pid {service_pid}). "
            "Quit it from the menu, or run 'qswap menubar --uninstall-service', "
            "before running it in the foreground."
        )
        return 1
    # menubar is import-safe without the extra; a missing rumps surfaces from
    # run() as a ClaudeSwitchError with the install hint.
    from ai_quota_swap.menubar import run as menubar_run

    return menubar_run(switcher)


def _menubar_start(args) -> int:
    """Handle plain ``qswap menubar``: make the service run, say what happened, return.

    First setup registers the menu bar at login and says so, since a command
    that quietly arranged to run at every login would be a surprise. Later runs
    only make sure the service is up: a quit job is kickstarted, a plist pinning
    an old program is repaired, a running one is left alone and reported.
    """
    from ai_quota_swap import launch_agent
    from ai_quota_swap.menubar import framework_build_warning

    missing = _menubar_extra_missing()
    if missing:
        error(missing)
        return 1
    unsupported = framework_build_warning()
    # Before ensure(): the menu bar reads its toggle once at launch, so a
    # changed value needs the write first and a running service restarted.
    previous = None
    if args.auto_switch is not None:
        previous = _set_menubar_auto_switch(args.auto_switch)
    toggled = previous is not None and previous != args.auto_switch
    try:
        result = launch_agent.ensure(restart=toggled)
    except BaseException:
        # Don't leave the toggle changed with no service behind it.
        if toggled:
            _set_menubar_auto_switch(previous)
        raise

    pid = f" (pid {result['pid']})" if result["pid"] else ""
    action = result["action"]
    if action == "already-running":
        print(f"Menu bar is already running{pid}.")
    elif action == "first-setup":
        print(f"Menu bar started{pid}.")
        print(
            dimmed(
                "  It keeps running after you close this terminal and starts at "
                "login. Settings → Start at login in the menu turns that off; "
                "qswap menubar --uninstall-service removes it."
            )
        )
        print(f"  plist: {result['plist']}")
        print(f"  logs:  {result['stderr_log']}")
    elif action == "restarted":
        why = (
            "to apply the auto-switch setting"
            if toggled
            else "so the service runs the current build"
        )
        print(f"Menu bar restarted{pid} {why}.")
    else:
        print(f"Menu bar started{pid}.")
        if not result["start_at_login"]:
            print(dimmed("  Start at login is off, so this lasts until you log out."))
    if result["switched_off"]:
        # macOS allowed this start; the switch only governs the start at login.
        warning(
            f"  Switched off under {launch_agent.SYSTEM_SETTINGS_HINT}: it runs "
            "now because you asked, but macOS will not start it at login until "
            "you turn that back on.",
            file=sys.stderr,
        )
    _print_menubar_auto_switch(suggest_flag=True)
    if unsupported:
        # Said here too, not only at launch: the service survives reboots and
        # shows nothing, which is the worst version of the bug.
        warning(unsupported, file=sys.stderr)
    return 0


def _menubar_service(args) -> int:
    """Handle ``menubar --install-service|--uninstall-service|--service-status``.

    Split out of the dispatch chain because these three share one import and
    one output shape, and because the menu bar branch below them is a
    non-returning call — folding the service paths inline would leave the
    reader tracing which branches fall through to launching the app.
    """
    from ai_quota_swap import launch_agent

    if args.install_service:
        # Installing a service for a menu bar that this interpreter cannot draw
        # is the worst version of the bug: it survives reboots and shows
        # nothing. Say so here too, not only when the menu bar is launched.
        from ai_quota_swap.menubar import framework_build_warning

        missing = _menubar_extra_missing()
        if missing:
            error(missing)
            return 1
        unsupported = framework_build_warning()
        # Before install(): it restarts a running service, and the menu bar
        # reads its toggle once at launch.
        previous = None
        if args.auto_switch is not None:
            previous = _set_menubar_auto_switch(args.auto_switch)
        try:
            result = launch_agent.install()
        except BaseException:
            # Don't leave the toggle changed with no service behind it.
            if previous is not None and previous != args.auto_switch:
                _set_menubar_auto_switch(previous)
            raise
        print(f"Menu bar service installed ({result['label']}).")
        print(f"  plist: {result['plist']}")
        print(f"  logs:  {result['stderr_log']}")
        _print_menubar_auto_switch(suggest_flag=True)
        for key, old, new in result["profile_changes"]:
            warning(
                f"  now follows {key}={new or '(unset)'} (was {old or '(unset)'}); "
                "the service manages that profile from now on",
                file=sys.stderr,
            )
        print(
            dimmed(
                "It starts at login from now on; plain `qswap menubar` is enough "
                "to start it again. Re-run this after a qswap upgrade to point "
                "launchd at the new build."
            )
        )
        if unsupported:
            # The hint printed above is about upgrades. A reinstall does not
            # restart the service that is already running, so say that here.
            warning(
                unsupported + "\n  Then run: qswap menubar --install-service",
                file=sys.stderr,
            )
        return 0

    if args.uninstall_service:
        result = launch_agent.uninstall()
        if result["was_loaded"] or result["removed"]:
            print("Menu bar service removed.")
            for path in result["removed"]:
                print(dimmed(f"  removed {path}"))
            if result["cleared_override"]:
                print(dimmed("  cleared its switch under Login Items & Extensions"))
        else:
            print("Menu bar service was not installed.")
        if result["bundle_error"]:
            warning(
                f"  the wrapper app could not be removed: {result['bundle_error']}",
                file=sys.stderr,
            )
        return 0

    result = launch_agent.status()
    if not result["configured"] and not result["loaded"]:
        print("Menu bar service is not installed.")
        print(dimmed("Start it with: qswap menubar"))
        return 0
    state = result["state"] or ("loaded" if result["loaded"] else "stopped")
    pid = f" (pid {result['pid']})" if result["pid"] else ""
    print(f"Menu bar service: {state}{pid}")
    print(f"  starts at login: {'yes' if result['start_at_login'] else 'no'}")
    plist = result["plist"] if result["start_at_login"] else result["session_plist"]
    print(f"  plist: {plist or result['plist']}")
    if result["running_build"]:
        print(f"  build: {result['running_build']}")
    if result["stale_build"]:
        warning(
            f"  installed build is {result['installed_build']}; plain "
            "`qswap menubar` restarts it onto that",
            file=sys.stderr,
        )
    _print_menubar_auto_switch(suggest_flag=False)
    if result["switched_off"]:
        warning(
            f"  switched off under {launch_agent.SYSTEM_SETTINGS_HINT}; "
            "macOS will not start it at login until you turn that back on",
            file=sys.stderr,
        )
    if not result["configured"]:
        print(dimmed("launchd still has it loaded, but the plist is gone."))
    return 0


def _after_add(switcher) -> None:
    """After the first account is added: how to add a second one."""
    from ai_quota_swap import display

    try:
        count = switcher.managed_count()
    except Exception:
        return
    if count == 1:
        print(dimmed(display.ANOTHER_ACCOUNT_STEPS["claude"]))


def _check_switch_target(switcher, identifier: str, *, anyway: bool) -> list[str]:
    """Resolve ``identifier`` and run the switcher's pre-switch check; an
    identifier that does not resolve is left for ``switch_to`` to report.
    ``anyway`` (``--anyway``) only overrides the needs-re-login refusal;
    ``--force`` is passed to ``switch_to`` alone and keeps its meaning."""
    try:
        number, _email, _org = switcher.resolve_account(identifier)
        if number == switcher.current_account_number():
            return []
    except (ProviderError, TypeError, ValueError):
        return []
    notes = switcher.check_switch_target(number, anyway=anyway)
    return list(notes) if isinstance(notes, list) else []


# -- list and status across both providers ----------------------------------------


def _first_run(switcher) -> None:
    """Nothing managed by either provider: why, and the next step for each
    login this machine has. Read-only; never asks."""

    from ai_quota_swap import codex_cli, display

    live = switcher.live_login_email()
    from ai_quota_swap import clis

    found = None if live else clis.cli_found("claude")
    for line in display.nothing_managed_lines("claude", live_email=live, cli_found=found):
        print(line)
    codex_live = codex_cli.live_codex_email()
    if codex_live:
        for line in display.nothing_managed_lines("codex", live_email=codex_live, sentence=False):
            print(line)


def _list_accounts(switcher, *, token_status: bool, as_json: bool):
    """``qswap list``: Claude Code's accounts, then Codex's when any are
    managed (text), or the payload with ``providers.codex`` (JSON)."""
    from ai_quota_swap import codex_cli, display

    claude_n = switcher.managed_count()
    codex_n = codex_cli.codex_managed_count()
    if as_json:
        payload = switcher.list_accounts(show_token_status=False, json_output=True)
        return codex_cli.extend_unified_list(payload, json_output=True)
    if not claude_n and not codex_n:
        _first_run(switcher)
        return None
    if claude_n:
        switcher.list_accounts(show_token_status=token_status, json_output=False)
    else:
        print(bolded(display.provider_heading("claude", both=True)))
        print("  No Claude Code accounts are managed yet.")
        for line in display.nothing_managed_lines(
            "claude", live_email=switcher.live_login_email(), sentence=False,
        ):
            print(f"  {line}")
    codex_cli.extend_unified_list(None, json_output=False)
    if not codex_n:
        email = codex_cli.live_codex_email()
        if email:
            print()
            print(dimmed(f"Codex is logged in as {email} and not managed; add it with: qswap codex add"))
    return None


def _auto_switch_state() -> dict:
    """What the engines' heartbeats say: ``{"running": bool, "providers":
    [...], "dryRun": bool, "lastTickAt": float|None}``."""
    import time as _time

    from ai_quota_swap.autoswitch import engine_running, read_heartbeat
    from ai_quota_swap.paths import get_codex_data_dir

    now = _time.time()
    running: list[str] = []
    dry = False
    last: float | None = None
    for provider, root in (("claude", paths.get_backup_root()), ("codex", get_codex_data_dir())):
        try:
            beat = read_heartbeat(root)
        except Exception:
            beat = None
        if beat is None:
            continue
        tick = beat.get("lastTickAt")
        if isinstance(tick, (int, float)) and (last is None or tick > last):
            last = float(tick)
        if engine_running(beat, now):
            running.append(provider)
            dry = dry or bool(beat.get("dryRun"))
    return {"running": bool(running), "providers": running, "dryRun": dry, "lastTickAt": last}


def _auto_switch_line(state: dict) -> str:
    """One line for ``qswap status``: whether auto-switch is running."""
    import time as _time

    from ai_quota_swap import display

    if not state["running"]:
        return "Auto-switch is not running. Start it with: qswap auto"
    names = " and ".join(display.provider_name(p) for p in state["providers"])
    mode = " in dry-run mode (it switches nothing)" if state["dryRun"] else ""
    when = ""
    if state["lastTickAt"] is not None:
        when = f"; last check {display.ago(_time.time() - state['lastTickAt'])}"
    return f"Auto-switch is running for {names}{mode}{when}."


def _status(switcher, *, as_json: bool):
    """``qswap status``: both providers' active accounts in a few lines,
    how many accounts each manages, and whether auto-switch is running."""
    from ai_quota_swap import codex_cli, display

    claude_n = switcher.managed_count()
    codex_n = codex_cli.codex_managed_count()
    codex_live = codex_cli.live_codex_email()
    if as_json:
        payload = switcher.status(json_output=True)
        if isinstance(payload, dict):
            extra: dict = {}
            if codex_n or codex_live:
                try:
                    from ai_quota_swap.codex.switcher import CodexSwitcher

                    codex = CodexSwitcher(configure_logging=False)
                    codex.check_storage()
                    section = codex.status(json_output=True) or {}
                    section.pop("schemaVersion", None)
                    extra["codex"] = section
                except ProviderError as e:
                    extra["codex"] = {"active": None, "error": error_envelope(e)["error"]}
            if extra:
                payload["providers"] = extra
            state = _auto_switch_state()
            payload["autoSwitch"] = {
                "running": state["running"],
                "providers": state["providers"],
                "dryRun": state["dryRun"],
                "lastTickAt": state["lastTickAt"],
            }
        return payload
    if not claude_n and not codex_n:
        _first_run(switcher)
        return None
    for line in switcher.status_lines():
        print(line)
    if codex_n or codex_live:
        try:
            from ai_quota_swap.codex.switcher import CodexSwitcher

            codex = CodexSwitcher(configure_logging=False)
            for line in codex.status_lines():
                print(line)
        except ProviderError as e:
            print(bolded(display.provider_name("codex")))
            print(f"  {display.usage_unavailable(None, retrying=False)}: {e}")
    print()
    counts = [f"{claude_n} Claude Code account{'' if claude_n == 1 else 's'}"]
    if codex_n:
        counts.append(f"{codex_n} Codex account{'' if codex_n == 1 else 's'}")
    verb = "is" if claude_n + codex_n == 1 and len(counts) == 1 else "are"
    print(dimmed(f"{' and '.join(counts)} {verb} managed."))
    print(_auto_switch_line(_auto_switch_state()))
    return None


def main() -> None:
    """Main entry point for the CLI."""
    force_utf8_output()
    _use_native_tls()
    # `qswap codex <verb>`: the prefix goes before the dispatch below, whose
    # pre-dispatched verbs must be argv[0].
    provider_id, argv = providers.split_provider_prefix(sys.argv[1:])
    try:
        providers.ensure_available(provider_id)
    except ProviderError as e:
        # Refused before dispatch, so that verbs which never build a switcher
        # (config, upgrade, menu bar settings) cannot act on Claude Code's data
        # under another provider's prefix.
        if "--json" in argv:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    with selected_provider(provider_id):
        _dispatch(argv)


# Verbs with a parser of their own (pre-dispatched below), and every verb the
# command line knows; anything else is a usage error.
_OWN_PARSER_VERBS = (
    "run", "auto", "config", "map", "unmap", "unclaimed", "alias", "threshold",
    "priority", "api", "budget", "reset", "resets", "usage", "swap", "move", "doctor",
)
# Options of the legacy interface that take a value (so the value is not
# mistaken for a verb's positional argument).
_VALUE_OPTIONS = ("--account", "--hold", "--slot", "--email", "--alias", "--strategy", "--model")


def _help_and_usage(argv: list[str]) -> list[str] | None:
    """Help, the bare-`qswap` hint, and the usage errors the legacy parser
    would word badly. Returns the argv to dispatch, or None when handled.

    - ``qswap --help`` / ``qswap help`` print the map; ``qswap help VERB`` and
      ``qswap VERB --help`` print that verb's help, for every verb.
    - Bare ``qswap`` off a terminal prints a three-line hint (exit 0).
    - An unknown verb, or a verb missing its value, exits 64 naming the verb,
      never the hidden flag it is rewritten to.
    """
    from ai_quota_swap import helptext
    from ai_quota_swap.argparsing import usage_error

    if not argv:
        if sys.stdout.isatty() and sys.stdin.isatty():
            return argv
        print(helptext.BARE_HINT, end="")
        return None
    verb = argv[0]
    if verb in ("-h", "--help"):
        print(helptext.MAIN_HELP, end="")
        sys.exit(0)
    if verb == "help":
        rest = argv[1:]
        if not rest:
            print(helptext.MAIN_HELP, end="")
            sys.exit(0)
        target = rest[0]
        if target in ("-h", "--help"):
            print(helptext.legacy_help("help"), end="")
            sys.exit(0)
        if target == "codex":
            from ai_quota_swap import codex_cli

            codex_cli.dispatch(["help", *rest[1:]])
            return None
        if target in helptext.LEGACY_VERB_HELP:
            print(helptext.legacy_help(target), end="")
            sys.exit(0)
        if target in _OWN_PARSER_VERBS:
            _dispatch([target, "--help"])
            return None
        usage_error("qswap help", f"unknown command '{target}'; qswap help lists them")
    if verb.startswith("-"):
        if any(a in ("-h", "--help") for a in argv):
            print(helptext.MAIN_HELP, end="")
            sys.exit(0)
        return argv
    if verb in _OWN_PARSER_VERBS:
        return argv
    if verb not in helptext.LEGACY_VERB_HELP and verb not in _SUBCOMMAND_FLAGS:
        usage_error(
            "qswap", f"unknown command '{verb}'; qswap help lists them",
            as_json="--json" in argv,
        )
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(helptext.legacy_help(verb), end="")
        sys.exit(0)
    if verb in helptext.VALUE_VERBS:
        rest = argv[1:]
        # The value may follow other options (`remove --yes 3`): bring the
        # first positional to the front, where the legacy flag expects it.
        position = None
        skip = False
        for i, token in enumerate(rest):
            if skip:
                skip = False
                continue
            if token in _VALUE_OPTIONS:
                skip = True
                continue
            if token == "-" or not token.startswith("-"):
                position = i
                break
        if position is None:
            usage_error(
                f"qswap {verb}", f"{verb} needs {helptext.VALUE_VERBS[verb]}",
                usage=helptext.legacy_usage(verb), as_json="--json" in argv,
            )
        rest = [rest[position], *rest[:position], *rest[position + 1:]]
        return [verb, *rest]
    return argv


def _dispatch(argv: list[str]) -> None:
    """Run one command line whose provider prefix, if any, is already removed."""
    try:
        from ai_quota_swap.appearance import cli_should_probe, cli_theme
        # `run` execs a child that takes over the terminal, and `--json`
        # must stay machine-readable — never probe (and emit the OSC query)
        # in either case.
        probe = cli_should_probe(argv, colors_enabled=printer.colors_enabled())
        name = cli_theme(load_ui_settings(paths.get_backup_root()).theme, colors=probe)
        printer.set_theme(name)
    except Exception:
        pass  # theme is cosmetic; never block the CLI on it

    # `qswap codex <verb>` has its own verbs and parsers; none of the Claude
    # commands below (config, upgrade, menubar, ...) runs under that prefix.
    if _provider.get() == "codex":
        from ai_quota_swap import codex_cli

        codex_cli.dispatch(argv)
        return

    argv = _help_and_usage(argv)
    if argv is None:
        return

    # `run` and `auto` keep their dedicated pre-dispatch parsers.
    if argv and argv[0] == "run":
        _run_command(argv[1:])
        return  # only reachable in tests where exec/exit is mocked
    if argv and argv[0] == "auto":
        _auto_command(argv[1:])
        return  # only reachable in tests where sys.exit is mocked
    if argv and argv[0] == "config":
        _config_command(argv[1:])
        return
    if argv and argv[0] == "map":
        _map_command(argv[1:])
        return
    if argv and argv[0] == "unmap":
        _unmap_command(argv[1:])
        return
    if argv and argv[0] == "unclaimed":
        _unclaimed_command(argv[1:])
        return
    if argv and argv[0] == "alias":
        _alias_command(argv[1:])
        return
    if argv and argv[0] == "threshold":
        _threshold_command(argv[1:])
        return
    if argv and argv[0] == "priority":
        _priority_command(argv[1:])
        return
    if argv and argv[0] in ("api", "budget"):
        from ai_quota_swap import api_cli

        (api_cli.api_command if argv[0] == "api" else api_cli.budget_command)(argv[1:])
        return
    if argv and argv[0] in ("reset", "resets"):
        _reset_command(argv[1:])
        return
    if argv and argv[0] == "usage":
        _usage_command(argv[1:])
        return
    if argv and argv[0] == "swap":
        _swap_command(argv[1:])
        return
    if argv and argv[0] == "move":
        _move_command(argv[1:])
        return
    if argv and argv[0] == "doctor":
        from ai_quota_swap import doctor

        doctor.doctor_command(argv[1:])
        return

    # Bare `qswap` in an interactive terminal opens the TUI dashboard (like
    # lazygit/k9s); off one it prints a three-line hint (_help_and_usage).
    if not argv:
        argv = ["--tui"]

    # Memorable subcommands (`qswap switch <email>`, `qswap list`, `qswap help`, ...)
    # are rewritten to the equivalent flags so the original `--flag` interface
    # keeps working unchanged.
    argv = _translate_subcommand(argv)

    # Help is the map in helptext (handled before this parser); this parser
    # only parses, and its errors exit 64 (argparsing).
    from ai_quota_swap.argparsing import ArgumentParser as _Parser

    parser = _Parser(
        prog=_prog_name(),
        usage="%(prog)s <command> [options]",
        add_help=False,
    )

    # Version and debug flags (outside mutually exclusive group)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--token-status",
        action="store_true",
        help="Show source-labelled OAuth token diagnostics (use with 'list')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout (use with 'list', 'status', "
            "or 'switch'). See README 'JSON output for scripting'."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        metavar="{best,next-available}",
        help=(
            "With bare 'switch': pick the target by remaining 5h/7d quota. "
            "'best' jumps to the account with the most headroom; "
            "'next-available' rotates to the next account, skipping any at their limit"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "With 'switch --strategy': also count these models' per-model "
            "weekly limits when comparing accounts (comma-separated display "
            "names, or 'all'). Defaults to the autoswitch.model setting"
        ),
    )
    parser.add_argument(
        "--slot",
        type=int,
        metavar="NUM",
        help="Specify slot number when adding account (use with 'add' or 'add-token')",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help=(
            "Email address for the account. Optional with 'add-token'; "
            "defaults to setup-token-{slot}@token.local (or "
            "api-key-{slot}@token.local for API keys) since these tokens "
            "carry no real email metadata."
        ),
    )
    parser.add_argument(
        "--account",
        metavar="NUM|EMAIL",
        help="Limit export to one account (use with 'export')",
    )
    parser.add_argument(
        "--alias",
        metavar="NAME",
        help="Set a short display alias for the account (use with 'add')",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing accounts during import; with 'switch <num|email>', "
            "activate the stored credentials without backing up the current "
            "login first"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include full ~/.claude.json in export (default: oauthAccount only)",
    )
    parser.add_argument(
        "--anyway",
        action="store_true",
        help="With 'switch ACCOUNT': switch even though it needs re-login",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="With 'remove', 'purge', 'add --slot' or 'add-token --slot': do not ask",
    )
    parser.add_argument(
        "--hold",
        type=float,
        metavar="SECONDS",
        help=(
            "With 'import-usage': keep this machine from fetching the "
            "imported accounts for this many seconds (0 lifts an earlier hold)"
        ),
    )
    # `qswap menubar` runs the menu bar as a launchd service (launch_agent.py);
    # these pick another operation on that service, so at most one applies.
    service = parser.add_mutually_exclusive_group()
    service.add_argument(
        "--install-service",
        action="store_true",
        help=(
            "With 'menubar': register the menu bar to start at login and "
            "(re)start it now; plain 'qswap menubar' does this on first use (macOS)"
        ),
    )
    service.add_argument(
        "--uninstall-service",
        action="store_true",
        help="With 'menubar': stop the menu bar and remove its LaunchAgent (macOS)",
    )
    service.add_argument(
        "--service-status",
        action="store_true",
        help=(
            "With 'menubar': report whether the menu bar service is installed, "
            "running and set to start at login"
        ),
    )
    service.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "With 'menubar': run the menu bar attached to this terminal instead "
            "of as the background service"
        ),
    )
    parser.add_argument(
        "--auto-switch",
        dest="auto_switch",
        action="store_const",
        const=True,
        default=None,
        help=(
            "With 'menubar' or 'menubar --install-service': turn the menu bar's "
            "auto-switch on (it defaults to off)"
        ),
    )
    parser.add_argument(
        "--no-auto-switch",
        dest="auto_switch",
        action="store_const",
        const=False,
        help=(
            "With 'menubar' or 'menubar --install-service': turn the menu bar's "
            "auto-switch off"
        ),
    )

    # Legacy `--flag` interface. Still fully supported (bare subcommands rewrite
    # into these, see _translate_subcommand), but hidden from --help so the
    # subcommands shown in the description are the one documented interface.
    # The group is not `required` because the "no command" case is handled
    # explicitly below (a required group with every member suppressed prints a
    # broken empty-list error).
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--add-account",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--remove-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--disable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--enable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--list",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch-to",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--status",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--purge",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--export",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import",
        dest="import_",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import-usage",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--tui",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--watch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--menubar",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--upgrade",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--add-token",
        metavar="TOKEN|-",
        nargs="?",
        const="",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)

    # No action selected: emit a clean, subcommand-oriented message rather than
    # the raw argparse "one of the arguments ... is required" (which would list
    # the now-hidden legacy flags). Value actions can be falsy-but-set
    # (--add-token uses const=""), so test those with `is not None`.
    if not (
        args.add_account
        or args.list
        or args.switch
        or args.status
        or args.purge
        or args.tui
        or args.watch
        or args.menubar
        or args.upgrade
        or args.remove_account is not None
        or args.disable_account is not None
        or args.enable_account is not None
        or args.switch_to is not None
        or args.export is not None
        or args.import_ is not None
        or args.import_usage is not None
        or args.add_token is not None
    ):
        parser.error("no command given; qswap help lists the commands")

    # An empty value satisfies the guard above (the option *is* set) but matches
    # no truthiness-tested branch of the dispatch chain below, so qswap would
    # fall out of it and exit 0 having done nothing: `qswap export "$DEST"` with
    # an unset DEST must not look like a completed backup. Rejected here, ahead
    # of the modifier guards, so the error names the empty value instead of
    # blaming an --account/--force/--full that was used correctly. --add-token is
    # exempt: its const="" means "prompt me for the token".
    for subcommand, metavar, value in (
        ("remove", "NUM|EMAIL", args.remove_account),
        ("disable", "NUM|EMAIL", args.disable_account),
        ("enable", "NUM|EMAIL", args.enable_account),
        ("switch", "NUM|EMAIL", args.switch_to),
        ("export", "PATH", args.export),
        ("import", "PATH", args.import_),
        ("import-usage", "PATH", args.import_usage),
    ):
        if value is not None and not value:
            parser.error(f"'{subcommand}' requires a non-empty {metavar}")

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with 'list'")

    if args.json and not (args.list or args.status or args.switch or args.switch_to):
        parser.error("--json can only be used with 'list', 'status', or 'switch'")

    if args.json and args.token_status:
        # Token status is not part of the JSON v1 schema; reject rather than
        # silently ignore it (a future additive field can add it).
        parser.error("--token-status cannot be combined with --json")

    if args.strategy is not None and not args.switch:
        parser.error("--strategy can only be used with bare 'switch'")

    if args.model is not None and args.strategy is None:
        # Meaningless on a direct-target switch or plain rotation — nothing
        # usage-aware reads it there, so reject loudly rather than ignore.
        parser.error(
            "--model can only be used with 'switch --strategy best' or "
            "'switch --strategy next-available'"
        )

    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with 'add' or 'add-token'")

    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with 'add-token'")

    if args.account is not None and not args.export:
        parser.error("--account can only be used with 'export'")

    if args.alias is not None and not args.add_account:
        parser.error("--alias can only be used with 'add'")

    if args.anyway and not args.switch_to:
        parser.error("--anyway can only be used with 'switch ACCOUNT'")

    if args.force and not (args.import_ or args.switch_to):
        parser.error("--force can only be used with 'import' or 'switch ACCOUNT'")

    if args.full and not args.export:
        parser.error("--full can only be used with 'export'")

    if args.yes and not (
        args.remove_account or args.purge or args.add_account or args.add_token is not None
    ):
        parser.error("--yes can only be used with 'remove', 'purge', 'add' or 'add-token'")

    if args.hold is not None and args.import_usage is None:
        parser.error("--hold can only be used with 'import-usage'")

    if args.hold is not None and not (math.isfinite(args.hold) and args.hold >= 0):
        parser.error("--hold must be a non-negative number of seconds")

    if (
        args.install_service
        or args.uninstall_service
        or args.service_status
        or args.foreground
    ) and not args.menubar:
        parser.error(
            "--install-service, --uninstall-service, --service-status and "
            "--foreground can only be used with 'menubar'"
        )

    if args.auto_switch is not None and (
        not args.menubar
        or args.uninstall_service
        or args.service_status
        or args.foreground
    ):
        parser.error(
            "--auto-switch/--no-auto-switch can only be used with "
            "'menubar' or 'menubar --install-service'"
        )

    # Self-upgrade runs before switcher init so we don't touch config/keychain
    # just to upgrade the tool itself.
    if args.upgrade:
        from ai_quota_swap.update_check import run_self_upgrade

        try:
            sys.exit(run_self_upgrade())
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            sys.exit(130)

    # Initialize switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision)
    # are presented like every other ProviderError: clean stderr line,
    # exit 1, no traceback.
    # JSON-capable commands return a payload; the CLI is the single point that
    # serializes it (so no command writes JSON to stdout itself).
    payload: dict | None = None
    try:
        switcher = _switcher(debug=args.debug)

        # Check for root (unless in container) - POSIX only
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if args.add_account:
            if args.yes:
                switcher.add_account(slot=args.slot, alias=args.alias, assume_yes=True)
            else:
                switcher.add_account(slot=args.slot, alias=args.alias)
            _after_add(switcher)
        elif args.add_token is not None:
            if args.yes:
                switcher.add_account_from_token(
                    token=args.add_token, email=args.email, slot=args.slot, assume_yes=True,
                )
            else:
                switcher.add_account_from_token(
                    token=args.add_token,
                    email=args.email,
                    slot=args.slot,
                )
        elif args.remove_account:
            if args.yes:
                switcher.remove_account(args.remove_account, assume_yes=True)
            else:
                switcher.remove_account(args.remove_account)
        elif args.disable_account is not None:
            switcher.set_account_disabled(args.disable_account, True)
        elif args.enable_account is not None:
            switcher.set_account_disabled(args.enable_account, False)
        elif args.list:
            payload = _list_accounts(switcher, token_status=args.token_status, as_json=args.json)
        elif args.switch:
            from ai_quota_swap.settings import load_settings, parse_model_names

            # Only the usage-aware strategies read model limits: --model wins;
            # otherwise the persistent autoswitch.model setting applies
            # (announced by switch(), never silently).
            if args.strategy is None:
                models, model_source = (), None
            elif args.model is not None:
                models, model_source = parse_model_names(args.model), "cli"
            else:
                models = parse_model_names(load_settings(switcher.backup_dir).model)
                model_source = "autoswitch.model" if models else None
            payload = switcher.switch(
                strategy=args.strategy,
                json_output=args.json,
                models=models,
                model_source=model_source,
            )
            if payload is not None and models:
                payload["models"] = list(models)
                payload["modelSource"] = model_source
        elif args.switch_to:
            # A target that needs re-login would leave the user on a dead
            # login: refused unless --force. One whose usage cannot be read,
            # or is old, switches with a one-line warning (ux.md). The engine
            # calls switch_to directly and runs its own checks.
            notes = _check_switch_target(switcher, args.switch_to, anyway=args.anyway)
            if not args.json:
                for note in notes:
                    warning(f"Warning: {note}")
            payload = switcher.switch_to(
                args.switch_to, json_output=args.json, force=args.force
            )
            if isinstance(payload, dict) and notes:
                payload["warnings"] = [*notes, *payload.get("warnings", [])]
        elif args.status:
            payload = _status(switcher, as_json=args.json)
        elif args.purge:
            purged = switcher.purge(assume_yes=True) if args.yes else switcher.purge()
            # Cancelling changes nothing: the menu bar stays when purge did not run.
            if purged is not False and sys.platform == "darwin":
                # The menu bar's LaunchAgent, wrapper app and logs live outside
                # the directory purge just removed; "all traces" means them too.
                # After purge() so a refused purge leaves the menu bar alone.
                from ai_quota_swap import launch_agent

                try:
                    gone = launch_agent.uninstall(backup_dir=switcher.backup_dir)
                except ProviderError as e:
                    warning(f"Menu bar service not removed: {e}", file=sys.stderr)
                else:
                    if gone["was_loaded"] or gone["removed"]:
                        print("Removed the menu bar service, its app bundle and logs.")
        elif args.export:
            from ai_quota_swap.transfer import export_accounts

            export_accounts(switcher, args.export, account=args.account, full=args.full)
        elif args.import_:
            from ai_quota_swap.transfer import import_accounts

            import_accounts(switcher, args.import_, force=args.force)
        elif args.import_usage:
            from ai_quota_swap.transfer import import_usage

            import_usage(switcher, args.import_usage, hold_s=args.hold)
        elif args.tui:
            from ai_quota_swap.tui import run as tui_run

            sys.exit(tui_run(switcher))
        elif args.watch:
            from ai_quota_swap.tui import run as tui_run

            sys.exit(tui_run(switcher, start="watch"))
        elif args.menubar:
            if sys.platform != "darwin":
                error("The menu bar is only available on macOS.")
                sys.exit(1)
            if args.install_service or args.uninstall_service or args.service_status:
                sys.exit(_menubar_service(args))
            from ai_quota_swap import launch_agent

            # launchd's own job runs the menu bar here (recognised by pid, so a
            # plist from before the --foreground flag still works); everything
            # else asks launchd to run it and returns.
            service_pid = launch_agent.service_pid()
            worker = service_pid is not None and service_pid == os.getpid()
            if args.foreground or worker:
                sys.exit(_menubar_foreground(switcher, service_pid, worker=worker))
            sys.exit(_menubar_start(args))
    except ProviderError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error envelope
        # there (exit 1) instead of a red stderr line.
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)

    if args.json and payload is not None:
        print(json.dumps(payload, indent=2))

    # Passive update notification (never fails). Skipped after --purge so we
    # don't immediately recreate <backup_root>/cache/update_check.json inside
    # the directory we just deleted. Skipped after --upgrade as a safety guard
    # in case the dispatch is later refactored to fall through. Skipped when
    # `updates.check` is false, so no request goes to PyPI at all.
    if (
        not args.purge
        and not args.upgrade
        and not args.json
        and load_update_settings(paths.get_backup_root()).check
    ):
        from ai_quota_swap.update_check import check_for_update

        msg = check_for_update(__version__)
        if msg:
            print(f"\n{muted(msg)}", file=sys.stderr)


if __name__ == "__main__":
    main()
