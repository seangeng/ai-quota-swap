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

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are "
            "unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  qswap run 2
  qswap run user@example.com
  qswap run 2 --no-share
  qswap run 2 --no-share-history
  qswap run 2 --require-session
  qswap run 2 -- --resume
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to run (number or email). Omit to use the current "
        "directory's mapping (see `qswap map`).",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents/rules "
            "from ~/.claude into the session profile (and remove previously "
            "shared items). Chat history is not affected: see "
            "--no-share-history"
        ),
    )
    parser.add_argument(
        "--share-history",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Share conversation history (projects/ and history.jsonl) from "
            "~/.claude into the session profile, so every account sees one "
            "unified history. This is the default; history the profile "
            "already accumulated is merged into ~/.claude first. "
            "--no-share-history keeps this account's history separate for "
            "this launch, `qswap config set run.shareHistory false` for "
            "every launch. On Windows, projects/ is shared either way and "
            "history.jsonl only when symlinks are allowed (Developer Mode, "
            "or an elevated terminal)."
        ),
    )
    parser.add_argument(
        "--require-session",
        action="store_true",
        help=(
            "Refuse to launch when the account is already the active default "
            "login, instead of running plain claude on that login (which a "
            "later switch could pull out from under the session)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
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
    parser = argparse.ArgumentParser(
        prog="qswap map",
        description=(
            "Map a stored account to a directory so `qswap run` (with no "
            "account) auto-launches it there. With no arguments, lists all "
            "mappings."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  qswap map 2 ~/work/client-app
  qswap map user@example.com          # map the current directory
  qswap map                           # list all mappings
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to map (number or email). Omit to list mappings.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to map (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
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
        if previous and previous.get("email") != email:
            prev_email = previous.get("email")
            print(
                f"{accent('Mapped')} {shown} → Account-{account_num} ({email}) "
                f"{muted(f'(was {prev_email})')}"
            )
        else:
            print(f"{accent('Mapped')} {shown} → Account-{account_num} ({email})")
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unmap_command(argv: list[str]) -> None:
    """Handle `qswap unmap [PATH]` — remove a directory→account mapping."""
    parser = argparse.ArgumentParser(
        prog="qswap unmap",
        description="Remove a directory → account mapping (default: current directory).",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to unmap (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
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
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} unclaimed",
        description=(
            "List stashed credential entries, or purge one by id. "
            "Purging deletes the bytes — recovery is /login + `qswap add`."
        ),
    )
    parser.add_argument(
        "--purge",
        metavar="ID",
        help="Delete this entry's bytes and manifest row",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        entries = switcher.list_unclaimed_credentials()

        if args.purge:
            if args.purge not in entries:
                error(f"Error: no unclaimed entry {args.purge}")
                sys.exit(1)
            switcher._store._remove_unclaimed_credential(args.purge)
            print(f"{accent('Purged')} {args.purge}")
            return

        if not entries:
            print(dimmed("No unclaimed credential entries"))
            return
        for entry_id, meta in sorted(entries.items()):
            slot = meta.get("configSlot") or "?"
            reason = meta.get("reason") or "orphaned (no manifest row)"
            print(f"{entry_id}  slot {slot}  {reason}")
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
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} swap",
        description=(
            "Exchange two accounts' slot numbers, so they trade places in "
            "`qswap list` and as numeric targets. Aliases, backups, and "
            "session history move with their account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  qswap swap 1 2
  qswap swap dev user@example.com
        """,
    )
    parser.add_argument("first", metavar="NUM|EMAIL|ALIAS", help="One account")
    parser.add_argument("second", metavar="NUM|EMAIL|ALIAS", help="The other account")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        num_a, num_b = switcher.swap_accounts(args.first, args.second)
        print(f"{accent('Swapped')} Account {num_a} and Account {num_b}:")
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        for num in sorted((num_a, num_b), key=int):
            email = accounts.get(num, {}).get("email", "")
            print(f"  {num}: {email}")
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
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} move",
        description=(
            "Assign an account to a slot number. An empty slot relocates the "
            "account there and frees its old slot; an occupied slot swaps the "
            "two. Aliases, backups, and session history move with the account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  qswap move user@example.com 1   move an account onto shortcut 1
  qswap move dev 1                by alias
  qswap move 2 1                  by number (swaps if slot 1 is taken)
        """,
    )
    parser.add_argument("account", metavar="NUM|EMAIL|ALIAS", help="Account to move")
    parser.add_argument("slot", metavar="SLOT", help="Destination slot number")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        num_src, num_target, swapped = switcher.move_account(args.account, args.slot)
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        if num_src == num_target:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{dimmed('Already in')} slot {num_target}: {email}")
        elif swapped:
            print(f"{accent('Swapped')} Account {num_src} and Account {num_target}:")
            for num in sorted((num_src, num_target), key=int):
                email = accounts.get(num, {}).get("email", "")
                print(f"  {num}: {email}")
        else:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{accent('Moved')} {email} to slot {num_target}")
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
    parser = argparse.ArgumentParser(
        prog="qswap alias",
        description=(
            "Set, remove, or list a short display alias for an account. "
            "Once set, the alias can be used anywhere an account number or "
            "email is accepted (switch, remove, run, map)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  qswap alias 2 dev
  qswap alias user@example.com dev
  qswap alias 2 --unset
  qswap alias                         # list all aliases
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to alias (number or email). Omit to list aliases.",
    )
    parser.add_argument(
        "alias_name",
        nargs="?",
        metavar="NAME",
        help="Alias to set (letters, digits, ., -, _; not purely numeric).",
    )
    parser.add_argument("--unset", action="store_true", help="Remove the account's alias")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
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
            rows = switcher.list_aliases()
            if not rows:
                print(dimmed("No aliases set"))
                return
            print(bolded("Aliases:"))
            for num, alias_name, email in rows:
                print(f"  {num}: {alias_name} {muted(f'({email})')}")
            return

        if args.unset:
            account_num = switcher.unset_alias(args.account)
            print(f"{accent('Removed alias')} for Account {account_num}")
        else:
            account_num, normalized = switcher.set_alias(args.account, args.alias_name)
            print(f"{accent('Set alias')} '{normalized}' for Account {account_num}")
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


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
    parser = argparse.ArgumentParser(
        prog="qswap threshold",
        description=(
            "Set, remove, or list a per-account switch-away threshold. The "
            "per-account value replaces autoswitch.threshold for that account "
            "only, so one shared account can reserve headroom without capping "
            "the whole fleet."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  qswap threshold 2 85
  qswap threshold user@example.com 99.9
  qswap threshold 2 --unset           # back to the global default
  qswap threshold                     # list the global default and overrides

Valid range: {ACCOUNT_THRESHOLD_MIN:g}-{ACCOUNT_THRESHOLD_MAX:g} (percent used).
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to set (number, email, or alias). Omit to list.",
    )
    parser.add_argument(
        "threshold",
        nargs="?",
        metavar="PCT",
        help=(
            f"Percent used at which to switch away "
            f"({ACCOUNT_THRESHOLD_MIN:g}-{ACCOUNT_THRESHOLD_MAX:g})."
        ),
    )
    parser.add_argument(
        "--unset", action="store_true", help="Remove the account's override"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
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
    print(bolded("Switch-away thresholds:"))
    print(f"  {dimmed('global default')}  {default:g}%")

    rows = sorted(
        (
            (num, policy.threshold)
            for num, policy in switcher.account_policies().items()
            if policy.threshold is not None
        ),
        key=lambda row: (not row[0].isdigit(), int(row[0]) if row[0].isdigit() else 0),
    )
    if not rows:
        print(dimmed("  No per-account overrides set"))
        return
    for num, value in rows:
        print(f"  Account-{num}  {value:g}%")


def _priority_command(argv: list[str]) -> None:
    """Set, clear, or inspect tiers without enabling the strategy implicitly."""
    parser = argparse.ArgumentParser(
        prog="qswap priority",
        description=(
            "Set or list account priority tiers for the optional "
            "priority-consume-first strategy. Higher numbers are used first; "
            "accounts in the same tier are ordered by soonest weekly reset."
        ),
    )
    parser.add_argument("account", nargs="?", metavar="NUM|EMAIL|ALIAS")
    parser.add_argument(
        "priority", nargs="?", metavar="TIER",
        help=f"Integer from 0 to {ACCOUNT_PRIORITY_MAX} (default 0)",
    )
    parser.add_argument("--unset", action="store_true", help="Return to priority 0")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if args.unset and args.priority is not None:
        parser.error("--unset does not take a TIER argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL|ALIAS is required with --unset")
    if args.account is not None and args.priority is None and not args.unset:
        parser.error("TIER is required (or pass --unset)")
    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        if args.account is None:
            print(bolded("Account priorities (higher first; default 0):"))
            for num, policy in switcher.account_policies().items():
                print(f"  Account-{num}  {switcher.account_email(num)}  {policy.priority}")
            print(dimmed("Used only by: qswap auto --strategy priority-consume-first"))
            return
        switcher.set_account_priority(args.account, None if args.unset else args.priority)
    except (ValueError, ProviderError) as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


def _reset_command(argv: list[str]) -> None:
    """Handle `qswap reset [NUM|EMAIL|ALIAS] [--grant ID] [--yes] [--json]`.

    Bare: list every account's usage-reset grants, the auto policy, and what
    the engine would spend right now. With an account: spend one reset there
    (asks first unless --yes). Exit 1 when nothing was spent.
    """
    import time as _time

    parser = argparse.ArgumentParser(
        prog="qswap reset",
        description=(
            "List or use usage resets: banked refills of a usage window that "
            "Anthropic grants eligible plans (Claude Code's /limit-reset). A "
            "reset sets the windows it clears back to 0%; the weekly reset "
            "day stays where it was."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  qswap reset                     # every account's resets + the auto plan
  qswap reset 2                   # use one reset on account 2 (asks first)
  qswap reset 2 --grant launch-2610-fable --yes
  qswap reset --json

Automatic use (qswap auto, the TUI, the menu bar) follows resets.auto:
  qswap config set resets.auto last-resort   # default
  qswap config set resets.auto active        # the account in use, at its limit
  qswap config set resets.auto on-limit
  qswap config set resets.auto off
        """,
    )
    parser.add_argument("account", nargs="?", metavar="NUM|EMAIL|ALIAS")
    parser.add_argument("--grant", metavar="ID", help="Which grant to spend (default: the best usable one)")
    parser.add_argument("-y", "--yes", action="store_true", help="Do not ask before spending")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    from ai_quota_swap import resets
    from ai_quota_swap.autoswitch import (
        STATE_FILENAME,
        _read_state_file,
        record_reset_claim,
        reset_request_id,
    )

    in_flight = False
    try:
        switcher = _switcher(debug=args.debug)
        _guard_root(switcher)
        rs = switcher.reset_settings()
        if not rs.enabled:
            error(
                "Error: usage resets are off — "
                "`qswap config set resets.enabled true` turns them on"
            )
            sys.exit(1)
        if args.account is None:
            _print_resets(switcher, as_json=args.json)
            return

        num, email, _org = switcher.resolve_account(args.account)
        entry = switcher.refresh_account_usage(num)
        last_good = entry.last_good if entry is not None else None
        status = last_good.get("resets") if isinstance(last_good, dict) else None
        now = _time.time()
        if status is None:
            why = entry.last_error if entry is not None and entry.last_error else "no reading"
            error(f"Error: no usage-reset status for Account-{num} ({why})")
            sys.exit(1)
        if not status.get("eligible"):
            reason = status.get("ineligible_reason") or "unknown"
            error(f"Error: Account-{num} has no usage resets (not eligible: {reason})")
            sys.exit(1)
        grants = status.get("grants") or []
        if args.grant:
            grant = next((g for g in grants if g["id"] == args.grant), None)
            if grant is None:
                error(f"Error: Account-{num} has no grant '{args.grant}' (`qswap reset` lists them)")
                sys.exit(1)
        else:
            usable = sorted(
                (g for g in grants if resets.grant_unusable_reason(g, now) is None),
                key=lambda g: (
                    g["id"] != status.get("next_grant_id"),
                    resets.parse_ts(g.get("ends_at")) or math.inf,
                ),
            )
            if not usable:
                error(f"Error: Account-{num} has no reset it can use right now")
                for g in resets.unused_grants(status, now):
                    print(dimmed(
                        f"  {resets.grant_kind(g)} ({g['id']}): "
                        f"{resets.grant_unusable_reason(g, now)}"
                    ))
                sys.exit(1)
            grant = usable[0]
        unusable = resets.grant_unusable_reason(grant, now)
        if unusable:
            error(f"Error: that reset can't be used right now ({unusable})")
            sys.exit(1)

        kind = resets.grant_kind(grant)
        if not args.yes:
            if not sys.stdin.isatty():
                error("Error: refusing to spend a reset without --yes in a non-interactive shell")
                sys.exit(1)
            print(_describe_reset(num, email, grant, last_good, status, now))
            answer = input(f"Use this {kind} reset? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print(dimmed("Kept — nothing was spent."))
                return

        state = _read_state_file(switcher.backup_dir / STATE_FILENAME)
        claims = state.get("resetClaims") if isinstance(state.get("resetClaims"), dict) else {}
        request_id = reset_request_id(claims.get(num), grant["id"])
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
                "account": {"number": int(num), "email": email},
                "grantId": grant["id"],
                "result": result.to_json(),
            }))
        elif result.ok:
            left = f" · {result.resets_left} left on this grant" if result.resets_left is not None else ""
            cleared = ", ".join(
                resets.WINDOW_LABELS.get(k) or k for k in result.cleared
            ) or kind
            print(accent(f"Used a {kind} reset on Account-{num} ({email}): {cleared} refilled{left}"))
            fresh = switcher.usage_entries_by_account(fetch=set()).get(num)
            if fresh is not None:
                from ai_quota_swap.switcher import _usage_entry_lines

                for line in _usage_entry_lines(fresh):
                    print(f"   {line}")
        else:
            detail = (result.reason or result.detail or "").replace("_", " ")
            error(
                f"Reset not used: {result.result}" + (f" ({detail})" if detail else "")
            )
        if not result.ok:
            sys.exit(1)
    except ProviderError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        if in_flight:
            # The POST may have reached the server; the pending record keeps
            # its request id, so a retry (`qswap reset N` or the engine)
            # cannot spend a second one.
            print(f"\n{yellowed('Interrupted mid-claim — the reset may have been used; `qswap reset` shows where things stand')}")
        else:
            print(f"\n{dimmed('Operation cancelled — nothing was spent')}")
        sys.exit(130)


def _describe_reset(
    num: str, email: str, grant: dict, usage: dict | None, status: dict, now: float
) -> str:
    """What spending ``grant`` would do, in the terms Claude Code uses."""
    from ai_quota_swap import resets
    from ai_quota_swap.oauth import reset_clock_string
    from datetime import datetime, timezone

    kind = resets.grant_kind(grant)
    lines = [bolded(f"Account-{num} ({email}) · {kind} reset · {grant['id']}")]
    if grant.get("label"):
        lines.append(dimmed(f"  {grant['label']}"))
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
    from ai_quota_swap.settings import load_settings

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
    settings = load_settings(switcher.backup_dir)
    plan = preview_reset_plan(switcher, settings, entries, now=now)
    state = _read_state_file(switcher.backup_dir / STATE_FILENAME)
    used_today = reset_claims_today(state, now)
    current = switcher.current_account_number()

    if as_json:
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
            "auto": {
                "mode": rs.auto,
                "threshold": rs.threshold,
                "minHoursLeft": rs.min_hours_left,
                "expiryDays": rs.expiry_days,
                "expiryThreshold": rs.expiry_threshold,
                "usedToday": used_today,
                "maxPerDay": MAX_AUTO_RESETS_PER_DAY,
            },
            "plan": plan.to_json() if plan else None,
            "accounts": accounts,
        }))
        return

    mode = {
        "off": "off (only `qswap reset N` spends)",
        "active": f"active — when the account in use reaches {rs.threshold:g}% on a window its reset clears",
        "last-resort": f"last-resort — when the active account is at {rs.threshold:g}% and no switch helps",
        "on-limit": f"on-limit — when any account reaches {rs.threshold:g}%",
    }[rs.auto]
    print(bolded("Usage resets") + dimmed(f"   auto: {mode}"))
    for num, entry in entries.items():
        email = switcher.account_email(num)
        marker = f" {accent('(active)')}" if num == current else ""
        print(f"  {num}: {email}{marker}")
        status = entry.last_good.get("resets") if isinstance(entry.last_good, dict) else None
        if status is None:
            why = entry.last_error or entry.sentinel or "no reading"
            print(f"     {dimmed('└')} {dimmed(f'reset status unavailable ({why})')}")
            continue
        grants = resets.unused_grants(status, now)
        if not status.get("eligible") or not grants:
            reason = status.get("ineligible_reason")
            note = f"not eligible ({reason})" if not status.get("eligible") and reason else "no resets held"
            print(f"     {dimmed('└')} {dimmed(note)}")
            continue
        for j, g in enumerate(grants):
            branch = dimmed("└" if j == len(grants) - 1 else "├")
            why_not = resets.grant_unusable_reason(g, now)
            state_text = "usable now" if why_not is None else why_not
            end = resets.use_by(resets.parse_ts(g.get("ends_at")))
            if end:
                state_text += f" · use by {end}"
            tag = " · next" if g["id"] == status.get("next_grant_id") else ""
            count = f"{g['resets_left']}×"
            line = f"{count:>3} {resets.grant_kind(g):<7} {state_text}"
            print(f"     {branch} {muted(line)}  {dimmed(g['id'] + tag)}")
    if rs.auto != "off":
        if plan is not None:
            hours = f", {plan.hours_left / 24:.1f}d before it resets on its own" if plan.hours_left else ""
            print(accent(
                f"Auto would now use a {plan.grant_kind} reset on Account-{plan.account} "
                f"({plan.reason}: {plan.window} {plan.pct:.0f}%{hours})"
            ))
        else:
            print(dimmed("Auto would not use a reset right now."))
        print(dimmed(f"Auto resets in the last 24h: {used_today}/{MAX_AUTO_RESETS_PER_DAY}"))


def _auto_command(argv: list[str]) -> None:
    """Handle `qswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built, like `run` (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted).
    """
    import signal
    import time as _time

    parser = argparse.ArgumentParser(
        prog="qswap auto",
        description=(
            "Automatically switch accounts when the active one nears its "
            "5h/7d rate limit. Runs a foreground polling loop; use --once "
            "for a single tick (cron-friendly)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes with --once:
  0  switched to another account
  1  error (network trouble, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Examples:
  qswap auto                       # foreground loop, switch at 90% used
  qswap auto --threshold 80        # switch earlier
  qswap auto --weekly-threshold 97 # let the weekly window run higher before switching
  qswap auto --model Fable         # also switch when the Fable weekly limit is hit
  qswap auto --json                # one JSON event per line (for scripts)
  qswap auto --once; echo $?       # single tick, outcome in exit code
  qswap auto --dry-run             # log decisions, never actually switch

Defaults live in settings.json in the backup root; flags override them.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate once, maybe switch, and exit (exit code = outcome)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON event per line on stdout",
    )
    parser.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="Poll interval in loop mode (min 15; default 60)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help=(
            "Switch when the active account's 5h window (or any window, "
            "without a weekly line) reaches this utilization (50-99.9; "
            "default 90)"
        ),
    )
    parser.add_argument(
        "--weekly-threshold",
        type=float,
        metavar="PCT",
        help=(
            "Separate switch line for the 7-day and per-model weekly windows "
            "(50-99.9; default: the threshold). A flag cannot unset a "
            "configured value; use 'qswap config unset'"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default 300)",
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "Also switch when a per-model weekly limit is hit, not just the "
            "account-wide 5h/7d windows. One name or a comma-separated list "
            "(e.g. Fable, Opus, Sonnet, Haiku, or 'Fable,Opus'), or 'all' "
            "for every per-model window an account reports"
        ),
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow switching onto managed API-key accounts as a last resort "
            "(they bill per token; default: excluded)"
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=AUTOSWITCH_STRATEGIES,
        default=None,
        help=(
            "Target selection: 'best' (most quota left; default), "
            "'weekly-first' (switch at the threshold like best, onto the "
            "account whose weekly window resets soonest), "
            "'consume-first' (proactively use the account whose weekly window "
            "resets soonest), or 'priority-consume-first' (highest account "
            "priority, then soonest weekly reset; only targets below all switch lines)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report, but never switch or write state",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    from ai_quota_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent, pct_label
    from ai_quota_swap.printer import accent, yellowed
    from ai_quota_swap.settings import load_settings, merged_with_cli

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        print(json.dumps(event.to_json()), flush=True)

    def human_emit(event: AutoSwitchEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        line = event.human()
        if event.kind == "switch" or (event.kind == "limit-reset" and not getattr(event, "dry_run", False)):
            line = accent(line)
        elif event.kind in ("error", "account-quarantined"):
            line = yellowed(line)
        elif event.kind in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        print(f"{stamp}  {line}", flush=True)

    try:
        switcher = _switcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        engine = AutoSwitchEngine(
            switcher,
            settings,
            jsonl_emit if args.json else human_emit,
            dry_run=args.dry_run,
        )

        if args.once:
            sys.exit(engine.tick().value)

        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        if not args.json:
            weekly = (
                f", weekly {pct_label(settings.weekly_threshold)}%"
                if settings.weekly_threshold is not None
                else ""
            )
            print(
                dimmed(
                    f"Auto-switch running: threshold {pct_label(settings.threshold)}%"
                    f"{weekly}, every {settings.interval_seconds:.0f}s"
                    f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                )
            )
        sys.exit(engine.run_loop())
    except ProviderError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Auto-switch stopped')}",
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

    key_lines = "\n".join(
        f"  {spec.dotted:<34}{spec.help} (default {format_setting_value(spec.default)})"
        for spec in SETTING_SPECS.values()
    )
    parser = argparse.ArgumentParser(
        prog="qswap config",
        description=(
            "Read and edit ai-quota-swap settings (settings.json in the "
            "backup root)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Keys:
{key_lines}

Examples:
  qswap config                              # list effective settings
  qswap config get autoswitch.threshold
  qswap config set autoswitch.threshold 80
  qswap config set autoswitch.weeklyThreshold 97  # own line for the 7d/per-model windows
  qswap config unset autoswitch.threshold   # back to the default
  qswap config path                         # where settings.json lives
        """,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (with list or get)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="action", metavar="{list,get,set,unset,path}")

    p_list = sub.add_parser("list", help="Show all effective settings (the default)")
    p_get = sub.add_parser("get", help="Print one setting's effective value")
    p_get.add_argument("key", metavar="KEY", help="Dotted key, e.g. autoswitch.threshold")
    for p in (p_list, p_get):
        # SUPPRESS: without it the subparser's False default would clobber a
        # pre-verb `qswap config --json` in the shared namespace.
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Emit machine-readable JSON to stdout",
        )
    p_set = sub.add_parser("set", help="Validate and persist one setting")
    p_set.add_argument("key", metavar="KEY")
    p_set.add_argument("value", metavar="VALUE")
    p_unset = sub.add_parser("unset", help="Remove one setting (revert to the default)")
    p_unset.add_argument("key", metavar="KEY")
    sub.add_parser("path", help="Print the settings.json location")

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
                print(muted(f"{args.key} is not set; nothing to do"), file=sys.stderr)
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
    if argv and argv[0] in ("reset", "resets"):
        _reset_command(argv[1:])
        return
    if argv and argv[0] == "swap":
        _swap_command(argv[1:])
        return
    if argv and argv[0] == "move":
        _move_command(argv[1:])
        return

    # Bare `qswap` in an interactive terminal opens the TUI dashboard (like
    # lazygit/k9s). TTY-gated on both ends so scripts and pipes keep getting
    # the usage error, and `qswap tui` stays the explicit spelling.
    if not argv and sys.stdout.isatty() and sys.stdin.isatty():
        argv = ["--tui"]

    # Memorable subcommands (`qswap switch <email>`, `qswap list`, `qswap help`, ...)
    # are rewritten to the equivalent flags so the original `--flag` interface
    # keeps working unchanged.
    argv = _translate_subcommand(argv)

    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        usage="%(prog)s <command> [args] [options]",
        description="""Multi-Account Switcher for Claude Code

Commands:
  %(prog)s help                       show this help
  %(prog)s list                       list managed accounts
  %(prog)s status                     show current account
  %(prog)s switch                     rotate to the next account
  %(prog)s switch <num|email>         switch to a specific account
  %(prog)s add                        add the current account
  %(prog)s add-token [TOKEN|-]        register a setup-token or API key
  %(prog)s remove <num|email>         remove an account
  %(prog)s disable <num|email>        hold an account out of auto-rotation
  %(prog)s enable <num|email>         return a disabled account to rotation
  %(prog)s threshold <num|email> <pct>  set one account's switch-away point
  %(prog)s threshold <num|email> --unset  clear it (use the global default)
  %(prog)s threshold                  list the global default and overrides
  %(prog)s priority <num|email> <tier>  set priority (higher first; default 0)
  %(prog)s priority                   list account priorities
  %(prog)s run <num|email> [-- ...]   run as an account, this terminal only
  %(prog)s run                        run the current dir's mapped account
  %(prog)s map <num|email> [path]     map a directory to an account
  %(prog)s map                        list directory mappings
  %(prog)s unmap [path]               remove a directory mapping
  %(prog)s alias <num|email> <name>   set a short alias for an account
  %(prog)s alias <num|email> --unset  remove an account's alias
  %(prog)s alias                      list all aliases
  %(prog)s swap <a> <b>               exchange two accounts' slot numbers
  %(prog)s move <a> <slot>            assign an account to a slot (swaps if taken)
  %(prog)s auto                       auto-switch when nearing rate limits
  %(prog)s reset                      list usage resets (+ what auto would use)
  %(prog)s reset <num|email>          use one usage reset on that account
  %(prog)s config [set KEY VALUE]     show or change settings (settings.json)
  %(prog)s unclaimed [--purge ID]     list or drop stashed credential entries
  %(prog)s export <path>              export accounts
  %(prog)s import <path>              import accounts
  %(prog)s import-usage <path>        adopt usage another machine read (list --json)
  %(prog)s tui                        interactive dashboard (also: bare %(prog)s)
  %(prog)s watch                      dashboard, opened on the live watch page
  %(prog)s menubar                    macOS menu bar app, kept running by launchd
  %(prog)s menubar --service-status   is it running? does it start at login?
  %(prog)s upgrade                    self-upgrade to latest
  %(prog)s purge                      remove all ai-quota-swap data

Aliases: ls=list  rm=remove  update=upgrade""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Flags combine with subcommands:
  %(prog)s switch --strategy best           # pick the account with most quota left
  %(prog)s switch --strategy next-available # rotate, skipping rate-limited accounts
  %(prog)s switch user@example.com
  %(prog)s list --token-status
  %(prog)s list --json
  %(prog)s import-usage usage.json --hold 600  # adopt another machine's list --json
  %(prog)s add --slot 3                      # add to a specific slot
  %(prog)s add-token sk-ant-oat01-... --email me@example.com
  %(prog)s run 2 -- --resume                 # forward args after '--' to claude
  %(prog)s auto --once                       # single auto-switch tick (cron-friendly)
  %(prog)s config set autoswitch.threshold 80

The original flag spellings (%(prog)s --switch, %(prog)s --list, ...) keep working.
        """,
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
        parser.error("no command given — try '%(prog)s help'" % {"prog": _prog_name()})

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

    if args.force and not (args.import_ or args.switch_to):
        parser.error("--force can only be used with 'import' or 'switch <num|email>'")

    if args.full and not args.export:
        parser.error("--full can only be used with 'export'")

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
            switcher.add_account(slot=args.slot, alias=args.alias)
        elif args.add_token is not None:
            switcher.add_account_from_token(
                token=args.add_token,
                email=args.email,
                slot=args.slot,
            )
        elif args.remove_account:
            switcher.remove_account(args.remove_account)
        elif args.disable_account is not None:
            switcher.set_account_disabled(args.disable_account, True)
        elif args.enable_account is not None:
            switcher.set_account_disabled(args.enable_account, False)
        elif args.list:
            payload = switcher.list_accounts(
                show_token_status=args.token_status,
                json_output=args.json,
            )
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
            payload = switcher.switch_to(
                args.switch_to, json_output=args.json, force=args.force
            )
        elif args.status:
            payload = switcher.status(json_output=args.json)
        elif args.purge:
            switcher.purge()
            if sys.platform == "darwin":
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
