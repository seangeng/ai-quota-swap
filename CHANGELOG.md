# Changelog

## Unreleased

The first release under the name ai-quota-swap, version 0.1.0. The project started as a fork of [claude-swap](https://github.com/realiti4/claude-swap) 1.1.0b1, and everything that version did still works: switching Claude Code accounts by hand or automatically, parallel sessions with `run`, the dashboard, the macOS menu bar, JSON output, export and import.

The package is `ai-quota-swap` and the command `qswap`. It keeps its own data directory, Keychain service, LaunchAgent and `QSWAP_*` environment variables, so it installs beside claude-swap. Accounts move across with `cswap export` and `qswap import`.

### Command line

- `qswap --help` is a map of the commands grouped by task; every verb has its own `--help`, and `qswap help VERB` prints it. Bare `qswap` off a terminal prints a three-line hint.
- `qswap doctor` checks Claude Code, Codex, the data directory, the saved-login store, the macOS menu bar service and install location, and auto-switch, and says how to fix anything that is not right. `--json`, and `--online` for network checks.
- One wording everywhere: accounts are `#2 work`; an account the tool will not use until its owner logs in again is `needs re-login` (in `list`, `status`, the auto log and `--json` as `needsRelogin`); a failed reading is `usage unavailable (HTTP 503), retrying`; an old one `as of 3h 5m ago`.
- `list` and `status` share one account block, coloured by severity (yellow from the account's switch line, red at the limit, which is also said in words). `status` covers Claude Code and Codex and says whether auto-switch is running.
- `qswap auto` prints one line per check, in words, and lists the other accounts when a switch is being decided (`--verbose`: on every check).
- `switch N` refuses an account that needs re-login unless `--anyway` (which only overrides that refusal; `--force` keeps its meaning), and warns before switching to one whose usage cannot be read or is old. A rotation (`switch` with no account, `--strategy best` or `next-available`, `qswap codex switch`) skips such accounts, one line each, as it skips disabled ones.
- Read-only commands never ask; a question is only asked on a terminal, and without one the command stops with a line naming `--yes` (now on `remove`, `purge`, `add --slot`, `add-token --slot` and `qswap codex remove`). Cancelling `purge` changes nothing, the menu bar included.
- Usage errors exit 64 (not 2, which `auto --once` uses for "nothing to do"); every `--json` failure is the error envelope, and every payload carries `schemaVersion`. `priority --json` is new.
- Every message fits 80 columns, and names commands by verb, never by a legacy flag.

### Codex (experimental)

- `qswap codex add`, `list`, `status`, `switch`, `remove`, `alias`, `disable`, `enable`, `threshold` and `priority` keep several Codex CLI logins and switch between them.
- `qswap codex login` logs another account in under a temporary Codex home, so the login in use is never signed out.
- `qswap list` shows a Codex section with live usage, and `list --json` adds `providers.codex`.
- `qswap codex auto` runs the shared auto-switch engine over Codex accounts and switches only once the active account is out (`codex.threshold`, default 99), because a switch restarts Codex's background server.
- `qswap codex reset` lists and spends Codex reset credits, and auto mode spends them under the same `resets.*` settings as Claude.
- qswap never refreshes a Codex login and never runs `codex logout`. Tested against fakes; not yet run with two real Codex accounts.

### Menu bar (macOS)

- Codex accounts get a section of their own once Codex manages one; clicking a row switches that provider, and the add, disable and remove menus cover both, named with their provider.
- One auto-switch toggle per provider (*Auto-switch Claude Code*, *Auto-switch Codex*), each hosting that provider's engine in its own thread with its own settings (`codex.threshold` for Codex). The first row says, in words, which engines run; *Settings → Title follows* puts Claude Code's, Codex's or the closest-to-its-limit account in the title.
- Every row and notification uses the command line's words: `#2 work`, `needs re-login` (also for an account the engine set aside), `usage unavailable (HTTP 503), retrying`, `as of 3h 5m ago`, held resets by kind, `extra`, API-key spend. Notifications are titled `ai-quota-swap` and say each thing once, across both engines.
- A cost row, `Today $12.40 · 7 days $179.72 (estimated)`, from the ledger, which the menu bar scans in the background every 15 minutes.
- Adding from a token is one dialog (token, optional email, setup-token or API key); removing asks what `qswap remove` asks; switching to an account that needs re-login asks first with `qswap switch`'s explanation.
- `menubar_settings.json` gains `codex_auto_switch_enabled` and `title_follows`; an older file loads as before, and keys a build does not know are kept.
- The menu is described by one pure function (`menubar.build_menu`) and drawn by a thin rumps layer; `scripts/demo/run.py SCENARIO --menubar` prints it.

### Usage and cost

- `qswap usage` reports tokens and an estimated cost at API prices from the session files Claude Code and Codex write, by day, account, model, project, provider or session. It runs locally and stores no message content.
- The price table is copied from the vendors' published pages; `pricing.json` overrides it.
- An activation log records which account was active when, so usage is attributed to accounts.
- A report reads only the session files its date range needs.

### Usage resets

- `qswap reset` lists every account's usage resets and spends one. `qswap list` says what an account holds ("Holds 1 Fable reset"), the dashboard shows a badge, and the menu bar a notification.
- The auto-switch engine spends resets under `resets.auto` (`last-resort` by default, or `active`, `on-limit`, `off`), only on a fresh reading, at most 3 a day, with a request id that makes a retry safe.
- Resets have kinds (Fable or another model, 5-hour, weekly, full), read from the windows they clear. The planner spends the narrowest one that helps, and spends a weekly or full reset on a per-model limit only when `resets.weeklyForModelLimit` allows it. JSON output gains `scope`.

### API accounts

- API-key account rows show spend today and this month: exact per key from Anthropic's Admin API once the organization is registered, otherwise an estimate from the local ledger, labelled as one.
- `qswap api org add`, `list` and `remove` register an organization's Admin key. It is stored like a saved login, and qswap sends only GET requests with it.
- `qswap budget` sets a monthly budget per API-key account. The engine warns at 80%, never switches to an account at its budget, and leaves an active one that reaches it.
- `qswap usage --billed` shows an organization's usage and cost as Anthropic reports them.

### Tooling

- `providers.py` declares the contracts a provider's switcher implements, and every command builds its switcher through one factory. `ProviderError` is the base of every provider error.
- Tests run with a temporary `CODEX_HOME`, a guard on the real Codex login, and a block on outbound connections.
- CI runs the suite on Linux (Python 3.12 and 3.14), macOS and Windows, and lints with ruff.
- `scripts/demo/` makes screenshots, recordings and a tour of every dashboard screen from invented accounts, in a sandbox that blocks the Keychain, the network and process spawns.
- Design docs in `docs/design/`, plus `docs/architecture.md`, `CONTRIBUTING.md` and `SECURITY.md`.
- `.vscode/launch.json` is gone: its debug entries ran the tool, `purge` included, against the real home.
