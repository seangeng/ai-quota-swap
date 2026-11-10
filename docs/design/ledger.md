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
| Codex | `$CODEX_HOME/sessions/**/rollout-*.jsonl[.zst]` and `archived_sessions/`, following per-file symlinks and skipping missing targets | `token_usage_record` lines, one per completed response: `payload.thread_id`, `response_id`, `usage{input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}`. Older files without them: cumulative `token_count` events (`info.total_token_usage`, `info.last_token_usage`). Model from the latest `turn_context.payload.model`, project from the first `session_meta.payload.cwd`, a UTC `timestamp` on every line |

Roots come from `paths.py` helpers, never from hard-coded home paths, so tests are isolated.

## Counting each token once

- Claude Code writes one `assistant` line per content block of a response, all carrying the
  response's `message.usage`, and a resumed session replays earlier responses into its new
  file. The dedupe key is `message.id` plus `requestId` (`message.id` alone when `requestId` is
  missing). Input and cache counts are equal across the copies; `output_tokens` grows to its
  final value on the last line (verified on real files: of 219,831 multi-line responses the
  highest output count was on the last line in all but one). So the highest output count wins,
  inside a batch and across scans: `seen` stores the output counted for each key, and a later,
  higher count adds only the difference.
- Sub-agent transcripts (`isSidechain`, `subagents/`) are separate requests with their own ids
  and are counted. `<synthetic>` API-error placeholders carry no usage and are skipped.
- Codex: `token_usage_record` lines are the source, keyed by `thread_id` plus `response_id`.
  Forks never copy them, and no `response_id` repeats across files. `token_count` events are a
  fallback for threads without records, and only until a thread's first record (Codex writes a
  response's record before its `token_count`; the CLI version is no guide, since real threads
  from 0.153 to 0.162 exist both with and without records). They are not safe to sum: forks
  replay the parent's cumulative totals with new timestamps, and `token_count` misses some
  responses. In the fallback, a file counts
  its own deltas only (the inherited prefix, after a second `session_meta` or before
  `subagent_history_start_ordinal`, is skipped), keyed by the session and the consecutive
  totals so a replay elsewhere lands on the same key. A total that goes down means a new
  context: the response's own `last_token_usage` is counted, never a negative difference.
- A Codex thread changes path and format over its life (`.jsonl` compressed in place to
  `.jsonl.zst` after 7 idle days; moved to `archived_sessions/`). Its scan cursor is keyed by
  thread id, not path, and resumes in the new file at the old offset when the bytes before it
  end a line; otherwise the file is read again and dedupe absorbs it.
- Lines without usage are skipped with a substring check before any JSON parsing.

## Storage

SQLite at `<data root>/ledger.db`.

- `files(path PRIMARY KEY, provider, inode, size, mtime_ns, offset, state)`: scan cursor. For
  Codex the key is `codex-thread:<thread id>` and `state` holds the file's path and the reader's
  context (model, project, fallback totals). A file that shrank or changed inode is read again
  from the start; dedupe makes that safe. A cursor written mid-file carries no size, so an
  interrupted scan resumes instead of mistaking the file for read.
- `seen(key INTEGER PRIMARY KEY, out INTEGER) WITHOUT ROWID`: 64-bit hash of each dedupe key and
  the output count counted for it.
- `buckets(provider, hour, account, model, project, session, input, output, cache_read,
  cache_write_5m, cache_write_1h, reasoning, requests)`, primary key on the first six columns.
  `hour` is the UTC epoch hour. Rows are added to, never rewritten. For Codex, cache writes go
  in `cache_write_5m` and are priced at OpenAI's 30-minute write price.
- `sessions(provider, session, owner, first_ts)`: the account Claude Code stamped as the
  session's starter (`session-owner` line) and the session's first counted event.
- `meta(key, value)`: schema version. A ledger with another schema version is rebuilt.

Costs are not stored. They are computed when a report is printed, so a corrected price table
applies to old usage too.

The first scan of a large history must stream, commit in batches, show progress on a TTY, and
be safe to interrupt and resume.

## Attribution to accounts

Transcripts do not say which account served a request.

- Each provider root gets an append-only `activations.jsonl`: `{ts, number, email, org,
  source}`. The switcher appends on every switch (`source: "switch"`), and whenever it notices
  that the live login differs from the last line (`source: "observed"`), at the point where
  `list`, the dashboard, the menu bar and the engine resolve the live login, and before a
  `qswap usage` scan.
- An event belongs to the account whose activation interval contains its timestamp. Usage from
  before the first line is reported as unattributed. There is no backfill.
- `qswap run` profiles: a profile with its own `projects/` directory is attributed by path.
  With history shared into the default tree no line names the profile, but Claude Code
  (2.1.281 and later) writes `session-owner` lines with the `accountUuid` and
  `organizationUuid` of the account that started the session, set once and never updated. At
  report time, a session whose owner differs from the account the log gives for its first
  event was started from a profile (whose login never changes), and all of it goes to the
  owner. A default-tree session keeps time-based attribution, so a switch mid-session moves
  its later usage to the new account. Sessions starting before the first activation line stay
  unattributed.
- Sessions with entrypoint `claude-desktop` (and `claude-desktop-3p`, `local-agent`) run on a
  token the desktop app supplies, not the CLI login qswap switches; they are reported under a
  separate "desktop app" account.
- Codex has no per-turn account field; its usage is attributed from
  `<data root>/codex/activations.jsonl`, which the Codex switcher will write, and is
  unattributed until then.

## Prices

`pricing.py` holds a table: model id prefix to dollars per million tokens for input, output,
cache read, 5-minute cache write, and 1-hour cache write. The longest matching prefix wins. A
prefix matches the exact id or the id plus a dated snapshot suffix, never a different version
or variant (`claude-opus-4` does not price `claude-opus-4-9`), so an unknown model shows `n/a`
instead of a wrong price. User entries ending in `*` match any suffix.

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
