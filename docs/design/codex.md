# Design: Codex provider

Status: accepted, 2026-11-10. Verified against the open-source Codex CLI at `rust-v0.164.0`
and `rust-v0.167.0`, and against three read-only calls on a live account. Nothing here has
been exercised with two Codex accounts yet; the feature ships marked experimental until it has.

Builds on [providers.md](providers.md).

## Facts the design rests on

1. A ChatGPT login is one JSON record: `$CODEX_HOME/auth.json` (default `~/.codex`), with
   `tokens.{id_token, access_token, refresh_token, account_id}` and `last_refresh`. Codex can
   also keep it in the OS keyring (`cli_auth_credentials_store = keyring | auto`).
2. Access tokens last 10 days. Refresh tokens rotate on use, and presenting a used one is a
   permanent failure for that login.
3. A running Codex process never adopts a different account from a swapped `auth.json`. It
   keeps the old token until it expires, then stops with a "signed in to another account"
   error. It never writes the old account back.
4. Two long-lived processes hold a login in memory: the per-home background server that the
   interactive TUI starts (`codex app-server`, pid in `app-server-daemon/daemon.pid`), and the
   ChatGPT desktop app, which always uses `~/.codex`.
5. `codex logout` revokes the refresh token at OpenAI. Every saved copy of that login dies.
6. Usage: `GET https://chatgpt.com/backend-api/wham/usage` with `Authorization: Bearer` and
   `ChatGPT-Account-ID`. It answers a plain `ai-quota-swap/<version>` User-Agent. First-party
   clients poll it every 5 to 60 seconds.
7. Windows are identified by `limit_window_seconds`, not by position. A Pro account reports
   only a weekly window, as `primary_window`.
8. Reset credits: `GET /wham/rate-limit-reset-credits` lists them. Spending one is
   `POST /wham/rate-limit-reset-credits/consume` with `{redeem_request_id, credit_id}`; the
   request id is an idempotency key and must be reused on retry.

## Rules

- **qswap never refreshes a Codex login.** Not the live one, not a saved one. Codex refreshes a
  login itself the next time it is used, even if the access token expired while saved. A saved
  login whose access token has expired shows "usage unknown until used", stays switchable by
  hand, and is not an auto-switch candidate. Revisit once rotation has been tested on a
  throwaway account.
- **qswap never runs `codex logout` and never calls the revoke endpoint.** `remove` deletes
  only qswap's saved copy, and says so.
- **Records are copied byte for byte.** qswap parses a record to read claims but never
  re-serializes it.
- **`file` storage only.** If Codex is configured for keyring storage, or `auth.json` is
  missing while a keyring item exists, every Codex command stops with one line saying so.

## Identity

Decode the JWT claims without verifying signatures (they are our own tokens, read locally).

| qswap field | Source |
|---|---|
| `email` | id_token `email`, else access token `https://api.openai.com/profile`.`email` |
| `uuid` | `chatgpt_user_id` under `https://api.openai.com/auth` |
| `organizationUuid` | `tokens.account_id` (the ChatGPT account or workspace) |
| plan label | `plan_type` from the usage response, else the id_token's `chatgpt_plan_type` |

An account is `(uuid, organizationUuid)`. One person with a personal plan and a Team seat is
two accounts and needs two logins.

## Storage

`<data root>/codex/`:

- `sequence.json`: registry, same shape as Claude's (numbers, records, aliases, policy fields).
- `credentials/account-<n>.json`: the saved record, verbatim, mode 0600, directory 0700,
  written atomically. Codex itself keeps logins in a 0600 file by default, so this matches the
  protection the login already has.
- `cache/usage.json`, `.lock`, `autoswitch_state.json`, `activations.jsonl`: as for Claude.

## Commands (first version)

`qswap codex add | list | status | switch | remove | alias | disable | enable | threshold |
priority | auto | reset`, plus the Codex section in `qswap list` and `list --json`.

