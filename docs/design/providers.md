# Design: providers

Status: accepted, 2026-11-10.

## Decision

A provider is a **switcher object**. Each provider has its own switcher class, its own data
root, and its own engine instance. The Claude Code switcher is not refactored: it already is
the Claude provider. Codex gets a second switcher that exposes the same members.

We wrap; we do not extract. Moving Claude logic out of `switcher.py` behind an abstract
interface was rejected: a full read of the code found that it would change session profile
paths (which log accounts out on macOS), usage-store identity (which wipes poll state), lock
ordering, and about 180 test patch targets, for no user-visible gain.

## What is shared and what is per provider

| Shared, provider-neutral | Per provider |
|---|---|
| auto-switch engine (`autoswitch.py`) | switcher class |
| usage store and poll policy | data root and registry |
| window math (`relevant_windows`, headroom, lines, pace) | credential capture, activation, refresh |
| reset planner (`resets.plan_reset`) | usage fetch and normalization |
| row rendering, JSON rows, TUI cards, menu bar rows | reset listing and claim |
| settings file | session isolation (`run`) |

## Data roots

- Claude: the data root itself (unchanged, so existing stores keep working).
- Codex: `<data root>/codex/`. It holds its own registry, `cache/usage.json`, `.lock`,
  `autoswitch_state.json`, sessions, and log entries.

The engine keeps its state under `switcher.backup_dir`. Pointing the Codex switcher's
`backup_dir` at `<data root>/codex` gives Codex its own cooldown, quarantine, health counters,
and daily reset brake without touching the engine's state code.

Account numbers are per provider. Claude account 2 and Codex account 2 are different accounts.

## Contract 1: engine host

The engine reaches its switcher through these members only. A provider must implement all of
them with the same shapes. They are declared as a `typing.Protocol` in
`ai_quota_swap/providers.py`.

| Member | Returns |
|---|---|
| `provider_id`, `display_name`, `cli_name` | `"claude"`, `"Claude Code"`, `"claude"` (new attributes) |
| `backup_dir` | the provider's data root |
| `current_account_number()` | number of the account whose login is live, or None |
| `has_live_login()` | True when some login is live, managed or not |
| `switchable_account_numbers()` | rotation order |
| `disabled_account_numbers()` | ordered list of numbers |
| `account_kind_for(n)` | `"oauth"` or `"api_key"` |
| `account_email(n)` | str |
| `account_identity(n)` | `{email, organizationUuid, uuid}`; a provider maps its own ids onto these keys |
| `backfill_account_uuid(n, uuid)` | may be a no-op |
| `account_policies()` | per-account threshold, priority, disabled |
| `read_account_credentials(n, email)` | opaque stored login, only passed back to the two accessors below |
| `credential_expires_at_ms(creds)` | access-token expiry in epoch ms; None for a login that has no expiry; raises `CredentialFormatError` when `creds` is not a login (new) |
| `credential_fingerprint(creds)` | stable hash that changes when the login is replaced (new) |
| `live_session_pids_for(n)` | pids of `run` sessions using this account |
| `consume_backup_grant(n)` | `RefreshOutcome{credentials, error, token_account}`; `error` uses the existing kinds |
| `usage_entries_by_account(fetch, scheduled)` | `{number: UsageEntry}` in the reading shape below |
| `refresh_usage({n})` | same, forced |
| `claim_usage_reset(n, grant_id, request_id)` | `ClaimResult` |
| `switch_to(n, json_output=True)` | `{switched, from, to, warnings, reason}` |
| `set_poll_policy_inputs(...)`, `clear_poll_policy_inputs()` | None |
| `reset_settings()` | `ResetSettings` |

The two new accessors replace the engine's direct calls into `oauth.extract_oauth_data` and
`oauth.credential_fingerprint`. The Claude switcher implements them with exactly that code.

Optional members, outside the Protocol (added in M2b): `new_reset_request_id()`,
`is_reset_request_id(value)`, `add_command`, `recover_hint`. The engine reads them with
`getattr` and falls back to Claude Code's behaviour, so the Claude switcher defines none of
them and its request ids and event text are unchanged.

Rules for implementers:

- No member may take the registry lock and then call the network. Fetch first, then re-check
  under the lock.
