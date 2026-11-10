"""``qswap --help``, ``qswap help VERB``, and the help of the verbs that are
rewritten to the legacy ``--flag`` interface.

docs/design/ux.md: every verb answers ``--help`` with its own usage, a one or
two line description, its options and at least one example, and ``qswap help
VERB`` prints the same; ``qswap --help`` is a map that fits a screen. The
verbs ``cli`` rewrites to hidden flags (``list`` → ``--list``) have no parser
of their own, so their help is built here from small help-only parsers in the
same format as every other verb's.
"""

from __future__ import annotations

from ai_quota_swap.argparsing import ArgumentParser

MAIN_HELP = """\
usage: qswap <command> [options]

Switch between Claude Code and Codex accounts, by hand or automatically.

Accounts
  list                  every account and its usage
  status                the active accounts, and whether auto-switch runs
  switch [ACCOUNT]      switch to an account, or to the next one
  add                   save the current Claude Code login as an account
  add-token [TOKEN]     add an account from a setup-token or API key
  remove ACCOUNT        stop managing an account
  alias ACCOUNT NAME    give an account a short name
  disable, enable       hold an account out of rotation, or bring it back
  swap, move            change account numbers

Automatic switching and resets
  auto                  switch before the active account runs out
  threshold, priority   per-account switch line and order
  reset [ACCOUNT]       list usage resets, or spend one
  menubar               macOS menu bar app that can switch for you

Sessions
  run ACCOUNT           run Claude Code as an account in this terminal only
  map, unmap            pick the account for a directory

Usage and cost
  usage                 tokens and estimated cost by day, model, account
  budget                monthly budgets for API-key accounts
  api org               register an organization's Admin API key

Codex
  codex COMMAND         the same for Codex logins (qswap codex help)

Setup and maintenance
  doctor                check that everything qswap needs is in place
  config                show or change settings
  export, import        move accounts to another machine
  import-usage          adopt the usage another machine read
  tui, watch            the interactive dashboard
  unclaimed             saved logins that no account claims
  upgrade               upgrade qswap
  purge                 remove all of qswap's data

Options
  -h, --help            this map (qswap help COMMAND for one command)
  --version             print the version
  --debug               log debug detail

ACCOUNT is a number (2), an email, or an alias. The old flag spellings
(qswap --list, qswap --switch-to 2, ...) still work.
"""

#: Bare ``qswap`` off a terminal (in a terminal it opens the dashboard).
BARE_HINT = """\
qswap switches between several Claude Code and Codex accounts.
  qswap help    the commands
  qswap list    your accounts and their usage
"""


def _parser(verb: str, usage: str, description: str, examples: list[str]) -> ArgumentParser:
    lines = "\n".join(f"  {e}" for e in examples)
    return ArgumentParser(
        prog=f"qswap {verb}",
        usage=f"qswap {verb} {usage}".rstrip(),
        description=description,
        epilog=f"examples:\n{lines}",
        add_help=True,
    )


def _account_arg(parser: ArgumentParser, help_text: str = "number, email or alias") -> None:
    parser.add_argument("account", metavar="ACCOUNT", help=help_text)


