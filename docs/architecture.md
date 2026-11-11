# Architecture

A map for contributors: how the code is divided, where data lives, and where to start for the changes people make first. The reasons behind the big decisions are in [docs/design/](design/), and this page links to them.

## The idea

A provider is a switcher object with its own data root and its own engine instance. Claude Code's is `ClaudeAccountSwitcher` in `switcher.py`, whose data root is the data directory itself. Codex's is `CodexSwitcher` in `codex/switcher.py`, whose data root is `<data directory>/codex/`. Each switcher owns its account registry, its saved logins, how it reads usage, and how it activates a login.

Everything else is shared: the auto-switch engine (`autoswitch.py`), the usage store and its poll cadence (`usage_store.py`, `poll_policy.py`), the window math (`oauth.relevant_windows`, `lines.py`, `pace.py`), the reset planner (`resets.plan_reset`), the JSON row builders, and the settings file. The engine reaches a switcher only through the `EngineHost` Protocol in `providers.py`; the dashboard and menu bar use `SurfaceHost`. Because the engine keeps its state under `switcher.backup_dir`, each provider gets its own cooldown, quarantine and daily reset cap for free. [design/providers.md](design/providers.md) has the contracts and why the Claude code was wrapped rather than extracted.

On the command line, `qswap <verb>` is Claude Code and `qswap codex <verb>` is Codex. `cli.main` strips the prefix (`providers.split_provider_prefix`) and every handler builds its switcher through one factory (`cli._switcher`), so the prefix selects the provider for the whole command.

A `qswap list` runs like this: `cli._dispatch` calls `ClaudeAccountSwitcher.list_accounts`, which asks `_collect_usage_entries` for each account's reading. That asks the usage store (`UsageStore.reserve`) which accounts may be fetched now, fetches those with `oauth.try_fetch_usage_for_account`, records the results (`UsageStore.record`) and serves the rest from the store. The rows are printed by `_usage_entry_lines`, and `codex_cli.extend_unified_list` adds the Codex section.

## Modules

Sizes are rounded and move with every change; they are here so you know what you are opening.

### Command line

| Module | Lines | What it does |
|---|---|---|
| `cli.py` | 3,000 | `main`, the verb dispatch, and the handlers for most Claude verbs |
| `codex_cli.py` | 700 | `qswap codex <verb>`, and the Codex section of `qswap list` |
| `api_cli.py` | 800 | `qswap api org`, `qswap budget`, and the `usage --billed` section |
| `printer.py` | 300 | Terminal colours and the accent, muted and warning styles |
| `appearance.py` | 200 | Light or dark terminal detection (OSC 11) and theme choice |
| `labels.py` | <100 | The one spelling of an account in messages (`#2 work`) |
| `display.py` | 800 | The shared state phrases (needs re-login, usage unavailable, as of, nothing managed) and the account block `list` and `status` print; pure functions the dashboard and menu bar can import |
| `prompts.py` | 100 | The one place a question is asked: never off a terminal, a no on cancel |
| `argparsing.py` | 100 | The `ArgumentParser` every verb uses: exit 64, the JSON envelope under `--json`, help at 80 columns |
| `helptext.py` | 300 | `qswap --help` (the map), the bare-`qswap` hint, and the help of the verbs rewritten to legacy flags |
| `autolog.py` | 1,100 | The `qswap auto` log: one line per check, in words |
| `doctor.py` | 1,200 | `qswap doctor`, the read-only self-check |
| `json_output.py` | 500 | The `--json` row and envelope shapes, schema version 1 |
| `__main__.py` | <100 | `python -m ai_quota_swap` |

### The Claude Code provider

