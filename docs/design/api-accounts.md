# Design: API accounts

Status: accepted, 2026-11-10. Implemented 2026-11-10. The Admin API details below were checked
against Anthropic's published reference on 2026-11-10 (sources in the module docstring of
`src/ai_quota_swap/admin_api.py`); where it disagreed with the first draft of this file, the
reference won and this file was corrected (see [Corrections](#corrections-from-the-reference)).

## Goal

An API-key account has no quota windows, so today its row says "API key (no quota)" and
nothing else. After this milestone:

- an API-key account shows what it has spent today and this month;
- it can have a monthly budget, and auto-switch stops using it at the budget;
- `qswap usage --billed` shows an organization's actual API usage and cost, as Anthropic
  reports it.

Scope: Anthropic API keys used by Claude Code. OpenAI API keys are out of scope for now: Codex
exposes no usage for them, and they are not managed accounts.

## Two sources of spend

| Source | What it knows | Needs |
|---|---|---|
| The local ledger | Tokens Claude Code sent while this account was active on this machine, priced from the bundled table | nothing; an estimate, labelled as one |
| Anthropic's Admin API | Tokens per API key across every machine and tool, and the organization's billed cost | an Admin API key for the organization |

A row always shows the best source available and says which one it is.

## Which Admin API

Anthropic has two report APIs for Console organizations:

- the Usage and Cost API, `GET /v1/organizations/usage_report/messages` and
  `GET /v1/organizations/cost_report`, now labelled legacy but "remains supported". Data
  "typically appears within 5 minutes".
- the Usage and Cost Analytics API, `GET /v1/organizations/analytics/usage` and `/cost`, in
  beta (`anthropic-beta: analytics-usage-cost-2026-08-24`). It can split cost by API key, but
  its data "typically refreshes within 4 hours of the underlying usage and can take up to 24
  hours".

qswap uses the first: a budget brake that learns about spend a day late is not a brake. If the
legacy API is retired, the per-key figure moves to the analytics usage endpoint (same token
fields) and the freshness note in the docs changes.

Every request sends the Admin key in `x-api-key` and `anthropic-version: 2026-10-21` (the
current version), and a `user-agent` naming qswap, as the guide asks of integrations.

## Organizations and Admin keys

An Admin key (`sk-ant-admin01-…`) can manage an organization: members, invites, workspaces,
and API keys (read, rename, archive; not create). It is far more powerful than the reports
need. Console Admin keys have no selectable scopes, and the other credentials the Admin API
accepts (an `org:admin` OAuth token, or a personal or service account key not scoped to a
workspace) are no narrower for this. The docs and the `add` prompt say this plainly. qswap
accepts only `sk-ant-admin` keys. Admin keys exist only for organizations, not individual
accounts.

- `qswap api org add [NAME]`: reads the key from a hidden prompt or stdin, never from an
  argument. Checks it with one read-only call, `GET /v1/organizations/me` (answers the
  organization's `id` and `name`; the name is the default NAME), then stores it the way saved
  logins are stored on that platform (macOS Keychain `ai-quota-swap` / `admin-org-<name>`,
  otherwise a 0600 file `credentials/.admin-org-<name>.enc`). The registry
  (`api_orgs.json`) holds names and organization ids, never a key.
- `qswap api org list | remove NAME`. `remove` deletes the key, the links and the cached
  answers; `qswap purge` deletes every stored key.
- Without a stored key, `ANTHROPIC_ADMIN_KEY` in the environment is used for that one command
  and never written anywhere (its answers are cached in memory only, and its links are not
  stored).
- qswap only ever sends GET requests with an Admin key; the request function refuses any
  other method before opening a connection.

## Linking an account to its key id

Usage reports name keys by id, not by secret. For each registered organization, list its API
keys (`GET /v1/organizations/api_keys`, `limit=1000`, paged by `after_id=<last_id>`) when an
account needs it: when a managed API-key account appears that the last listing was never
checked against (at most every ten minutes), and daily. Each key carries
`partial_key_hint`, string or null, in the reference's example `sk-ant-api03-R2D...igAA`.

The matching rule: a managed key matches a hint when it starts with everything before the
`...` (which must begin `sk-ant-` and add something) and ends with everything after it (at
least four characters), with something in between. An account links only when exactly one
listed key across all registered organizations matches it and no other managed account
matches that key; otherwise nothing is linked and the row says why ("2 listed API keys share
this key's hint", "another managed account matches the same API key").

Store the matched `apiKeyId`, the organization name (`apiOrg`) and the hint (`apiKeyHint`) on
the account record. The stored hint is re-checked against the account's current key on every
pass, without the network, so a replaced key relinks. No match means the key belongs to
another organization: fall back to the ledger.

## Spend on an account row

The reading for an API-key account is a dict in the shape of the existing `spend` key. It is
carried beside the bare "API key (no quota)" sentinel rather than in its place
(`UsageEntry.api_spend`): the sentinel is what keeps the account out of window ranking and off
the subscription usage endpoint, and every surface already branches on it.

- `spend.used`: month-to-date dollars (None when nothing is known). `spend.limit`: the budget,
  when one is set. `spend.pct`: used over limit, when there is a limit. `spend.currency`:
  `USD`. `spend.resets_at`: the next month's start.
- `today`: dollars so far today.
- `source`: `admin-api` or `ledger` (None when there is no figure).
- `as_of`, `note` (why the figure is not Anthropic's), `month`, and for a linked key `org`
  and `api_key_id`.

Every consumer of the sentinel and of `spend` was traced (list rows, `list --json`, `status
--json`, TUI cards, mini rows and auto view, menu bar rows, engine, poll policy, import-usage)
and these properties hold:

- an API-key account is still never ranked by quota windows and never polled on the
  subscription usage endpoint (the sentinel and `decision_value()` are unchanged);
- `spend` still never triggers a switch by itself; the budget rule below does;
- JSON stays additive: `usageStatus` for API-key accounts stays `api_key` and `usage` stays
  `null`; the reading is a new row field `apiSpend`, the budget a new field `budget`.
  `import-usage` only reads rows whose status is `ok`, so it is unaffected.

Month-to-date cost for a key is computed from the usage report's tokens with the bundled price
table, because the cost report this API offers cannot be split per key (it groups by
`description` and `workspace_id` only). The usage report is asked for this month to date,
daily buckets (`bucket_width=1d`, `limit=31`), `group_by[]=api_key_id`, `model` and `speed`
(fast mode has its own price), filtered by `api_key_ids[]` to the linked keys: one request per
organization covers every linked account. `ending_at` is the start of tomorrow UTC, since the
reference includes a bucket that ends at `ending_at`. The organization total in `qswap usage
--billed` comes from the cost report itself, and the two are not expected to match to the
cent: the per-key figure leaves out web search, code execution, discounts and credits.

The local figure is read from the ledger without a scan, read-only, with UTC month and day
bounds, and refreshes when the ledger is scanned (`qswap usage`, the menu bar every 15
minutes, the dashboard every 5, and the engine before it enters a key and every 5 minutes
while a key with a budget or credit is active; each records `last_scan_at`). Tokens of a
model with no price count toward a budget or credit at the dearest rate in the table for
their kind of token, never as zero (`unpriced_tokens` says how many).

Months and days are UTC, as in Anthropic's reports (buckets snap to UTC boundaries).

## Budgets

`qswap budget ACCOUNT AMOUNT` sets a monthly budget in dollars; `qswap budget ACCOUNT off`
removes it; `qswap budget` lists them. Stored on the account record like `threshold`, and
carried across a `--slot` re-add and an `import --force` like the other user settings.

- At or over budget, an API-key account is not a candidate for auto-switch.
- If the active account is an API-key account at or over budget, the engine treats it as at
  its limit (trigger `at-limit`) and looks for another account.
- At 80% the engine emits one `budget-warning` event per account per month, remembered in
  `autoswitch_state.json` (`budgetWarnings`).
- A budget or credit needs a figure no older than 20 minutes (`as_of`: Anthropic's report
  time or the ledger's last scan). Without one the engine treats the key as at its limit
  (`spend-unknown`), and the rows say `limit not enforced: no spend figure`.
- A key the engine judged at its limit stays so in `autoswitch_state.json` until the month
  rolls over (budget) or the budget or credit is set again, so a reading that drops because
  its source got worse does not make it a candidate.
- A key with neither a budget nor a credit is never entered by the engine. The rest of the
  rules for using a key are in [api-key-sessions.md](api-key-sessions.md#live-activation-the-default-for-api-key-accounts).

A budget is a brake inside qswap, not a hard cap at Anthropic. The docs say so and point to
workspace spend limits in the Console for a real cap.

## `qswap usage --billed`

Adds a section from the Admin API for each registered organization, for the same window as
UTC days, and the `--by` grouping where the reports support it: day, model, key (`account`
reads as key) and workspace. The usage report groups by all four; the cost report has daily
buckets only and groups by `description` (which carries `model` and `cost_type`) and
`workspace_id`, not by key, so per-key cost is estimated from tokens and labelled so, with the
cost report's total beside it. Cost amounts are decimal strings in cents (`"123.45"` is
$1.23). It is the only part of `qswap usage` that uses the network, and only when asked.

## Fetching

- One module owns the Admin API calls (`admin_api.py`): pagination (`next_page` passed back as
  `page` for reports, `last_id` as `after_id` for keys), a cache under `cache/admin_api.json`
  with a five-minute lifetime (data lags about five minutes; Anthropic supports polling once a
  minute, so this is well inside it), and backoff on failure: 1, 2, 4 minutes up to 30, a 429's
  `retry-after`, 30 minutes for a rejected key. A stale answer up to an hour old is shown,
  marked, while backing off.
- Failures never break `list` or the engine: the row falls back to the ledger, with a short
  note.
- All HTTP is faked in tests.

## Corrections from the reference

What the first draft assumed, and what Anthropic's reference says:

1. The draft named "the usage report" and "the cost report" without endpoints. They are the
   legacy Usage and Cost API; a newer Analytics API exists. Added [Which Admin API](#which-admin-api)
   and why the legacy one is used.
2. "Anthropic's cost report cannot be split per key": true of the legacy cost report, false of
   the Analytics cost endpoint, which groups by `api_key_id` but lags hours. Reworded.
3. "Anthropic offers no narrower key": Console Admin keys indeed have no scopes, but other
   credentials (an `org:admin` OAuth token, unscoped personal and service account keys) can
   call the Admin API too. None is narrower for reports; qswap accepts Admin keys only.
4. "Checks it with one read-only call": the call is `GET /v1/organizations/me`.
5. Linking "by the key's partial hint": the field is `partial_key_hint`, nullable, shaped like
   `sk-ant-api03-R2D...igAA`; the matching and ambiguity rules are now written down. Under
   version 2026-10-21 a key's workspace is `scope.workspace_id`.
6. "Reports lag by a few minutes; do not poll faster than every five": the reference says data
   typically appears within five minutes and polling once a minute is supported. The
   five-minute cache stays, as a lag-matched choice rather than a limit.
7. The `--by` groupings: the cost report has daily buckets only and cannot group by key or
   model directly (model comes through `description`), so per-key cost is an estimate.
8. Cost amounts are decimal strings in cents, not dollars.

Changed while implementing, not from the reference: the reading is carried beside the
sentinel, not in its place, and its JSON is a new row field rather than the `usage` object, so
that the three properties above hold without touching the engine's window logic.
