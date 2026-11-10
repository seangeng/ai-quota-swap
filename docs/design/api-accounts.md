# Design: API accounts

Status: accepted, 2026-11-10. The Admin API details below must be checked against Anthropic's
published reference before any request is written; where the reference disagrees, it wins and
this file is corrected.

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

## Organizations and Admin keys

An Admin key (`sk-ant-admin…`) can manage an organization: members, workspaces, API keys. It
is far more powerful than the reports need, and Anthropic offers no narrower key for Console
organizations. The docs and the `add` prompt say this plainly.

- `qswap api org add [NAME]`: reads the key from a hidden prompt or stdin, never from an
  argument. Checks it with one read-only call, then stores it the way saved logins are stored
  on that platform (macOS Keychain, otherwise a 0600 file).
- `qswap api org list | remove NAME`.
- Without a stored key, `ANTHROPIC_ADMIN_KEY` in the environment is used for that one command
  and never written anywhere.
- qswap only ever sends GET requests with an Admin key.

## Linking an account to its key id

Usage reports name keys by id, not by secret. For each registered organization, list its API
keys once (and when an unknown account appears) and match each managed API-key account by the
key's partial hint. Store the matched `api_key_id` and organization name on the account
record. No match means the key belongs to another organization: fall back to the ledger.

## Spend on an account row

For an API-key account, the reading becomes a dict with the existing `spend` key instead of
the bare "API key (no quota)" sentinel:

- `spend.used`: month-to-date dollars. `spend.limit`: the budget, when one is set.
  `spend.pct`: used over limit, when there is a limit. `spend.currency`: `USD`.
- `today`: dollars so far today.
- `source`: `admin-api` or `ledger`.

Trace every consumer of the sentinel and of `spend` before changing the reading (list rows,
JSON, TUI cards, menu bar, engine, poll policy), and keep these properties:

- an API-key account is still never ranked by quota windows and never polled on the
  subscription usage endpoint;
- `spend` still never triggers a switch by itself; the budget rule below does;
- JSON stays additive: `usageStatus` for API-key accounts keeps its current value.

Month-to-date cost for a key is computed from the usage report's tokens with the bundled price
table, because Anthropic's cost report cannot be split per key. The organization total in
`qswap usage --billed` comes from the cost report itself, and the two are not expected to
match to the cent.

Months and days are UTC, as in Anthropic's reports.

## Budgets

`qswap budget ACCOUNT AMOUNT` sets a monthly budget in dollars; `qswap budget ACCOUNT off`
removes it; `qswap budget` lists them. Stored on the account record like `threshold`.

- At or over budget, an API-key account is not a candidate for auto-switch.
- If the active account is an API-key account at or over budget, the engine treats it as at
  its limit and looks for another account.
- At 80% the engine emits one warning event per month.
- With no budget, behaviour is unchanged.

A budget is a brake inside qswap, not a hard cap at Anthropic. The docs say so and point to
workspace spend limits in the Console for a real cap.

## `qswap usage --billed`

Adds a section from the Admin API for each registered organization, for the same window and
`--by` grouping where the report supports it (day, model, key, workspace): tokens by type and
billed cost. It is the only part of `qswap usage` that uses the network, and only when asked.

## Fetching

- One module owns the Admin API calls: pagination, a cache under `cache/` with a short
  lifetime (reports lag by a few minutes; do not poll faster than every five), and backoff on
  429 and 5xx.
- Failures never break `list` or the engine: the row falls back to the ledger, with a short
  note.
- All HTTP is faked in tests.

## Not in this milestone

OpenAI organization usage, spend limits set through the Admin API, alerts outside the tool,
and per-project budgets.
