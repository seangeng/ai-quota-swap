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

A switch to an API-key account moves every Claude Code session onto the key within a few seconds, running ones included: [Switch to an API key](#switch-to-an-api-key). That has been tested on Claude Code 2.1.322 with sessions in print mode; [Limits](#limits) says what has not. Auto-switch treats an API-key account differently, as [Set a budget](#set-a-budget) explains.

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
stays logged in underneath. claude.ai connectors and Remote Control are
unavailable while a key is in use. Go back with: qswap switch 1
```

The switch adds one line to Claude Code's user settings, `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`), as the first member of the object:

```json
{
  "apiKeyHelper": "/Users/maya/.local/bin/qswap key-helper 5",
  "model": "opus"
}
```

The command is the `qswap` of this install, by absolute path, so a session with another `PATH` finds it; the path is quoted when it has spaces or shell characters in it. On Linux the line also names qswap's data directory (`--data-dir`), because Claude Code started from a desktop launcher or an editor may have another `XDG_DATA_HOME`, or none.

Claude Code watches that file. About a second after the write (measured on Claude Code 2.1.322), every session that reads it, running or new, sends its requests with the key that `qswap key-helper 5` prints, instead of the claude.ai login. Nothing else changes: not the login, not the saved key, not `~/.claude.json`. Running sessions keep their conversation. Some things do change for them:

- Claude Code does not load claude.ai connectors (the MCP servers that come with the claude.ai login) while a key is in use, and Remote Control does not work with an API key, so a session you drive from your phone loses that link (Anthropic's documentation). Neither has been tested here, nor whether a running session gets them back after you leave the key.
- The first request on the key cannot reuse the login's prompt cache, so it pays for the cache again, at the key's price.
- Claude Code keeps the key it read for 5 minutes, and reads it again sooner after any change to the settings or a rejected request (Claude Code 2.1.322; `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` changes the 5 minutes).

### Which sessions follow the line

Every Claude Code session that reads your user settings, except:

- a session where a higher settings layer sets its own `apiKeyHelper`. Claude Code reads managed settings first, then `--settings`, the project's `.claude/settings.local.json`, its `.claude/settings.json`, and your user settings last (Anthropic's documentation). qswap cannot change those; `qswap doctor` warns about the managed settings and the current project's.
- a session whose environment, or the `env` block of a settings file, sets `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or a cloud provider (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`): those come before the line. `CLAUDE_CODE_OAUTH_TOKEN` does not, so a session pinned to a setup-token that way follows the key too.
- `qswap run` sessions, which keep their own account ([below](#sessions-started-with-qswap-run)).
- the Claude desktop app and cloud sessions, which do not run `apiKeyHelper` (Anthropic's documentation).
- a running session whose `ConfigChange` hook blocks settings changes: Anthropic's documentation says such a hook keeps the change from taking effect, without a message.

On a machine whose managed settings pin the login (`forceLoginMethod`, `forceLoginOrgUUID`), Anthropic's documentation says Claude Code refuses `apiKeyHelper` at startup, so new sessions would not start while a key is active.

### The account underneath

The subscription that was active stays logged in. `qswap list` shows both:

```text
$ qswap list
Claude Code accounts
  #1 personal (maya@example.com) · underneath · Max 20x
     ├ 5h      46%  resets 16:55 · in 2h 35m
     ├ 7d      38%  resets Nov 13 20:20 · in 3d 6h
     └ Fable   52%  resets Nov 13 20:20 · in 3d 6h
     Holds 1 Fable reset · usable once a limit is reached · use by Dec 28
...
  #5 ci · active · API key · over #1 personal
     $2.06 today · $25.22 this month of $40 (63%) · estimate
...
```

`qswap status` shows only the active account, so it shows the key's row, `over #1 personal`. The key is the active account; the subscription is *underneath* it and is not active. Its windows still read as usual. When auto-switch entered the key, the row adds `by auto-switch`. Under `--json`, the key's row has `"active": true`, `"how": "helper"` and `"enteredBy"` (`user` or `engine`), and the subscription's row has `"underneath": true` ([json-output.md](json-output.md)).

### Leaving the key

```text
$ qswap switch 1
Switched #5 ci → #1 personal (maya@example.com)
Running Claude Code sessions are back on #1 personal within a few seconds.
```

Switching to the account underneath removes the line, and that is all. Switching to another subscription removes the line and then switches the login as usual; sessions are on it within about 30 seconds. Switching from one key to another replaces the line, and sessions move to the new key within a few seconds. `qswap switch` with no account, and its strategies, never land on a key; on a key they go back to the account underneath (`--strategy best` to a subscription with more room, if there is one). When there is no subscription to go to, they stay on the key and say what to do. An API-key account is entered by naming it, or by auto-switch ([Set a budget](#set-a-budget)).

`qswap switch 5` while #5 is already the active key checks that its line still answers, and repairs the line, the record and the run profiles if they need it; `--json` lists what it repaired in `warnings`.

If qswap created `~/.claude/settings.json` for the line, it removes the file again when the key is left and nothing else was added to it. Otherwise it changes that one key and keeps everything else as it was: the other keys and their order, the indentation, a byte order mark, the file mode, a symbolic link to your dotfiles. It never writes a file it cannot parse.

### Changing accounts while a key is active

- `qswap add-token NEWKEY --slot 5 --yes` replaces the key of the active account. The line stays the same, so `Running Claude Code sessions pick up the new key within 5 minutes, or at once after a request is rejected.`
- `qswap add` after a `/login` saves the new login but leaves the sessions on the key, and says so: `#5 ci is still the active API key; Claude Code sessions use it, not this login. To use this login: qswap switch 8`.
- `qswap remove 5` takes the line out first and says where sessions go. `qswap swap` and `qswap move` refuse to renumber the account the line names, and `add-token` never moves it to another slot.

### Removing the line by hand

If qswap is gone, or you want the subscription back without it, delete the `apiKeyHelper` line from `~/.claude/settings.json`. qswap puts its key first in the file, on a line of its own, so that one line is all there is to delete and the file stays valid JSON. Sessions are back on the subscription about a second later. Then run `qswap doctor`: it confirms the file still parses, puts nothing back by itself, and names what is left over (below).

### A settings file you sync

The line holds this machine's path to `qswap` and an account number that only this machine's qswap knows. Do not commit or sync `~/.claude/settings.json` (a link into a dotfiles repository, a sync tool) while a key is active: on another machine the line either fails every new session (`Your apiKeyHelper script is failing`) or moves every session to that machine's own #5. Leave the key with `qswap switch 1` before you commit, or keep the line out with a filter. If it got there, `qswap switch N` on that machine removes a line that cannot give a key, or delete the line by hand.

### Sessions started with qswap run

A `qswap run` session has its own profile, which shares `~/.claude/settings.json` through a link. While the line is in, each such profile gets a copy of the settings without it instead, so those sessions stay on their own account; when the line is removed the link comes back. Settings changed inside a run session in the meantime stay in its copy. A profile created while a key is active gets the copy too. If a profile cannot be given its copy (it cannot be written, or the disk is full), the switch is refused and nothing changes, since that profile would follow the line onto the key.

`qswap run 5` runs one session on the key, and nothing else moves: [sessions.md](sessions.md#run-on-an-api-key).

### When the line cannot be added

qswap adds nothing, and says why, when something in the settings file would stop the line from working or the file cannot be changed. Another `apiKeyHelper` there is already in use by every session, and comes before any key qswap could save, so the one way forward is to remove it:

```text
$ qswap switch 5
Error: not switched. ~/.claude/settings.json already sets another
apiKeyHelper, and every Claude Code session uses it now, before any login or
saved key, so qswap cannot add the key line for #5 ci beside it.
Remove apiKeyHelper from ~/.claude/settings.json, then: qswap switch 5
```

A credential or provider variable in the file's `env` block (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`) comes before the line, so sessions would never use the key. The refusal names the variable, never its value:

```text
$ qswap switch 5
Error: not switched. ~/.claude/settings.json sets ANTHROPIC_API_KEY in its env
block, which Claude Code obeys before any key line, so sessions would not use
#5 ci.
Remove ANTHROPIC_API_KEY from the env block of ~/.claude/settings.json, then:
qswap switch 5
```

`ANTHROPIC_BASE_URL` in that block sends the key to another host. The switch goes ahead and says where: `Warning: ~/.claude/settings.json sets ANTHROPIC_BASE_URL, so Claude Code sends the key of #5 ci to llm-gateway.example.com.`

A file qswap cannot parse or write is refused with its fix first, and then the older way, which logs running sessions out:

```text
$ qswap switch 5
Error: not switched. qswap cannot add the key line for #5 ci to the Claude
Code settings: ~/.claude/settings.json is not valid JSON (line 2: Illegal
trailing comma before end of object). Without it, the key can only be saved
for Claude Code, which a running session does not pick up. No Claude Code
session is running on the shared login.
Sessions started after the switch use the key.
Fix the JSON in ~/.claude/settings.json, then: qswap switch 5
To switch anyway:
  qswap switch 5 --log-out-sessions
```

`--log-out-sessions` takes the older way: the key is saved where Claude Code reads it once, at start (the macOS Keychain item `Claude Code`, or `primaryApiKey` in `~/.claude.json` elsewhere), and the login is taken out of Claude Code's login item. A running session cannot pick a saved key up, so each one started on a subscription answers `Not logged in · Please run /login` about 30 seconds later, until you switch back or restart it. The login item itself stays, with your MCP server sign-ins in it. While another `apiKeyHelper` or one of those variables is set, `--log-out-sessions` is refused too, since either comes before a saved key as well.

The count in the refusal is the "Running instances" `qswap list` shows. Sessions inside a `qswap run` profile have their own login and are not counted, and neither is a process that has exited. When qswap cannot read Claude Code's session records (a record half written, or in a shape it does not know), it says it cannot tell whether sessions are running. Claude Code names each record after its process id, so a record qswap cannot read is ignored when that process is no longer running. Otherwise the refusal names the file and what to do:

```text
This session record could not be read:
  /Users/maya/.claude/sessions/4101.json
If Claude Code process 4101 is gone, delete the file.
```

`qswap doctor` lists such records too. Under `--json` the refusal is the error envelope (`KeyHelperUnavailableError`), with the reason in `error.keyHelper`, the count in `error.sessions`, and the records it could not read counted in `error.unreadable` and listed in `error.unreadableRecords`. A refusal that does not offer `--log-out-sessions` adds `"logOutSessions": false`.

The menu bar and the dashboard ask nothing when the line can be added. When it cannot be, and the older way is open, they ask *Use the saved key instead?*, with Cancel as the default and *Switch and log out sessions* to go ahead; otherwise they show the refusal.

Leaving a saved key is safe: qswap never rewrites an API-key account's saved key when it leaves it, and the account you switch to gets its own login together with the MCP sign-ins of the moment. Sessions started on the key move back to the subscription within about 30 seconds, and sessions that stopped with "Not logged in" pick the login up again and continue their conversation. From one saved key to another, a session started on the first keeps it until you restart it, and the switch lists every such session under the key it keeps, by process id and directory. Saving a changed key the same way for the account already active (after an import) lists the sessions that keep the previous key too.

Because only the sessions started after a saved-key switch use the key, `qswap usage` splits that period's usage by each session's first event: a session that began before the switch stays with the account that was active then. The split is approximate (a session started on the key and still running after you switch away is all the key's), and the report says so under its table (`Approximate: ...`; `attributionApproximate` in `--json`).

### What qswap doctor checks

- qswap's own line for an API-key account is reported as the active key, not as a warning, once doctor has run the line's command with `--check`: `ok    active API key: #5 ci, through qswap's key line in ~/.claude/settings.json, which runs ~/.local/bin/qswap`.
- A line for an account that no longer exists, or is not an API-key account, fails: new sessions get no key ("apiKeyHelper script is failing"), and running ones keep the key they have until the settings change. The fix is `qswap switch N` to the account underneath, which removes the line.
- A line that does not answer fails: it runs a qswap that is no longer there (a reinstall moved it), a program that is not executable, or an interpreter that cannot import qswap. `qswap switch 5` writes the line again for this qswap.
- Run profiles left with a settings copy after the line is gone (qswap stopped between the two steps), or still sharing the settings while the line is in, fail, and `qswap switch N` repairs them.
- A line that came back after qswap took it out (another program wrote an older copy of the settings) warns, with the switch that leaves it.
- A key qswap made active whose line is no longer in the file (another program wrote an older copy the other way) warns, and so does `activeAccountNumber` naming it; the next `qswap switch` clears both.
- A key auto-switch entered while no auto-switch is running warns: nothing watches its limit.
- `~/.claude/settings.json` that cannot be parsed while qswap's record names a key fails, with the fix.
- Another `apiKeyHelper`, a variable in the settings' `env` block that comes before the line (or `ANTHROPIC_BASE_URL`, which sends the key elsewhere), `apiKeyHelper` or those variables in the managed settings or the current project's settings, and `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` exported in your shell, warn. `qswap switch` warns about the shell variables too; with a key active, `CLAUDE_CODE_OAUTH_TOKEN` follows the key.

`qswap list`, `qswap status`, the dashboard and the menu bar say the same about each of these states, with the same command.

## See what it spent

`qswap list`, the dashboard and the menu bar show the spend on the account's row:

```text
$ qswap list
...
  #5 ci · API key
     $2.06 today · $25.22 this month of $40 (63%) · estimate
...
```

Months and days are UTC, as in Anthropic's reports, so the month starts again at midnight UTC on the 1st. The row takes these forms:

| Row | When |
|---|---|
| `$2.06 today · $25.22 this month` | No budget |
| `... this month of $40 (63%)` | A budget is set |
| `... this month of $20 (126%, over budget)` | At or past the budget |
| `$25.22+ this month` | Some of the tokens are from a model with no price. For a budget or a credit they count at the dearest rate in the price table, and the second line says how many (`+ counts 1.2M tokens of models with no price at the dearest rate`) |
| `· estimate` at the end | The figure comes from this machine's session files, not from Anthropic ([Where the figure comes from](#where-the-figure-comes-from)) |
| `· limit not enforced: no spend figure` at the end | A budget or a credit is set, but the figure is more than 20 minutes old or there is none. Auto-switch treats the key as at its limit until the spend is measured; the second line says so, with `qswap usage`, which measures it |
| `no usage recorded on this machine` | The estimate is $0 today and this month, or nothing has been scanned yet (no usage ledger on this machine). A one-line row (menu bar, dashboard) says `no usage recorded` |
| `$0 of $500 this month · no usage recorded on this machine` | The same, with a budget. A one-line row says `$0 of $500 this month` |
| `spend unknown: this machine's usage ledger could not be read; run: qswap usage` | The ledger exists but could not be read (written by another version, or damaged). Never shown as $0. A one-line row says `spend unknown` |
| `no spend data yet` | A registered organization could not give a figure (for example, the key is not in it), and there is no estimate either. A second line says why |

A second line, when there is one, says why the figure is not Anthropic's (`key not found in a registered organization; estimate from this machine's transcripts`), how old an estimate is once it is more than an hour old (`estimate as of the last qswap usage, 3h ago`), or how old Anthropic's figure is while it cannot be refreshed (`Admin key rejected, figures from 12m ago`).

`qswap budget` lists every API-key account's budget and spend:

```text
$ qswap budget
Monthly budgets for API-key accounts (UTC month):
  5: ci  $25.22 of $40 this month (63%) (estimate) · $2.06 today
A budget is a brake inside qswap, not a cap at Anthropic: auto-switch stops using the account at its budget. For a hard cap, set a workspace spend limit in the Console.
```

`qswap list --json` and `qswap budget --json` add an `apiSpend` object and a `budget` field to the account ([json-output.md](json-output.md)).

## Set a budget

```text
$ qswap budget 5 500
Set a monthly budget of $500 for #5 api-key-5.
  A budget is a brake inside qswap, not a cap at Anthropic: auto-switch stops using the account at its budget. For a hard cap, set a workspace spend limit in the Console.
```

The account can be a number, email or alias. The amount is US dollars per UTC month, above 0 and at most $10,000,000, such as `500`, `49.50` or `$1,187.03`; more than two decimals, a misplaced comma, zero or a negative number is a usage error (exit 64). `qswap budget 5 off` removes it. A subscription account is refused: budgets apply to API-key accounts only.

### What auto-switch does with an API key

Auto-switch (`qswap auto`, the menu bar, the dashboard's auto view) never ranks an API-key account against subscriptions. With `--include-api-key-accounts` (`autoswitch.includeApiKeyAccounts`) it may use one as a last resort; [auto-switch.md](auto-switch.md#api-key-accounts-and-budgets) has the log lines. Its rules:

- **When.** Only when the active subscription is at its limit and no other subscription can take over, after any usage reset it would spend. Never because an account reached its switch line, and never because usage cannot be read.
- **Which key.** Only one with a monthly budget or a credit, and a spend figure no older than 20 minutes. A key with neither is never used by auto-switch, which says so once (`Not switching to an API key: #5 ci has no budget or credit; set one with: qswap budget 5 AMOUNT`). You can still switch to it yourself. A key whose figure is missing or old is not used either (`its spend could not be measured for 20 minutes`). To keep the figure fresh, auto-switch reads this machine's session files itself before it moves to a key, and every 5 minutes while one is active.
- **Running sessions** follow the move, so they do not hold it. Where the line cannot be added, auto-switch does not take the `--log-out-sessions` way by itself: it stays and says so once.
- **Coming back.** A key auto-switch moved to is left for a subscription once that subscription is under its own lines with at least 5 points of room in every window, and the cooldown (`autoswitch.cooldownSeconds`) has passed since the move (`Switched #5 ci → #1 personal · a subscription has room again`).
- **A key you chose** is never left because a subscription has room: only at its budget, when its credit is spent or expired, when its spend has not been measured for 20 minutes, or when you switch (`on the API key you chose`).
- **At the limit.** At or over the budget, with the credit spent or expired, or with no fresh figure, a key is never a target. An active one is left at once, without waiting for the cooldown, for the subscription with the most room, and when none has room, back to the account underneath (`· monthly budget reached; no subscription has room, so back to the account underneath`). Never for another key. A limit once reached stays reached, even if a later figure reads lower (an estimate in place of Anthropic's figure): a budget until the month rolls over, a credit until you record it again, and either until you set it again.
- **A login qswap does not manage** under the key is never replaced: at the key's limit auto-switch only removes the line, and otherwise it stays.
- **With no login at all** under the key, it cannot leave; it says so once, with the command that would.
- **When it stops** (Ctrl-C on `qswap auto`, the menu bar's toggle off or quit) on a key it entered, it leaves the sessions on the key and says that nothing watches its limit now, and how to go back. `qswap status`, `qswap list`, the menu bar, the dashboard and `qswap doctor` say the same until an engine runs again.
- An active API-key account that is not on the key line (a `--log-out-sessions` switch) under its budget, or without one, is left alone (`#5 ci is an API key · no quota to watch`); with `--include-api-key-accounts` auto-switch counts its missing usage as unreadable and fails over after `autoswitch.unhealthyTicks` checks (3 by default).
- At 80% of the budget it logs one warning per account per month, which the menu bar also shows as a notification:

  ```text
  14:20:00  #5 ci has spent $25.22 of its $30.00 monthly budget (84%) (estimate)
  ```

### What a budget is not

A budget is a brake inside qswap, not a cap at Anthropic. It changes only what auto-switch does. Claude Code keeps working on the account if you switch to it by hand, and anything else that uses the same key is not affected. For a hard cap, set a spend limit on the key's workspace in the Claude Console.

Figures also arrive late: Anthropic's by about five minutes, the local estimate only when `qswap usage` runs. An account can go past its budget before qswap notices.

## Record a prepaid credit

A Console organization can hold a prepaid balance. No API key can read it, but qswap can count it down for you:

```text
$ qswap credit 5 1187 --expires 2026-12-31
Recorded $1,187 of credit for #5 ci, usable until 2026-12-31 UTC.
  Spend counts against it from 14:00 UTC Nov 10 (this machine's ledger, an
  estimate).
  A credit is the count qswap keeps of what you recorded, less the spend it
  measures from then on. It is not the balance at Anthropic, which no key can
  read.
```

From then on the account's row counts down: `API key · $1,150 of $1,187 credit left`, with a second line saying from when (`credit counted from 14:00 UTC Nov 10, expires Dec 31`). What feeds it is the spend qswap measures for the key: Anthropic's figure when the account is linked to a registered organization, which counts every request made with the key; otherwise this machine's estimate, which sees only Claude Code on this machine while the key was active (`· estimate`). Either way it is an estimate of the balance, never the balance. Anthropic's figures come in whole UTC days, so the day you record the credit counts in full (`counted from the start of Nov 10 UTC`); the estimate counts from the hour.

```text
$ qswap credit
Credit recorded for API-key accounts:
  5: ci  $1,187 of $1,187 credit left · estimate
     credit counted from 14:00 UTC Nov 10, expires Dec 31
A credit is the count qswap keeps of what you recorded, less the spend it
measures from then on. It is not the balance at Anthropic, which no key can
read.
```

- `qswap credit 5 off` removes it, and `qswap credit` lists every credit (`--json` too). Recording a credit again starts the count again from now.
- The amount is parsed like a budget's (`1187`, `1,187.03`, `$1,187.03`). `--expires DATE` is the last UTC day the credit can be used; a date already past is refused (`Error: --expires 2026-11-09 is past (UTC); give the credit's last usable day`). Once it has passed, the row reads `credit expired Dec 31`.
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

When Anthropic's report cannot be read (the key was rejected, the Admin API is rate limiting or unreachable), qswap backs off and the row keeps the last figure for up to an hour, then falls back to the local estimate, with the reason on the second line. The local estimate changes when this machine's session files are read: by `qswap usage`, by the menu bar every 15 minutes, by the dashboard every 5 minutes, and by auto-switch before it moves to a key and every 5 minutes while a key with a budget or credit is active. `qswap list` and `qswap status` read none themselves, so their rows say `limit not enforced: no spend figure` once the estimate is 20 minutes old; run `qswap usage` to bring it up to date.

## What the organization was billed

`qswap usage --billed` adds what Anthropic reports for each registered organization over the same dates as the rest of `qswap usage`, by day, model, key or workspace. Its total is Anthropic's cost report. See [usage.md](usage.md#what-anthropic-billed).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `autoswitch.includeApiKeyAccounts` | `false` | Let auto-switch use an API-key account with a budget or credit as a last resort, when every subscription is at its limit |
| `usage.headerProbe` | `false` | Read setup-token usage from the rate-limit headers of a one-token Haiku request (uses that account's quota) |

Change one with `qswap config set usage.headerProbe true`. The full list is in [settings.md](settings.md).

## Limits

- The live switch has been tested on Claude Code 2.1.322, in print mode (`claude -p`), with a throwaway home's user settings and a made-up key ([CONTRIBUTING.md](../CONTRIBUTING.md#the-live-switch-end-to-end)). Not yet tested: an interactive session, a claude.ai login underneath on the user settings file, Remote Control and connectors in a running session. qswap does not check the Claude Code version.
- A budget is enforced by qswap's auto-switch only, and only while a spend figure no older than 20 minutes exists; without one auto-switch keeps off the key.
- Anthropic's figure needs an Admin key, which can do far more than read reports.
- The local estimate sees only this machine's Claude Code usage while the account was active.
- The menu bar's new build, which shows the budget notification, has not been run by hand yet ([menubar.md](menubar.md)).
- Budgets and the Admin API apply to Claude Code API-key accounts. Codex accounts are covered in [codex.md](codex.md).
