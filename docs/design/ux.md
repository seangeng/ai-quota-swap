# Design: output and interaction rules

Status: accepted, 2026-11-10; revised the same day after the command line was brought into
line with it (paths and commands, colour, provider prefixes, `--anyway`). These rules apply to
the CLI, the dashboard, the menu bar, and the docs. When a rule and existing output disagree,
the output is the bug.

## One word per thing

| Thing | Say | Do not say |
|---|---|---|
| An account, in a message or log line | `#2 work`, or `#2 maya@example.com` when it has no alias. Add the email in parentheses on first mention where there is room: `#2 work (maya@example.com)` | `Account-2`, `Account 2`, `account 2`, `slot 2` |
| An account's number as a place | `slot 3`, only for `move`, `swap` and `--slot` | |
| Both providers in one view | A view in sections relies on the provider heading ("Claude Code accounts", "Codex accounts") and labels its rows `#2 work`. A mixed table, and a message that could be about either provider, says `Claude #2 work` / `Codex #2 work`, or has a Provider column | two rows with the same label in one table |
| The account in use | `active` | `current`, `live` |
| The quota windows | `5h`, `7d`, and the model's name (`Fable`) in tables and bars; "5-hour window", "weekly window" in sentences | `session`, `weekly` as labels |
| When a window refills | `resets 16:55 · in 2h 35m` | |
| A usage reset someone holds | "1 Fable reset", "2 weekly resets", "1 five-hour reset" (a count of the 5-hour kind is spelled out, never "1 5-hour reset"); Codex: "1 reset credit" | `grant`, a `resets:` row that looks like a window |
| Extra-usage spend on a subscription | `extra` | `$$` |
| An account the tool will not use until its owner logs in again | `needs re-login` | `quarantined`, `refresh token dead`, `invalid_grant` |
| A reading that could not be refreshed | `usage unavailable (HTTP 503), retrying`; a stale one: `as of 3h ago` | `usage unknown`, `?` |
| Nothing managed yet | `No accounts are managed yet.` followed by the next step | three other phrasings |
| The folder holding qswap's data | `data directory` | `backup root` |
| Why the engine switched | "5h reached 91%", "limit reached", "usage unreadable" | `proactive`, `at-limit`, `failover` |

Reason codes such as `below-threshold` stay in JSON. Text output says the same thing in words.
Setting values such as `last-resort` and `consume-first` stay as they are and are explained
wherever they are listed.

## Every message

- Says what happened and, if the user has to act, the exact command to run next.
- Names commands by their verb spelling (`qswap add`, `qswap switch 1 --force`), never by a
  legacy flag.
- Fits 80 columns. Longer text is wrapped by the tool, not by the terminal. A table that
  cannot fit truncates its widest text column with an ellipsis. Two things are never broken:
  a single path, and a command meant to be copied (with its arguments). When either does not
  fit, it goes on a line of its own, even if that line is longer than 80 columns.
- Uses `→` for "to", `·` between facts, and whole percentages. A comparison shows both sides
  at the same precision.
- Uses colour only for meaning, the same meaning everywhere: a usage figure is yellow at or
  past the account's own switch line and red at its limit (which is also said in words:
  "limit reached"); the active marker is bold; accent is for actions taken (Switched, Added,
  Removed) and aliases; dim is for stale or secondary text. Nothing depends on colour alone.

## Commands

- Every verb answers `--help` with its own usage, a one or two line description, its options,
  and at least one example. `qswap help VERB` prints the same.
- `qswap --help` is a map: verbs grouped by task with one line each, global options only, and
  it fits a screen.
- A read-only command never changes anything and never asks a question.
- A prompt is only shown on a terminal. Without one, the command stops with one line naming
  the flag that confirms it (`--yes`). No command ever ends in a traceback.
- Cancelling changes nothing.
- A command that would leave the user worse off says so first: switching to an account that
  needs re-login is refused unless `--anyway`; switching to one whose usage is unreadable or
  stale goes ahead with a one-line warning; switching to an API-key account when qswap
  cannot add its key line to Claude Code's settings is refused with the fix, and goes ahead
  only with `--log-out-sessions`, since then a session started on a subscription would stop
  with "Not logged in"; where the saved key could not help either (another `apiKeyHelper`,
  a credential in the settings' `env` block), that flag is not offered and is refused
  ([api-key-sessions.md](api-key-sessions.md)). A rotation (`switch` with no account, its
  strategies, `qswap codex switch`) skips an account that needs re-login, as it skips a
  disabled one, with one line each ("Skipped #2 work: needs re-login."), and when nothing is
  left it stays put and says the next step. It never lands on an API-key account, which is
  used by naming it.
- One flag, one meaning. `--anyway` only overrides the needs-re-login refusal, and
  `--log-out-sessions` only the API-key one. `--force` keeps
  its own meaning per provider (Claude Code: write the saved login without saving the current
  one first; Codex: replace a live login qswap does not manage) and never overrides a refusal
  by itself. A message that tells the user to override something names the flag that does
  exactly that and nothing more.
- Exit codes: 0 success; 1 failure; 64 for a usage error (unknown verb, bad flag, missing
  argument). `auto --once` keeps its documented codes (0 switched, 1 error, 2 nothing to do,
  3 blocked), which is why usage errors are not 2.
- Every `--json` payload has `schemaVersion`, and every failure under `--json` is the error
  envelope. JSON only gains fields.

## First run and dead ends

Every state a newcomer can be in has a next step on screen: no CLI installed, not logged in,
logged in but not managed, managed with one account, a provider with no accounts. An empty
table is replaced by a sentence saying why it is empty.

## The same state looks the same everywhere

`list`, `status`, the dashboard, the menu bar, and the auto log use the same words, the same
severity, and the same age format for one state. If the engine would skip an account, every
view shows why.

## For contributors

The repository never ships a configuration that runs the tool against a real home by
accident. The documented way to run it during development is the demo harness.
