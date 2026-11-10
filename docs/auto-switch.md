# Switch accounts automatically

`qswap auto` watches the active account's usage and switches to another account before it runs out. After reading this page you can pick a strategy, set switch lines, read the log, and leave the engine running unattended.

## Start it

```text
$ qswap auto
Auto-switch running: switch at 90% · checks every 60s · Ctrl-C to stop
14:20:00  #1 personal at 5h 86% (switch at 90%) · no switch needed
            #2 work         5h 12% · 7d 21%
            #3 night-shift  5h 93% · 7d 81% · over its switch line
14:21:00  #1 personal at 5h 88% (switch at 90%) · no switch needed
14:22:00  Switched #1 personal → #2 work · 5h reached 91%
            #2 work         5h 12% · 7d 21%
            #3 night-shift  5h 93% · 7d 81% · over its switch line
```

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/screenshots/raw/auto.png">
  <img src="../assets/screenshots/raw/auto-light.png" alt="qswap auto: two checks of #1 personal below its 90% line, then a switch to #2 work when its 5h window reaches 91%" width="800">
</picture>

It runs in the foreground until you press Ctrl-C. Other ways to run it:

```bash
qswap auto --dry-run --verbose   # say what it would do; never switch or spend a reset
qswap auto --once; echo $?       # one check; the outcome is the exit code
qswap auto --threshold 85 --weekly-threshold 97
```

