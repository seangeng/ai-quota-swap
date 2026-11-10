# Design: output and interaction rules

Status: accepted, 2026-11-10. These rules apply to the CLI, the dashboard, the menu bar, and
the docs. When a rule and existing output disagree, the output is the bug.

## One word per thing

| Thing | Say | Do not say |
|---|---|---|
| An account, in a message or log line | `#2 work`, or `#2 maya@example.com` when it has no alias. Add the email in parentheses on first mention where there is room: `#2 work (maya@example.com)` | `Account-2`, `Account 2`, `account 2`, `slot 2` |
| An account's number as a place | `slot 3`, only for `move`, `swap` and `--slot` | |
| Both providers in one view | `Claude #2 work`, `Codex #2 work`, or a Provider column | two rows with the same label |
| The account in use | `active` | `current`, `live` |
| The quota windows | `5h`, `7d`, and the model's name (`Fable`) in tables and bars; "5-hour window", "weekly window" in sentences | `session`, `weekly` as labels |
| When a window refills | `resets 16:55 · in 2h 35m` | |
| A usage reset someone holds | "1 Fable reset", "2 weekly resets"; Codex: "1 reset credit" | `grant`, a `resets:` row that looks like a window |
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
  cannot fit truncates its widest text column with an ellipsis.
- Uses `→` for "to", `·` between facts, and whole percentages. A comparison shows both sides
  at the same precision.
- Uses colour only for meaning, the same meaning everywhere: accent for the active account and
  for actions taken, yellow from the warning line, red from the limit, dim for stale or
  secondary text. Nothing depends on colour alone.

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
  needs re-login is refused unless `--force`; switching to one whose usage is unreadable or
  stale goes ahead with a one-line warning.
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