| Module | Lines | What it does |
|---|---|---|
| `switcher.py` | 8,800 | `ClaudeAccountSwitcher`: the registry, add, remove, switch, list and status output, usage collection, purge |
| `credentials.py` | 2,100 | Where saved logins live: Keychain or `.enc` files, fallbacks, reconciliation |
| `macos_keychain.py` | 200 | Keychain access through the `security` CLI |
| `oauth.py` | 1,300 | Token parsing and refresh, the usage request and its normalization, window math |
| `subscription.py` | 300 | The plan label (Max, Pro, Team) from the profile endpoint |
| `session.py` | 2,500 | `qswap run`: per-account Claude Code profiles and shared history; `qswap run N` on a key; the settings copies run profiles hold while a key is active |
| `key_activation.py` | 400 | An API key made active through Claude Code's `apiKeyHelper`: who is active and who is underneath, entering and leaving a key, the repair `switch` runs first |
| `claude_settings.py` | 450 | Reads Claude Code's user settings and edits its one `apiKeyHelper` key, keeping everything else as it was |
| `key_helper.py`, `entry.py` | 200 | `qswap key-helper N`, answered before the command line module loads: prints the saved key and nothing else |
| `mappings.py` | 100 | `qswap map`: directory to account bindings |
| `claude_locks.py` | 200 | Taking Claude Code's own lock directories while writing its files |
| `paths.py` | 300 | Where Claude Code and qswap keep their files, mirroring Claude Code's rules |
| `process_detection.py` | 400 | Which Claude Code instances are running |
| `transfer.py` | 800 | `qswap export` and `qswap import` |
| `migrations.py` | 300 | Run-once data migrations |

### The Codex provider (`codex/`)

| Module | Lines | What it does |
|---|---|---|
| `codex/switcher.py` | 2,000 | `CodexSwitcher`: registry, capture, switch, list, `qswap codex login` |
| `codex/auth.py` | 500 | Codex's `auth.json`: locate, read, decode, lock, replace |
| `codex/usage.py` | 600 | The `/wham/usage` read and reset credits, in qswap's reading shape |
| `codex/processes.py` | 200 | Which Codex processes hold a login, and the background-server restart |

### Shared engine and usage

| Module | Lines | What it does |
|---|---|---|
| `providers.py` | 300 | The `EngineHost` and `SurfaceHost` Protocols, the provider registry and factory |
| `autoswitch.py` | 3,700 | `AutoSwitchEngine`: each tick polls, decides, switches and spends resets; typed events |
| `lines.py` | 100 | A switch line per usage window (5-hour and weekly thresholds) |
| `pace.py` | 200 | Whether a weekly window is ahead of pace |
| `resets.py` | 1,300 | Usage resets: status parsing, kinds, the claim request, `plan_reset` |
| `usage_store.py` | 1,400 | `UsageStore`: last good readings, fetch and backoff state, who may fetch next |
| `poll_policy.py` | 300 | How often each account's usage may be read |
| `snapshot_source.py` | 100 | The read path the dashboard and menu bar use |
| `settings.py` | 800 | `settings.json`: sections, defaults, validation, `SETTING_SPECS` |
| `models.py` | 300 | Data classes: `AccountSnapshot`, `AccountsSnapshot`, `AccountPolicy`, `Platform` |
| `exceptions.py` | 200 | `ProviderError` and its subclasses |

### Ledger and API accounts

| Module | Lines | What it does |
|---|---|---|
| `ledger/usage.py` | 200 | `run_usage`: scan, then report; the CLI's one entry into the ledger |
| `ledger/scanner.py` | 700 | The incremental, resumable scan of session files |
| `ledger/readers.py` | 400 | Claude Code and Codex transcript lines into token events |
| `ledger/store.py` | 300 | The SQLite store, `ledger.db` |
| `ledger/report.py` | 600 | Grouping, attribution fix-ups, prices, rendering |
| `ledger/pricing.py` | 300 | The bundled price table and `pricing.json` overrides |
| `ledger/activations.py` | 300 | `activations.jsonl`: which account was live when |
| `ledger/spend.py` | 200 | Today's and this month's spend for one account, read without scanning |
| `api_spend.py` | 600 | Spend and budgets on API-key account rows |
| `admin_api.py` | 800 | Anthropic's Admin API reads (GET only) |
| `admin_orgs.py` | 300 | Registered organizations and their stored Admin keys |

