# Menu bar (macOS)

The menu bar app puts the active account's usage in the macOS menu bar and lists every Claude Code and Codex account in its menu. Click an account to switch to it, and turn on auto-switch per provider. This page covers installing it, starting it, what each row means, its settings, its notifications and its files.

This build of the menu bar, with both providers and per-provider auto-switch, has not yet been run by hand on a Mac. It is tested through `menubar.build_menu`, the function that builds every row of the menu, and through unit tests only.

## Install

The menu bar needs the `menubar` extra (it adds `rumps`). The package is not on PyPI yet, so install from the repository:

```bash
uv tool install 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'
# or: pipx install 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'
```

If qswap is already installed without the extra, `qswap menubar` stops with `The menu bar needs the 'menubar' extra. Install it with: uv tool install --force 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'` (the pipx form when you installed with pipx).

Keep the install out of `~/Documents`, `~/Desktop` and `~/Downloads`. The uv and pipx defaults already are. See [Limits](#limits).

## Start and stop

`qswap menubar` hands the menu bar to launchd and returns. The first time, it prints `Menu bar started (pid N).`, then says it keeps running after you close the terminal and starts at login, and prints where its plist and log are. From then on launchd starts it at login and restarts it if it crashes. Running `qswap menubar` again prints `Menu bar is already running (pid N).`

Each start also says whether it will switch accounts. Auto-switch is off until you turn it on, and until then the start warns `auto-switch: off -- the menu bar shows usage but will not switch accounts; turn it on from the menu, or re-run with --auto-switch`.

| Command | Does |
|---|---|
| `qswap menubar` | start it; the first time, also set it to start at login |
| `qswap menubar --auto-switch` | turn Claude Code's auto-switch on and start the menu bar, restarting a running one to apply it (`--no-auto-switch` turns it off) |
| `qswap menubar --service-status` | installed? running, with its pid? starts at login? which build? which auto-switch toggles are on? |
| `qswap menubar --install-service` | register the start at login again and restart it now |
| `qswap menubar --uninstall-service` | stop it and remove its LaunchAgent and wrapper app |
| `qswap menubar --foreground` | run it attached to this terminal, for debugging; refused while the service runs |

*Quit* in the menu stops it until the next login. *Settings → Start at login* turns the start at login off, after which `qswap menubar` starts it for the current session only.

`qswap upgrade` restarts a running menu bar when the build changed. After an upgrade made another way, the next `qswap menubar` restarts it onto the new build, and `--service-status` warns until then.

## What the menu shows

The demo harness prints the menu as an outline for its synthetic accounts. In the outline, `✓` is a check mark, `⚠` a row drawn in orange, `▸` a submenu, and `(greyed)` a row you cannot click:

```text
$ uv run python scripts/demo/run.py fleet --menubar
title: ⇄ personal · 46% · 38%
  Auto-switch off · usage read 1m ago  (greyed)
────
  Claude Code accounts  (heading)
✓ #1 personal · 5h 46% · 7d 38% · Fable 52% · 1 Fable reset
  #2 work · 5h 12% · 7d 21% · extra 18%
  ⚠ #3 night-shift · 5h 93% · 7d 81% · Fable 97% · 1 five-hour reset
  #4 side · disabled · 5h 0% · 7d 9%
  #5 ci · API key · $0.00 today · $0.00 this month · estimate
────
  Codex accounts  (heading)
✓ #1 maya@example.com · 5h 22% · 7d 47%
  #2 work · 5h 8% · 7d 15% · 1 reset credit
────
  Rotate to next (Claude Code)
  Switch to best (Claude Code)
  Next available (Claude Code)
────
  Add account ▸
      Add from a setup-token or API key…
      Log in another Codex account…
...
  Refresh now
  Quit
```

**The title** is `⇄`, then the active account's alias (or its email up to the `@`), then its 5-hour and weekly percentages. *Settings → Title percentage*, *Show model limits in title* (adds `Fable 52%`) and *Show account name in menu bar* change it. With Codex accounts managed, *Settings → Title follows* picks Claude Code's active account, Codex's, or whichever is closest to its limit. `⚠ ⇄` means an auto-switch engine stopped.

**The first row** says which engines run and how old the active accounts' readings are: `Auto-switch off · usage read 1m ago`, `Auto-switch running for Claude Code and Codex`, `Auto-switch: Claude Code running, Codex off`. A reading older than three refresh intervals (and at least 15 minutes) reads `usage as of 2h 0m ago`. When an engine stopped, the row reads `⚠ Auto-switch stopped, click to restart`; click it to restart, and hover for the reason. With the menu bar's toggle off and a `qswap auto` running in a terminal, it says `running in qswap auto`.

**The accounts** come one row each, with the active account checked. Codex gets its own section once it manages an account. A row is at most 72 characters and uses `qswap list`'s words:

- the account, `#2 work` (or `#1 maya@example.com` without an alias), and `disabled` for one held out of rotation;
- each window as a bare percentage, and `extra 18%` for extra-usage spend. Once a window is at its limit it adds when it comes back: `5h 100% (resets in 1h 12m)`;
- the usage resets held: `1 Fable reset`, `1 five-hour reset`, `1 reset credit` for Codex;
- or a state in place of the figures: `needs re-login`, `usage unavailable (HTTP 503), retrying`, `usage not read yet`, an API-key account's spend;
- `as of 3h 4m ago` after an old reading, and `re-login in 5d 12h` while a saved login nears its expiry.

A row is orange when it needs you: needs re-login, a window at or past the account's switch line, or an API-key account over its budget. Hover a row for its full `qswap list` block. Click a row to switch that provider to the account. For an account that needs re-login, a *Switch anyway?* dialog first shows the explanation `qswap switch` gives, and its *Switch anyway* button does what `--anyway` does. Switches made anywhere else (`qswap switch`, Claude Code's `/login`, `codex login`) show within about a second.

