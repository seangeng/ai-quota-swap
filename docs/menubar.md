# Menu bar app (macOS)

Reference for `qswap menubar`. Installing it is in the [README](../README.md#menu-bar-macos).

## Lifecycle

`qswap menubar` hands the menu bar to launchd and returns: it keeps running after you close the terminal, comes back at login and is restarted if it crashes. Running the command again only tells you it is already up. *Quit* in the menu stops it until the next login. *Settings → Start at login* turns the login start off, after which `qswap menubar` runs it for the current session only. `qswap menubar --uninstall-service` removes it altogether, and `qswap menubar --foreground` runs it attached to the terminal instead, for debugging.

```bash
qswap menubar --service-status      # running? pid? starts at login? auto-switch on?
qswap menubar --install-service     # register the login start again and restart it now
qswap menubar --uninstall-service   # stop it and remove the LaunchAgent
qswap menubar --foreground          # run attached to this terminal, for debugging
```

## What the menu shows

The menu uses the same words as `qswap list`, `qswap status`, the dashboard and the `qswap auto` log ([design/ux.md](design/ux.md)), so a state reads the same everywhere.

The **title** is `⇄` followed by the active account's alias (or the part of its email before `@`) and its 5h and 7d percentages, for example `⇄ personal · 46% · 38%`. *Settings → Title percentage* chooses which windows it shows and *Show model limits in title* adds per-model limits such as `Fable 52%`. A refused setup-token reads `limit reached`. When Codex manages an account, *Settings → Title follows* chooses whose active account the title shows: Claude Code's (the default), Codex's, or whichever active account is closest to its limit. A `⚠` in front of the icon means an auto-switch engine stopped and is waiting for a click.

The **first row** says, in words, whether auto-switch is running for each provider and how old the active accounts' readings are: `Auto-switch off · usage read 2m ago`, `Auto-switch running for Claude Code and Codex · usage read just now`, `Auto-switch: Claude Code running, Codex off · usage as of 2h 0m ago`. When an engine stopped (it failed to start, or its thread ended) the row reads `⚠ Auto-switch stopped, click to restart` and a click restarts it; its tooltip says why. When the menu bar's own engine is off and a `qswap auto` in a terminal is running for that provider, the row says `running in qswap auto`.

Then the **accounts**, one row each, the active one checked. With no Codex account managed this is Claude Code's list alone, as it always was. Once Codex manages one, there are two sections, *Claude Code accounts* and *Codex accounts*. A row reads like this:

```text
#1 personal (maya@example.com) · 5h 46% (in 2h 35m) · 7d 38% (in 3d 6h) · Fable 52% (in 3d 6h) · holds 1 Fable reset
```

- each window with its percentage and when it refills; `ahead of pace` on a weekly window that is being used faster than the week passes; `limit reached` at 100%; `extra 18%` for extra-usage spend;
- `holds 1 Fable reset and 1 weekly reset` (Codex: `holds 1 reset credit`) for the usage resets it holds;
- `disabled` for an account held out of rotation;
- `needs re-login` for an account whose saved login was refused, or that the auto-switch engine set aside until its owner logs in again;
- `usage unavailable (HTTP 503), retrying` when no reading could be taken, and `as of 3h 5m ago` on a reading older than three refresh intervals (at least 15 minutes);
- `API key · $4.12 today · $212.40 this month of $500 (42%) · estimate` for an API-key account, as `qswap list` shows it;
- `re-login in 5d 12h` at the end while a saved login nears its expiry.

A row that needs you (needs re-login, a limit reached, an API-key account over its budget) is drawn in orange. Hovering over a row shows the account's full block as `qswap list` prints it, with reset clocks, the plan and the next step. Clicking a row switches that provider to the account. Switching to an account that needs re-login first asks, with the same explanation `qswap switch` gives when it refuses; *Switch anyway* is what `--anyway` does on the command line.

With nothing managed the section says `No accounts are managed yet.` and the next step for each login on this machine; a line that names a live login adds it when clicked.

The **cost row**, `Today $12.40 · 7 days $179.72 (estimated)`, is the total of both providers over today and the last seven days, the same figures `qswap usage` reports, at API list prices (a `+` marks a total that leaves out tokens of a model with no known price). It is information; clicking it does nothing. It appears once the ledger holds anything. The menu bar reads the ledger without scanning, and brings it up to date in the background a minute after it starts and then every 15 minutes: a scan reads every changed session file and can take seconds on a large history, and the figures are estimates over a day and a week, so a quarter of an hour of lag changes nothing. Running `qswap usage` updates the row at once.

Below that:

- *Rotate to next*, *Switch to best*, *Next available*: Claude Code's rotation strategies (with both providers they say `(Claude Code)`).
- *Add account*: *Add the current Claude Code login* or *Add the current Codex login* when that provider is logged in to an account qswap does not manage; *Add from a setup-token or API key…*, one dialog with the token first and an optional email (without one the account is named `setup-token-N@token.local` or `api-key-N@token.local`, as `qswap add-token` does); *Log in another Codex account…*, which shows the command to run in a terminal (`qswap codex login`) and copies it, since the menu bar cannot host Codex's sign-in.
- *Disable / enable account* and *Remove account*: every account, named with its provider (`Codex #2 work`) when both providers are managed. Removing asks the question `qswap remove` asks; for a Codex account it adds that only qswap's copy is deleted and why not to run `codex logout`.
- *Refresh current credentials*: saves Claude Code's live login into its account again.
- *Switch history*: the last ten switches from the log, newest first (`#3 ops → #1 personal · 2026-11-10 14:20`), and *Open full log…*.

## Settings

*Settings* holds the display preferences, one auto-switch toggle per provider, the switch lines, and *Start at login*. They live in `menubar_settings.json` in the data directory:

| Key | Default | Meaning |
|---|---|---|
| `show_account_name` | `true` | the account's name in the title |
| `title_pct` | `"both"` | which percentages the title shows: `off`, `5h`, `7d`, `both` |
| `title_scoped` | `false` | add per-model limits to the title |
| `refresh_interval` | `60` | seconds between reads: `30`, `60` or `300` |
| `auto_switch_enabled` | `false` | *Auto-switch Claude Code* |
| `codex_auto_switch_enabled` | `false` | *Auto-switch Codex* (shown once Codex manages an account) |
| `title_follows` | `"claude"` | whose active account the title shows: `claude`, `codex`, or `closest` |

A file written by an earlier build loads as it did: Claude Code's auto-switch as it was, Codex's off, the title on Claude Code. The CLI and the menu bar both write this file; a key a build does not know is kept and written back unchanged. The switch lines are qswap's own settings, not the menu bar's: *Auto-switch threshold* (with both providers, *Claude Code threshold*) sets `autoswitch.threshold`, and *Codex threshold* sets `codex.threshold`.

## Auto-switch

Each toggle that is on runs the same engine as [`qswap auto`](../README.md#automatic-switching) (`qswap codex auto` for Codex), in a thread of its own, with that provider's settings: the `autoswitch.*` settings for both, and `codex.threshold` as Codex's switch line. The menu bar and the command line therefore stay in sync. Both toggles are off until you turn them on; `qswap menubar --auto-switch` turns Claude Code's on from the terminal (`--no-auto-switch` off), and both that command and `--service-status` say which toggles are on. *Auto-switch Codex* asks first, because a Codex switch restarts Codex's background server, which interrupts work in progress. A hosted engine's heartbeat names `qswap menubar`, so `qswap status` and `qswap doctor` can tell it from a `qswap auto` in a terminal.

## Notifications

Every notification is titled `ai-quota-swap` and says one thing, in the auto log's words. When both providers are managed the account is named with its provider.

| Event | Example | How often |
|---|---|---|
| An engine switched | `Switched Claude #1 personal → #2 work · 5h reached 91%` | every switch |
| You switched from the menu | `Switched #1 personal → #2 work. Claude Code picks up the new account within 30 seconds; restart it to apply now.` | every switch |
| A usage reset was used | `Used a Fable reset on #1 personal · Fable reached 100%, 3d before it resets on its own · 0 left on this grant` | every reset |
| A reset could not be used | `Did not use a Fable reset on #1 personal: refused` | once per account and result |
| An account needs re-login | `#4 side needs re-login: its saved login was refused. Log in to it in Claude Code (/login), then run: qswap add` | once per account and reason |
| Every account is at its limit | `All accounts are at their limit; the first resets 15:32 · in 1h 12m` | once per provider until the next switch or reset time |
| An API-key budget nears | `#5 ci has spent $410.00 of its $500.00 monthly budget (82%)` | once per account and month |
| A setting does nothing | `Warning: autoswitch.model 'Fabel' matches no limit any account reports` | once, whichever engine noticed |
| An engine's check failed | `Auto-switch error: network unreachable. It retries by itself.` | once per message an hour, across both engines |
| An engine stopped | `Auto-switch stopped: the engine loop ended on its own. Open the menu to restart it.` | every time |
| An engine did not start | `Codex auto-switch did not start: Codex keeps its login in the OS keyring …` | every attempt |

## Errors

A failed action (a switch, an add, a remove) shows the command line's error message in an alert; nothing a click does can end the app. Anything unexpected also goes to the log with its traceback.

## How it is built

`menubar.build_menu` turns a snapshot of both providers and the settings into the whole menu (title, sections, rows with their text, check marks, attention and an action), and `menubar_app` only draws that and maps each action to a handler, so everything the menu says is tested without macOS. Reads run in background threads (`menubar_data`), the engines in theirs (`menubar_engines`), and notification texts and their de-duplication are in `menubar_notify`. The demo harness prints the menu for any scenario: `uv run python scripts/demo/run.py fleet --menubar`. Switches made elsewhere (the CLI, Claude Code's `/login`, `codex login`) show within about a second: the menu bar stats Claude Code's config file and Codex's `auth.json` once a second and reads them only when they change.

## The wrapper app

`qswap menubar` runs from a small wrapper app, `/Applications/AI Quota Swap Menubar.app`, that it builds (and refreshes after upgrades) on every start. The wrapper gives it the bundle identifier `com.ai-quota-swap.menubar`, which menu bar managers (Dimble, Ice, Bartender, SaneBar) need to list, hide and order the icon. If `/Applications` is not writable the bundle lands in the data directory with a one-line warning; there, the first time a manager hides anything the icon goes down with it. Set `AI_QUOTA_SWAP_NO_BUNDLE=1` to run in the plain interpreter instead.

## Files

The plist lives at `~/Library/LaunchAgents/com.aiquotaswap.menubar.plist` while Start at login is on and in the data directory while it is off; logs go to `~/Library/Logs/com.aiquotaswap.menubar.{log,err}`.

## Upgrades

`qswap upgrade` restarts a running menu bar when the build changed; after an upgrade made some other way, the next `qswap menubar` notices the old build and restarts it.

## Environment

The service follows the `CLAUDE_CONFIG_DIR`, `CLAUDE_SECURESTORAGE_CONFIG_DIR` and `XDG_DATA_HOME` of the shell that set it up, and `--install-service` from a shell in another profile says what moved.

## Login Items & Extensions

If you switched it off under System Settings → General → Login Items & Extensions → Allow in the Background, `qswap menubar` still starts it when you ask, but says that macOS will not start it at login until you turn that back on.