### Dashboard and menu bar

| Module | Lines | What it does |
|---|---|---|
| `tui/app.py` | 700 | `QswapApp`: both providers' switchers, the snapshot poll loop, every action (switch, add, remove), the cost line |
| `tui/dashboard.py` | 600 | The main screen and its menus, the switch picker and the watch screen |
| `tui/autoview.py` | 600 | The live auto-switch screen: one real engine per provider, and "Next best" |
| `tui/usageview.py` | 200 | The usage screen, over `ledger.usage.run_usage` and `ledger.report.render_text` |
| `tui/widgets.py` | 800 | Usage bars; cards and one-line rows drawn from `display.AccountBlock`; the accounts panel and how it fits |
| `tui/data.py` | 500 | `FleetSource` (each provider's snapshot and the facts around it), `block_for`, blocking actions, the ledger reads |
| `tui/modals.py` | 200 | Confirmations, token entry, a command to run elsewhere |
| `tui/theme.py` | 200 | The Textual themes and shared colours |
| `menubar.py` | 1,700 | The menu bar's settings, its typed menu description and `build_menu`, the one pure function that turns both providers' state into the whole menu; the title, the health row, the dialogs' words |
| `menubar_app.py` | 800 | The rumps glue: draws a `Menu`, maps each `Action` to a handler; only loaded with rumps |
| `menubar_data.py` | 300 | The menu bar's blocking reads (both providers, the ledger's cost totals, the log, heartbeats), always off the main thread |
| `menubar_engines.py` | 300 | One auto-switch engine per provider, each in its own thread; noticing switches made outside the menu bar |
| `menubar_notify.py` | 300 | Notification texts and their de-duplication across the two engines |
| `launch_agent.py` | 900 | Running the menu bar under launchd |
| `app_bundle.py` | 300 | The wrapper `.app` that gives the menu bar a bundle id |

### Plumbing

| Module | Lines | What it does |
|---|---|---|
| `fsutil.py` | 400 | Atomic writes (0600 by default), links and junctions, retries on Windows |
| `locking.py` | <100 | `FileLock` |
| `cache.py` | <100 | Small JSON caches with a time to live, under `cache/` |
| `logging_config.py` | <100 | The rotating log file in the data directory |
| `spawn.py` | 100 | Running `ps`, `security` and `claude` without `fork()` on macOS |
| `update_check.py` | 500 | The daily newer-version check, and `qswap upgrade` |

## The data directory

`~/.ai-quota-swap` on macOS and Windows; `$XDG_DATA_HOME/ai-quota-swap` (default `~/.local/share/ai-quota-swap`) on Linux and WSL. `paths.get_backup_root` decides.

```
sequence.json            the Claude account registry: numbers, emails, organizations, aliases, policies
credentials/             saved logins as base64 .enc files (all platforms but macOS, and macOS when the
                         Keychain is unusable); Admin keys the same way
configs/                 each account's saved Claude Code config
sessions/<n>-<email>/    `qswap run` profiles
history-conflicts/       transcripts a history merge kept rather than overwrite
cache/                   usage store (usage.json), plans, the update check, Admin API answers
settings.json            `qswap config`
autoswitch_state.json    engine cooldown, quarantine (accounts set aside: "needs re-login"), health counters, reset claims
autoswitch_heartbeat.json  when a running engine last checked, for `qswap status` and `qswap doctor`
mappings.json            `qswap map`
ledger.db                `qswap usage`; rebuilt from session files if deleted
activations.jsonl        which Claude account was live when
pricing.json             your own prices (optional)
api_orgs.json            registered organizations, without keys
menubar_settings.json    menu bar preferences and its auto-switch toggles
ai-quota-swap.log        the log
.lock, .migrations.json  the registry lock, and the migrations already applied
codex/                   the Codex provider's data root
  sequence.json          its registry
  credentials/           saved logins, account-<n>.json, byte-for-byte copies, 0600
  cache/usage.json       its usage store
  autoswitch_state.json  its engine state
  activations.jsonl      which Codex account was live when
```