Flags override the [settings](#settings) for that run only. A value out of range is clamped, with a warning such as `Warning: --interval 5 is out of range; using 15s`.

After a switch, a running Claude Code picks up the new login within about 30 seconds on macOS (its Keychain cache) and from its next message where the login is kept in a file. The default line is 90 rather than 95 to cover that delay and a heavy turn that keeps spending meanwhile. Switches take the same credential locks Claude Code uses, so a switch never lands in the middle of Claude Code refreshing its token.

## How it picks the next account

Choose a strategy with `--strategy NAME` or `qswap config set autoswitch.strategy NAME`.

`best` (the default) stays on the active account until one of its windows reaches its switch line, then moves to the account with the most room left under its own lines.

`weekly-first` switches at the same moment as `best` and lands only where `best` could. Among the accounts that qualify, it picks the one whose weekly window resets soonest, so weekly quota that would expire unused is spent first. When it has to escape (the active account at its limit, or its usage unreadable) it ranks by room left, as `best` does.

`consume-first` does not wait for the line. Whenever another account with room has a weekly window that resets sooner than the active account's, it moves there. While the active account's weekly reset time is unknown it stays put, and the log says `no weekly reset known yet`.

`priority-consume-first` follows the tiers you set with `qswap priority`: the highest tier that has an account under all of its lines, then the soonest weekly reset within that tier. It moves back up as soon as a higher-tier account is usable again, after the cooldown. It never lands on an account at or over a switch line, even when the active account is at its limit; it holds and keeps checking instead.

For a one-off pick by hand, `qswap switch --strategy best` jumps to the account with the most quota left (and stays put if it cannot prove one has more), and `qswap switch --strategy next-available` moves to the next account in order, skipping any at their limit. Both skip disabled accounts and accounts that need re-login, and both count the per-model limits in `autoswitch.model` unless you pass `--model`.

## Switch lines

A switch line is the percent used at which auto-switch leaves an account. Every window is compared against its own line, and the window closest to its line decides. The log quotes that comparison: `#1 personal at 5h 86% (switch at 90%)`. A line is where the engine starts looking, not a hard cap: with no account under its lines, every strategy except `priority-consume-first` still moves to the account with the most room (see [Cooldown and hysteresis](#cooldown-and-hysteresis)).

- **The global line**, `autoswitch.threshold` (default 90; `--threshold`), applies to the 5-hour window, and to every window while no weekly line is set.
- **The weekly line**, `autoswitch.weeklyThreshold` (unset; `--weekly-threshold`), takes over the weekly window and any per-model window, leaving the global line to the 5-hour window. The 5-hour window refills in hours, so running past its line costs a turn; the weekly window is your budget for the week, so you may want to run it closer to full: `qswap config set autoswitch.weeklyThreshold 97`. `qswap config unset autoswitch.weeklyThreshold` goes back to one line.
- **A line per account**, set with `qswap threshold` (50 to 99.9), replaces the global line for that account. With a weekly line set, it applies to that account's 5-hour window only. `qswap threshold 3 --unset` removes it.

  ```text
  $ qswap threshold 3 85
  Set the switch line of #3 night-shift (ops@example.net) to 85%
  $ qswap threshold
  Switch lines
    default (autoswitch.threshold)  90%
    #3 night-shift (ops@example.net)  85%
  ```

- **Priority tiers**, set with `qswap priority 2 5` (a whole number from 0 to 1000; default 0), are read only by `priority-consume-first`. `qswap priority` lists them and `qswap priority 2 --unset` returns an account to 0.
- **Per-model limits.** By default only the 5-hour and weekly windows count. If you run into a model's own weekly limit first, add `--model Fable` or `qswap config set autoswitch.model Fable` and that window counts too. Use the names of the per-model rows in `qswap list` (case does not matter); separate several with commas, or give `all`. A name that no account reports gets one `Warning:` line in the log.
- **Accounts with no 5-hour window.** When the usage API reports no 5-hour window for an account (an Ultra plan has none), it is judged on its weekly window and any `--model` windows alone.

## Cooldown and hysteresis

Four rules keep the engine from bouncing between accounts.

- **Cooldown.** After an automatic switch, the engine waits `autoswitch.cooldownSeconds` (default 300; `--cooldown`) before the next switch made at a line; the log says `cooling down, 4m left`. Leaving an account that is at its limit, over its budget, or unreadable does not wait.
- **Hysteresis.** Under `best` and `weekly-first`, a switch made at the line lands only on an account that is under all of its own lines and has at least `autoswitch.hysteresisPct` (default 10) points more room than the active account. Two accounts close to the line never trade places.
- **No straight return.** After a switch made at the line, the engine does not go back to the account it left until that account has recovered: more room than when it was left, or a reset that came nearer. A switch you make by hand lifts this.
- **Everyone over the line.** When the active account and every other account are at or over their lines, there is nowhere healthy to land, so `best`, `weekly-first` and `consume-first` move to the account with the most room before a limit, once it has twice the active account's remaining room or 3 points more, whichever is less. `priority-consume-first` holds.

When every account is at its limit, the log says `All accounts are at their limit; the first resets 15:32 · in 1h 12m`, and the engine sleeps until that reset, checking at least every 10 minutes because quota can come back early.

If an account holds a usage reset, the engine may spend it instead of switching, under the `resets.*` settings ([resets.md](resets.md)).

## Holding accounts out

`qswap disable 4` holds an account out of rotation: auto-switch and a bare `qswap switch` skip it, and `qswap switch 4` can still pick it. `qswap enable 4` puts it back. The log lists it as `disabled`.

Auto-switch also skips an account that a running `qswap run` session is using ([sessions.md](sessions.md)).

## API-key accounts and budgets

An API-key account has no usage windows, so auto-switch never ranks it against subscriptions, and the log lists it as `API key, not used`. By default, when an API-key account is active, the engine leaves it there: `#5 ci is an API key · no quota to watch`.

`--include-api-key-accounts` (or `autoswitch.includeApiKeyAccounts true`) makes API-key accounts a last resort: the engine lands on one only when it must leave the active account and no subscription has room. With this on, the engine does not stay on an active API-key account either. It counts the account's usage as unreadable (`#5 ci is an API key · failover in 2 more checks`) and, after `autoswitch.unhealthyTicks` checks, moves to another account with room, a subscription first, giving `usage unreadable` as the reason.

A monthly budget (`qswap budget 5 500`) changes this for that account. From 80% of the budget the log prints one budget warning a month (`budget-warning` in JSON), for example `#5 ci has spent $25.16 of its $30.00 monthly budget (84%) (estimate)`. At or over the budget the account is never a target, and if it is active the engine leaves it as it leaves an account at its limit, without waiting for the cooldown. Here `#5 ci` is active with $25.16 spent against a $20 budget:

```text
$ qswap auto --once
14:20:00  #5 ci has spent $25.16 of its $20.00 monthly budget (126%) (estimate)
14:20:00  Switched #5 ci → #2 work · monthly budget reached
            #1 personal     5h 46% · 7d 38%
            #2 work         5h 12% · 7d 21%
            #3 night-shift  5h 93% · 7d 81% · over its switch line
            #4 side         disabled
```

Without a budget, or before its spend is known, nothing changes. How spend is measured (`(estimate)` marks a figure worked out on this machine), and what a budget does not do, is in [api-accounts.md](api-accounts.md).

## When something fails

The engine keeps running through failures and does not switch on a guess.

- **Stale readings.** A reading counts while it is under 5 minutes old. While refreshing it fails, or while qswap has scheduled the next read for later, it keeps counting for up to an hour (when the usage endpoint is rate limiting, until the window resets). Past that the account's usage is unknown and it cannot be picked; the log shows the old figures with their age, as in `as of 3h 4m ago (timed out)`.
- **The active account unreadable.** After `autoswitch.unhealthyTicks` checks in a row without a reading (default 3), the engine fails over to any account with room, without waiting for the cooldown. Until then the log counts down: `failover in 2 more checks`.
- **An expired token on an idle machine.** Claude Code refreshes its token when you next send a message, so an expired token on the active account usually means nobody is working. The engine holds (`Claude Code is idle, resumes on next use`) and checks every 5 minutes. After 30 minutes of this it treats the account as unreadable, in case its refresh token is dead.
- **A saved login that was refused.** Before switching, the engine refreshes the target's token if it expires within 10 minutes. If the refresh is refused, the account is set aside and shows `needs re-login` in the log, `qswap list`, `qswap status`, the dashboard and the menu bar. Log in to it in Claude Code (`/login`) and run `qswap add`; the engine puts it back by itself and logs `#4 side is back in rotation (login replaced)`.
- **No account can take over.** The check names each account's reason and the next step:

```text
$ qswap auto --once
14:20:00  #1 personal at 5h 100% (limit reached) · nothing to switch to
            #2 work         needs re-login
            #3 night-shift  usage unavailable (HTTP 503), retrying
            #4 side         needs re-login
            #5 spare        5h 40% · 7d 55% · as of 3h 4m ago (timed out)
            To use #2 work and #4 side again, log in to each in Claude Code
            (/login), then run: qswap add
```

## Reading the log

Each check prints one line: the time, the active account, the window closest to its line against that line, and the decision. A switch gives its reason in words: `5h reached 91%` (a line), `limit reached`, `usage unreadable` (failover), `monthly budget reached`, `its weekly window resets sooner` (`consume-first`), or `higher priority` (`priority-consume-first`).

The indented lines under a check give each other account's windows, or why it cannot be used: `over its switch line`, `limit reached`, `needs re-login`, `disabled`, `usage unavailable (HTTP 503), retrying`, `API key, not used`. They print on the first check, whenever a switch is being decided (the active account at or over its line, a switch, or no account able to take over), and on every check with `--verbose`.

Other events get a line of their own: errors (`Error: ... (will retry)`), long sleeps (`Sleeping 12m, until 14:32`), accounts set aside or back in rotation, usage resets spent, warnings and budget warnings. Lines fit 80 columns and carry no colour when the output is not a terminal.

`--json` prints one JSON event per line instead, each with `schemaVersion` and an `event` kind (`poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `budget-warning`, and others). The fields are in [json-output.md](json-output.md).

## Running it unattended

### macOS: the menu bar

The menu bar app hosts the same engine, one per provider, with the same settings. `qswap menubar --auto-switch` turns it on for Claude Code. See [menubar.md](menubar.md).

### Linux: a systemd user service

An example unit, not yet run against real accounts. `ExecStart` needs the absolute path of `qswap`; `command -v qswap` prints it.

```ini
# ~/.config/systemd/user/qswap-auto.service
[Unit]
Description=qswap auto-switch

[Service]
ExecStart=/home/you/.local/bin/qswap auto
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now qswap-auto
journalctl --user -u qswap-auto -f      # the log
```

If your shell sets `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `XDG_DATA_HOME`, set the same values in the unit with `Environment=`. Use a user unit: qswap refuses to run as root outside a container. A user service stops when you log out unless lingering is on (`loginctl enable-linger`).

`qswap auto` loops until it is stopped. On SIGTERM (`systemctl --user stop qswap-auto`) it finishes the check in progress and exits with 0, so `Restart=on-failure` leaves it stopped. Ctrl-C prints `Auto-switch stopped.` and exits with 130.

### cron

`--once` runs one check and exits, and `--json` makes the log one event per line:

```text
*/5 * * * * /home/you/.local/bin/qswap auto --once --json >> ~/qswap-auto.log 2>&1
```

cron's `PATH` usually does not include the directory qswap is installed in, so give the absolute path. Cooldown, set-aside accounts and the failover count are kept in a state file, so separate `--once` runs behave like one loop. Between runs nothing watches, so a five-minute schedule reacts later than the loop does. On macOS, cron has not been tested with saved logins in the login Keychain; use the menu bar.

### Is it running?

Each engine writes `autoswitch_heartbeat.json` in the data directory (Codex's in `codex/` inside it) after every check, a dry run too. `qswap status` and `qswap doctor` read it:

```text
$ qswap status
...
Auto-switch is running for Claude Code; last check just now.
```

Only a looping engine (`qswap auto`, `qswap codex auto` or the menu bar) counts as running. A `--once` run from cron shows as `not running · last ticked 3m ago` in `qswap doctor`. The heartbeat is not removed when an engine stops, so for a few minutes afterwards (until its announced next check plus two minutes) `qswap status` still says it is running.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | `--once`: switched (with `--dry-run`: would switch). The loop: stopped by SIGTERM |
| `1` | an error: the check failed (network trouble, no target's token could be refreshed), or qswap could not start (Codex with no accounts, run as root) |
| `2` | `--once`: nothing to do: no switch needed, cooling down, a switch deferred, or no active account qswap manages |
| `3` | `--once`: blocked: a switch is wanted but no account can take over |
| `64` | a usage error: unknown flag, bad value |
| `130` | interrupted with Ctrl-C |

## Codex

`qswap codex auto` runs the same engine over your Codex accounts, with the same flags, log and exit codes, and its own cooldown, set-aside list and failover count in `codex/` inside the data directory. What differs:

- Its line is `codex.threshold` (default 99) instead of `autoswitch.threshold`, because a Codex switch restarts Codex's background server and interrupts work in progress. By default it moves only when the active account is out. Every other `autoswitch.*` setting applies to both providers.
- A saved Codex login whose token has expired reads `usage unknown until used` and is never picked automatically. `qswap codex switch N` picks it, and Codex renews the token the next time it runs; qswap never refreshes a Codex login.
- When a running Codex may be renewing the active login's token, the switch waits (`switch deferred, retrying`) and the next check tries again.
- `--include-api-key-accounts` does not apply.

More in [codex.md](codex.md).

## Settings

Change one with `qswap config set autoswitch.cooldownSeconds 600`; remove it with `qswap config unset autoswitch.cooldownSeconds`. Flags override them for one run. The full list is in [settings.md](settings.md).

| Setting | Default | Meaning |
|---|---|---|
| `autoswitch.threshold` | `90` | Switch line for the 5-hour window, and for every window while no weekly line is set (50 to 99.9). `--threshold` |
| `autoswitch.weeklyThreshold` | (none) | Switch line for the weekly and per-model windows (50 to 99.9). `--weekly-threshold` |
| `autoswitch.intervalSeconds` | `60` | Seconds between checks (15 to 3600). `--interval` |
| `autoswitch.cooldownSeconds` | `300` | Least time between switches made at a line (0 to 86400). `--cooldown` |
| `autoswitch.hysteresisPct` | `10` | Points of room a target must have over the active account for a switch at the line (0 to 50) |
| `autoswitch.strategy` | `best` | `best`, `weekly-first`, `consume-first` or `priority-consume-first`. `--strategy` |
| `autoswitch.includeApiKeyAccounts` | `false` | Allow API-key accounts as a last resort. `--include-api-key-accounts` |
| `autoswitch.unhealthyTicks` | `3` | Checks in a row without a reading of the active account before failover (1 to 100) |
| `autoswitch.model` | (none) | Per-model limits that also count, comma-separated, or `all`. `--model` |
| `codex.threshold` | `99` | `qswap codex auto`'s switch line, in place of `autoswitch.threshold` (50 to 99.9) |

## Limits

- A check every 60 seconds is not a usage request every 60 seconds. A check reads the active account and at most one other account when they are due, and reads every account only when the active one is within 15 points of a line or unreadable. A reading younger than 3 minutes is reused; the active account is read once a minute while it climbs within 15 points of its line; idle accounts and accounts at their limit are read about every 10 minutes. Request volume stays about flat however many accounts you manage.
- Codex switching has been tested only against fakes, never with two real Codex accounts.
- The latest menu bar build (both providers, auto-switch per provider) has not yet been run by hand.
- The systemd unit above has not been run against real accounts, and cron on macOS has not been tested.