**The cost row**, `Today $17.07 · 7 days $179.72 (estimated)`, totals both providers over today and the last seven days at API list prices, as `qswap usage` does ([usage.md](usage.md)). It appears once the ledger holds anything. Clicking it does nothing. The menu bar reads new session files a minute after it starts and every 15 minutes after that. A `qswap usage` run shows up at the next refresh.

**Below the cost row:**

- *Rotate to next*, *Switch to best*, *Next available*: Claude Code's rotation strategies (`qswap switch`, `--strategy best`, `--strategy next-available`).
- *Add account*: *Add the current Claude Code login (…)* or *Add the current Codex login (…)* when a provider is signed in with a login qswap does not manage; *Add from a setup-token or API key…*, one dialog with the token and an optional email ([api-accounts.md](api-accounts.md)); *Log in another Codex account…*, which shows `qswap codex login` to run in a terminal and copies it if you click *Copy command*.
- *Disable / enable account* and *Remove account*: every account, named with its provider (`Codex #2 work`) when both are managed. Removing asks what `qswap remove` asks.
- *Refresh current credentials*: saves the login Claude Code is signed in with into its account again.
- *Switch history*: the last ten switches, newest first, and *Open full log…*.
- *Refresh now*: read usage again now.

With nothing managed, the accounts section says `No accounts are managed yet.` and the next step for each login on this machine. Click a line that names a login to add it.

## Auto-switch

*Settings → Auto-switch Claude Code* and *Settings → Auto-switch Codex* (shown once Codex manages an account) each run the same engine as `qswap auto` or `qswap codex auto` ([auto-switch.md](auto-switch.md)), in a thread of its own. Both use the `autoswitch.*` settings, and Codex uses `codex.threshold` as its switch line. The menu bar's engines switch for real; there is no dry run here. Turning on Codex's toggle asks first, because a Codex switch restarts Codex's background server, which interrupts work in progress.

*Settings → Claude Code threshold* and *Codex threshold* (with Claude Code alone, *Auto-switch threshold*) write the switch line to `settings.json` and restart that engine. An engine's heartbeat names `qswap menubar`, so `qswap status` and `qswap doctor` can tell it from a `qswap auto` in a terminal.

## Settings

The menu's own preferences live in `menubar_settings.json` in the data directory (`~/.ai-quota-swap` on macOS). Change them from the menu; the menu bar reads the file when it starts.

| Setting | Default | Meaning |
|---|---|---|
| `show_account_name` | `true` | the account's name in the title |
| `title_pct` | `"both"` | percentages in the title: `off`, `5h`, `7d` or `both` |
| `title_scoped` | `false` | add per-model limits (`Fable 52%`) to the title |
| `refresh_interval` | `60` | seconds between reads: `30`, `60` or `300` |
| `auto_switch_enabled` | `false` | *Auto-switch Claude Code* |
| `codex_auto_switch_enabled` | `false` | *Auto-switch Codex* |
| `title_follows` | `"claude"` | whose active account the title shows: `claude`, `codex` or `closest` |

A file from an earlier build loads as it did, and a key this build does not know is kept. The switch lines are qswap settings, shared with the command line ([settings.md](settings.md)):

