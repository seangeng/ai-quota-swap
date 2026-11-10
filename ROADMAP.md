# Roadmap

Status: active, 2026-11-10. Name: `ai-quota-swap`, command `qswap`.

This project started as a fork of [claude-swap](https://github.com/realiti4/claude-swap) and is
becoming its own tool: one place to see how much quota every AI coding account has left, switch
to the best one automatically, spend limit resets at the right moment, and know what the tokens
were worth. Claude Code first, Codex second, API keys third.

## Where it stands

Inherited from claude-swap 1.1.0b1: multi-account switching for Claude Code, the auto-switch
engine, parallel sessions (`run`), the TUI, the macOS menu bar, JSON output, export and import.

Added in this fork so far: usage resets for Claude (`reset`, a `resets:` row, auto-spend with the
modes `active`, `last-resort`, `on-limit`, `off`, a daily brake, and a locked two-engine
reservation).

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

### M2. Codex

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
- M2b: `qswap codex auto` on the shared engine; reset credits (list, manual spend, auto-spend
  under the shared `resets.*` policy).
- M2c: `qswap codex run`, one `CODEX_HOME` per account, for parallel accounts.

Rules: qswap never refreshes a Codex login, never runs `codex logout`, and copies login
records byte for byte.

### M3. Token and cost ledger

Design: [docs/design/ledger.md](docs/design/ledger.md).

`usage` command: tokens by day, account, model, and project, with an API-equivalent dollar
figure.

- Incremental readers for Claude Code transcripts (`~/.claude/projects/**/*.jsonl`) and Codex
  session files, into a small SQLite store.
- Attribution: a structured switch log says which account was active when; `run` sessions
  attribute exactly.
- A bundled price table with a user override file. Costs are labeled as estimates.
- Per subscription: value used against plan price, cache hit rate.
- History of quota readings, so the dashboard can show burn rate and "weekly limit reached
  Thursday 15:00 at this pace".

### M4. API accounts

API-key accounts currently read "API key (no quota)".

- With an Anthropic Admin key: real usage and cost from `GET /v1/organizations/usage_report/messages`
  and `/v1/organizations/cost_report`, plus org rate limits.
- Without one: spend estimated from the M3 ledger.
- A monthly budget per API account. The engine stops routing to an account at its budget.
- Opt-in overflow: when every subscription is exhausted, fall back to an API account with a cap.
- OpenAI API keys for Codex, same shape (endpoints to verify).

### M5. Engine

- One reset policy across providers.
- `where`: the best account per provider right now, and when each exhausted one returns.
- Wait or switch: don't jump to an account with 1% left when the current one resets in 40
  minutes.
- Hooks: run a user command on switch, limit reached, and reset used.

### M6. Surfaces

- TUI: provider sections, history sparklines, a usage and cost screen.
- Menu bar: Codex rows, cost today.
- `statusline`: a one-line output for Claude Code's status line.
- A documented, versioned JSON schema for every command.

### M7. Public release

- A README a developer can act on in one screen: what it does, install, first account, the
  five commands that matter. Framed screenshots and short demo recordings of the dashboard,
  `list`, auto-switch, and a reset being spent, all made from a fake account store.
- Task-first docs under `docs/`: quickstart, auto-switch, resets, Codex, usage and cost,
  sessions, menu bar, JSON schema, troubleshooting.
- `SECURITY.md` on how credentials are stored and which unofficial endpoints are used.
  `CONTRIBUTING.md`, `CHANGELOG.md`.
- CI on macOS, Linux, and Windows. Releases to PyPI by trusted publishing.
- A scrub pass: no personal emails, tokens, or account data in tests, fixtures, or history.
- GitHub description and topics: `claude-code`, `codex`, `account-switcher`, `multi-account`,
  `rate-limit`, `usage-tracking`.

## Not planned

- A proxy or load balancer that sits between the CLI and the API.
- A desktop GUI. The TUI and the menu bar cover it.
- Gemini, Cursor, and others in the first release. The provider seam leaves room.

## Order

M0, then M1. M2 and M3 run in parallel after M1. M4 needs M3. M5 and M6 follow M2. M7 last.
A first public release makes sense after M2 and M3.
