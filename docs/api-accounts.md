# API-key accounts: spend and budgets

An API-key account has no 5-hour or weekly window, so qswap shows what it spent instead, today and this month. After this page you can add a Console API key, see its spend, give it a monthly budget that auto-switch respects, and read Anthropic's own figures with an organization's Admin key.

## Add an API key

```text
$ qswap add-token sk-ant-api03-... --email build@example.com
Added #6 build@example.com (from API key)
```

`qswap add-token` detects the kind of token. Any value that starts with `sk-ant-` and is not an OAuth token (`sk-ant-oat...`, `sk-ant-ort...`) is stored as an API key: workspace keys (`sk-ant-api03-...`), personal keys (`sk-ant-usr-...`) and the other Console key families. Anything else is stored as a setup-token (below).

- `-` reads the token from standard input (`pbpaste | qswap add-token -`). With no token at all, qswap asks for it at a hidden prompt on a terminal. Both keep the key out of your shell history.
- `--email` labels the account. Without it the account is stored as `api-key-N@token.local`, N being the account number, and named `#N api-key-N` wherever qswap names it (`--json` keeps the stored value).
- `--slot N` stores it in slot N; `--yes` replaces an account already in that slot without asking.
- Adding makes no request to Anthropic. The key is stored like a saved login (see [../SECURITY.md](../SECURITY.md)).

