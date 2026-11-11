# Settings

After reading this page you can see every qswap setting, change it, and put it back to its default. The tables below are generated from the code, so they list every setting there is.

## See and change settings

`qswap config` lists every setting and its value. `(default)` marks a setting you have not set:

```text
$ qswap config
autoswitch.threshold              90           (default)
autoswitch.weeklyThreshold        (none)       (default)
autoswitch.intervalSeconds        60           (default)
autoswitch.cooldownSeconds        300          (default)
autoswitch.hysteresisPct          10           (default)
autoswitch.strategy               best         (default)
autoswitch.includeApiKeyAccounts  false        (default)
autoswitch.unhealthyTicks         3            (default)
autoswitch.model                  (none)       (default)
ui.theme                          auto         (default)
run.shareHistory                  true         (default)
updates.check                     true         (default)
usage.headerProbe                 false        (default)
resets.enabled                    true         (default)
resets.auto                       last-resort  (default)
resets.threshold                  95           (default)
resets.minHoursLeft               24           (default)
resets.expiryDays                 7            (default)
resets.expiryThreshold            50           (default)
resets.weeklyForModelLimit        false        (default)
codex.restartDaemon               true         (default)
codex.threshold                   99           (default)
```

Change one, read it back, and return it to its default:

```text
$ qswap config set autoswitch.threshold 85
autoswitch.threshold = 85
$ qswap config get autoswitch.threshold
85
$ qswap config unset autoswitch.threshold
autoswitch.threshold unset (default: 90)
```

`set` checks the value before it writes anything:

```text
$ qswap config set autoswitch.threshold 120
Error: autoswitch.threshold must be between 50 and 99.9
$ qswap config set resets.auto sometimes
Error: resets.auto must be one of: off, active, last-resort, on-limit
```

`qswap config path` prints where the file is. `qswap config --json` and `qswap config get KEY --json` print JSON ([json-output.md](json-output.md)).

## The file

Settings live in `settings.json` in the data directory: `~/.ai-quota-swap/` on macOS and Windows, `$XDG_DATA_HOME/ai-quota-swap/` (default `~/.local/share/ai-quota-swap/`) on Linux and WSL. `qswap config set` writes only the key you name, so a setting you never set follows the default of the version you run. You can edit the file by hand. Keys qswap does not know are kept. A missing or unreadable file means every setting is at its default; qswap logs a warning and carries on.

One file serves both providers. The `autoswitch.*` and `resets.*` settings apply to Claude Code and Codex alike, except the switch line: `qswap codex auto` uses `codex.threshold`.

Flags override settings for one run. `qswap auto --threshold 80` switches at 80% this time and leaves `autoswitch.threshold` as it is.

## Every setting

<!-- BEGIN GENERATED SETTINGS: scripts/settings_doc.py writes this part -->

### Auto-switch (both providers)

| Setting | Default | Meaning | Change it |
|---|---|---|---|
| `autoswitch.threshold` | `90` | The switch line, in percent used. Auto-switch looks for another account once the active account's 5-hour window reaches it, or any window while `autoswitch.weeklyThreshold` is not set. Codex uses `codex.threshold` instead. `qswap threshold` sets it for one account. A number from 50 to 99.9. | `qswap config set autoswitch.threshold 85` |
| `autoswitch.weeklyThreshold` | `(none)` | A separate switch line for the 7-day and per-model windows. Not set: `autoswitch.threshold` covers every window. A number from 50 to 99.9. | `qswap config set autoswitch.weeklyThreshold 97` |
| `autoswitch.intervalSeconds` | `60` | Seconds between auto-switch checks. A number from 15 to 3600. | `qswap config set autoswitch.intervalSeconds 120` |
| `autoswitch.cooldownSeconds` | `300` | The least time between two switches made before a limit. A switch away from an account at its limit does not wait for it. A number from 0 to 86400. | `qswap config set autoswitch.cooldownSeconds 600` |
| `autoswitch.hysteresisPct` | `10` | How many points of headroom a target needs over the active account before a switch below the limit, so two accounts near the line do not trade places on every check. A number from 0 to 50. | `qswap config set autoswitch.hysteresisPct 5` |
| `autoswitch.strategy` | `best` | How auto-switch picks the account to move to. [auto-switch.md](auto-switch.md) explains each. One of `best`, `weekly-first`, `consume-first`, `priority-consume-first`. | `qswap config set autoswitch.strategy weekly-first` |
| `autoswitch.includeApiKeyAccounts` | `false` | Let auto-switch move to an API-key account as a last resort, when the active subscription is at its limit and no other has room. API keys are billed per token, so only a key with a monthly budget or a credit, and a spend figure no older than 20 minutes, is used. Running Claude Code sessions follow the move through the key line; the engine comes back once a subscription has 5 points of room after the cooldown, and leaves the key at once at its budget or credit. [api-accounts.md](api-accounts.md) has the rules. `true` or `false`. | `qswap config set autoswitch.includeApiKeyAccounts true` |
| `autoswitch.unhealthyTicks` | `3` | Checks in a row on which the active account's usage cannot be read before auto-switch moves to another account. A whole number from 1 to 100. | `qswap config set autoswitch.unhealthyTicks 5` |
| `autoswitch.model` | `(none)` | Also switch when one of these models' weekly limits is reached: one name (`Fable`), several (`Fable,Opus`) or `all`, as the per-model rows of `qswap list` name them. Not set: only the 5-hour and 7-day windows count. | `qswap config set autoswitch.model Fable` |

### Usage resets (both providers)

