# Codex accounts

qswap can keep several OpenAI Codex CLI logins (ChatGPT accounts) and switch between them, with live usage for each. It is experimental: it has not yet been run with two real Codex accounts, so treat the first switches with care and keep an eye on the warnings it prints.

## What works today

- `qswap codex add`, `login`, `list`, `status`, `switch`, `remove`, `alias`, `disable`, `enable`, `threshold`, `priority`, `auto`, `reset`. `qswap codex help` lists them.
- Live usage for every saved account, in `qswap codex list` and in a "Codex accounts" section of `qswap list`. `--json` works on `list`, `status`, `switch`, `reset` and `auto` (see [json-output.md](json-output.md)).
- The plan (Plus, Pro, Team, ...) and any usage reset credits the account holds.
- `qswap codex auto`: the same auto-switch engine as `qswap auto`, switching when the active account runs out (see [Automatic switching](#automatic-switching)).
- `qswap codex reset`: list Codex usage reset credits and spend one, by hand or automatically (see [Usage reset credits](#usage-reset-credits)).
- `qswap usage` attributes Codex tokens to the account that was live, from the switches qswap records, and notices a login changed outside qswap each time it runs.

Not yet: `qswap codex run` (one account per terminal). Spending a credit has been tested only against a fake server; see [Usage reset credits](#usage-reset-credits).

## Add your first account

Log in with Codex as usual, then save that login:

```bash
codex login
qswap codex add
```

`qswap codex add` captures whatever login is in `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). Running it again for an account that is already saved refreshes the saved copy instead of adding a duplicate.

## Add another account

```bash
qswap codex login                    # log in with the other account in the browser
qswap codex login -- --device-auth   # arguments after -- go to codex login
```

Do not run plain `codex login` again in your normal Codex home: before it logs in, it signs the current login out at OpenAI, which also kills the copy qswap saved. `qswap codex login` avoids that. It makes a private, empty Codex home (mode 0700) inside qswap's data directory, runs `codex login` there with your terminal attached, saves the login it leaves the way `qswap codex add` does, and deletes the temporary home. Your normal `~/.codex` and the live account do not change; use `qswap codex switch` to move to the new account. Logging in to an account qswap already has updates its saved copy. `--alias NAME` and `--slot NUM` work as for `add`.

If `codex login` fails, is interrupted, or leaves no `auth.json` (for example because a managed configuration forces keyring storage), nothing is saved and the temporary home is deleted. A temporary home a crash left behind is removed by the next `qswap codex login` once it is a day old.

The same thing by hand, if `qswap codex login` cannot run `codex`:

```bash
tmp="$(mktemp -d)"
CODEX_HOME="$tmp" codex login        # log in with the other account
CODEX_HOME="$tmp" qswap codex add
rm -rf "$tmp"                        # deleting the file does not sign the login out
```

## Switch

```bash
qswap codex switch 2                 # or an email or alias
qswap codex switch                   # rotate to the next account
```

What happens:

1. qswap saves the login that is live now as its account's copy, so a token renewal Codex did since the last save is kept.
2. It writes the target's saved login into `auth.json`, holding the same two locks Codex 0.167 takes (`auth.lock`, then `auth.json.lock`). A symlinked `auth.json` stays a symlink.
3. New `codex` sessions start on the new account.

Who keeps the old account:

- **Running `codex` sessions** keep it until their access token runs out, then stop with a "signed in to another account" error. Restart them to pick up the switch. qswap lists their pids in a warning.
- **The background server** that interactive sessions attach to is restarted for you (`codex app-server daemon restart`), because new sessions would otherwise keep the old account. The restart interrupts work in progress on it. Turn it off with `qswap config set codex.restartDaemon false`; the switch then tells you to run that command yourself.
- **The ChatGPT desktop app** always uses `~/.codex` and keeps the old account until you quit and reopen it. qswap warns when it is running; it does not touch it.

qswap refuses to switch, and changes nothing, when:

- the live login is not one it has saved. Run `qswap codex add` first, or pass `--force` to overwrite it (it is then lost unless saved elsewhere).
- the live access token expires within 10 minutes while Codex is running on that home, because Codex may be renewing it at that moment. Try again a few minutes later.

## Usage

Windows are named by their length: `5h` and `7d` as for Claude, anything else (a daily window, a model-specific limit) by its own name. Two states have no numbers:

- `token expired`: the live login's access token ran out while Codex was idle. Codex renews it the next time it runs.
- `usage unknown until used`: a saved login's access token ran out while it sat unused. Codex renews it the first time it runs on that account, for example right after you switch to it.

Usage is read from `https://chatgpt.com/backend-api/wham/usage` with each login's own token, on the same polling schedule as Claude accounts, so repeated `list` calls do not send repeated requests.

## Automatic switching

```bash
qswap codex auto                     # foreground loop
qswap codex auto --once; echo $?     # one tick, outcome in the exit code
qswap codex auto --dry-run --json    # decisions as JSON events, nothing changed
```

It is the engine behind `qswap auto` (rules in [auto-switch.md](auto-switch.md)), run over the Codex accounts, with the same flags, output and exit codes. What differs:

- **The line.** A Codex switch restarts the background server, which interrupts work in progress, so by default the engine switches only when the active account is out: `codex.threshold`, default 99, replaces `autoswitch.threshold`. Set it lower to switch earlier (`qswap config set codex.threshold 90`), or pass `--threshold`. Every other `autoswitch.*` setting (cooldown, hysteresis, strategy, weekly line, ...) and the per-account `qswap codex threshold` and `priority` apply as for Claude Code.
- **Accounts it will not pick.** A saved login whose access token has expired reads "usage unknown until used" and is never a target: qswap does not refresh Codex logins, so it cannot know whether the account has room. Switch to it by hand once and Codex renews it.
- **An idle live login.** When the live login's access token ran out while Codex was idle, the engine waits instead of failing over, as it does for Claude Code.
- **A deferred switch.** When the live access token is about to expire while Codex runs on that home, a switch is refused for now (Codex may be renewing it). The engine reports `no-switch` with reason `switch-deferred` and tries again on the next tick; it is not an error.
- **State.** Cooldown, quarantine and the daily cap on automatic credit spends are kept in `codex/autoswitch_state.json`, apart from Claude Code's. Settings are shared: they live in the one `settings.json`.

With a single Codex account it never switches, but it still spends a credit when that account runs out (below).

## Usage reset credits

Some accounts hold credits that reset the account's Codex usage windows to 0% (Codex's `/usage`, "Redeem reset"). Each credit is used up when spent, and a spent credit cannot be undone.

```bash
qswap codex reset                    # every account's credits, and what auto would spend now
qswap codex reset 2                  # spend one on account 2 (asks first; --yes skips the question)
qswap codex reset 2 --grant <id> --yes
```

Every credit is a full reset (Codex titles them "Full reset (Weekly + 5 hr)"). It resets the windows the account reports for its core Codex limit: 5h and weekly, or weekly alone on Pro, where it works as a weekly reset. `qswap codex reset` names each credit that way:

```
  2: a@example.com (active)
     └  1× full reset   clears all usage · usable now · use by Dec 9  rlrc_6f1b0d8e · next
```

Whether a credit also resets an additional, model-specific limit is not known, so qswap does not count on it: auto mode never spends a credit for an additional limit alone.

Automatic spending follows the same `resets.*` settings as Claude Code's usage resets, with the same meaning and the same kind rules (see [Usage resets](../README.md#usage-resets)): `resets.auto` is `last-resort` by default (spend when the active account has reached `resets.threshold`, 95 by default, and no switch helps), or `active`, `on-limit`, `off`; `resets.minHoursLeft` keeps a credit when the window resets on its own soon anyway; `resets.expiryDays` spends credits that are about to expire. A credit is spent only for the 5-hour or weekly window, since those are what it resets. Before spending, qswap re-reads the account, reserves the spend in its state file so two engines cannot both spend, and stops after 3 automatic spends in 24 hours. `resets.enabled false` stops reading the credit list.

Each spend carries a request id (a UUID) that qswap stores before sending; when the answer never arrives, the retry sends the same id, so the server cannot spend a second credit for it. An interrupted `qswap codex reset` says so; run `qswap codex reset` to see where things stand.

Spending has only been tested against a fake server so far. The answer codes qswap maps (`reset`, `already_redeemed`, `nothing_to_reset`, `no_credit`) come from the open-source Codex CLI.

## What qswap never does

- It never refreshes a Codex login, live or saved. Codex rotates its own tokens.
- It never runs `codex logout` and never revokes a token. `qswap codex remove` deletes only qswap's saved copy.

Never run `codex logout` on a login qswap has saved: it signs the login out at OpenAI, so every saved copy of it stops working, and you have to log in to that account again.

## Limits

- File storage only. If Codex keeps its login in the OS keyring (`cli_auth_credentials_store = "keyring"` or `"auto"` in `config.toml`), or `auth.json` is missing while a keyring entry exists, every `qswap codex` command stops with one line saying so. A keyring setting forced by managed configuration is noticed only once `auth.json` is gone.
- ChatGPT logins only. An API-key login is refused.
- One live account per Codex home; `qswap codex run` (an account per terminal) is not there yet.
- Process detection uses `ps` and is not available on Windows, where the running-process warnings and the near-expiry check are skipped.
- Codex 0.164 takes neither lock and writes `auth.json` in place, so a 0.164 process renewing its token during a switch can still race it. qswap re-reads the file after writing and reports an error if it changed.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `codex.restartDaemon` | `true` | Restart Codex's background server after a switch so new sessions use the new account. |
| `codex.threshold` | `99` | `qswap codex auto`'s switch line, in place of `autoswitch.threshold` (50 to 99.9). |

`autoswitch.*` (except the threshold) and `resets.*` apply to Codex as they do to Claude Code.

## Files

Codex data lives in `codex/` inside qswap's data directory: `sequence.json` (the accounts), `credentials/account-N.json` (each saved login, byte for byte, mode 0600), `cache/usage.json`, `autoswitch_state.json` (the engine's cooldown, quarantine and credit spends), and `activations.jsonl` (which account was live when, for `qswap usage`). `qswap codex login` creates a `login-home-*` directory there while it runs and deletes it afterwards.