def _json(parser: ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (docs/json-output.md)")


def _list() -> ArgumentParser:
    p = _parser("list", "[--json] [--token-status]",
                "List every managed account with its usage: Claude Code's, then "
                "Codex's. Read-only.",
                ["qswap list", "qswap list --json"])
    _json(p)
    p.add_argument("--token-status", action="store_true",
                   help="add each account's sign-in token details")
    return p


def _status() -> ArgumentParser:
    p = _parser("status", "[--json]",
                "Show the active Claude Code and Codex accounts, how many are "
                "managed, and whether auto-switch is running. Read-only.",
                ["qswap status", "qswap status --json"])
    _json(p)
    return p


def _switch() -> ArgumentParser:
    p = _parser(
        "switch", "[ACCOUNT] [--force] [--strategy best|next-available] [--json]",
        "Make an account the active Claude Code login. Without ACCOUNT, move to "
        "the next one. An account that needs re-login is refused unless --force.",
        ["qswap switch 2", "qswap switch --strategy best", "qswap switch work --json"],
    )
    p.add_argument("account", metavar="ACCOUNT", nargs="?",
                   help="number, email or alias; omit to rotate")
    p.add_argument("--force", action="store_true",
                   help="with ACCOUNT: switch even if it needs re-login, and write "
                        "its saved login without saving the current one first")
    p.add_argument("--strategy", metavar="best|next-available",
                   help="without ACCOUNT: 'best' picks the account with the most "
                        "quota left; 'next-available' skips accounts at their limit")
    p.add_argument("--model", metavar="NAMES",
                   help="with --strategy: also weigh these per-model limits "
                        "(comma-separated, or 'all')")
    _json(p)
    return p


def _add() -> ArgumentParser:
    p = _parser("add", "[--slot NUM] [--alias NAME] [--yes]",
                "Save the login Claude Code is using now as a managed account. "
                "Run it again for an account already saved to refresh its login.",
                ["qswap add", "qswap add --alias work", "qswap add --slot 3"])
    p.add_argument("--slot", metavar="NUM", help="store it as this account number")
    p.add_argument("--alias", metavar="NAME", help="give it a short name")
    p.add_argument("-y", "--yes", action="store_true",
                   help="replace the account already in --slot without asking")
    return p


def _add_token() -> ArgumentParser:
    p = _parser(
        "add-token", "[TOKEN|-] [--email EMAIL] [--slot NUM] [--yes]",
        "Add an account from a Claude Code setup-token or a Console API key, "
        "without logging in on this machine. The kind is detected.",
        ["qswap add-token sk-ant-oat01-... --email me@example.com",
         "pbpaste | qswap add-token -"],
    )
    p.add_argument("token", metavar="TOKEN|-", nargs="?",
                   help="the token; '-' reads it from stdin; omitted, asks for it")
    p.add_argument("--email", metavar="EMAIL",
                   help="a label for it (default: setup-token-N@token.local "
                        "or api-key-N@token.local)")
    p.add_argument("--slot", metavar="NUM", help="store it as this account number")
    p.add_argument("-y", "--yes", action="store_true",
                   help="replace the account already in --slot without asking")
    return p


def _remove() -> ArgumentParser:
    p = _parser("remove", "ACCOUNT [--yes]",
                "Stop managing an account and delete qswap's saved copy of its "
                "login. The login itself stays signed in. Alias: rm.",
                ["qswap remove 3", "qswap remove old@example.com --yes"])
    _account_arg(p)
    p.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    return p


def _disable() -> ArgumentParser:
    p = _parser("disable", "ACCOUNT",
                "Hold an account out of rotation: auto-switch and a bare switch "
                "skip it. qswap switch ACCOUNT can still pick it.",
                ["qswap disable 4"])
    _account_arg(p)
    return p


def _enable() -> ArgumentParser:
    p = _parser("enable", "ACCOUNT",
                "Return a disabled account to rotation.",
                ["qswap enable 4"])
    _account_arg(p)
    return p


def _export() -> ArgumentParser:
    p = _parser("export", "PATH [--account ACCOUNT] [--full]",
                "Write managed accounts, logins included, to a file for "
                "qswap import on another machine. Keep the file private.",
                ["qswap export accounts.json", "qswap export one.json --account 2"])
    p.add_argument("path", metavar="PATH", help="the file to write")
    p.add_argument("--account", metavar="ACCOUNT", help="only this account")
    p.add_argument("--full", action="store_true",
                   help="include the whole ~/.claude.json, not only the login")
    return p


def _import() -> ArgumentParser:
    p = _parser("import", "PATH [--force]",
                "Add the accounts in a qswap export file. Accounts already "
                "managed are skipped unless --force.",
                ["qswap import accounts.json", "qswap import accounts.json --force"])
    p.add_argument("path", metavar="PATH", help="the export file")
    p.add_argument("--force", action="store_true",
                   help="overwrite accounts that are already managed")
    return p


def _import_usage() -> ArgumentParser:
    p = _parser("import-usage", "PATH [--hold SECONDS]",
                "Adopt the usage another machine read (its qswap list --json), "
                "so this one need not read the same accounts again.",
                ["qswap import-usage usage.json",
                 "qswap import-usage usage.json --hold 600"])
    p.add_argument("path", metavar="PATH", help="a qswap list --json file")
    p.add_argument("--hold", metavar="SECONDS",
                   help="keep this machine from reading those accounts for this "
                        "long (0 lifts an earlier hold)")
    return p


def _purge() -> ArgumentParser:
    p = _parser("purge", "[--yes]",
                "Remove all of qswap's data: saved logins, settings, logs, and on "
                "macOS the menu bar service. Your Claude Code login is not touched.",
                ["qswap purge", "qswap purge --yes"])
    p.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    return p


def _upgrade() -> ArgumentParser:
    return _parser("upgrade", "",
                   "Upgrade qswap with the tool that installed it (uv or pipx). "
                   "Alias: update.",
                   ["qswap upgrade"])


def _tui() -> ArgumentParser:
    return _parser("tui", "",
                   "Open the interactive dashboard. Bare qswap in a terminal "
                   "does the same.",
                   ["qswap tui"])


def _watch() -> ArgumentParser:
    return _parser("watch", "",
                   "Open the dashboard on its live page, every account as a card.",
                   ["qswap watch"])


def _menubar() -> ArgumentParser:
    p = _parser(
        "menubar", "[--install-service | --uninstall-service | --service-status | "
                   "--foreground] [--auto-switch | --no-auto-switch]",
        "Run the macOS menu bar app as a background service that starts at "
        "login. Its auto-switch is off until you turn it on.",
        ["qswap menubar", "qswap menubar --auto-switch", "qswap menubar --service-status"],
    )
    p.add_argument("--install-service", action="store_true",
                   help="register it to start at login and (re)start it now")
    p.add_argument("--uninstall-service", action="store_true",
                   help="stop it and remove its service")
    p.add_argument("--service-status", action="store_true",
                   help="is it installed, running, starting at login?")
    p.add_argument("--foreground", action="store_true",
                   help="run it attached to this terminal")
    p.add_argument("--auto-switch", action="store_true",
                   help="turn its auto-switch on")
    p.add_argument("--no-auto-switch", action="store_true",
                   help="turn its auto-switch off")
    return p


def _help() -> ArgumentParser:
    p = _parser("help", "[COMMAND]",
                "Show the map of commands, or one command's help.",
                ["qswap help", "qswap help switch", "qswap help codex"])
    p.add_argument("command", metavar="COMMAND", nargs="?", help="a command")
    return p


#: Verbs rewritten to legacy flags, and their help-only parsers.
LEGACY_VERB_HELP = {
    "list": _list,
    "ls": _list,
    "status": _status,
    "switch": _switch,
    "add": _add,
    "add-token": _add_token,
    "remove": _remove,
    "rm": _remove,
    "disable": _disable,
    "enable": _enable,
    "export": _export,
    "import": _import,
    "import-usage": _import_usage,
    "purge": _purge,
    "upgrade": _upgrade,
    "update": _upgrade,
    "tui": _tui,
    "watch": _watch,
    "menubar": _menubar,
    "help": _help,
}

#: Verbs of the legacy interface that need a value, and what to call it in
#: a usage error (instead of the hidden flag's name).
VALUE_VERBS = {
    "remove": "an account (number, email or alias)",
    "rm": "an account (number, email or alias)",
    "disable": "an account (number, email or alias)",
    "enable": "an account (number, email or alias)",
    "export": "a file to write",
    "import": "an export file to read",
    "import-usage": "a qswap list --json file to read",
}


def legacy_help(verb: str) -> str | None:
    """The help text of a verb rewritten to a legacy flag, or None."""
    build = LEGACY_VERB_HELP.get(verb)
    return build().format_help() if build else None


def legacy_usage(verb: str) -> str | None:
    """Just the usage line of such a verb, for a usage error."""
    build = LEGACY_VERB_HELP.get(verb)
    return build().format_usage() if build else None
