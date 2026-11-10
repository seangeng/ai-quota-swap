# Tokens and cost: `qswap usage`

`qswap usage` shows how many tokens you used, on which account, model and project, and what they would have cost at API prices.

```bash
qswap usage                          # the last 7 days, one row per day
qswap usage --days 30 --by model
qswap usage --since 2026-11-01 --until 2026-11-07 --by account
qswap usage --provider codex --by project
qswap usage --json
```

## What it reads

- **Claude Code:** the transcripts under `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects/`), sub-agent transcripts included. A `qswap run` profile that keeps its own history (`--no-share-history`) is read too.
- **Codex:** the session files under `~/.codex/sessions/` and `~/.codex/archived_sessions/` (or under `$CODEX_HOME`), compressed ones (`.jsonl.zst`) included.

Nothing is sent anywhere. Only token counts, model names, project directories, session ids and timestamps are kept, in `ledger.db` in the qswap data directory. No message content is stored.

A run reads only the session files its dates need, newest first. A file last changed more than a day before the first day cannot hold usage from those days, so it waits until a wider `--days` or `--since`, or `--all`, asks for it, and the report ends with a line saying how many such files are not in the ledger yet (`scan.olderFilesNotInLedger` in `--json`). Progress shows in a terminal, Ctrl-C keeps what was read so far so the next run picks up from there, and later runs read only what was added since.

Compressed Codex files need Python 3.14 or newer, or the `zstandard` package. Without either, they are skipped and the report says how many.

## What the numbers mean

| Column | Meaning |
|---|---|
| Input | Input tokens that were not served from the prompt cache |
| Output | Output tokens, including thinking and reasoning tokens |
| Cache read | Input tokens served from the prompt cache |
| Cache write | Input tokens written to the prompt cache (5-minute and 1-hour writes together; `--json` keeps them apart) |
| Est. cost | What the row would have cost at the vendor's API list price |

Each response is counted once. Claude Code writes a response several times as it streams and copies earlier responses into a resumed session's file; Codex keeps one record per response. Rows are grouped by your local time zone.

## The cost is an estimate

The dollar figure is what the same tokens would cost on the pay-as-you-go API. A Claude or ChatGPT subscription is not billed this way, so treat it as a measure of value used, not a bill. Specifically:

- Prices come from a table in qswap, copied from the Anthropic and OpenAI pricing pages on the date `qswap usage` prints (and `--json` reports as `pricing.fetched`).
- Long-context and prompt-length tiers use the base price: Claude Haiku 5.5 prompts over 100,000 tokens and OpenAI requests over 272K input tokens cost more on the API than shown.
- Batch, data-residency (US-only inference) and OpenAI Fast pricing are not applied. Claude fast mode is.
- A model the table does not know shows its tokens with `n/a` for cost, and is named under the table. A cost marked `+` leaves those tokens out.

## Your own prices

Put a `pricing.json` next to `ledger.db` in the qswap data directory (`~/.ai-quota-swap/` on macOS and Windows, `~/.local/share/ai-quota-swap/` on Linux) to add models or correct prices, in US dollars per million tokens:

```json
{
  "models": {
    "gpt-5.3-codex-spark": {"input": 1.75, "output": 14, "cache_read": 0.175},
    "claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write_5m": 5, "cache_write_1h": 8},
    "codex-*": {"input": 2, "output": 10},
    "some-model": null
  }
}
```

`input` and `output` are required; a missing cache price defaults to the input price. An entry matches its exact model id and dated snapshots of it (`claude-opus-4` also prices `claude-opus-4-20250514`); end it in `*` to match every id that starts with it. `null` marks a model as unpriced. The file is read on every run, and a corrected price applies to old usage too.

## Flags

| Flag | Effect |
|---|---|
| `--days N` | The last N days, today included (default 7) |
| `--since DATE` | First day, `YYYY-MM-DD` |
| `--until DATE` | Last day, included (with `--since`; default today) |
| `--by day\|model\|account\|project\|provider\|session` | How rows are grouped (default `day`); `key` and `workspace` too, with `--billed` |
| `--provider claude\|codex` | Only one CLI's usage |
| `--json` | The same data as JSON with `schemaVersion: 1` |
| `--no-scan` | Report from the ledger without reading new session data |
| `--rescan` | Rebuild the ledger from the session files |
| `--all` | Read every session file into the ledger, not only those the dates need, without rebuilding it |
| `--billed` | Also show what Anthropic reports for each registered organization (below) |

## What Anthropic billed: `--billed`

```bash
qswap usage --billed                      # the last 7 days, by day
qswap usage --billed --by model --days 30
qswap usage --billed --by key             # per API key, labelled with the account it is linked to
qswap usage --billed --by workspace
```

`--billed` adds a section per organization registered with `qswap api org add` (or named by `ANTHROPIC_ADMIN_KEY` for that one command; see [api-accounts.md](api-accounts.md#register-an-organization)): tokens by type from Anthropic's usage report and cost from its cost report, in US dollars, for the same dates. It is the only part of `qswap usage` that uses the network, and only when you ask for it.

- **Days are UTC**, as in Anthropic's reports, while the rest of `qswap usage` uses your local days. The section's heading says so.
- **Groupings.** By day, model, key (`--by account` reads as key) or workspace. `--by key` and `--by workspace` need `--billed`. With project, provider or session the section is shown by day and says why.
- **Cost per key is estimated.** Anthropic's cost report cannot be split by API key, so with `--by key` each key's cost is its tokens at list prices, marked `Est. cost`; the total is the cost report's.
- **The cost report is the bill's view.** It includes web search and code execution (shown as their own rows with `--by model`) and excludes Priority Tier, which is billed separately. It will not match the estimated cost above it, which prices only this machine's tokens at list prices.
- **The last few minutes may be missing.** Anthropic's reports lag by about five minutes; answers are kept for five minutes so repeated runs do not ask again.

When there is nothing to show, the section says so in one line: no organization registered, the Admin key was rejected (with how to replace it), the Admin API is rate limiting, or no usage has been reported for the window yet. `--json` adds the section as `billed` ([json-output.md](json-output.md#qswap-usage---json)).

## How usage is attributed to accounts

Transcripts do not record which account served them, so qswap keeps its own record: `activations.jsonl` in the data directory gets a line each time qswap switches accounts, and each time it notices the live login changed some other way (a `/login`, another tool). It notices when `qswap list`, the dashboard, the menu bar, auto-switch or `qswap usage` runs. Each response belongs to the account that was live at its timestamp.

- **Before the first line, usage is unattributed.** qswap starts the record the first time it runs after this feature arrived; it does not guess at older history.
- **A login change made outside qswap** is attributed from the moment qswap notices it, not from when it happened.
- **`qswap run` sessions.** When a profile keeps its own history, its files belong to its account. When history is shared (the default), its transcripts land in the same folder as everyone else's and nothing in a line says which profile wrote it. Claude Code does stamp each session with the account that started it; a session that started on an account other than the default login is a `run` session (a profile's login never changes), so the whole session goes to that account. A session on the default login follows the record above, including through a switch in the middle of it. Sessions from Claude Code versions that predate the stamp are attributed by time.
- **The Claude desktop app** runs Claude Code on its own login, which qswap does not switch. Its sessions are listed as "Claude desktop app (its own login)".
- **Codex** is attributed from `codex/activations.jsonl` in the data directory, which Codex account switching will write; until then Codex usage is unattributed.

`qswap usage --by account` shows the unattributed share under the table, and `--json` reports it as `unattributedShare`.
