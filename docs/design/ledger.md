# Design: token and cost ledger

Status: accepted, 2026-11-10.

## Goal

`qswap usage` answers: how many tokens did I use, on which account, model, and project, and
what would that have cost at API prices. It reads what the CLIs already write to disk. It makes
no network calls and stores no message content.

## Sources

| Provider | Files | Token fields |
|---|---|---|
| Claude Code | `<config home>/projects/**/*.jsonl`, plus each `run` session profile's `projects/` when it is not a link into the shared tree | per assistant message: `message.usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and the 5-minute and 1-hour split under `cache_creation` when present), `message.model`, `timestamp`, `cwd`, `sessionId` |
| Codex | `$CODEX_HOME/sessions/**/rollout-*.jsonl` and `archived_sessions/` | `token_count` events: `info.last_token_usage` and the cumulative `info.total_token_usage` (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`) |

Roots come from `paths.py` helpers, never from hard-coded home paths, so tests are isolated.

## Counting each token once

- Claude Code writes the same message more than once (streaming updates, and resumed sessions
  replay earlier messages into a new file). The dedupe key is `message.id` plus `requestId`.
  When the same key appears with different token counts, the last one wins only inside one
  scan batch; across scans the first stored one wins. Verify on real files which of the
  duplicates carries the final counts and encode that rule.
- Codex totals are cumulative per session. Use per-turn `last_token_usage` when present,
  otherwise the difference between consecutive totals. A total that goes down means a new
  context; treat it as a fresh start, never as negative usage.
- Lines without a usage block are skipped with a substring check before any JSON parsing.

## Storage

SQLite at `<data root>/ledger.db`.

- `files(path PRIMARY KEY, provider, inode, size, mtime_ns, offset, state)`: scan cursor. A file
  that shrank or changed inode is read again from the start; dedupe makes that safe.
- `seen(key INTEGER PRIMARY KEY) WITHOUT ROWID`: 64-bit hash of each dedupe key.
- `buckets(provider, hour, account, model, project, session, input, output, cache_read,
  cache_write_5m, cache_write_1h, reasoning, requests)`, primary key on the first six columns.
  `hour` is the UTC epoch hour. Rows are added to, never rewritten.
- `meta(key, value)`: schema version.

Costs are not stored. They are computed when a report is printed, so a corrected price table
applies to old usage too.

The first scan of a large history must stream, commit in batches, show progress on a TTY, and
be safe to interrupt and resume.

## Attribution to accounts

Transcripts do not say which account produced them.

- Each provider root gets an append-only `activations.jsonl`: `{ts, number, email, org,
  source}`. The switcher appends on every switch (`source: "switch"`), and whenever it notices
  that the live login differs from the last line (`source: "observed"`).
- An event belongs to the account whose activation interval contains its timestamp. Usage from
  before the first line is reported as unattributed. There is no backfill.
- Usage written from a `run` session profile belongs to that profile's account. Investigate
  how to tell this when history is shared into the default tree, and say in the report if it
  cannot be told.

## Prices

`pricing.py` holds a table: model id prefix to dollars per million tokens for input, output,
cache read, 5-minute cache write, and 1-hour cache write. The longest matching prefix wins.

- Build the table from the vendors' published pricing pages, fetched when the table is
  written. Record the source URL and date in the file. Do not write prices from memory.
- A user file `<data root>/pricing.json` overrides or adds entries.
- A model with no price shows tokens and `n/a` for cost, and is named in a footer.
- Tiered prices (long-context surcharges) use the base tier. Say so in the docs.
- Every dollar figure is labelled an estimate at API prices. Subscription usage is not billed
  this way.

## Command

```
qswap usage [--days N | --since DATE [--until DATE]]
            [--by day|model|account|project|provider|session]
            [--provider claude|codex] [--json] [--no-scan] [--rescan]
```

Default: the last 7 days by day, one row per day with input, output, cache read, cache write,
and estimated cost, then a total. `--json` returns the same data with `schemaVersion: 1`.
`--no-scan` reports from the database as it is. `--rescan` rebuilds it.

## Not in the first version

Quota history and burn-rate forecasts, budgets, and usage for API keys read from vendor admin
APIs. They build on this ledger.
