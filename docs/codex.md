# Codex accounts

qswap keeps several OpenAI Codex CLI logins (ChatGPT accounts), shows each one's usage, and switches between them by hand or automatically. After this page you can add your accounts, switch, and know what keeps the old account after a switch.

## What works today

- `qswap codex add`, `login`, `list`, `status`, `switch`, `remove`, `alias`, `disable`, `enable`, `threshold`, `priority`, `auto` and `reset`. `qswap codex help` lists them and `qswap codex help VERB` explains one.
- Usage for every saved account, in `qswap codex list` and in a "Codex accounts" section of `qswap list`. `--json` works on `list`, `status`, `switch`, `reset`, `auto` and `priority` ([json-output.md](json-output.md)).
- `qswap usage --provider codex` attributes Codex tokens to the account that was active, from the switches qswap records ([usage.md](usage.md)).

Codex support is experimental. It has been tested against fakes and with one real account, and has never been run with two real Codex accounts. No real reset credit has been spent with it. Treat your first switches with care and read the warnings a switch prints.

## Add your first account

Log in with Codex as usual (`codex login`), then save that login:

```
$ qswap codex add
Added #1 maya@example.com
To add another account without signing this one out, run: qswap codex login
```

`qswap codex add` saves the login in `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). Run it again for an account that is already saved and it updates the saved copy instead of adding a second one. `--alias NAME` names the account and `--slot NUM` picks its number.

## Add another account

```
$ qswap codex login
Logging in under a temporary Codex home; your current Codex login stays live.
Added #3 ops@example.net
```

Codex's own login prompts appear between those two lines. Arguments after `--` go to `codex login`, for example `qswap codex login -- --device-auth`. `--alias` and `--slot` work as for `add`.

Do not run plain `codex login` again in your normal Codex home. Before it logs in, it signs the login already there out at OpenAI, which also kills the copy qswap saved. `qswap codex login` creates a private, empty Codex home (mode 0700) in qswap's data directory, runs `codex login` there on your terminal, saves the login it leaves, and deletes the temporary home. Your `~/.codex` and the active account do not change; switch with `qswap codex switch` when you want the new one.

If `codex login` fails, is interrupted, or leaves no `auth.json` (for example because Codex is set to keep logins in the OS keyring), nothing is saved and the temporary home is deleted. A temporary home left behind by a crash is removed by the next `qswap codex login` once it is a day old.

The same thing by hand, if `qswap codex login` cannot run `codex`:

```bash
tmp="$(mktemp -d)"
CODEX_HOME="$tmp" codex login        # log in with the other account
CODEX_HOME="$tmp" qswap codex add
rm -rf "$tmp"                        # deleting the file does not sign the login out
```

## Switch

```
$ qswap codex switch 2
Switched #1 maya@example.com → #2 work (maya@northwind.example.com)
  #2 work (maya@northwind.example.com) · active · Team
     ├ 5h       8%  resets 19:15 · in 4h 55m
     └ 7d      15%  resets Nov 16 16:20 · in 6d 2h
     Holds 1 reset credit · use by Nov 22
New codex sessions use this account.
```

`qswap codex switch` with no account moves to the next one, and skips, one line each, any account that is disabled, has no saved login, or needs re-login.

What a switch does:

1. Saves the login in `auth.json` as its account's copy, so a token renewal Codex did since the last save is kept.
2. Writes the target's saved login into `auth.json`, holding the two locks Codex 0.167 takes (`auth.lock`, then `auth.json.lock`). A symlinked `auth.json` stays a symlink.
3. Reads the file back and reports an error if it no longer holds the target.
4. Records the switch for `qswap usage`.

New `codex` sessions start on the new account. These keep the old one:

- **Running `codex` sessions** keep it until their access token runs out, then stop with a "signed in to another account" error. Restart them. qswap lists their pids in a warning.
- **Codex's background server**, which interactive sessions attach to. qswap restarts it for you (`codex app-server daemon restart`) and says so, because new sessions would otherwise attach to the old account. The restart interrupts work in progress on it. `qswap config set codex.restartDaemon false` leaves it running; the switch then names the command to run yourself.
- **The ChatGPT desktop app** always uses `~/.codex` and keeps the old account until you quit and reopen it. qswap warns when it is running and does not touch it.

qswap refuses, and changes nothing, when:

- The login in `auth.json` is not one it has saved. Run `qswap codex add` first, or pass `--force` to overwrite it (it is then lost unless saved elsewhere). A bare `qswap codex switch` saves such a login as a new account instead and asks you to run the switch again.
- The target needs re-login. `--anyway` switches to it regardless.
- The access token in `auth.json` expires within 10 minutes while Codex is running on that home, because Codex may be renewing it at that moment. Try again a few minutes later.
- The target's saved login is missing or belongs to another account. Log in to it again with `qswap codex login`.

A target whose usage cannot be read, or whose token expired while it sat saved, is switched to with a one-line warning.

## See usage

```
$ qswap codex list
Codex accounts
  #1 maya@example.com · active · Plus
     ├ 5h      22%  resets 18:00 · in 3h 40m
     └ 7d      47%  resets Nov 14 17:20 · in 4d 3h

  #2 work (maya@northwind.example.com) · Team
     ├ 5h       8%  resets 19:15 · in 4h 55m
     └ 7d      15%  resets Nov 16 16:20 · in 6d 2h
     Holds 1 reset credit · use by Nov 22
```

```
$ qswap codex status
Codex
  #1 maya@example.com · active · Plus
     ├ 5h      22%  resets 18:00 · in 3h 40m
     └ 7d      47%  resets Nov 14 17:20 · in 4d 3h