- `consume_backup_grant` never refreshes the live login or a login a running session owns.
- Every member that the engine calls must return or raise a `ProviderError`; nothing else.

## Contract 2: usage reading

A reading is a dict. Consumers read these keys by name:

- `five_hour`, `seven_day`: `{pct: number | None, resets_at: tz-aware ISO-8601 string}`. Either
  may be absent. `pct >= 100` means exhausted.
- `scoped`: list of `{name, pct}` with a numeric `pct`, optional `resets_at` and
  `share_of_weekly`. Names must not be `5h`, `7d`, or `rejected`.
- `resets`: reset status with `grants[]`, in the shape `resets.parse_status` returns.
- `spend`, `rejected`, `source`: optional, as today.

A provider maps its own windows onto these names by window length: up to 6 hours is
`five_hour`, 7 days is `seven_day`, and anything else is a `scoped` entry named for what it
limits. If a provider has no weekly window, the weekly strategies have nothing to rank by, so
its engine must run with strategy `best`.

A provider that cannot read usage returns no reading. The engine treats three unreadable ticks
as a failed account and fails over, so a provider must not report "unreadable" for a state it
knows is healthy.

## Contract 3: surfaces

`accounts_snapshot(fetch=...)` returns the existing `AccountsSnapshot`. The TUI, the menu bar,
and `list` render from it. Actions the surfaces call: `switch_to`, `switch`, `add_account`,
`remove_account`, `set_account_disabled`.

Errors: every provider error derives from `ProviderError`. `ClaudeSwitchError` is a subclass,
so existing handlers and tests keep working. (`SwitchError` already names the switch-failure
class that appears in JSON error output, so the base has its own name.)

## Command line

`qswap <verb>` is Claude Code, as before. `qswap codex <verb>` is Codex. `codex` must be the
first argument. Inside `cli.py` every handler gets its switcher from one factory, so the prefix
selects the provider for the whole command. A verb a provider does not support fails with a
one-line message.

Unified views show both: `qswap list` prints a Codex section after the Claude accounts when
Codex accounts exist, and `list --json` adds a top-level `providers.codex` object. The existing
top-level keys keep their meaning, so the JSON contract is only extended.

## Before switching

Added 2026-11-11. The user's side is in [../resets.md](../resets.md#before-switching).

`resets.beforeSwitch` is off by default. Off, auto-switch moves at the switch line, and auto
mode spends the active account's reset only if that account reaches `resets.threshold` anyway.
On, under `resets.auto active` and `on-limit`, auto-switch stays past the line while the
active account holds a reset of its own that auto mode would spend once the window over the
line reaches `resets.threshold`. The reset is spent there (`reset-ahead`). Under `off` and
`last-resort`, or with `resets.enabled false`, the setting does nothing.

It changes when auto-switch moves, never what the planner spends. The hold asks the same
planner, with every guard, and the reset is spent at `resets.threshold` as with the setting
off. A reset auto mode would keep, and one whose last claim failed or was refused, never hold.

Two more cases never hold:

- A reset usable only once a limit is reached. Nothing can spend it below the limit, and the
  server may still refuse it there. It stays banked, as with the setting off.
- An active account with no reading from the last five minutes, or with a failed read since
  its last one. Auto mode spends a reset only on a reading from the last two minutes. A stored
  reading stays trusted for switching long after the account stops answering, so a hold on it
  would keep the engine on an account nothing can spend the reset on.

The cost: the active account runs past its switch line, up to `resets.threshold`. With
`resets.threshold` 100 it sits at its limit until the next check reads it there and spends
the reset. A `resets.threshold` at or below the switch line avoids that, and then the reset
comes first without this setting.

The hold is part of the engine tick that `qswap auto` and `qswap codex auto` share, and it
reads the setting through `reset_settings()`. So it holds for a Codex reset credit as it does
for a Claude Code reset.

## Tests

- `CODEX_HOME` points at a temp dir in every test, and the real `~/.codex/auth.json` is covered
  by the real-store guard.
- A global network block makes any unpatched HTTP call fail the test. Today such a call would
  reach the real endpoint.

## Not decided here

- Whether a saved Codex login may be refreshed from outside (it is not, for now). How Codex
  reset credits are spent is in [codex.md](codex.md#reset-credits).
- Running both engines in one process (menu bar, TUI). That is a surfaces milestone.
