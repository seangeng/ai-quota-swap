<!-- README rule: say what the user can do, give a minimal example, and mention only caveats that change their decision.
     Mechanics go in docs/, rationale in the PR. When behavior changes, edit the existing text instead of appending. -->

# ai-quota-swap

Switch between multiple Claude Code accounts by live quota. Switch by hand without logging out, or let it switch for you before you hit a rate limit. Track usage for every account in a live dashboard, run accounts in parallel, and spend usage resets automatically. Works with both the Claude Code CLI and the VS Code extension. The command is `qswap`.

It also keeps several OpenAI Codex CLI logins and switches between them (experimental, see [Codex accounts](#codex-accounts-experimental)), and `qswap usage` reports tokens and estimated cost per account. What comes next is in [ROADMAP.md](ROADMAP.md).

## Installation

ai-quota-swap is not on PyPI yet (a release is coming), so install it from the repository.

### Using uv (recommended)

```bash
uv tool install git+https://github.com/seangeng/ai-quota-swap
```

### Using pipx

```bash
pipx install git+https://github.com/seangeng/ai-quota-swap
```

### From source

```bash
git clone https://github.com/seangeng/ai-quota-swap.git
cd ai-quota-swap
uv sync
uv run qswap help
```

### Updating

```bash
qswap upgrade          # uv/pipx installs on macOS/Linux: auto-detects and upgrades
# or run your installer directly:
uv tool upgrade ai-quota-swap
pipx upgrade ai-quota-swap
```

### Coming from claude-swap

ai-quota-swap keeps its own data directory and Keychain entries, so it starts empty even when claude-swap is installed. Move your accounts across with an export. Stop claude-swap's background work first (its menu bar, and any `cswap auto` loop or cron entry), so it cannot refresh a login between the export and the import:

```bash
cswap menubar --uninstall-service         # macOS, if you used its menu bar
cswap export ~/claude-swap-export.json    # with claude-swap
qswap import ~/claude-swap-export.json    # with ai-quota-swap
rm ~/claude-swap-export.json              # it holds your logins in plain text
```

An export carries the accounts and their aliases, nothing else. Set preferences again with `qswap config`, per-account thresholds, priorities and disabled flags with `qswap threshold`, `qswap priority` and `qswap disable`, and directory mappings with `qswap map`. Then remove claude-swap:

```bash
cswap purge                               # deletes claude-swap's copies of your logins
uv tool uninstall claude-swap             # or: pipx uninstall claude-swap
```

Do not run both tools against the same accounts. Each one refreshes the logins it has saved, a refresh replaces the token, and the other tool is left holding logins that no longer work.

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
qswap add
```

### Add more accounts

Log in with another account, then:

```bash
qswap add
```

Do not run `/logout` first: current Claude Code may revoke the refresh token stored for the account you are leaving.

### Switch accounts

Rotate to the next account:

```bash
qswap switch
```

Or switch to a specific account:

```bash
qswap switch 2
qswap switch user@example.com
qswap switch dev                # or by alias, once set with `qswap alias 2 dev`
```

Not sure which one? `qswap list` is the dashboard — every account's plan, 5-hour and 7-day usage and reset times at a glance:

```bash
qswap list
```

Or let ai-quota-swap auto-pick by remaining quota — `qswap switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Automatic switching

Let ai-quota-swap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account with the most quota left — before you hit the limit, and safe to run while Claude Code is working:

```bash
qswap auto                     # foreground loop, polls every 60s
qswap auto --threshold 80      # switch earlier
qswap auto --weekly-threshold 97   # let the weekly window run higher before switching
qswap auto --model Fable       # also switch when the Fable weekly limit is hit
qswap auto --strategy weekly-first   # spend the soonest-expiring weekly quota first (more strategies below)
qswap auto --once              # single check-and-switch, for cron/scripts
qswap auto --dry-run           # log what it would do, never switch
```

<details>
<summary>Strategies, per-account controls & behavior</summary>

**Strategies** (`--strategy`, or `qswap config set autoswitch.strategy`):

| Strategy | Switches | Lands on |
|---|---|---|
| `best` (default) | when the active account reaches its threshold | the account with the most quota left |
| `weekly-first` | same as `best` | the account whose weekly window resets soonest |
| `consume-first` | at the threshold, and earlier whenever an account that resets sooner has room | the account whose weekly window resets soonest |
| `priority-consume-first` | whenever a higher-tier account is usable | your highest tier (`qswap priority`), then the soonest weekly reset |

The weekly ones exist because weekly quota left unused at its reset is lost.

**Per-account controls**

- `qswap disable <num|email>` holds an account out of rotation: auto-switch, bare `qswap switch`, and the `best` / `next-available` strategies skip it until `qswap enable` puts it back. It keeps its login and is still a valid explicit `qswap switch <num|email>` target.
- `qswap threshold <num|email> <pct>` gives one account its own switch-away line (50–99.9) in place of the global one.
- `qswap priority <num|email> <tier>` ranks accounts for `priority-consume-first`: higher tiers (0–1000, default 0) are used first. The other strategies ignore tiers.

**Good to know**

- **Thresholds are switch-away lines, not hard caps.** With nothing under its line, or the active account at its limit, auto-switch still lands somewhere rather than stall. `priority-consume-first` is the exception: it never lands on an account at or over its line, so it holds and keeps polling.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping between accounts near the threshold.
- `--weekly-threshold 97` gives the 7-day and per-model windows their own line, so the weekly budget can run higher than the 5-hour window. `--threshold`, and any per-account threshold, then governs the 5-hour window only.
- `--model Fable` folds that model's weekly limit into the decision, for when you hit a per-model limit before the account-wide ones. Names are the per-model rows in `qswap list` (e.g. `Fable: 100%`), matched case-insensitively.
- Accounts with no 5-hour window (Ultra) are judged on their 7-day and `--model` windows alone.
- If a usage check fails it keeps using the last-known numbers for a limited time while it retries, and an expired token on an idle machine makes it hold rather than fail over.
- An account whose refresh token has died is quarantined and reported until you [re-add it](#refresh-expired-tokens).
- API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * qswap auto --once --json >> ~/.qswap-auto.log 2>&1
```

Defaults like the threshold and cooldown are configurable with `qswap config set autoswitch.threshold 80` — flags override them (see [Configuration](#configuration)).

</details>

The exact rules (margins, landing, polling cadence, tier handling) are in [docs/auto-switch.md](docs/auto-switch.md).

### Usage resets

Anthropic occasionally grants eligible plans banked **usage resets** (Claude Code's `/limit-reset`): each one sets the windows it clears back to 0% when you choose, and is lost if unused by its date. ai-quota-swap reads every account's resets along with its usage and can spend them for you:

```bash
qswap reset                    # every account's resets, and what auto would use now
qswap reset 2                  # use one on account 2 (asks first; --yes to skip)
qswap config set resets.auto on-limit   # how auto-switch spends them (below)
```

Resets are not interchangeable. A Fable reset clears only Fable usage; a weekly reset clears all weekly usage. Spend the wrong one and it either does nothing for the limit you hit or uses up a reset worth more. Each reset has a kind, read from the windows it clears:

| Kind | Clears | Auto mode spends it when |
|---|---|---|
| Fable reset (or another model's) | that model's weekly limit, and gives back what the model used of the all-models weekly limit | that model's limit is reached |
| 5-hour reset | the 5-hour window | the 5-hour window is reached |
| weekly reset | the all-models weekly window, and any per-model window it also lists | the all-models weekly window is reached |
| full reset | the 5-hour and the all-models weekly windows (every Codex credit is one) | the 5-hour or the all-models weekly window is reached |

When two resets would both clear the limit you hit, auto mode spends the narrower one: a Fable reset before a weekly one, even when the weekly one is listed first or expires sooner. It never spends a weekly or full reset when only a per-model limit is reached, unless you allow it with `qswap config set resets.weeklyForModelLimit true`; `qswap reset` says when that rule is the only thing keeping a reset back. A reset whose shape qswap doesn't recognize, or that lists a window qswap doesn't know, is shown as "reset" with the windows it names and is never spent automatically.

`qswap reset N` can spend any kind. Without `--grant` it picks the narrowest reset that clears a limit you've hit, and before you confirm a weekly or full reset on a per-model limit it says what you'd give up: "This weekly reset clears all weekly usage; only Fable is at its limit (7d is at 31%)."

`qswap list` shows a `resets:` row under any account that holds one, counted by kind once the account holds more than one kind (`2 Fable · 1 weekly · use by Mar 9`); the dashboard and the menu bar count them the same way. Whenever the auto engine runs (`qswap auto`, the dashboard's live mode, the menu bar), `resets.auto` decides when it spends one, at most one per tick:

| `resets.auto` | Uses a reset when |
|---|---|
| `last-resort` (default) | the active account reaches `resets.threshold` (95%) on a window a reset clears, and switching accounts can't relieve it: no account has room there, or the engine doesn't watch that window (a Fable limit without `--model Fable`). If the active account holds no useful reset, it refills an account the engine can then switch to. |
| `active` | the account in use reaches `resets.threshold` on a window one of its own resets clears: it refills rather than switching away. Pair with `resets.threshold 100` to spend only at the limit. |
| `on-limit` | any account in rotation reaches `resets.threshold` on a window a reset clears. |
| `off` | never; `qswap reset N` still works. |

<details>
<summary>Guards & settings</summary>

- **Worth it, or kept.** A reset is skipped when its window refills on its own within `resets.minHoursLeft` (24h), or when another window would still sit over the line afterwards: a Fable reset does nothing for an account whose all-models weekly window stays at 98%, and a weekly reset does nothing for one whose Fable window, if the reset doesn't list it, stays at 100%. A Fable reset that also gives back Fable's share of the weekly window gets credit for it. A Fable reset is never spent for the all-models weekly limit itself, even though Claude Code offers one there.
- **Not lost to expiry.** Every auto mode also spends a reset once its expiry is within `resets.expiryDays` (7) per reset held, at `resets.expiryThreshold` (50%), measured on the window that reset is for: the all-models weekly window for a weekly reset, the model's window for a Fable reset. Three resets expiring together start 21 days out, and the one closest to its deadline goes first, whatever its kind.
- `resets.weeklyForModelLimit` (off) lets auto mode spend a weekly or full reset when only a per-model limit is reached.
- **Never twice on old numbers.** After a claim, the account is held out until a reading taken after it arrives. An unconfirmed claim is retried with the same request id, so it can't spend a second reset. Auto spends at most 3 a day across all accounts, and never on disabled or quarantined accounts. `--dry-run` only reports.
- `resets.enabled false` stops reading reset status. The status rides on the normal usage request, with Claude Code's client agent string, because the server only reports resets to Claude Code.

</details>

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
qswap run 2                     # launch Claude Code as account 2, here only
qswap run user@example.com      # by email
qswap run 2 -- --resume         # everything after '--' is forwarded to claude
qswap run 2 --no-share-history  # keep this account's chat history to itself
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, MCP servers, etc.) and share its chat history, so a conversation started under one account shows up in `--resume` under the others.

<details>
<summary>How it behaves — chat history, MCP servers, tokens</summary>

- **Chat history.** Shared by default: every account reads and writes the history in `~/.claude`. History an account kept on its own before is merged in on its next launch, and nothing already saved is lost (a file that differs on both sides is kept aside in `history-conflicts/` in the backup directory). `qswap config set run.shareHistory false` gives each account its own history; `--no-share-history` does it for one launch.
- **On Windows**, conversations are shared through a directory junction, which needs no special rights; the prompt-history file (`history.jsonl`) is shared only when Windows lets qswap create a symlink (Developer Mode, or an elevated terminal) and otherwise stays per-account.
- **MCP servers.** User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch — manage them there; changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not — HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` stops sharing your `~/.claude` setup (settings, skills, etc.) and removes the mirrored MCP config (profiles that never mirrored are left alone). Chat history is separate: it stays shared unless you also pass `--no-share-history`.
- **Your default account.** Running the account that is already your default login launches plain `claude` on that login instead of a session (a second copy of the active credential would go stale). Scripts that need the isolation guaranteed can pass `--require-session`, which refuses instead.
- **Tokens.** A session refreshes its own copy of the account's token; once it exits, the credential it rotated is captured back into the account's stored backup before a switch, swap, move, or usage check uses it. While the session is still running, `qswap switch` refuses to move the default login onto its account if the stored backup has already fallen behind (activating it could only fail) — exit the session first, or pick another account.
- **Usage.** While a session runs, its account's usage is read with the session's own credential, which qswap never refreshes. A read the server refuses shows as token expired, and isn't retried, until the session renews the credential on its next call.

</details>

<details>
<summary>Map accounts to directories — auto-pick per repo</summary>

Bind a directory to an account, and a bare `qswap run` there launches that account in session mode — e.g. work account in work repos, personal elsewhere:

```bash
qswap map 2 ~/work/client-app   # map a directory to account 2
qswap map user@example.com      # map the current directory
qswap map                       # list mappings
qswap unmap ~/work/client-app   # remove one (defaults to current directory)

cd ~/work/client-app/src
qswap run                       # → account 2, session mode
```

Subfolders inherit the nearest mapped ancestor. In an unmapped directory, `qswap run` just launches plain `claude` with your default login. Mappings are per-machine (not part of `qswap export`) and are cleaned up when their account is removed.

</details>

### Codex accounts (experimental)

```bash
codex login && qswap codex add      # save the Codex login you are using
qswap codex login                   # log in another account and save it; the live one stays
qswap codex list                    # every saved Codex account with its usage
qswap codex switch 2                # make account 2 the live Codex login
qswap codex auto                    # switch automatically when the active account runs out
qswap codex reset                   # list (and spend) Codex usage reset credits
```

Add further accounts with `qswap codex login`, not plain `codex login`: in your normal Codex home, `codex login` signs the current login out first. Never run `codex logout` on a login qswap has saved; it kills every saved copy. A switch restarts Codex's background server and warns about sessions and the desktop app that keep the old account, so `qswap codex auto` switches only once the active account is out (`codex.threshold`, default 99). Steps and limits: [docs/codex.md](docs/codex.md).

### Interactive dashboard (TUI)

Run `qswap` on its own (or `qswap tui`) for the full-screen dashboard: live usage for every account, switching, and the auto-switcher, all keyboard-driven. `qswap watch` opens it straight to the live monitor. Works on macOS, Linux, and Windows.

<img src="assets/tui-watch.png" width="760" alt="qswap watch — live 5h/7d usage bars for every account, with reset times and the active account marked">

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
qswap add
```

This will update the stored credentials without creating a duplicate.

If the refresh token itself has died (auto-switch quarantines and reports such an account), log in with it and re-run `qswap add --slot N`, or replace its stored credentials from a known-good export: a plain `qswap import backup.qswap` replaces dead-token slots on its own (a stale export can carry an already-superseded token).

### Other commands

```bash
qswap run 2                     # Run an account in this terminal only (session mode)
qswap auto                      # Auto-switch when nearing rate limits (see above)
qswap reset                     # List usage resets; `qswap reset 2` uses one (see above)
qswap usage                     # Tokens and estimated API cost for the last 7 days (docs/usage.md)
qswap usage --by account        # ... by account; also --by model|project|provider|session, --days N
qswap config                    # Show or edit settings (see Configuration below)
qswap list                      # Show all accounts with 5h/7d usage and reset times
qswap list --token-status       # Add source-labelled OAuth token diagnostics
qswap status                    # Show current account
qswap add --slot 3              # Add account to a specific slot (prompts before overwrite)
qswap add --alias dev           # Add account and give it a short alias
qswap remove 2                  # Remove an account
qswap disable 2                 # Hold an account out of auto-rotation (keeps its login)
qswap enable 2                  # Return a disabled account to rotation
qswap threshold 2 85            # Switch away from account 2 at 85%, not the global default
qswap threshold 2 --unset       # Return account 2 to the global default
qswap threshold                 # Show the global default and every per-account override
qswap priority 2 100            # Use account 2 before lower tiers (priority-consume-first strategy)
qswap priority 2 --unset        # Return account 2 to tier 0
qswap priority                  # Show every account's tier
qswap alias 2 dev               # Give an account a short alias (usable anywhere NUM|EMAIL is)
qswap alias 2 --unset           # Remove an account's alias
qswap alias                     # List all aliases
qswap move 2 1                  # Assign an account to a slot (relocates to an empty slot, swaps if taken)
qswap swap 2 3                  # Exchange two accounts' slot numbers
qswap unclaimed                 # List stashed credential entries (slot + why they were stashed)
qswap unclaimed --purge ID      # Drop one (deletes its bytes; recover with /login + `qswap add`)
qswap tui                       # Interactive dashboard (also: bare `qswap`)
qswap watch                     # Dashboard, opened on the live watch page
qswap upgrade                   # Upgrade ai-quota-swap to the latest version
qswap purge                     # Remove all ai-quota-swap data
```

The original flag spellings (`qswap --switch`, `qswap --list`, ...) keep working.

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `qswap switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps only the account-specific Claude login when you switch accounts;
  live account-independent OAuth state (such as MCP server logins) is
  preserved instead of being overwritten by a slot's older snapshot
- Account credentials stored securely using platform-appropriate methods
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died until you [re-add them](#refresh-expired-tokens)
- Usage numbers refresh every few minutes — faster for an account being used or close to switching, slower for idle ones — keeping qswap comfortably inside Anthropic's rate limits however many dashboards you keep open on a machine. An age note like `· 6m ago` just means the next scheduled check hasn't come yet, not that something is stuck.
- The plan shown after each slot (`· Ultra`, `· Pro`, `· Team`) comes from your account profile and refreshes every six hours, independently of usage; a subscription that has lapsed shows as a highlighted `Free`.

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.ai-quota-swap/` |
| macOS | macOS Keychain | `~/.ai-quota-swap/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/ai-quota-swap/` |

Session-mode profiles (`qswap run`) live under the backup directory in `sessions/`. Tool preferences (`settings.json`) and auto-switch state (`autoswitch_state.json` — cooldown and quarantined accounts; delete it to reset) live in the backup directory root, as do the token ledger behind `qswap usage` (`ledger.db`, rebuilt from your session files if deleted), the record of which account was live when (`activations.jsonl`), and your own prices (`pricing.json`, optional).

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

## Menu bar (macOS)

<details>
<summary>Optional macOS menu bar app — usage at a glance, click to switch</summary>

Needs the `menubar` extra (macOS only):

```bash
uv tool install 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'
# or: pipx install 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'
qswap menubar
```

Shows every account's 5h / 7d / spend usage and switches with a click (specific / rotate / best / next-available), plus the TUI's add / disable-enable / remove / refresh actions. `qswap menubar` hands it to launchd and returns: it keeps running after you close the terminal, comes back at login, and is restarted if it crashes. *Quit* in the menu stops it until the next login; *Settings → Start at login* turns the login start off.

Auto-switching is off until you turn it on, with *Settings → Auto-switch accounts* or `qswap menubar --auto-switch`. It runs the same engine as [`qswap auto`](#automatic-switching) with the same `autoswitch.*` settings, so the menu bar and CLI stay in sync, and the menu's first row shows whether it is running, off, or stopped (click to restart) and when usage was last refreshed.

```bash
qswap menubar --service-status      # running? pid? starts at login? auto-switch on?
qswap menubar --auto-switch         # turn auto-switch on (--no-auto-switch: off)
qswap menubar --uninstall-service   # stop it and remove the LaunchAgent
qswap menubar --foreground          # run attached to this terminal, for debugging
```

`qswap upgrade` restarts a running menu bar when the build changed. The wrapper app in `/Applications`, file locations, environment handling and the Login Items switch are in [docs/menubar.md](docs/menubar.md).

</details>

## Advanced

### Configuration

Tool preferences live in `settings.json` in the backup root; `qswap config` reads and edits it with validation, so you never have to find the file or guess valid ranges.

<details>
<summary>Commands & usage</summary>

```bash
qswap config                              # list effective settings ("(default)" = not set)
qswap config get autoswitch.threshold
qswap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
qswap config set autoswitch.weeklyThreshold 97  # own switch line for the 7-day/per-model windows
qswap config set autoswitch.model Fable   # per-model switching (see "auto"); Fable,Opus for several
qswap config set run.shareHistory false   # each account keeps its own chat history (see "run")
qswap config set updates.check false      # no daily PyPI lookup for the "newer version" hint
qswap config set usage.headerProbe true   # opt in: setup-token usage via a 1-token request (see "add-token")
qswap config set resets.weeklyForModelLimit true  # let auto spend a weekly reset on a Fable limit (see "Usage resets")
qswap config unset autoswitch.threshold   # back to the default
qswap config path                         # where settings.json lives
```

`qswap config --help` lists every key with its valid range and default. Hand-editing the file still works — `qswap config` is just a safer front door. `list` and `get` take `--json` for scripting.

</details>

### Backup and migration

Move account data between machines or back it up:

```bash
qswap export backup.qswap                    # All accounts to a file
qswap export backup.qswap --account 2        # One account
qswap export backup.qswap --full             # Include full ~/.claude.json and credential object (same-PC backup)
qswap import backup.qswap                    # Skips accounts that already exist
qswap import backup.qswap --force            # Overwrite existing
```

The export file is plaintext JSON and, by default, carries only each account's own login — machine-shared MCP/plugin OAuth tokens and the device token stay on the source machine (`--full` keeps everything, for same-PC backups). If you need encryption, pipe through your tool of choice (e.g. `qswap export - | gpg -c > backup.gpg`).

If an imported account is the one you're currently logged in as, activate the imported credentials with `qswap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

### Share usage readings between machines

Machines that hold the same accounts can end up sharing one usage-endpoint rate limit, so their polling adds up (this is the limit on how often usage can be read, not your subscription quota). `import-usage` lets one machine poll and hand its readings to the others:

```bash
qswap list --json | ssh laptop qswap import-usage - --hold 600
```

<details>
<summary>How it works — matching, holds & when a hold ends</summary>

Machines share that rate limit when they share a login (moved between them with `export`/`import`, so the same token is live on each), or when the account's usage requests are limited per account rather than per token.

The input is `qswap list --json` output. Each row with `usageStatus: "ok"` is matched to a local account by email and organization, and adopted when it is newer than the reading already stored. Its age comes from `usageAgeSeconds`, so the two machines' clocks never have to agree; a script that delays the hand-over should add the delay to that field. `--hold SECONDS` keeps every collector on the receiving machine (`list`, `status`, `auto`, the dashboard, the menu bar) from fetching those accounts for that long, and the held reading stays trusted for switch decisions meanwhile. A hold never runs past the reading's earliest window reset (per-model windows included), nor past an hour after the reading was taken. Renew it with each hand-over, or lift it early with `--hold 0`; when it lapses, the machine goes back to fetching for itself.

</details>

### JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
qswap list --json                   # all accounts with usage/quota
qswap status --json                 # current active account
qswap switch --strategy best --json # switch, then report the result
qswap switch 2 --json
```

<details>
<summary>Example output</summary>

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

</details>

`qswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

Every optional field (usage age, last-known usage, fetch errors, pace, login expiry, …) is listed in [docs/json-output.md](docs/json-output.md).

### Add an account from a raw token or API key

Register a long-lived setup-token (from `claude setup-token`) or a Console API key (`sk-ant-api03-...` workspace key, `sk-ant-usr-...` personal key, or a service-account key) directly, without the browser login — useful on headless servers or when receiving a token from another machine. The token type is auto-detected:

```bash
qswap add-token sk-ant-oat01-...             # OAuth setup-token
qswap add-token sk-ant-api03-...             # API key (any Console key family)
qswap add-token sk-ant-oat01-... --slot 3
qswap add-token - --slot 3                   # read token from stdin
qswap add-token --email user@example.com     # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local` (or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

<details>
<summary>Limits — setup-token usage & API-key accounts</summary>

- **Setup-tokens show `usage unavailable` by default**, because the usage endpoint does not answer them (their scope is inference-only). With `qswap config set usage.headerProbe true`, qswap reads their 5h/7d utilization (and Fable's weekly window) from the rate-limit headers of a 1-output-token Haiku request instead, sent at the normal poll cadence. Each probe is a real (tiny) request on that account's subscription, which is why it is off by default.
- **What the probe can't do.** Other per-model windows and extra-usage spend are not in those headers. A reading missing the 5h or 7d window (or a window `autoswitch.model` names) is shown but not used for auto-switching. An account the server rejects for quota is marked as at its limit, with no further probe, until the refusal's `Retry-After` passes.
- **API-key accounts.** Any bare `sk-ant-...` value that is not an OAuth token (`sk-ant-oat...`) registers a managed API-key account (the kind Claude Code uses after `/login` with a key). It switches like any other account, but API keys have no subscription quota, so they show no usage and the usage-aware `switch` strategies never skip them as rate-limited. Use keys scoped to a single workspace: a multi-workspace key needs an `anthropic-workspace-id` header on every request, which Claude Code does not add for a stored key.

</details>

**Note:** Third-party providers configured through `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` are not managed or switched; those exported variables stay in effect after a switch, so unset them to use the swapped account.

## Documentation

- [docs/auto-switch.md](docs/auto-switch.md) — the exact auto-switch rules: hysteresis margins, landing rules, per-account thresholds and tiers, polling cadence. Read it to find out why it did or didn't switch.
- [docs/json-output.md](docs/json-output.md) — every field in `--json` output, including the optional ones. Read it before writing a script against `qswap list --json` or `qswap auto --json`.
- [docs/codex.md](docs/codex.md) — Codex accounts: adding a second account without signing the first out, what a switch does to running sessions, the background server and the desktop app, `qswap codex auto`, usage reset credits, and what qswap never does. Read it before your first Codex switch.
- [docs/usage.md](docs/usage.md) — `qswap usage`: what it reads, what the token and cost columns mean, why the cost is an estimate, your own prices, and how usage is attributed to accounts. Read it before trusting the account split.
- [docs/menubar.md](docs/menubar.md) — how the macOS menu bar runs under launchd: start at login, the wrapper app in `/Applications`, file locations, upgrades, the Login Items switch. Read it when the icon is missing or the service misbehaves.

`qswap --help` and `qswap <command> --help` document every flag.

## Uninstall

Remove all data:

```bash
qswap purge
```

Then uninstall the tool:

```bash
uv tool uninstall ai-quota-swap
# or
pipx uninstall ai-quota-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT

Built on [claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol.