On macOS the saved logins and Admin keys are Keychain items under the service `ai-quota-swap`, and the menu bar adds a LaunchAgent plist and a wrapper app in `/Applications` ([menubar.md](menubar.md)).

## To change X, look at Y

| To change | Start at | Also read |
|---|---|---|
| What `qswap list` prints | `display.render_block` and `block_from_entry` (one account's block, both providers), `ClaudeAccountSwitcher._account_block` and `list_accounts`, `CodexSwitcher.account_block`, `cli._list_accounts` (both providers and the first run) | [design/ux.md](design/ux.md); `json_output.account_row` if `--json` changes too |
| A message's words | `display.py` for any account state; `labels.account_label` for an account's name; `prompts.confirm` for a question | [design/ux.md](design/ux.md) |
| A new setting | A field on the section's dataclass in `settings.py` (`ResetSettings`, `AutoSwitchSettings`, ...), its `load_*_settings` function, and a `SettingSpec` in `SETTING_SPECS`, which `qswap config` validates and lists; then add its meaning and an example value to `scripts/settings_doc.py` and run it to regenerate [settings.md](settings.md) (`tests/test_docs.py` fails until you do) | [settings.md](settings.md) |
| A new verb | `cli._dispatch`: a verb with its own parser gets a `_<verb>_command(argv)` handler built on `argparsing.ArgumentParser` and listed in `_OWN_PARSER_VERBS`; a verb that maps to a legacy flag goes in `_SUBCOMMAND_FLAGS` and gets a help page in `helptext.LEGACY_VERB_HELP`. Add it to `helptext.MAIN_HELP`. Codex verbs: `codex_cli.dispatch` and `codex_cli.HELP` | The Commands section of [design/ux.md](design/ux.md) |
| How usage is fetched | `ClaudeAccountSwitcher._collect_usage_entries`, `UsageStore.reserve` and `record`, `oauth.try_fetch_usage_for_account`, `oauth.build_usage_result`; for Codex, `codex/usage.py` | `poll_policy.py`, Contract 2 in [design/providers.md](design/providers.md) |
| The auto-switch decision | `AutoSwitchEngine._tick_inner` and `_rank_candidates`; the lines in `lines.py` | [auto-switch.md](auto-switch.md) |
| Reset planning | `resets.plan_reset` and `grant_scope`; the engine's call in `AutoSwitchEngine._maybe_use_reset`; the manual path in `cli._reset_command` | [resets.md](resets.md) |
| A Codex behaviour | `CodexSwitcher` (`switch_to`, `_handle_holders`, `login_new_account`), `codex/auth.py`, `codex/processes.py` | [design/codex.md](design/codex.md), [codex.md](codex.md) |
| The ledger and prices | `ledger/readers.py` (what counts), `ledger/report.py` (grouping and attribution), the tables in `ledger/pricing.py` | [design/ledger.md](design/ledger.md), [usage.md](usage.md) |
| The dashboard | `tui/dashboard.py` (screens), `tui/widgets.py` (cards and bars), `tui/data.py` (`block_for`: a snapshot row in `display`'s words), `tui/app.py` (actions) | `scripts/demo/tour.py` screenshots every screen |
| The menu bar | `menubar.build_menu` (every row and its words), `menubar_notify.Notifier` (notifications), `menubar_app` (drawing and handlers), `launch_agent.py`; `scripts/demo/run.py fleet --menubar` prints the menu for a scenario | [menubar.md](menubar.md) |

API-key spend, budgets and credits are in `api_spend.py` and `admin_api.py`; [design/api-accounts.md](design/api-accounts.md) explains them. The live switch to an API key is `key_activation.py` with `claude_settings.py` and `key_helper.py`; [design/api-key-sessions.md](design/api-key-sessions.md) explains it.
