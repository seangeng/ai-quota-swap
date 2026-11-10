# Changelog

## Unreleased

The first release under the name ai-quota-swap, version 0.1.0. The project started as a fork of [claude-swap](https://github.com/realiti4/claude-swap) 1.1.0b1, and everything that version did still works: switching Claude Code accounts by hand or automatically, parallel sessions with `run`, the dashboard, the macOS menu bar, JSON output, export and import.

The package is `ai-quota-swap` and the command `qswap`. It keeps its own data directory, Keychain service, LaunchAgent and `QSWAP_*` environment variables, so it installs beside claude-swap. Accounts move across with `cswap export` and `qswap import`.

### Codex (experimental)

- `qswap codex add`, `list`, `status`, `switch`, `remove`, `alias`, `disable`, `enable`, `threshold` and `priority` keep several Codex CLI logins and switch between them.
- `qswap codex login` logs another account in under a temporary Codex home, so the login in use is never signed out.
- `qswap list` shows a Codex section with live usage, and `list --json` adds `providers.codex`.
- `qswap codex auto` runs the shared auto-switch engine over Codex accounts and switches only once the active account is out (`codex.threshold`, default 99), because a switch restarts Codex's background server.
- `qswap codex reset` lists and spends Codex reset credits, and auto mode spends them under the same `resets.*` settings as Claude.
- qswap never refreshes a Codex login and never runs `codex logout`. Tested against fakes; not yet run with two real Codex accounts.

### Usage and cost

- `qswap usage` reports tokens and an estimated cost at API prices from the session files Claude Code and Codex write, by day, account, model, project, provider or session. It runs locally and stores no message content.
- The price table is copied from the vendors' published pages; `pricing.json` overrides it.
- An activation log records which account was active when, so usage is attributed to accounts.
- A report reads only the session files its date range needs.

### Usage resets

- `qswap reset` lists every account's usage resets and spends one. `qswap list` gains a `resets:` row, the dashboard a badge, and the menu bar a notification.
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