- `add` captures the login currently in `auth.json`. `codex login` over an existing login
  revokes it: every login flow first calls `clear_existing_auth_before_login`, which runs
  `logout_with_revoke` (`cli/src/login.rs` at rust-v0.164.0 and rust-v0.167.0). So a second
  account is logged in under an empty, throwaway home (`CODEX_HOME="$tmp" codex login`) and
  added from there (`CODEX_HOME="$tmp" qswap codex add`); see [../codex.md](../codex.md).
- `switch` follows the procedure below.

Not in the first version: `run` (one `CODEX_HOME` per account), directory mappings, API-key
accounts (Codex exposes no quota for them), export and import.

## Switch procedure

1. Read the live record. If its access token expires within 10 minutes and any Codex process
   is running on this home, stop with a retryable error: Codex may be about to rotate it.
2. Take an exclusive lock on `$CODEX_HOME/auth.lock`, then on `auth.json.lock` beside the
   resolved `auth.json` (create either if missing, never delete them). Codex 0.167 takes the
   same two locks in that order.
3. Re-read the live record under the locks. If it belongs to a managed account, save it as
   that account's stored copy (this keeps any refresh Codex did). If it is unmanaged, stop
   unless `--force`, so an unsaved login is never overwritten.
4. Write the target's record to a temp file in the same directory (0600), fsync, and rename it
   over the resolved `auth.json`. A symlinked `auth.json` stays a symlink.
5. Release the locks. Re-read and confirm the account id.
6. Append to `activations.jsonl`.
7. Deal with holders of the old login:
   - Background server running on this home: restart it with
     `codex app-server daemon restart` when `codex.restartDaemon` is true (the default), since
     otherwise new TUI sessions keep using the old account. Report it in the result.
   - ChatGPT desktop app running and the home is `~/.codex`: do not touch it; add a warning
     that it must be restarted to follow the switch.
   - Other running `codex` processes: add a warning that they stay on the old account.

Restarting the background server interrupts work in progress, so the Codex engine defaults to
switching only when the active account is out (`codex.threshold`, default 99), not ahead of
time. Lower it to switch earlier.

## Usage reading

From `/wham/usage`, into the shared reading shape:

- For `primary_window` and `secondary_window`: 18,000 s ±5% is `five_hour`; 604,800 s ±5% is
  `seven_day`; any other length is a `scoped` entry named `daily`, `monthly`, or `<N>h`.
  `pct` is `used_percent`, `resets_at` is `reset_at` as a UTC ISO string.
- `additional_rate_limits[]` become `scoped` entries named by `limit_name`.
- `rate_limit.allowed == false` with no window at 100 or more adds `rejected` with the
  earliest reset time, so the account reads as exhausted.
- `rate_limit_reset_credits.available_count > 0` triggers one more GET for the credit list,
  mapped into `resets.grants[]`: `id` = credit id, `ends_at` = `expires_at`, one use left,
  `clears` = the core windows present, `label` = the credit's title.
- 401 on the live login means the access token expired while Codex was idle: report the
  existing "token expired" state. 401 on a saved login: "usage unknown until used".

Polling uses the existing poll policy. Do not send `x-openai-codex-luna-reserve`.

## Reset credits

`claim_usage_reset(n, grant_id, request_id)` posts to the consume endpoint with the account's
access token. `request_id` must be a UUID v4, stored before the request and reused on retry,
exactly as the Claude path stores its request id. Result mapping:

| Codex `code` | qswap result |
|---|---|
| `reset` | `reset` |
| `already_redeemed` | `already_used` (counts as done) |
| `nothing_to_reset` | `ineligible` |
| `no_credit` | `unavailable` |
| HTTP 401 or 403 | `auth_error` |
| HTTP 429 | `rate_limited` |
| anything else, or no answer | `error` |

Auto-spend goes through the shared engine path and the shared `resets.*` settings, with the
same fresh-read, reservation, and daily cap as Claude. `qswap codex auto` is useful with a
single account: it spends a credit when that account runs out.

## Open questions (need a second account or a throwaway login)

- Whether the background server reconnects running TUIs after a restart.
- Whether a refresh token survives 10+ days saved and unused.
- Which windows one credit resets on plans with both a 5-hour and a weekly window.
- Swapping in keyring mode.
- Sharing sessions and history between per-account homes for `run`.