An API-key account switches like any other (`qswap switch 6`), and Claude Code then uses the key. Auto-switch treats it differently, as [Set a budget](#set-a-budget) explains.

### Setup-tokens

A setup-token is the long-lived token `claude setup-token` prints. It is added with the same command:

```text
$ qswap add-token sk-ant-oat01-...
Added #7 setup-token-7 (from token)
```

The usage endpoint does not answer setup-tokens, so by default their row reads:

```text
  #7 setup-token-7
     usage unavailable (setup-tokens cannot read usage;
     `qswap config set usage.headerProbe true` reads it from a 1-token request)
```

With `usage.headerProbe` on, qswap reads the 5-hour, weekly and Fable windows from the rate-limit headers of a request for one Haiku output token, sent at the normal polling pace. Each probe is a real request that counts against that account's quota, which is why it is off by default.

## See what it spent

`qswap list`, the dashboard and the menu bar show the spend on the account's row:

```text
$ qswap list
...
  #5 ci · API key
     $2.06 today · $25.16 this month of $40 (63%) · estimate
...
```

Months and days are UTC, as in Anthropic's reports, so the month starts again at midnight UTC on the 1st. The row takes these forms:

| Row | When |
|---|---|
| `$2.06 today · $25.16 this month` | No budget |
| `... this month of $40 (63%)` | A budget is set |
| `... this month of $20 (126%, over budget)` | At or past the budget |
| `$25.16+ this month` | Some of the tokens are from a model with no price, and are left out |
| `· estimate` at the end | The figure comes from this machine's session files, not from Anthropic ([Where the figure comes from](#where-the-figure-comes-from)) |
| `no usage recorded on this machine` | The estimate is $0 today and this month, or nothing has been scanned yet (no usage ledger on this machine). A one-line row (menu bar, dashboard) says `no usage recorded` |
| `$0 of $500 this month · no usage recorded on this machine` | The same, with a budget. A one-line row says `$0 of $500 this month` |
| `spend unknown: this machine's usage ledger could not be read; run: qswap usage` | The ledger exists but could not be read (written by another version, or damaged). Never shown as $0. A one-line row says `spend unknown` |
| `no spend data yet` | A registered organization could not give a figure (for example, the key is not in it), and there is no estimate either. A second line says why |

A second line, when there is one, says why the figure is not Anthropic's (`key not found in a registered organization; estimate from this machine's transcripts`), how old an estimate is once it is more than an hour old (`estimate as of the last qswap usage, 3h ago`), or how old Anthropic's figure is while it cannot be refreshed (`Admin key rejected, figures from 12m ago`).

`qswap budget` lists every API-key account's budget and spend:

```text
$ qswap budget
Monthly budgets for API-key accounts (UTC month):
  5: ci  $25.16 of $40 this month (63%) (estimate) · $2.06 today
A budget is a brake inside qswap, not a cap at Anthropic: auto-switch stops using the account at its budget. For a hard cap, set a workspace spend limit in the Console.
```

`qswap list --json` and `qswap budget --json` add an `apiSpend` object and a `budget` field to the account ([json-output.md](json-output.md)).

## Set a budget

```text
$ qswap budget 5 500
Set a monthly budget of $500 for #5 api-key-5.
  A budget is a brake inside qswap, not a cap at Anthropic: auto-switch stops using the account at its budget. For a hard cap, set a workspace spend limit in the Console.
```

The account can be a number, email or alias. The amount is US dollars per UTC month, above 0 and at most $10,000,000, such as `500` or `49.50`. `qswap budget 5 off` removes it. A subscription account is refused: budgets apply to API-key accounts only.

What auto-switch (`qswap auto`, the menu bar, the dashboard's auto view) does with an API-key account; [auto-switch.md](auto-switch.md) has the rest of its rules:

- It never ranks one against subscriptions. It lands on one only as a last resort, when it must move and no subscription has room, and only with `--include-api-key-accounts` (`autoswitch.includeApiKeyAccounts`).
- An active API-key account under its budget, or without one, is left alone (`#5 ci is an API key · no quota to watch`). With `--include-api-key-accounts`, auto-switch instead counts its missing usage as unreadable and fails over to another account after `autoswitch.unhealthyTicks` checks (3 by default): `#5 ci is an API key · failover in 2 more checks`.
- At 80% of the budget it logs one warning per account per month, which the menu bar also shows as a notification:

  ```text
  14:20:00  #5 ci has spent $25.16 of its $30.00 monthly budget (84%) (estimate)
  ```

- At or over the budget the account is never a target. If it is the active account, auto-switch treats it as at its limit and moves to an account with room, without waiting for the cooldown.
- With no budget, or no figure to hold it against, nothing changes.

### What a budget is not

A budget is a brake inside qswap, not a cap at Anthropic. It changes only what auto-switch does. Claude Code keeps working on the account if you switch to it by hand, and anything else that uses the same key is not affected. For a hard cap, set a spend limit on the key's workspace in the Claude Console.

Figures also arrive late: Anthropic's by about five minutes, the local estimate only when `qswap usage` runs. An account can go past its budget before qswap notices.

## Register an organization's Admin key

The most complete figure is Anthropic's usage report for the key, which counts every request made with it from any machine or tool. Reading it needs an Admin API key (`sk-ant-admin...`) for the organization the key belongs to:

```bash
qswap api org add               # paste the key at the hidden prompt
qswap api org add acme < key.txt
qswap api org list
qswap api org remove acme
```

`add` reads the key from a hidden prompt, or from the first line of standard input; it never takes the key as an argument. It checks the key with one read of the organization's details and saves nothing if that fails (a rejected key, HTTP 403, no answer). Then it stores the key, then links your API-key accounts to the organization's keys and prints one line per account. The name defaults to one made from the organization's name.

The key is kept in the macOS Keychain (item `ai-quota-swap`, account `admin-org-<name>`). On Linux and Windows, or when the Keychain cannot be used, it is a file only your user can read, `credentials/.admin-org-<name>.enc` in the data directory, base64-encoded and not encrypted. `remove` deletes it, and so does `qswap purge`.

To try it without storing anything, set `ANTHROPIC_ADMIN_KEY` for one command. qswap uses it only while no organization is registered and never writes it anywhere.

### What an Admin key can do

`qswap api org add` prints this before it asks for the key: an Admin key can manage your whole organization (members, invites, workspaces and API keys; it can rename or archive keys), and Anthropic offers no read-only key for usage reports. qswap sends it only in GET requests that read the organization's details, its key list, and the usage and cost reports, and refuses any other method before a request is made. The key goes in one request header, never in a URL, a log line, an error message or the cache. Still:

- Create a dedicated Admin key for qswap with an expiry date (Console, Settings, Admin keys).
- Remove it with `qswap api org remove` when you no longer need it.

### How accounts are matched to keys

Anthropic's reports name keys by id. Its key list gives each key a hint, the start and end of the secret, such as `sk-ant-api03-R2D...igAA`. qswap links an account when exactly one listed key in all registered organizations matches the account's key, and no other managed account matches the same one. Otherwise nothing is linked and the row's second line says why (`2 listed API keys share this key's hint; not linked`). An account whose key is in no registered organization is estimated locally.

The link is stored on the account and checked against its key on every read, without the network. qswap lists an organization's keys once a day, or sooner when it sees a key the last listing was not checked against, so a replaced key is relinked within about ten minutes.

## Where the figure comes from

| Source | Row | What it counts |
|---|---|---|
| Anthropic's usage report | no tag | Every request made with the key, anywhere, about five minutes behind. Read at most once every five minutes per organization |
| This machine's session files | `estimate` | Claude Code requests from this machine while the account was active, as of the last `qswap usage` run |

Both are tokens priced with the table behind `qswap usage` ([usage.md](usage.md#the-cost-is-an-estimate)), so neither is your invoice to the cent. Fast mode is priced; discounts, credits and long-context tiers are not, and charges that are not tokens (web search, code execution) are left out.

When Anthropic's report cannot be read (the key was rejected, the Admin API is rate limiting or unreachable), qswap backs off and the row keeps the last figure for up to an hour, then falls back to the local estimate, with the reason on the second line. The local estimate changes only when `qswap usage` runs, so run it now and then, or from cron, if you rely on a budget without a registered organization.

## What the organization was billed

`qswap usage --billed` adds what Anthropic reports for each registered organization over the same dates as the rest of `qswap usage`, by day, model, key or workspace. Its total is Anthropic's cost report. See [usage.md](usage.md#what-anthropic-billed).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `autoswitch.includeApiKeyAccounts` | `false` | Let auto-switch land on an API-key account as a last resort |
| `usage.headerProbe` | `false` | Read setup-token usage from the rate-limit headers of a one-token Haiku request (uses that account's quota) |

Change one with `qswap config set usage.headerProbe true`. The full list is in [settings.md](settings.md).

## Limits

- A budget is enforced by qswap's auto-switch only, and only once a spend figure exists.
- Anthropic's figure needs an Admin key, which can do far more than read reports.
- The local estimate sees only this machine's Claude Code usage while the account was active.
- The menu bar's new build, which shows the budget notification, has not been run by hand yet ([menubar.md](menubar.md)).
- Budgets and the Admin API apply to Claude Code API-key accounts. Codex accounts are covered in [codex.md](codex.md).