2 Codex accounts are managed.
Auto-switch is not running. Start it with: qswap codex auto
```

Windows are named by their length: `5h` and `7d` as for Claude Code, a window of another length by its own (`daily`, `monthly`), and an additional limit by the name Codex gives it. A plan with only a weekly window shows only `7d`. Two states have no numbers:

```
$ qswap codex list
Codex accounts
  #1 maya@example.com · active · Plus
     ├ 5h      41%  resets 16:25 · in 2h 5m
     └ 7d     100%  resets Nov 11 21:20 · in 1d 7h · limit reached

  #2 work (maya@northwind.example.com) · Team
     usage unknown until used: Codex renews the expired token on first use
```

- `usage unknown until used`: a saved login's access token ran out while it sat unused. Codex renews it the first time it runs on that account, for example right after you switch to it.
- `token expired`: the active login's access token ran out while Codex was idle. Codex renews it the next time it runs.

Usage comes from `chatgpt.com/backend-api/wham/usage`, read with each login's own token on the same polling schedule as Claude Code accounts, so repeated `list` calls do not send repeated requests.

## Automatic switching

```bash
qswap codex auto                     # foreground loop
qswap codex auto --once; echo $?     # one check; 0 switched, 1 error, 2 nothing to do, 3 blocked
qswap codex auto --dry-run           # say what it would do, switch nothing
```

This is the engine behind `qswap auto`, run over the Codex accounts with the same flags, output and exit codes. Its rules are in [auto-switch.md](auto-switch.md). What differs:

- **The line.** A Codex switch restarts the background server and interrupts work, so by default the engine switches only when the active account is out: `codex.threshold` (99) replaces `autoswitch.threshold`. Set it lower to switch earlier (`qswap config set codex.threshold 90`), or pass `--threshold`. `qswap codex threshold` and `qswap codex priority` set per-account lines and order.
- **Accounts it will not pick.** A saved login reading `usage unknown until used` is never a target, since qswap does not refresh Codex logins and cannot know whether it has room. Switch to it by hand once and Codex renews it.
- **A deferred switch.** When the token in `auth.json` is about to expire while Codex runs, the switch waits for the next check (`no-switch`, reason `switch-deferred` in `--json`). It is not an error.
- **Its own state.** Cooldown, quarantine and the daily count of automatic credit spends are kept apart from Claude Code's, in `codex/autoswitch_state.json`. Settings are shared.

With one Codex account it never switches, but it still spends a reset credit when that account runs out, if `resets.auto` allows.

## Reset credits

```
$ qswap codex reset
Reset credits
Auto: last-resort · when the active account reaches 95% and no switch helps

  #1 maya@example.com · active
     └ no reset credits reported
  #2 work (maya@northwind.example.com)
     └ 1 reset credit · clears all usage · next
       usable now · use by Nov 22 · id rlrc_demo_2611_northwind

Auto would not use a reset credit right now.
Spent automatically in the last 24h: 0 of 3.
```

`qswap codex reset 2` spends one on #2 (it asks first; `--yes` skips the question, `--grant ID` picks the credit). qswap treats every credit as a full reset: it resets the 5-hour and weekly windows, or the weekly window alone on a plan that has only that one. That a credit resets both windows on a plan that has both is not yet confirmed. A credit on a saved login whose access token has expired cannot be spent until Codex renews that login. A spent credit cannot be undone. Automatic spending follows the `resets.*` settings, with the guards and kinds described in [resets.md](resets.md).

## What qswap never does

- It never refreshes a Codex login, active or saved. Codex renews its own tokens.
- It never runs `codex logout` and never revokes a token. `qswap codex remove` deletes only qswap's saved copy.

Never run `codex logout` on a login qswap has saved. It signs the login out at OpenAI, every saved copy of it stops working, and you have to log in to that account again.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `codex.restartDaemon` | `true` | Restart Codex's background server after a switch, so new sessions use the new account. |
| `codex.threshold` | `99` | `qswap codex auto`'s switch line (50 to 99.9), in place of `autoswitch.threshold`. |

Every other `autoswitch.*` setting applies to Codex as it does to Claude Code, and so does every `resets.*` setting. Change one with `qswap config set codex.threshold 95`. Every setting is in [settings.md](settings.md).

## Files

Codex data lives in `codex/` in qswap's data directory:

- `sequence.json`: the accounts.
- `credentials/account-N.json`: each saved login, byte for byte, mode 0600.
- `cache/usage.json`: the usage readings.
- `autoswitch_state.json`: the engine's cooldown, quarantine and credit spends.
- `activations.jsonl`: which account was active when, for `qswap usage`.
- `ai-quota-swap.log`: the log.

`qswap codex login` creates a `login-home-*` directory there while it runs and deletes it afterwards.

## Limits

- File storage only. If Codex keeps its login in the OS keyring (`cli_auth_credentials_store = "keyring"` or `"auto"` in `config.toml`) or in memory (`"ephemeral"`), or `auth.json` is missing while a keyring entry exists, every `qswap codex` command stops with one line saying so. A keyring setting forced by managed configuration is noticed only once `auth.json` is gone.
- ChatGPT logins only. An API-key login is refused.
- One active account per Codex home. `qswap codex run` (an account per terminal) is not available yet.
- On Windows qswap cannot list processes (it uses `ps`), so it does not warn about running Codex sessions or the desktop app, and the near-expiry check sees only the background server.
- Codex 0.164 takes neither lock and writes `auth.json` in place, so a 0.164 process renewing its token during a switch can still race it. qswap reads the file back after writing and reports an error if it changed.