| Setting | Default | Meaning |
|---|---|---|
| `autoswitch.threshold` | `90` | Claude Code's switch line; the menu offers 80, 90, 95 and 98 |
| `codex.threshold` | `99` | Codex's switch line; the menu offers 90, 95, 98 and 99 |

`qswap config set autoswitch.threshold 95` changes it too, but a running engine picks the change up only when it restarts. Use the menu's threshold submenu, or turn the toggle off and on.

## Notifications

Every notification is titled `ai-quota-swap`. With both providers managed, the account is named with its provider (`Claude #1 personal`).

| Event | Example | How often |
|---|---|---|
| An engine switched | `Switched #1 personal → #2 work · 5h reached 91%` | every switch |
| You switched from the menu | `Switched #1 personal → #2 work. Claude Code picks up the new account within 30 seconds; restart it to apply now.` | every switch |
| A usage reset was used | `Used a Fable reset on #1 personal · Fable reached 100%, 3d before it resets on its own · 0 left on this grant` | every reset |
| A reset was not used | `Did not use a Fable reset on #1 personal: refused` | once per account and result |
| An account needs re-login | `#4 side needs re-login: its saved login was refused. Log in to it in Claude Code (/login), then run: qswap add` | once per account and reason |
| Every account is at its limit | `All accounts are at their limit; the first resets 15:32 · in 1h 12m` | once per provider until the next switch or reset time |
| An API-key budget nears | `#5 ci has spent $410.00 of its $500.00 monthly budget (82%)` | once per account and month |
| A setting matches nothing | `Warning: autoswitch.model 'Fabel' matches no limit any account reports` | once, whichever engine noticed |
| An engine's check failed | `Auto-switch error: network unreachable. It retries by itself.` | once per message an hour |
| An engine stopped | `Auto-switch stopped: the engine loop ended on its own. Open the menu to restart it.` | every time |
| An engine did not start | `Codex auto-switch did not start: No Codex accounts are managed yet.` | every attempt |

A failed action (a switch, an add, a remove) shows the command line's error in an alert.

## Files

| File | What |
|---|---|
| `~/Library/LaunchAgents/com.aiquotaswap.menubar.plist` | the LaunchAgent while Start at login is on; in the data directory while it is off |
| `~/Library/Logs/com.aiquotaswap.menubar.log`, `.err` | the service's output |
| `~/.ai-quota-swap/ai-quota-swap.log` | the switch log that *Switch history* reads |
| `/Applications/AI Quota Swap Menubar.app` | the wrapper app, rebuilt on every start |

The wrapper app gives the process the bundle identifier `com.ai-quota-swap.menubar`, which menu bar managers (Dimble, Ice, Bartender, SaneBar) need to list, hide and order the icon. `AI_QUOTA_SWAP_NO_BUNDLE=1 qswap menubar --foreground` runs without it; the service does not pass that variable on. The service does take the `CLAUDE_CONFIG_DIR`, `CLAUDE_SECURESTORAGE_CONFIG_DIR`, `XDG_DATA_HOME`, proxy and certificate variables (`HTTPS_PROXY`, `SSL_CERT_FILE` and the like) of the shell that set it up, and `--install-service` from a shell with other values says what changed.

## How it is built

`menubar.build_menu` turns both providers' state and the settings into the whole menu, and a thin `rumps` layer draws it and maps each row to a handler. The design is in [architecture.md](architecture.md).

## Limits

- This build has not been run by hand on a Mac. Codex account switching has only been tested with fakes ([codex.md](codex.md)).
- macOS only. Elsewhere `qswap menubar` prints `The menu bar is only available on macOS.` and exits 1. `qswap auto` switches accounts on every platform.
- **Install location.** macOS privacy rules keep launchd agents out of `~/Documents`, `~/Desktop` and `~/Downloads`, so the service cannot start when qswap runs from there (an editable install from a checkout in `~/Documents`, for example). `qswap menubar` does not notice. `qswap doctor` reports it as `qswap runs from ~/Documents/…; macOS keeps background services out of ~/Documents`. Reinstall outside those folders with `uv tool install --force 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'`, then run `qswap menubar` ([troubleshooting.md](troubleshooting.md)).
- On macOS 26, a framework build of Python (Homebrew's, python.org's) has been seen not to draw the icon: the process runs and logs nothing. `qswap menubar` warns about it. With uv, reinstall on uv's own Python: `uv tool install --managed-python --force 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'`.
- If you switched it off under System Settings → General → Login Items & Extensions → Allow in the Background, `qswap menubar` still starts it when you ask, but macOS will not start it at login.
- When `/Applications` is not writable, the wrapper app goes in the data directory, and a menu bar manager that hides anything hides this icon too.
