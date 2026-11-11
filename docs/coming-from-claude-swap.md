# Coming from claude-swap

ai-quota-swap started as a fork of claude-swap 1.1.0b1. This page moves your accounts from claude-swap (`cswap`) to qswap and lists what you set again by hand.

qswap keeps its own data directory, Keychain service, LaunchAgent and environment variables, and never reads claude-swap's data, so it starts empty beside claude-swap. Accounts move across in an export file. Install qswap first ([quickstart.md](quickstart.md)). The `cswap` commands below are those of claude-swap 1.1.0b1; if yours is another version, check `cswap help`.

Do not let both tools run against the same accounts. Each one refreshes the logins it has saved, a refresh replaces the token, and the other tool is left holding a login that no longer works. Both would also switch the same Claude Code login.

## Before the export

Note what you set in claude-swap, because the export does not carry it: `cswap config` (settings), `cswap threshold`, `cswap priority`, `cswap map`, and the accounts `cswap list` shows as disabled.

If you ran `cswap run` with history sharing off, each account's own chat history is in claude-swap's session profiles, which `cswap purge` deletes. Run `cswap run N --share-history` once per account and exit Claude Code: that merges the history into `~/.claude`, where qswap's sessions share it too.

Close every `cswap run` session. A session refreshes its own copy of the login, while `cswap export` reads the copy claude-swap saved, which catches up when claude-swap next switches to that account or reads its usage. To be sure, `cswap switch N` to each account that ran a session, then switch back.

Then stop claude-swap's background work, so it cannot refresh a login between the export and the import: run `cswap menubar --uninstall-service` if you use its macOS menu bar, stop any `cswap auto` loop, and remove any `cswap auto --once` line from your crontab. From here on, run no cswap command other than `cswap export` and, after the import, `cswap purge`.

## Export and import

```bash
cswap export ~/claude-swap-export.json    # with claude-swap
qswap import ~/claude-swap-export.json    # with ai-quota-swap
rm ~/claude-swap-export.json              # it holds your logins in plain text
```

Importing a claude-swap 1.1.0b1 export with three accounts into an empty qswap:

```
$ qswap import ~/claude-swap-export.json
Imported a@example.com → slot 1
Imported b@example.com → slot 2
Imported api-key-3 → slot 3
Done: 3 imported, 0 overwritten, 0 skipped
```

An export carries each account's login (or the key of an API-key account), its email, organization, number, date added and alias, and which account was active. qswap keeps each number when it is free, and takes the active account from the file when it has none recorded. An account qswap already manages is skipped unless you pass `--force`, and an alias another qswap account already uses is dropped with a warning. `--force` never changes an account's kind: a login is not written over an API-key account, nor a key over a subscription (`Skipped #5 ci: it is an API-key account and the export holds a login for it. To replace it anyway, remove it first: qswap remove 5`). An entry with neither a login nor a key in it, only MCP sign-ins, is skipped with a line saying so. Unless you exported with `--full`, MCP and plugin OAuth logins and the device token stay behind.

It does not carry settings, disabled accounts, per-account switch lines, priorities, directory mappings, `cswap run` profiles, usage readings or menu bar settings.

If the account Claude Code is logged in to is in the file, the import ends with a note about it. Usually it says the account is already the active one and nothing needs doing: Claude Code keeps using its own login, which is at least as fresh as the exported copy. Only when that login was not working (its refresh was refused), or for an API-key account whose key changed, does the note give `qswap switch N --force`, which loads the imported copy over the live one. For a key, running Claude Code sessions keep the previous key until you restart them, and the switch lists them.

If an account shows `needs re-login` in `qswap list` after the import, a refresh had already replaced the login it was exported with. Log in with that account in Claude Code and run `qswap add`.

## Set your preferences again

| In claude-swap | In qswap |
|---|---|
| `cswap config` | `qswap config set KEY VALUE`; every claude-swap 1.1.0b1 setting has the same name here ([settings.md](settings.md)) |
| `cswap threshold` | `qswap threshold N PCT` |
| `cswap priority` | `qswap priority N TIER` |
| disabled accounts | `qswap disable N` |
| `cswap map` | `qswap map N PATH` ([sessions.md](sessions.md)) |
| menu bar | `qswap menubar`; its auto-switch is off until you turn it on ([menubar.md](menubar.md)) |

## Remove claude-swap

```bash
cswap purge                     # claude-swap's data directory, saved logins and session profiles
uv tool uninstall claude-swap   # or: pipx uninstall claude-swap
```

`cswap purge` asks first. On macOS it also removes claude-swap's menu bar service. It leaves the Claude Code login you are using alone.

## What changed in names

| | claude-swap | ai-quota-swap |
|---|---|---|
| Package | `claude-swap` | `ai-quota-swap` |
| Command | `cswap`, `claude-swap` | `qswap`, `ai-quota-swap` |
| Data directory, macOS and Windows | `~/.claude-swap-backup` | `~/.ai-quota-swap` |
| Data directory, Linux and WSL | `$XDG_DATA_HOME/claude-swap` | `$XDG_DATA_HOME/ai-quota-swap` |
| Keychain service (macOS) | `claude-swap` | `ai-quota-swap` |
| Menu bar LaunchAgent | `com.cswap.menubar` | `com.aiquotaswap.menubar` |
| Run the menu bar without its wrapper app | `CLAUDE_SWAP_NO_BUNDLE` | `AI_QUOTA_SWAP_NO_BUNDLE` |

`$XDG_DATA_HOME` defaults to `~/.local/share`. The `cswap` command is not shipped with qswap, which is why the two can be installed side by side. The verbs are the ones claude-swap 1.1.0b1 had, plus new ones, and the old flag spellings still work (`qswap --list`, `qswap --switch-to 2`), so a script mostly needs `cswap` changed to `qswap`. A few behaviours did change, for example `qswap switch N` refuses an account that needs re-login unless `--anyway`, and usage errors exit with 64; see [../CHANGELOG.md](../CHANGELOG.md). qswap's own `QSWAP_CLAUDE_CLI_VERSION` (the Claude Code version qswap names when it reads usage resets) is new.
