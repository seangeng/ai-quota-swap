# JSON output

Add `--json` to a qswap command and a script can read what you see on screen: each account's quota and when it refills, whether a login needs renewing, what a switch did, every decision the auto-switch engine makes, token costs, and what `qswap doctor` found. This page shows each command's real output, the rules that keep scripts working across releases, and every field.

## Commands

| Command | Prints |
|---|---|
| `list`, `codex list` | `{schemaVersion, activeAccountNumber, accounts}`; `qswap list` adds `providers.codex` |
| `status`, `codex status` | `{schemaVersion, active, totalManagedAccounts}`; `qswap status` adds `providers.codex` and `autoSwitch` |
| `switch`, `codex switch` | a [switch result](#switch) |
| `reset`, `codex reset` | `{schemaVersion, auto, plan, accounts}`; with an account, `{schemaVersion, account, grantId, result}` |
| `priority`, `codex priority` | `{schemaVersion, accounts: [{number, email, priority}]}`; setting one, `{schemaVersion, account: {number, email}, priority}` |
| `config`, `config list` | `{schemaVersion, path, settings: [{key, value, isSet}]}`; `config get KEY`, `{schemaVersion, key, value, isSet}` |
| `usage` | a [token and cost report](#usage) |
| `budget` | `{schemaVersion, accounts: [{number, email, budget, apiSpend}]}`; setting one, `{schemaVersion, number, email, budget, changed}` |
| `credit` | `{schemaVersion, accounts: [{number, email, credit, apiSpend}]}` for the accounts with a credit; setting one, `{schemaVersion, number, email, credit, changed}`. `credit` is `{amount, since, expires}` (`null` once removed) |
| `api org list` | `{schemaVersion, organizations: [{name, organizationName, organizationId, storage, added, linkedAccounts}], environmentKey}`; never a key |
| `doctor` | `{schemaVersion, checks: [{id, group, status, summary, fix}], counts, online}` |
| `auto`, `codex auto` | one [event](#auto-switch-events) per line |

Any other command answers `--json` with a usage error (exit 64). Real output follows; `...` marks lines left out.

```
$ qswap list --json
{
  "schemaVersion": 1,
  "activeAccountNumber": 1,
  "accounts": [
    {
      "number": 1,
      "email": "maya@example.com",
...
      "active": true,
      "usageStatus": "ok",
      "usage": {
        "fiveHour": {
          "pct": 46.0,
          "resetsAt": "2026-11-10T16:55:30+00:00",
          "countdown": "2h 35m",
          "clock": "16:55"
        },
...
      },
...
      "usageAgeSeconds": 119.2,
      "needsRelogin": false
    },
...
  ],
...
}
$ qswap status --json
{
  "schemaVersion": 1,
  "active": {
    "number": 1,
...
  },
  "totalManagedAccounts": 5,
...
  "autoSwitch": {
    "running": false,
    "providers": [],
    "dryRun": false,
    "lastTickAt": null
  }
}
```

```
$ qswap switch 2 --json
{
  "schemaVersion": 1,
  "switched": true,
  "from": {
    "number": 1,
    "email": "maya@example.com"
  },
  "to": {
    "number": 2,
    "email": "maya@northwind.example.com"
  },
  "strategy": "direct",
  "reason": "switched",
  "message": "Switched to Account-2 (maya@northwind.example.com)",
  "warnings": []
}
```

`reset` and `auto` print one object per line:

```
$ qswap reset 1 --yes --json
{"schemaVersion": 1, "account": {"number": 1, "email": "maya@example.com"}, "grantId": "launch-2611-fable", "result": {"result": "reset", "reason": null, "resetsLeft": 0, "cleared": ["seven_day_overage_included"], "weeklyResetsAt": "2026-11-13T20:20:30+00:00", "cooldownUntil": null, "detail": null}}
$ qswap auto --json
...
{"schemaVersion": 1, "event": "poll", "ts": "2026-11-10T14:22:00Z", "active": {"number": 1, "email": "maya@example.com"}, "headroomPct": {"1": 9.0, "2": 79.0, "3": 7.0}, "threshold": 90.0, "windowsPct": {"1": {"5h": 91.0, "7d": 38.0}, "2": {"5h": 12.0, "7d": 21.0}, "3": {"5h": 93.0, "7d": 81.0}}}
{"schemaVersion": 1, "event": "switch", "ts": "2026-11-10T14:22:00Z", "trigger": "proactive", "from": {"number": 1, "email": "maya@example.com"}, "to": {"number": 2, "email": "maya@northwind.example.com"}, "warnings": [], "dryRun": false}
```

```
$ qswap usage --json
{
  "schemaVersion": 1,
  "by": "day",
...
  "rows": [
    {
      "key": "2026-11-04",
      "label": "Wed 2026-11-04",
...
      "costUsd": 41.9432,
      "unpricedTokens": 0
    },
...
}
$ qswap doctor --json
{
  "schemaVersion": 1,
  "checks": [
...
    {
      "id": "codex.cli",
      "group": "codex",
      "status": "warn",
      "summary": "Codex was not found (codex is not on PATH)",
      "fix": "npm install -g @openai/codex"
    },
...
}
```

## Contract

- Every payload and every event carries `schemaVersion`, now `1`. It changes only for a breaking change.
- The contract is additive: payloads only gain fields, and new event kinds and `reason` values may appear. Ignore what you do not know.
- stdout holds only JSON: one document, or with `auto` one object per line. Notices go to stderr, and the daily update check does not run. Parse the JSON rather than matching text; indentation differs between commands.
- A command that fails prints an error envelope on stdout. `type` is the kind of error (`SwitchError`, `ConfigError`, `ValidationError`, `ConfirmationRequiredError` when a command needs `--yes` and has no terminal to ask on, `KeyHelperUnavailableError` when qswap cannot add the key line a switch to an API-key account needs, `UsageError`, ...). An error may add facts of its own beside `type` and `message` (`sessions`, `unreadable` when not 0, and `keyHelper`, the reason, for `KeyHelperUnavailableError`; `SessionsRunningError`, its parent type, carries the first two). Key scripts on `type` and the exit code; `message` is for people.
- The strings in a switch result are for people too. `message` and a rotation's skip lines (`Skipped Account-2 (needs re-login)`) keep the `Account-N` spelling of earlier releases, while other warnings and error messages name accounts the way the text output does (`#3 night-shift: usage unavailable (HTTP 503); switching anyway`). Use `from`, `to` and `number` instead.

A refused switch, to an account that needs re-login:

```
$ qswap switch 2 --json
{
  "schemaVersion": 1,
  "error": {
    "type": "SwitchError",
    "message": "#2 work needs re-login (its saved login was refused); not switched.\nLog in to it in Claude Code (/login), then run: qswap add\nTo switch to it anyway: qswap switch 2 --anyway"
  }
}
```

| Exit code | Meaning |
|---|---|
| 0 | Success. `auto --once`: it switched (with `--dry-run`, it would have) |
| 1 | A handled error, printed as the envelope. `doctor`: a check failed (its normal payload is printed). `auto --once`: an error |
| 2 | `auto --once` only: nothing to do, including a check that only spent a usage reset |
| 3 | `auto --once` only: blocked; it wanted to switch, but no account could take over |
| 64 | A usage error (unknown verb, bad flag, missing argument), printed as the envelope with `type` `UsageError` |
| 130 | Interrupted with Ctrl-C. No further JSON is printed |

## Account rows

`list` and `codex list` rows, and the `active` object of `status`, share these fields: `number`, `email`, `organizationName`, `organizationUuid`, `isOrganization`, `active` (list only; `status` has `managed: true` instead), `usageStatus` and `usage`. `usage` is an object only when `usageStatus` is `ok`, a reading recent enough to act on (at most 5 minutes old), and `null` for every other status: `unavailable` (no such reading; `usageError` says why, and `lastGoodUsage` may hold an older one), `relogin_required` (the saved login was refused; the text views say `needs re-login`), `token_expired` (the active login's token expired and renews on its own), `unknown_until_used` (Codex: a saved login whose token expired while unused; Codex renews it on first use), `api_key` (no quota; billed per token), `keychain_unavailable` (the active login could not be read from a locked or busy macOS Keychain), `foreign_credential` (the active login belongs to another account; switching to this one repairs it) and `no_credentials` (no saved login).

Optional fields, each absent unless its condition holds:

| Field | Present when | Meaning |
|---|---|---|
| `usageFetchedAt`, `usageAgeSeconds` | `usage` is an object | When the reading was taken, and its age |
| `lastGoodUsage`, `lastGoodFetchedAt`, `lastGoodAgeSeconds` | `usage` is null and an older reading exists | That reading, for display only |
| `usageError` | `usageStatus` is `unavailable` and a read failed | The failure: `http-503`, `http-429`, `timeout`, ... |
| `usageRetryAt` | qswap is waiting before it reads again | When the next read is due |
| `alias` | an alias is set | e.g. `"personal"` |
| `disabled` | the account is held out with `qswap disable` (list rows) | `true` |
| `threshold` | a per-account switch line is set with `qswap threshold` (list rows) | e.g. `85.0` |
| `needsRelogin` | always, in list rows and Claude Code's `status` row | `true` when the account will not be used until its owner logs in again |
| `setAside` | the auto-switch engine holds the account out | `{"reason": "invalid_grant"}` (its login was refused) or `"identity-conflict"` (it belongs to another account) |
| `loginExpiresAt` | Claude Code list rows whose saved login records an expiry | When it will need a fresh `/login` and `qswap add` |
| `plan` | list rows, once the plan has been read | `label` as the text views print it (`Max 20x`, `Team`, `Pro`, ...), the raw `tier`, `organizationType`, `billingType`, `subscriptionStatus` and `planDisplayName` when known, `expired` and `fetchedAt` |
| `budget` | an API-key account with a monthly budget (`qswap budget`) | Dollars a month, e.g. `500.0` |
| `credit` | an API-key account with a recorded credit (`qswap credit`) | `{amount, since, expires}`: dollars, when it was recorded (ISO time), and the last usable UTC day or `null` |
| `how` | the API-key account active through Claude Code's `apiKeyHelper` | `"helper"` ([api-accounts.md](api-accounts.md#switch-to-an-api-key)) |
| `underneath` | the subscription logged in under an active key | `true`; that row's `active` is `false` |
| `apiSpend` | an API-key account with a known spend, a budget, a registered organization, or a usage ledger that exists but could not be read (then `source` is `null` and `note` is `this machine's usage ledger could not be read; run: qswap usage`) ([api-accounts.md](api-accounts.md)) | `used` and `today` (dollars, UTC, `null` when unknown), `limit`, `pct`, `currency`, `source` (`admin-api`, `ledger` or `null`), `month`, `resetsAt`, and when known `asOf`, `organization`, `apiKeyId`, `unpricedTokens`, `note`; with a credit, `credit`: `{amount, used, left, since, expires, expired, spent, source}`, where `used` and `left` are `null` while there is no figure |

`usage` holds `fiveHour` and `sevenDay` when the account has them: `pct` (`null` when the server gave no figure), `resetsAt`, and the text view's `countdown` and `clock`. `scoped` lists per-model weekly windows, each with a `name` such as `Fable`. `spend` is extra usage on a subscription (`used`, `limit`, `pct`, `currency`, `resetsAt`, `countdown`, `clock`). A setup-token reading taken with `usage.headerProbe` adds `source: "headers"`, and a refusal adds `rejected: {retryAfterS, resetsAt}`. Once a week is a day old, `sevenDay` and each `scoped` window add `expectedPct` (where even use would be), `aheadOfPace`, `projectedExhaustionAt` and `willLastToReset`. The last two are rough linear projections, so only JSON shows them.

`usage.resets` lists the usage resets an account holds: `eligible`, `ineligibleReason`, `atLimit`, `nextGrantId`, `weeklyResetsAt`, `cooldownUntil`, `resetsLeft`, and `grants`. Each grant has `id`, `label`, `kind` (the model name such as `Fable`, `session`, `weekly`, `full`, or `reset` for a shape qswap cannot name), `scope` (`model`, `session` for the 5-hour window, `weekly`, `full`, or `unknown`, which is never spent automatically), `resetsLeft`, `resetsTotal`, `startsAt`, `endsAt`, `clears` and `clearsUnknown` (window names as the server spells them), `blocking`, `usableNow`, `useRequiresLimit`, `givesBackWeekly`, `paused`, `unavailableReason`, `unusableReason` and `percentUsed`.

Codex rows differ: `organizationUuid` is the ChatGPT account or workspace id, `organizationName` is empty, and `isOrganization` is `true` only on a workspace plan. `plan` has `label` (`Plus`, `Pro`, `Team`, ...), `tier`, `planDisplayName`, `expired` (always `false`) and `fetchedAt`. Each reset credit is one grant of kind `full`; its `scope` is `full`, or `weekly` on a plan with only a weekly window. Codex rows never carry `loginExpiresAt`, `budget` or `apiSpend`, and Codex's `status` row has no `needsRelogin`.

## List and status

Under an API key active through the key line, `activeAccountNumber` is the key and `underneathAccountNumber` the subscription logged in under it. When the key line names an account that does not exist or is not an API-key account, `list` and `status` add `keyHelperProblem`: `{number, why, fix}` with `why` `missing` or `not-api-key` and `fix` the command that removes the line. `status`'s `active` adds `how` (`helper`) and the payload `underneath` (`{number, email}`) under a key.

`qswap list --json` adds, only when there is something to say, `duplicateAccountWarnings` and `lockstepUsageWarnings` (sentences about two accounts that look like one), `unclaimedCredentials` (ids for `qswap unclaimed`), and `providers.codex`, `{activeAccountNumber, accounts}`, once a Codex account is managed. When the Codex accounts cannot be read, `providers.codex` is `{"activeAccountNumber": null, "accounts": [], "error": {type, message}}` and the Claude Code part is unaffected. Account numbers are per provider. `qswap import-usage` reads only the top-level `accounts`. With nothing managed, `accounts` is `[]` and `activeAccountNumber` is `null`.

`status`'s `active` is `null` when nobody is logged in, and `{email, managed: false}` for a login qswap does not manage (Codex may add `problem`). `totalManagedAccounts` counts Claude Code accounts. `providers.codex` (`{active, totalManagedAccounts}`, or `{active: null, error}`) appears when a Codex account is managed or Codex is logged in. `autoSwitch` says whether an engine is running, from its heartbeat: `running`, `providers`, `dryRun`, and `lastTickAt` in epoch seconds or `null`.

## Switch

`switched`, `from` and `to` (`{number, email}`; equal when nothing switched), `strategy` (`direct` for `switch N`, `rotation`, `best`, `next-available`), `reason`, `message` and `warnings`. `reason` is `switched`, or for `switched: false`: `already-active`, `already-best`, `candidates-exhausted`, `no-valid-target`, `only-one-account`, `unmanaged-account` or `usage-unavailable`. `how` says how the target was made active: `helper` (an API key through Claude Code's `apiKeyHelper`, which running sessions follow), `saved-key` (`--log-out-sessions`) or `login`. A target that needs re-login is refused with a `SwitchError` envelope unless `--anyway`. An API-key target for which qswap cannot add the key line (another `apiKeyHelper`, a settings file it cannot parse or write) is refused with a `KeyHelperUnavailableError` envelope whose `error` adds `keyHelper` (why), `sessions` (how many Claude Code sessions run on the shared login) and `unreadable` (session records qswap could not read), unless `--log-out-sessions`. A switch with an API-key account on either side adds `followup`, the lines the text output prints after `Switched` about what the running sessions do now. A target whose usage is unreadable or old is switched to, with a line in `warnings`. A Codex switch may add `backgroundServer`: `{pid, restarted: true}`, or `{pid, restarted: false, reason}` when `codex.restartDaemon` is off or the restart failed.

## Reset

Bare `reset --json` prints `auto` (the `resets.*` settings: `mode`, `threshold`, `minHoursLeft`, `expiryDays`, `expiryThreshold`, `weeklyForModelLimit`, plus `usedToday` and `maxPerDay`), `plan` (what auto-switch would spend now, or `null`: `account`, `grantId`, `grantKind`, `scope`, `reason`, `window`, `pct`, `windowResetsAt`, `hoursLeft`, `grantEndsAt`, `projected`), and `accounts`: `{number, email, active, resets, error}`, where `resets` has the shape of `usage.resets` and `error` says why it is `null` (`no reading`, `api key`, a read failure). Spending one prints `result`: `result`, `reason`, `resetsLeft`, `cleared`, `weeklyResetsAt`, `cooldownUntil`, `detail`. For Claude Code, `result` is `reset`, `already_used`, `not_limited`, `cooldown`, `ineligible` or `unavailable` as the server answered, or `rate_limited`, `auth_error` or `error` when no answer came. For Codex it is `reset` (with `detail` such as `"2 windows reset"` and `resetsLeft` `null`), `already_used` (counts as done), `ineligible` (`reason: "nothing_to_reset"`), `unavailable` (`reason: "no_credit"`), `auth_error`, `rate_limited` or `error`. See [resets.md](resets.md).

## Auto-switch events

Each line has `schemaVersion`, `event` and `ts`. Account numbers inside `headroomPct`, `windowsPct`, `fetchErrors`, the quarantine events and `plan.account` are strings.

| `event` | Fields | Emitted when |
|---|---|---|
| `poll` | `active`, `headroomPct` (account → percent left, `null` unknown), `threshold`, `windowsPct`, `fetchErrors`, `weeklyThreshold` when set | every check |
| `switch` | `trigger` (`proactive`, `at-limit`, `failover`, `consume-first`, `priority`, `key-return`: a key the engine moved to is left because a subscription has room), `from`, `to`, `warnings`, `dryRun` | it switched, or would have with `--dry-run` |
| `no-switch` | `reason`, `detail` | it stayed |
| `limit-reset` | `account`, `plan`, `scope`, `result` (`null` in a dry run), `dryRun` | it spent a usage reset |
| `account-quarantined` | `number`, `email`, `reason` (`invalid_grant`, `identity-conflict`) | it set an account aside; text says `needs re-login` |
| `account-unquarantined` | `number`, `email`, `reason` (`credentials-replaced`, `account-replaced`) | that account's login was replaced |
| `all-exhausted` | `earliestResetAt` | every account is at its limit |
| `budget-warning` | `account`, `usedUsd`, `budgetUsd`, `pct`, `month`, `source` | an API-key account reached 80% of its budget, once a month |
| `sleep` | `seconds`, `until` | the next check is further off than usual |
| `config-warning` | `message` | a setting matches nothing, such as an `autoswitch.model` no account reports |
| `error` | `message`, `transient` | a check failed |

`no-switch` reasons: `below-threshold`, `cooldown`, `active-idle`, `active-usage-unknown`, `active-api-key` (on an API key; one you switched to is left only at its budget or credit), `api-key-sessions` (an API-key account was the only way out, and a provider without the key line has Claude Code sessions running), `key-helper-unavailable` (an API-key account was the only way out, and qswap cannot add its key line), `no-subscription-room` (on a key the engine moved to, waiting for a subscription with room), `already-active`, `already-consuming-priority`, `already-consuming-soonest`, `no-active-account`, `no-candidates`, `no-comparison`, `no-qualifying-candidate`, `no-viable-target`, `reset-unknown`, `stale-usage`, `unmanaged-active-account`, and Codex's `switch-deferred` (Codex may be renewing the active login's token; the next check tries again). See [auto-switch.md](auto-switch.md).

## Usage

The request (`by`, `since` and `until` as local dates, `timezone` as a UTC offset, `provider` or `null`), then:

- `rows`, in the text view's order. `key` is the date, model id, project path, provider, session id, or account key (`email|organization`, `null` for unattributed usage, `desktop` for the Claude desktop app); `label` is what the text view prints. Account rows add `account` (`{number, email}`, with `number` `null` for an account qswap does not manage; `null` for unattributed and desktop rows) and `provider` (`null` for unattributed usage); session rows add `provider` and `project`.
- `total`, and in each row: `input`, `output` (reasoning included), `cacheRead`, `cacheWrite5m`, `cacheWrite1h`, `reasoning`, `requests`, `totalTokens`, `costUsd` (an estimate at API prices; `null` when no token in the row has a price) and `unpricedTokens`.
- `estimate` (always `true`), `unpricedModels` (`[{model, tokens}]`), `unattributedTokens`, `unattributedShare`, `attributionStarts` (per provider), `pricing` (`fetched`, `sources`, `override`, `warnings`).
- `scan`, unless `--no-scan`: `filesSeen`, `filesRead`, `bytesRead`, `newEvents`, `duplicates`, `missing`, `skippedCompressed`, `modifiedSince`, `filesSkipped`, `olderFilesNotInLedger`, `seconds`. `notes` when files were skipped or unreadable.
- `billed`, only with `--billed`: `since`, `until`, `timezone` (`"UTC"`), `startingAt`, `endingAt`, `by`, `notes`, and `organizations`, each with `name`, `organizationName`, `organizationId`, `rows` (`key`, `label`, token fields, `costUsd`, `costSource`: `cost-report` or `estimate`), `total` (token fields and `billedCostUsd`), `error`, `costError` when only the cost report failed, and `fetchedAt`.

See [usage.md](usage.md).

## Doctor

Each check has a stable `id` (`claude.cli`, `claude.login`, `claude.managed`, `claude.settings`, `claude.key_helper`, `claude.profile_settings`, `claude.api_key_helper`, `claude.env`, `claude.api_key_sessions`, `codex.cli`, `codex.storage`, `codex.login`, `codex.managed`, `data.dir`, `data.permissions`, `data.store`, `menubar.service`, `menubar.running`, `menubar.location`, `menubar.platform`, `auto.claude`, `auto.codex`, `install.python`, `install.method`, with `--online` `network.anthropic` and `network.chatgpt`, and `GROUP.error` for a check that could not run), its `group`, `status` (`pass`, `warn`, `fail` or `skip`), `summary`, and `fix`, the command to run, or `null`. Which checks appear depends on the machine. `counts` tallies the statuses; `online` says whether the network checks ran. See [troubleshooting.md](troubleshooting.md).
