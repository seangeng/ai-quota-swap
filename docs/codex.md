# Codex accounts

qswap can keep several OpenAI Codex CLI logins (ChatGPT accounts) and switch between them, with live usage for each. It is experimental: it has not yet been run with two real Codex accounts, so treat the first switches with care and keep an eye on the warnings it prints.

## What works today

- `qswap codex add`, `list`, `status`, `switch`, `remove`, `alias`, `disable`, `enable`, `threshold`, `priority`. `qswap codex help` lists them.
- Live usage for every saved account, in `qswap codex list` and in a "Codex accounts" section of `qswap list`. `--json` works on `list`, `status` and `switch` (see [json-output.md](json-output.md)).
- The plan (Plus, Pro, Team, ...) and any usage-reset credits the account holds.
- `qswap usage` attributes Codex tokens to the account that was live, from the switches qswap records.

Not yet: `qswap codex auto`, spending reset credits, and `qswap codex run`.

## Add your first account

Log in with Codex as usual, then save that login:

```bash
codex login
qswap codex add
```

`qswap codex add` captures whatever login is in `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). Running it again for an account that is already saved refreshes the saved copy instead of adding a duplicate.

## Add another account

Do not run `codex login` again in your normal Codex home. Before it logs in, `codex login` signs the current login out at OpenAI, which also kills the copy qswap just saved. Log the next account in under an empty, throwaway Codex home instead:

```bash
tmp="$(mktemp -d)"
CODEX_HOME="$tmp" codex login        # log in with the other account
CODEX_HOME="$tmp" qswap codex add
rm -rf "$tmp"                        # deleting the file does not sign the login out
```

Repeat for each further account. Your normal `~/.codex` stays on the account it was on.

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

## Files

Codex data lives in `codex/` inside qswap's data directory: `sequence.json` (the accounts), `credentials/account-N.json` (each saved login, byte for byte, mode 0600), `cache/usage.json`, and `activations.jsonl` (which account was live when, for `qswap usage`).
