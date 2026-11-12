# Roadmap

Status: active, 2026-11-11. Name: `ai-quota-swap`, command `qswap`.

This project started as a fork of [claude-swap](https://github.com/realiti4/claude-swap) and is
becoming its own tool: one place to see how much quota every AI coding account has left, switch
to the best one automatically, spend limit resets at the right moment, and know what the tokens
were worth. Claude Code first, Codex second, API keys third.

## Where it stands

0.1.0 is the first release, on PyPI as `ai-quota-swap`. What works:

- Claude Code accounts, from claude-swap 1.1.0b1 and extended: switching by hand and
  automatically, parallel sessions (`run`, `map`), export and import, JSON output.
- Codex accounts (experimental): add, `qswap codex login`, switch, auto-switch, reset credits.
  Tested against fakes and with one real account; not yet run with two real Codex accounts, and
  no real reset credit has been spent.
- Usage resets by kind for both providers, spent by hand or by the engine under `resets.auto`,
  with a fresh reading, a daily cap per provider and request-id reuse.
- `qswap usage`: tokens and estimated cost from the local session files of both CLIs.
- API-key accounts: spend, monthly budgets, Admin API figures, `usage --billed`.
- A dashboard and a macOS menu bar covering both providers, with an auto-switch engine per
  provider. The menu bar has been in daily use on the author's Mac since 2026-11-10.
- `qswap doctor`, task-first docs, screenshots and recordings made from invented accounts.

What remains, in rough order:

- Live tests with a second, real Codex account, and the first real reset-credit spend.
- `qswap codex run`: one `CODEX_HOME` per account, for parallel Codex sessions (M2c).
- `where`: the best account per provider now, and when each exhausted one returns (M5).
- Hooks: run a command on a switch, a limit reached, or a reset used (M5).
- OpenAI organization usage for API keys (M4, later).
- A scrub pass of the git history for personal data (M7).

## Principles

1. **Never refresh a live login from the side.** Only the CLI that owns a login rotates its
   tokens. Backups refresh under a lease.
2. **Irreversible actions get brakes.** Spending a reset or money needs a fresh reading, a daily
   cap, a dry run, and a log line.
3. **Unofficial endpoints stay behind one module per provider** and degrade to "unknown" when
   they change. Nothing else breaks.
4. **Local only.** No telemetry, no server. Credentials stay in the OS keychain or 0600 files.
5. **One vocabulary across providers.** A window has a label, a percent used, and a reset time,
   whether it came from Anthropic or OpenAI.

## Milestones

### M0. New identity (done)

- [x] New package, import, and command name: `ai-quota-swap`, `ai_quota_swap`, `qswap`. The old
  `cswap` command is not shipped, so this tool and claude-swap can be installed side by side.
- [x] Its own data directory and keychain service names. An existing claude-swap store moves over
  by export and import: `cswap export FILE` with claude-swap, then `qswap import FILE`. There is
  no direct reader of the old store. The two tools must not run against the same accounts: each
  would rotate the other's saved tokens and log accounts out.
- [x] `LICENSE` keeps the original MIT copyright line and adds ours. The README ends with a
  one-line credit to claude-swap.
- [x] Version restarts at `0.1.0`.

Done when: the full test suite passes under the new name and an export written by claude-swap
1.1.0b1 imports cleanly (`tests/test_transfer.py::TestImportClaudeSwapArchive`).

### M1. Provider seam (done)

Design: [docs/design/providers.md](docs/design/providers.md).

A provider is a switcher object with its own data root and its own engine instance. The Claude
Code switcher stays as it is and becomes the Claude provider; Codex gets a second switcher with
the same members. The engine, usage store, window math, reset planner, and renderers are shared.

- [x] `providers.py`: the engine-host contract (`EngineHost`) and the surface contract
  (`SurfaceHost`) as Protocols, and the provider registry, plus `provider_id`, `display_name`,
  and `cli_name` on the Claude switcher.
- [x] The engine stops calling Claude credential helpers directly and goes through the host
  (`credential_expires_at_ms`, `credential_fingerprint`).
- [x] One switcher factory in `cli.py`, and the `qswap codex <verb>` prefix.
- [x] A common base for provider errors. It is named `ProviderError`, because `SwitchError`
  already names the switch-operation error.
- [x] Tests: `CODEX_HOME` isolation, the real `~/.codex` under the store guard, and a global
  network block.

Done when: the existing tests pass unchanged and `qswap codex list` reports that no Codex
accounts are managed yet.

Result: the existing tests pass. Nine of them were reaching real endpoints (Anthropic usage and
token refresh, PyPI) and now patch those calls. Until M2a, `qswap codex <verb>` exited 1 with
"Codex support is not available yet".

### M2. Codex (M2a and M2b done; M2c open)

Design: [docs/design/codex.md](docs/design/codex.md). Experimental until it has been run with
two Codex accounts.

- [x] M2a: capture, list, remove, and switch Codex logins (`qswap codex ...`); live usage in
  `qswap list` and `--json`; plan label; warnings for the background server and desktop app.
  Done: `qswap codex add | list | status | switch | remove | alias | disable | enable |
  threshold | priority`, the Codex section in `qswap list` and `providers.codex` in
  `list --json`, the reset-credit list (read only), the activation log for `qswap usage`, and
  the `codex.restartDaemon` setting. User guide: [docs/codex.md](docs/codex.md). Not yet run
  with two real accounts. `codex login` over an existing login revokes it (verified in the
  CLI source at rust-v0.164.0 and rust-v0.167.0), so a second account is logged in under a
  throwaway `CODEX_HOME`.
- [x] M2b: `qswap codex auto` on the shared engine; reset credits (list, manual spend,
  auto-spend under the shared `resets.*` policy). Done: `qswap codex auto` (the shared engine
  over the Codex switcher, with its own state and the `codex.threshold` setting, default 99,
  since a switch interrupts Codex), `qswap codex reset` (list and spend, with the same
  bookkeeping, daily cap and request-id reuse as `qswap reset`; request ids are UUID v4), and
  `qswap codex login` (logs a further account in under a private temporary `CODEX_HOME` and
  saves it, so the live login is never signed out). `qswap usage` also notices a Codex login
  changed outside qswap. Spending a credit is tested against a fake server only; the first
  real spend is the maintainer's to make.
- [ ] M2c: `qswap codex run`, one `CODEX_HOME` per account, for parallel accounts.

Rules: qswap never refreshes a Codex login, never runs `codex logout`, and copies login
records byte for byte.

### M3. Token and cost ledger (done, two items open)

Design: [docs/design/ledger.md](docs/design/ledger.md). Guide: [docs/usage.md](docs/usage.md).

`usage` command: tokens by day, account, model, and project, with an API-equivalent dollar
figure.

- [x] Incremental readers for Claude Code transcripts (`~/.claude/projects/**/*.jsonl`) and
  Codex session files, into a small SQLite store.
- [x] Attribution: a structured switch log says which account was active when; `run` sessions
  attribute exactly.
- [x] A bundled price table with a user override file. Costs are labeled as estimates.
- [ ] Per subscription: value used against plan price, cache hit rate.
- [ ] History of quota readings, so the dashboard can show burn rate and "weekly limit reached
  Thursday 15:00 at this pace". (`--json` carries a linear projection on weekly windows today.)

### M4. API accounts (done)

Design: [docs/design/api-accounts.md](docs/design/api-accounts.md). Guide:
[docs/api-accounts.md](docs/api-accounts.md).

- Spend today and this month on every API-key account row (`list`, `list --json`, the
  dashboard, the menu bar): from Anthropic's usage report per key when an organization's Admin
  key is registered and the account is linked to its key, otherwise estimated from the local
  ledger and labelled as an estimate. Months and days are UTC.
- `qswap api org add|list|remove` for Admin keys, stored like saved logins; qswap only sends
  GET requests with them, and `ANTHROPIC_ADMIN_KEY` works for one command without being
  stored.
- `qswap budget`: a monthly budget per API-key account. The engine warns at 80% once a month,
  never switches to an account at its budget, and leaves an active one that reaches it.
- `qswap usage --billed`: an organization's usage and billed cost as Anthropic reports them,
  by day, model, key or workspace.
- [x] A live switch to an API key: qswap's `apiKeyHelper` line in Claude Code's user settings
  moves running sessions onto the key and back without logging anyone out; `qswap run N` on a
  key for one session; `qswap credit` for a prepaid balance
  ([docs/design/api-key-sessions.md](docs/design/api-key-sessions.md)). Tested end to end on
  Claude Code 2.1.322 with print-mode sessions and a made-up key.
- [x] The engine keeps a key's spend figure fresh itself: it scans the ledger before it enters
  a key and every 5 minutes while one is active.
- [ ] The first switch on a real `~/.claude/settings.json` with interactive sessions and a
  claude.ai login underneath, by hand: the end-to-end run had neither.
- [ ] Remote Control and claude.ai connectors in a running session when the key line appears
  and when it goes; then count the sessions attached through Remote Control in the switch
  output, and hold the engine's move to a key while any are attached.
- [ ] `qswap upgrade` while a key is active. The line names the installed `qswap`, which an
  upgrade replaces for a few milliseconds; sessions holding a cached key ride it out, so this
  waits until it shows up.
- [ ] Attributing `qswap run N` sessions on a key by a record of the launch, rather than by
  time and Claude Code's session-owner stamp.
- [ ] Later: OpenAI organization usage.

### M5. Engine

- [x] One reset policy across providers: Codex credits follow the same `resets.*` settings.
- [ ] `where`: the best account per provider right now, and when each exhausted one returns.
- [ ] Wait or switch: don't jump to an account with 1% left when the active one resets in 40
  minutes.
- [ ] Hooks: run a user command on switch, limit reached, and reset used.

### M6. Surfaces

- [x] TUI: provider sections, a usage and cost screen, an auto view per provider.
- [ ] TUI: history sparklines.
- [x] Menu bar: Codex rows, cost today, auto-switch per provider.
- [ ] `statusline`: a one-line output for Claude Code's status line.
- [x] Every `--json` payload versioned (`schemaVersion`) and documented in
  [docs/json-output.md](docs/json-output.md). A machine-readable schema file is not written yet.

### M7. Public release

- [x] A README a developer can act on in one screen: what it does, install, first account, the
  commands that matter. Screenshots and short recordings of the dashboard, `list`,
  auto-switch, and a reset being spent, all made from a fake account store. (Unframed: the
  ScreenFramed step needs a login the build machine does not have.)
- [x] Task-first docs under `docs/`: quickstart, auto-switch, resets, Codex, usage and cost,
  API accounts, sessions, dashboard, menu bar, settings, JSON output, troubleshooting, coming
  from claude-swap.
- [x] `SECURITY.md` on how credentials are stored and which unofficial endpoints are used.
  `CONTRIBUTING.md`, `CHANGELOG.md`.
- [x] CI on macOS, Linux, and Windows. A publish workflow for PyPI by trusted publishing.
- [x] The first release to PyPI: 0.1.0.
- [x] A scrub pass of the tests and fixtures: no personal emails, tokens, or account data.
- [ ] The same for the git history.
- [x] GitHub description and topics: `claude-code`, `codex`, `account-switcher`, `multi-account`,
  `rate-limit`, `usage-tracker`.

## Not planned

- A proxy or load balancer that sits between the CLI and the API.
- A desktop GUI. The TUI and the menu bar cover it.
- Gemini, Cursor, and others for now. The provider seam leaves room.

## Order

M0, then M1. M2 and M3 run in parallel after M1. M4 needs M3. M5 and M6 follow M2. M7 last.
