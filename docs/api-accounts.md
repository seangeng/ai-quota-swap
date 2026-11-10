# API-key accounts: spend and budgets

An API-key account (one you added with `qswap add-token sk-ant-api03-...`) has no 5-hour or weekly quota, so qswap cannot show a percentage for it. It shows what the account has spent instead, today and this month, and it can stop auto-switch from using the account once a monthly budget is reached.

## See what an API key has spent

`qswap list`, the dashboard and the menu bar show it on the account's row:

```
  3: api-key-3@token.local [personal]
     API key · $4.12 today · $212.40 this month of $500 (42%)
```

Without a budget the row ends at `this month`. A row that ends in `· estimate` was worked out from this machine's transcripts (see [Two sources](#two-sources)), and a line under it says why when Anthropic's figure could not be used.

`qswap budget` lists every API-key account's spend and budget, and `qswap list --json` adds an `apiSpend` object to the row ([json-output.md](json-output.md#optional-row-fields)).

Months and days are UTC, as in Anthropic's reports, so the month resets at midnight UTC on the first.

## Set a budget

```bash
qswap budget 3 500        # $500 a month for account 3 (number, email or alias)
qswap budget 3 49.50
qswap budget 3 off        # remove it
qswap budget              # list budgets and spend
```

With a budget set:

- At 80% of the budget, auto-switch (`qswap auto`, the menu bar, the dashboard's auto view) reports a `budget-warning` once that month. The menu bar shows it as a notification.
- At or over the budget, auto-switch does not switch to the account.
- If the account in use reaches its budget, auto-switch treats it as at its limit and switches to an account that has room, a subscription first. API-key accounts under their budget are used only as a last resort, and only with `--include-api-key-accounts`, as before.

An account with no budget behaves exactly as before. So does an account whose spend is not known yet: there is nothing to hold the budget against.

### What a budget is not

A budget is a brake inside qswap, not a cap at Anthropic. It only changes what qswap's auto-switch does. Claude Code keeps working on the account if you switch to it by hand, and anything else that uses the same key is not affected at all. For a hard cap, set a spend limit on the key's workspace in the Claude Console.

Anthropic's figures can also arrive a few minutes late, and the local estimate only updates when `qswap usage` runs, so an account can go a little past its budget before qswap notices.

## Register an organization

The most accurate figure comes from Anthropic's own usage report for the key, which counts every request made with it, from any machine or tool. Reading it needs an Admin API key for the organization the key belongs to:

```bash
qswap api org add          # paste the key at the hidden prompt
qswap api org add acme < admin-key.txt
qswap api org list
qswap api org remove acme
```

`add` reads the key from a hidden prompt, or from stdin when you pipe it in; it never takes the key as an argument, so it does not end up in your shell history. It checks the key with one read of the organization's details, stores it, then finds which of your API-key accounts belong to the organization. The name defaults to the organization's name.

Where the key is kept: in the macOS Keychain (item `ai-quota-swap`, account `admin-org-<name>`); on Linux and Windows, or when the Keychain is locked, in a file only your user can read (`credentials/.admin-org-<name>.enc` in the data directory), the same way qswap keeps saved logins. `remove` deletes it, and so does `qswap purge`.

To try it without storing anything, set `ANTHROPIC_ADMIN_KEY` in the environment of one command. qswap uses it only while no organization is registered, and never writes it anywhere.

### What an Admin key can do

An Admin key is far more powerful than reading reports. It can manage the whole organization: invite and remove members, change workspaces, and rename or archive API keys. Anthropic offers no read-only key for usage reports, so this is the key you need. qswap only ever sends GET requests with it, to read the list of API keys and the usage and cost reports, and never logs it or puts it in a URL or an error message. Still:

- Create a dedicated key for qswap with an expiry date (Console, Settings, Admin keys).
- Remove it with `qswap api org remove` when you stop using this.
- Admin keys exist only for organizations; an individual Console account has none.

### How accounts are matched to keys

Anthropic's reports name keys by id, not by the secret. qswap lists the organization's keys, each with a hint such as `sk-ant-api03-R2D...igAA` (the start and end of the key), and links an account when exactly one listed key matches its key. If two keys share a hint, or two of your accounts match the same key, nothing is linked and the row says so. An account whose key is in no registered organization is estimated locally. The link is stored on the account and re-checked against its key every time, so replacing the key relinks it.

## Two sources

| Source | Shown as | What it knows |
|---|---|---|
| Anthropic's usage report | no tag | Every request made with the key, anywhere, a few minutes behind. qswap reads it at most once every five minutes. |
| This machine's transcripts | `estimate` | Only what Claude Code sent from this machine while this account was the live login, up to the last `qswap usage` run. |

Both figures are tokens priced with the table behind [`qswap usage`](usage.md#the-cost-is-an-estimate), at list prices, so neither is your invoice to the cent:

- Discounts, credits, the Batch API and long-context surcharges are not applied (long prompts use the base tier). Fast mode is.
- Web search and code execution are not included; they are in `qswap usage --billed`.
- A model the price table does not know is left out, and the amount is marked `+`.

If Anthropic's report cannot be read (the key was rejected, the Admin API is rate limiting or unreachable), the row shows the last figure it had for up to an hour, then falls back to the local estimate, with the reason underneath.

The local estimate refreshes whenever `qswap usage` runs. Run it now and then (or from cron) if you rely on a budget without a registered organization; the row says how old an estimate is once it is more than an hour old.

## What the organization was billed

`qswap usage --billed` adds what Anthropic reports for each registered organization, for the same dates as the rest of `qswap usage`, by day, model, key or workspace. Its total is Anthropic's cost report. See [usage.md](usage.md#what-anthropic-billed---billed).
