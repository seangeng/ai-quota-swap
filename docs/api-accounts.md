# API-key accounts: spend and budgets

An API-key account has no 5-hour or weekly window, so qswap shows what it spent instead, today and this month. After this page you can add a Console API key, move running Claude Code sessions onto it and back, see its spend, give it a monthly budget or a prepaid credit that auto-switch respects, and read Anthropic's own figures with an organization's Admin key.

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

A switch to an API-key account moves every Claude Code session onto the key within a few seconds, running ones included: [Switch to an API key](#switch-to-an-api-key). Auto-switch treats an API-key account differently, as [Set a budget](#set-a-budget) explains.

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

## Switch to an API key

```text
$ qswap switch 5
Switched #1 personal → #5 ci (API key)
Running Claude Code sessions move to the key within a few seconds. #1 personal
stays logged in underneath. claude.ai connectors are unavailable while a key is
in use.
```

The switch adds one line to Claude Code's user settings, `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`):

```json
"apiKeyHelper": "/Users/you/.local/bin/qswap key-helper 5"
```

The path to `qswap` is quoted when it has spaces or shell characters in it. On Linux with `XDG_DATA_HOME` set, the line also names qswap's data directory (`--data-dir`), because Claude Code started from a desktop launcher or an editor may not have that variable.

Claude Code watches that file. Within a few seconds every session that reads it, running or new, sends its requests with the key that `qswap key-helper 5` prints, instead of the claude.ai login. Nothing else changes: not the login, not the saved key, not `~/.claude.json`. Running sessions keep their conversation. Two things do change for them: Claude Code turns its claude.ai connectors (the MCP servers that come with the claude.ai login) off while a key is in use, and the first request on the key rebuilds the prompt cache, which is paid at the key's price.

### The account underneath

The subscription that was active stays logged in. `qswap list` and `qswap status` show both:

```text
  #1 personal (maya@example.com) · underneath · Max 20x
     ├ 5h     46%  resets 16:55 · in 2h 35m
     └ 7d     38%  resets Nov 13 20:20 · in 3d 6h
  #5 ci · active · API key · over #1 personal
     $2.06 today · $25.16 this month · estimate
```

The key is the active account; the subscription is *underneath* it and is not active. Its windows still read as usual. Under `--json`, the key's row has `"active": true` and `"how": "helper"`, and the subscription's row has `"underneath": true` ([json-output.md](json-output.md)).

### Leaving the key

```text
$ qswap switch 1
Switched #5 ci → #1 personal (maya@example.com)
Running Claude Code sessions are back on #1 personal within a few seconds.
```

Switching to the account underneath removes the line, and that is all. Switching to another subscription removes the line and then switches the login as usual; sessions are on it within about 30 seconds. Switching from one key to another replaces the line, and sessions move to the new key within a few seconds. `qswap switch` with no account, and its strategies, never land on a key; on a key they go back to the account underneath (`--strategy best` to a subscription with more room, if there is one). An API-key account is entered by naming it, or by auto-switch ([Set a budget](#set-a-budget)).

If qswap created `~/.claude/settings.json` for the line, it removes the file again when the key is left and nothing else was added to it. Otherwise it changes that one key and keeps everything else as it was: the other keys and their order, the indentation, the file mode, a symbolic link to your dotfiles. It never writes a file it cannot parse.

### Removing the line by hand

If qswap is gone, or you want the subscription back without it, delete the `apiKeyHelper` line from `~/.claude/settings.json`. Sessions are back on the subscription within a few seconds. Then run `qswap doctor`: it puts nothing back by itself, but it names what is left over (below).

### Sessions started with qswap run

A `qswap run` session has its own profile, which shares `~/.claude/settings.json` through a link. While the line is in, each such profile gets a copy of the settings without it instead, so those sessions stay on their own account; when the line is removed the link comes back. Settings changed inside a run session in the meantime stay in its copy. A profile created while a key is active gets the copy too. If a profile cannot be given its copy (it cannot be written, or the disk is full), the switch is refused and nothing changes, since that profile would follow the line onto the key.

`qswap run 5` runs one session on the key, and nothing else moves: [sessions.md](sessions.md#run-on-an-api-key).

### When the line cannot be added

qswap adds nothing when the settings file already has an `apiKeyHelper` of someone else's, or cannot be parsed or written, and says why:

```text
$ qswap switch 5
Error: not switched. qswap cannot add the key helper for #5 ci to the Claude
Code settings: ~/.claude/settings.json already sets another apiKeyHelper.
Without it, the key can only be saved for Claude Code, which a running session
does not pick up. 6 Claude Code sessions are running on the shared login:
about 30 seconds after the switch each one stops with "Not logged in" until
you switch back or restart it.
Sessions started after the switch use the key.
To switch anyway:
  qswap switch 5 --log-out-sessions
```

`--log-out-sessions` takes the older way: the key is saved where Claude Code reads it once, at start (the macOS Keychain item `Claude Code`, or `primaryApiKey` in `~/.claude.json` elsewhere), and the login is taken out of Claude Code's login item. A running session cannot pick a saved key up, so each one started on a subscription answers `Not logged in · Please run /login` about 30 seconds later, until you switch back or restart it. The login item itself stays, with your MCP server sign-ins in it.

The count in the refusal is the "Running instances" `qswap list` shows. Sessions inside a `qswap run` profile have their own login and are not counted, and neither is a process that has exited. When qswap cannot read Claude Code's session records (a record half written, or in a shape it does not know), it says it cannot tell whether sessions are running. Claude Code names each record after its process id, so a record qswap cannot read is ignored when that process is no longer running. Otherwise the refusal names the file and what to do:

```text
This session record could not be read:
  /Users/maya/.claude/sessions/4101.json
If Claude Code process 4101 is gone, delete the file.
```

`qswap doctor` lists such records too. Under `--json` the refusal is the error envelope (`KeyHelperUnavailableError`), with the reason in `error.keyHelper`, the count in `error.sessions`, and the records it could not read counted in `error.unreadable` and listed in `error.unreadableRecords`.

The menu bar and the dashboard ask nothing when the line can be added. When it cannot, they ask *Use the saved key instead?*, with Cancel as the default and *Switch and log out sessions* to go ahead.

Leaving a saved key is safe: qswap never rewrites an API-key account's saved key when it leaves it, and the account you switch to gets its own login together with the MCP sign-ins of the moment. Sessions started on the key move back to the subscription within about 30 seconds, and sessions that stopped with "Not logged in" pick the login up again and continue their conversation. From one saved key to another, a session started on the first keeps it until you restart it, and the switch lists every such session under the key it keeps, by process id and directory. Saving a changed key the same way for the account already active (after an import) lists the sessions that keep the previous key too.

### What qswap doctor checks

- qswap's own line for an API-key account is reported as the active key, not as a warning.
- A line for an account that no longer exists, or is not an API-key account, fails: every session on it would get no key ("apiKeyHelper script is failing"). The fix is `qswap switch N` to the account underneath, which removes the line.
- A line that runs a qswap that is no longer there (a reinstall moved it) fails, and `qswap switch 5` points the line at this qswap again.
- Run profiles left with a settings copy after the line is gone (qswap stopped between the two steps) fail, and `qswap switch N` restores the links.
- Another `apiKeyHelper`, and `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` exported in your shell, warn: each makes Claude Code ignore the active account. `qswap switch` warns about those variables too.

`qswap list`, `qswap status`, the dashboard and the menu bar say the same about a line for no key, with the same command.

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

- It never ranks one against subscriptions. It moves to one only as a last resort, when it must move and no subscription has room, and only with `--include-api-key-accounts` (`autoswitch.includeApiKeyAccounts`). Running sessions follow the move, so they do not hold it. Where the line cannot be added, auto-switch does not take the `--log-out-sessions` way by itself: it stays and says so once.
- A key auto-switch moved to is left as soon as a subscription has room again (`Switched #5 ci → #1 personal · a subscription has room again`).
- A key you switched to yourself is never left because a subscription has room: only at its budget, when its credit is spent or expired, or when you switch (`on the API key you chose`).
- An active API-key account that is not on the key helper (a `--log-out-sessions` switch) under its budget, or without one, is left alone (`#5 ci is an API key · no quota to watch`); with `--include-api-key-accounts` auto-switch counts its missing usage as unreadable and fails over after `autoswitch.unhealthyTicks` checks (3 by default).
- At 80% of the budget it logs one warning per account per month, which the menu bar also shows as a notification:

  ```text
  14:20:00  #5 ci has spent $25.16 of its $30.00 monthly budget (84%) (estimate)
  ```

- At or over the budget the account is never a target. If it is the active account, auto-switch treats it as at its limit and moves to the subscription with the most room, without waiting for the cooldown, and when none has room, back to the account underneath (`· monthly budget reached; no subscription has room, so back to the account underneath`).
- With no budget, or no figure to hold it against, nothing changes.

### What a budget is not

A budget is a brake inside qswap, not a cap at Anthropic. It changes only what auto-switch does. Claude Code keeps working on the account if you switch to it by hand, and anything else that uses the same key is not affected. For a hard cap, set a spend limit on the key's workspace in the Claude Console.

Figures also arrive late: Anthropic's by about five minutes, the local estimate only when `qswap usage` runs. An account can go past its budget before qswap notices.

## Record a prepaid credit

A Console organization can hold a prepaid balance. No API key can read it, but qswap can count it down for you:

```text
$ qswap credit 5 1187 --expires 2026-12-31
Recorded $1,187 of credit for #5 ci as of now, usable until 2026-12-31.
  A credit is the count qswap keeps of what you recorded, less the spend it
  measures from then on. It is not the balance at Anthropic, which no key can
  read.
```

From then on the account's row counts down: `API key · $1,150 of $1,187 credit left`. The spend is Anthropic's figure when the account is linked to a registered organization, the local estimate otherwise (`· estimate`). Anthropic's figures come in whole UTC days, so the day you record the credit counts in full; the estimate counts from the hour.

- `qswap credit 5 off` removes it, and `qswap credit` lists every credit (`--json` too). Recording a credit again starts the count again from now.
- `--expires DATE` is the last UTC day the credit can be used. After it the row reads `credit expired Dec 31`.
- At zero (`credit spent ($0 of $1,187 left)`), or after the expiry date, auto-switch treats the account as at its budget: it leaves it, and will not move to it.
- A monthly budget and a credit can both be set; whichever is reached first applies. The row then shows both: `API key · $400 of $1,000 credit left · $600.00 of $500 this month (120%, over budget)`.
- Under `--json`, `apiSpend.credit` carries `amount`, `used`, `left`, `since`, `expires`, `expired`, `spent` and `source`, and the account row a `credit` field with what you recorded ([json-output.md](json-output.md)).

Like a budget, a credit is a brake inside qswap, not a cap at Anthropic.

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

- The live switch has been checked on one Claude Code process at a time, on a watched settings file; the first switch on your own `~/.claude/settings.json` with interactive sessions is the real test. Claude Code 2.1.322 or later.

- A budget is enforced by qswap's auto-switch only, and only once a spend figure exists.
- Anthropic's figure needs an Admin key, which can do far more than read reports.
- The local estimate sees only this machine's Claude Code usage while the account was active.
- The menu bar's new build, which shows the budget notification, has not been run by hand yet ([menubar.md](menubar.md)).
- Budgets and the Admin API apply to Claude Code API-key accounts. Codex accounts are covered in [codex.md](codex.md).
