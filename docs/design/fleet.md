# Fleet capacity snapshot and urgency routing (`where`, `route`)

Status: implemented (M5).

## Goal

When someone pays for subscriptions across several AI coding providers (Claude Code, Codex, Gemini CLI, Antigravity, Grok, OpenCode, Cursor, GitHub Copilot, Windsurf, Amp, Aider, Cline), picking the right tool for the next task is no longer a single-provider decision. Leaving 60% of a Codex window or three reset credits unused while Claude Code is at 95% wastes flat-rate value.

`qswap` already holds the live quota readings for Claude Code and Codex and the estimated spend ledger. This module turns that into:

1. A single secret-free machine snapshot at `~/.ai-quota-swap/fleet_status.json` (mode `0600`), updated on every `qswap where`, `qswap route`, and menu bar refresh tick.
2. `qswap where [--json]` to show every detected provider's headroom, reset countdown, and expiring credits.
3. `qswap route [--task KIND] [--caller PROVIDER] [--exclude LIST] [--require-cli] [--json]` to rank installed providers by task affinity, remaining headroom, and expiry urgency.

## Privacy boundary

`fleet_status.json` is designed to be read by sibling local tools (Workmesh, `studio-pixbar`, shell prompts, Harness wrappers) without touching credentials or Keychain items. It never writes:

- email addresses or user handles,
- organization or account UUIDs,
- OAuth tokens, refresh tokens, or API keys.

## Providers and quota modes

| Provider | Quota mode | Source |
|---|---|---|
| `claude` (Claude Code) | `live` | `sequence.json` + `cache/usage.json` |
| `codex` (Codex) | `live` | `codex/sequence.json` + `codex/cache/usage.json` |
| `opencode` (OpenCode) | `budget` | read-only SQLite read (`mode=ro`) of `~/.local/share/opencode/opencode.db` `session_v2` |
| `gemini`, `antigravity`, `grok`, `cursor`, `copilot`, `windsurf`, `amp`, `aider`, `cline` | `inferred` | local CLI/app/config detection, optionally overridden in `~/.ai-quota-swap/providers_extra.json` |

Optional providers (`cursor`, `copilot`, `windsurf`, `amp`, `aider`, `cline`) appear in `fleet_status.json` only when installed or explicitly configured in `providers_extra.json`.

## Ranking formula

For each installed, non-excluded provider with remaining headroom:

```text
score = task_affinity × headroom_factor × subscription_boost × expiry_boost × diversity_boost
```

- `subscription_boost`: prefers flat-rate subscriptions (`1.15`) over metered APIs (`0.92`) when room exists.
- `expiry_boost`: boosts providers with ≥20% headroom resetting within 2h (`2.0×`), 12h (`1.55×`), or 24h (`1.25×`), and providers holding unused reset credits (`1.35×`, or `2.2×` when expiring within 5 days).
- `diversity_boost`: gives a cross-harness bonus (`1.25×`) on `review` tasks when `--caller` differs from the candidate, and gently steers away (`0.65×`) when the caller itself is tight (`<= 15%` headroom).