| Setting | Default | Meaning | Change it |
|---|---|---|---|
| `resets.enabled` | `true` | Read which usage resets (Claude) and reset credits (Codex) each account holds, along with its usage. `false` stops reading them. `true` or `false`. | `qswap config set resets.enabled false` |
| `resets.auto` | `last-resort` | When auto-switch spends a reset by itself. [resets.md](resets.md) explains each mode; `qswap reset N` works in every mode. One of `off`, `active`, `last-resort`, `on-limit`. | `qswap config set resets.auto off` |
| `resets.threshold` | `95` | Spend a reset once a window it clears reaches this percent. A number from 50 to 100. | `qswap config set resets.threshold 100` |
| `resets.minHoursLeft` | `24` | Keep a reset when the window it would clear refills on its own within this many hours. A number from 0 to 168. | `qswap config set resets.minHoursLeft 12` |
| `resets.expiryDays` | `7` | Days of runway per unused reset before it expires. Inside that runway, auto mode spends it early rather than lose it. `0` turns this off. A number from 0 to 60. | `qswap config set resets.expiryDays 0` |
| `resets.expiryThreshold` | `50` | Inside that runway, spend the reset once the window it is for reaches this percent. A number from 0 to 100. | `qswap config set resets.expiryThreshold 70` |
| `resets.weeklyForModelLimit` | `false` | Let auto mode spend a weekly or full reset when only a per-model limit, such as Fable's, is reached. `qswap reset N` is never held back by it. `true` or `false`. | `qswap config set resets.weeklyForModelLimit true` |
| `resets.beforeSwitch` | `false` | Under `resets.auto` `active` or `on-limit`, stay on the active account past its switch line while it holds a reset of its own that auto mode spends at `resets.threshold`, and spend it there instead of moving at the line. With `resets.threshold` 100 the account is at its limit until the next check spends the reset. No effect under `off` and `last-resort`; see [resets.md](resets.md#when-auto-mode-spends-one). `true` or `false`. | `qswap config set resets.beforeSwitch true` |

### Codex

| Setting | Default | Meaning | Change it |
|---|---|---|---|
| `codex.restartDaemon` | `true` | After `qswap codex switch`, restart Codex's background server so new sessions use the new account. The restart interrupts work in progress on it. `true` or `false`. | `qswap config set codex.restartDaemon false` |
| `codex.threshold` | `99` | `qswap codex auto`'s switch line, in place of `autoswitch.threshold`. High by default because a Codex switch interrupts work in progress. A number from 50 to 99.9. | `qswap config set codex.threshold 90` |

### Sessions

| Setting | Default | Meaning | Change it |
|---|---|---|---|
| `run.shareHistory` | `true` | `qswap run` sessions read and write the one chat history in `~/.claude`. `false` gives each account its own; `--no-share-history` does that for one launch. `true` or `false`. | `qswap config set run.shareHistory false` |

### Usage readings

| Setting | Default | Meaning | Change it |
|---|---|---|---|
| `usage.headerProbe` | `false` | Read a setup-token account's 5-hour and 7-day usage from the rate-limit headers of a one-token Haiku request, since the usage endpoint does not answer setup-tokens. Each probe is a real request on that account. `true` or `false`. | `qswap config set usage.headerProbe true` |

### Appearance

| Setting | Default | Meaning | Change it |
|---|---|---|---|
| `ui.theme` | `auto` | Colours of the dashboard and of command output. `auto` follows the terminal's background. One of `dark`, `light`, `auto`. | `qswap config set ui.theme light` |

### Updates

| Setting | Default | Meaning | Change it |
|---|---|---|---|
| `updates.check` | `true` | Look up the newest release once a day and print a hint when there is one. `qswap upgrade` works either way. `true` or `false`. | `qswap config set updates.check false` |

<!-- END GENERATED SETTINGS -->

## Settings kept elsewhere

- Per account: `qswap threshold` (the account's own switch line), `qswap priority` (its tier for `priority-consume-first`), `qswap disable` and `enable`, `qswap alias`, and `qswap budget` and `qswap credit` for API-key accounts. They are stored with the account, and `qswap codex threshold`, `priority`, `disable`, `enable` and `alias` do the same for Codex accounts.
- Directory mappings for `qswap run`: `qswap map` ([sessions.md](sessions.md)).
- The macOS menu bar's display and auto-switch toggles: `menubar_settings.json`, changed from its *Settings* menu ([menubar.md](menubar.md)).
- Your own model prices for `qswap usage`: `pricing.json` ([usage.md](usage.md)).

## Environment variables

| Variable | Effect |
|---|---|
| `CLAUDE_CONFIG_DIR` | Where Claude Code keeps its configuration; qswap follows it, as Claude Code does. |
| `CODEX_HOME` | Where Codex keeps `auth.json` and its sessions (default `~/.codex`); qswap follows it. |
| `XDG_DATA_HOME` | Linux and WSL only: the data directory is `$XDG_DATA_HOME/ai-quota-swap`. |
| `ANTHROPIC_ADMIN_KEY` | An Admin API key for one command, used while no organization is registered and never stored ([api-accounts.md](api-accounts.md)). |
| `QSWAP_CLAUDE_CLI_VERSION` | The Claude Code version qswap names when it reads reset status, in place of the installed one it detects. |
| `AI_QUOTA_SWAP_NO_BUNDLE` | Set to `1` to run the macOS menu bar without its wrapper app ([menubar.md](menubar.md)). |
| `NO_COLOR` | Plain output, no colour. |
