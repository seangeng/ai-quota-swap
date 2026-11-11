# Design: API-key accounts and running sessions

Status: accepted, 2026-11-10. Written after the first real switch to an API-key account
logged out every running Claude Code session on the machine.

## What Claude Code does

Read from the Claude Code 2.1.322 binary and confirmed with isolated sessions on a throwaway
profile.

1. Auth is chosen on every request. A claude.ai login (`claudeAiOauth` in the
   `Claude Code-credentials` item) wins over a saved Console key whenever it is present.
2. The saved Console key (Keychain service `Claude Code`, or `primaryApiKey` off macOS) is
   looked up once per process and remembered for the life of the process, including
   "there is none". A running session never sees a key saved after it started, and a session
   started on a key keeps using it after the key is removed.
3. The login item is re-read through a cache of about 30 seconds. Deleting the item changes
   nothing for running sessions (the cache keeps the old login). An item that is present but
   has no `claudeAiOauth` replaces their login within about 30 seconds.
4. A session with neither a login nor a remembered key answers every prompt with
   "Not logged in · Please run /login". No request is sent.
5. Claude Code writes MCP server logins (`mcpOAuth`) into the same item. If the item is
   missing, the next session that touches an MCP login creates it again with only `mcpOAuth`.
6. `apiKeyHelper` in a settings file is the one key source a running session picks up: about
   one second after the settings change (measured end to end on 2.1.322, below). It turns the
   claude.ai login off for every process that reads that settings file. Removing the setting
   turns the login back on. Emptying the helper's output does not: the last key keeps being
   sent.
7. `ANTHROPIC_API_KEY` in the environment works in print mode without a prompt, and in an
   interactive session after a one-time approval.

## What went wrong

Switching to an API-key account deleted the login item and saved the key. Running sessions
carried on with the old login from their cache. Seven minutes later one session restarted,
started on the key, and recreated the login item with only MCP logins in it. Within thirty
seconds every other session had no login and no key (fact 2), and stopped. Switching back
then saved that MCP-only item as the API-key account's credential, pushing the real key into
the previous-copy slot.

## Part 1: make the existing switch safe and honest

- **Guard.** Switching to an API-key account while Claude Code sessions are running on the
  shared login is refused, with the number of sessions and what will happen to them.
  `--log-out-sessions` goes ahead. The menu bar asks, with Cancel as the default. The
  auto-switch engine never does it while sessions are running, and says so once.
- **Activation.** Save the key. Keep the login item: remove only `claudeAiOauth` from it, so
  MCP logins survive and the item is never missing. Do not add the key to Claude Code's
  list of approved environment keys.
- **Leaving an API-key account.** Its saved key is never rewritten by a backup. The live
  credential of an API-key account is the saved Console key, not the login item. The account
  being switched to gets its own login plus the live MCP logins.
- **Words.** No message or document says a running session picks up an API key. After a
  switch back to a subscription, sessions started on the key return to the subscription by
  themselves; after a switch from one key to another they do not, and are listed.
- **Doctor.** Reports an `apiKeyHelper` in Claude Code's settings, exported
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN`, and an API-key
  account that is active while older sessions are running: each one overrides or defeats the
  active account.

## Part 2: use a key without logging anyone out

Status: accepted, 2026-11-10; implemented 2026-11-10 and revised 2026-11-11 after a second
review (see [As built](#as-built)).

Tested on Claude Code 2.1.322, 2026-11-11: qswap's own switch, run against the real binary and
the real API in a throwaway home, with a made-up key the API rejects
([CONTRIBUTING.md](../../CONTRIBUTING.md#the-live-switch-end-to-end) says how to run it
again). Every session ran in print mode (`claude -p`), with the user settings file as the
real user layer. What it showed:

- A session started while qswap's line is in sends the key (`apiKeySource: apiKeyHelper`).
- One long-lived process followed qswap's write of the line, and of its removal, about one
  second after each write (1.02 to 1.07 s). The next request used the key, or no key once the
  line was gone.
- `qswap run 5` gave the key to that one process and left the user settings untouched.
- A line for an account that cannot give a key fails new sessions with "Your apiKeyHelper
  script is failing", and a session that already holds the key keeps sending it.

Not tested: an interactive session, a claude.ai login underneath on the user settings layer
(an earlier run moved one process from a claude.ai login to the key and back, on a project
settings file), Remote Control, and claude.ai connectors in a running session.

### What Claude Code does with `apiKeyHelper`

Each fact names where it comes from: the end-to-end run on 2.1.322 above, the 2.1.322 binary
(read, not run), or Anthropic's documentation.

- While the setting is present, the helper's output is the API key and the claude.ai login
  is not used. Every process that reads that settings file follows it, running or new
  (end-to-end run, print mode).
- User settings (`~/.claude/settings.json`) and project settings are watched, and a file
  replaced by a rename (how qswap writes) counts as a change. A running process acts on the
  change about one second after the write (end-to-end run). The `--settings` flag is read
  once at start and never again (an earlier run on 2.1.322, on a project settings file).
- The helper's output is cached for 5 minutes; `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` changes
  that (Anthropic's settings reference, and the 2.1.322 binary). The cache is cleared at every
  settings change and at every 401 (2.1.322 binary). So the helper runs when a session moves
  to the key, when the cache expires, after a rejected request, and after any change to the
  settings, the line's or another key's.
- When a background refresh of the cached key fails, a session that holds a key keeps
  sending it; a session with no key sends a blank credential, gets 401s, and shows "Your
  apiKeyHelper script is failing" (end-to-end run). A helper that prints nothing does the
  same, so removing the setting is the only way off.
- A key the API rejects (401) is retried: 13 requests over about 4.3 minutes per prompt, then
  "Invalid API key · Fix external API key" (end-to-end run).
- After 10 seconds without an answer an interactive session shows "apiKeyHelper is taking a
  while"; the helper is stopped after 10 minutes, and its output may be up to 16,384
  characters (2.1.322 binary).
- Which setting wins (Anthropic's documentation): managed settings, then `--settings`, then the
  project's `.claude/settings.local.json`, then its `.claude/settings.json`, then the user
  settings. An `apiKeyHelper` in a higher layer wins over qswap's line. Cloud provider
  variables (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`),
  `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` outrank the helper, in the environment or in
  a settings file's `env` block; the helper outranks `CLAUDE_CODE_OAUTH_TOKEN` and the
  claude.ai login. The helper's output is sent as both `X-Api-Key` and `Authorization:
  Bearer`, to `ANTHROPIC_BASE_URL` when that is set.
- Claude Code does not load claude.ai connectors (the MCP servers that come with the claude.ai
  login) while a helper is set, and Remote Control does not support API keys (Anthropic's
  documentation). What a running session does with either when the line appears or goes was
  not tested.
- No approval prompt or trust step applies to a helper in user settings or the flag layer
  (2.1.322 binary).
- A session started while the line is in writes its `session-owner` stamp only after the line
  is gone, and then with the login's account (2.1.322 binary).

### Key helper

`qswap key-helper N` prints account N's saved key and nothing else. It makes no network
call, takes no lock it could wait on, and exits 1 with one line on standard error when N is
not an API-key account or has no key (64 for a usage error, also one line). `--check` prints
nothing and exits 0 when the key can be read. It is what every helper setting below runs, by
absolute path to the installed `qswap`.

### Live activation (the default for API-key accounts)

Activating an API-key account adds `"apiKeyHelper": "<qswap> key-helper N"` to Claude Code's
user settings. Leaving it removes that line. Nothing else changes: not the login item, not
`~/.claude.json`. The subscription that was active stays logged in underneath, and sessions
return to it when the key is left.

- **Who is active.** An API-key account is active exactly when qswap's helper line for it is
  in the user settings. That is the first rule of working out the active account; the rest
  is unchanged. `status` and `list` show `#5 ci · active · API key · over #1 personal`, and
  add `by auto-switch` when the engine entered the key.
- **Switching.** To a key: add the line (replace it when moving from another key). To a
  subscription: remove the line, then the ordinary login switch if the target is not the
  account underneath. Rotation and the engine go through the same two steps. A rotation from
  a key picks its target before it touches the line, and stays on the key when there is none.
- **Messages.** To a key: running sessions move to it within a few seconds, the account
  underneath stays logged in, claude.ai connectors and Remote Control are unavailable while a
  key is in use, and the way back (`Go back with: qswap switch 1`; over a login qswap does
  not manage, or with no login, the way back the other surfaces give). Away from a key:
  sessions are back on the named account within a few seconds.
- **The settings file.** Read, change one key, write atomically, keep everything else as it
  was (key order, indentation, file mode, a byte order mark). In a file of several lines the
  line goes in as the first member, so that deleting it by hand leaves valid JSON. If the
  file is missing it is created with only that key and removed again when the key is left; a
  file that held an empty object gets its own text back. If it cannot be parsed, or already
  has an `apiKeyHelper` that is not qswap's, qswap changes nothing and says why.
- **What would defeat the line.** Before it writes the line qswap reads the `env` block of the
  same file. A credential or provider variable there (`ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
  `CLAUDE_CODE_USE_FOUNDRY`) would outrank the line, so the switch is refused, naming the
  variable and never its value. `ANTHROPIC_BASE_URL` there sends the key elsewhere: the
  switch goes ahead with one warning naming the host.
- **Run profiles.** A `qswap run` profile shares the user settings file through a link, so it
  would follow the helper. While qswap's helper line is installed, each run profile's link is
  replaced by a copy of the settings without the line; when the line is removed the links
  come back. A profile created in between gets the copy.
- **The engine.** With `autoswitch.includeApiKeyAccounts` on, the engine uses a key as a last
  resort, whether or not sessions are running, because they can follow:
  - It enters a key only when the active subscription is at its limit and no other
    subscription can take over: never at a switch line, never because usage is unreadable.
  - Only a key with a monthly budget or a credit, and only with a spend figure no older than
    20 minutes. A usage reset the planner spends comes first, and so does one whose answer
    or refill is still out. A reset the user's settings keep (`resets.minHoursLeft`, the
    daily cap, the `active` mode, the kind rule, `resets.auto off`) stays banked: the
    engine does not override them to stay off a key, and names each such reset on the
    switch with `qswap reset N` (`resets.kept_resets`, decision EM4). While the key it
    entered is active, status, list, the dashboard and the menu bar's key row name them too,
    for as long as it is true (`autoswitch.kept_resets_now`, from the stored readings).
  - A key it entered is left for a subscription under its own lines with at least 5 points of
    room in every window, once the cooldown has passed since the key was entered.
  - Any key is left at once at its budget, with its credit spent or expired, or when its spend
    has not been measured for 20 minutes: for the subscription with the most room, else the
    account underneath. Never for another key, and never by a login switch over a login
    qswap does not manage. Nor on a later check: until a subscription has room again the
    engine enters no other key (`key-left`).
- **The fallback.** Where the helper cannot be installed (another helper is set, the file
  cannot be parsed or written, a variable in its `env` block outranks the line), activation
  is refused with the reason and the fix. `--log-out-sessions` then takes the part 1 path:
  the saved key, which only sessions started afterwards will use. It is not offered, and is
  refused, while another helper or such a variable is set, since either outranks a saved key
  too.
- **Doctor.** qswap's own helper line is reported as the active key, not as a warning, after
  running the line's command with `--check`. A line for an account that cannot give a key, a
  line that does not answer, copies left in run profiles, run profiles still linked while the
  line is in, a line that came back after qswap removed it, and an engine-entered key nobody
  watches each get the repair command. So do the things that outrank the line: another
  helper, a variable in the settings' `env` block or in the shell, and `apiKeyHelper` or
  those variables in the managed settings or the current project's settings.

### One session

`qswap run N` for an API-key account starts Claude Code on the shared profile with
`--settings '{"apiKeyHelper": "<qswap> key-helper N"}'`. Only that process uses the key, for
as long as it lives. Arguments after `--` go to `claude` as for any `run`.

### Credit

`qswap credit N AMOUNT [--expires DATE]` records a balance for an API-key account as of now;
`qswap credit N off` removes it; `qswap credit` lists them. Spend measured from then on
counts down from it: Anthropic's figures when an organization is registered, the local
ledger otherwise. The row reads `API key · $1,150 of $1,187 credit left`. At zero, or after
the expiry date, the account is treated as at its budget: the engine leaves it and will not
move to it. A monthly budget and a credit can both be set; whichever is reached first
applies. A credit is qswap's own count, not Anthropic's balance, which no key can read.
Tokens of a model with no price count toward a budget or a credit at the dearest rate in the
table, never as zero.

### Ledger

Each activation record says how the account was activated (`helper` or `saved-key`). Under
the helper every session changes over within seconds, so attributing usage by time is right.
For a saved-key period only sessions started after the activation used the key; usage in
such a period is attributed to the key only for sessions whose first event is after the
activation, and the report says the split is approximate. A session's `session-owner` stamp
marks a `qswap run` session only when it comes before the session's first event: a session
started under the line is stamped late, with the login's account, and its key-period usage
stays on the key. Nor does a stamp naming the login under the key mark one when the
session's first counted event falls while the key is active through the line (a session
opened as the subscription ran out, just before the engine entered the key): `qswap run`
for that login is refused while the key is active, so its usage stays by time, on the key.
A stamp naming another login still marks a `qswap run` session.

### As built

What the code does where this part leaves room, or where it had to differ:

- **The command the line runs** is this install's console script, found from the package's
  own environment: the `RECORD` file the installer wrote names the scripts it made, and the
  package's location is resolved first, so this also works from inside the menu bar's app
  bundle. Of that script, the link first on `PATH` is used when it leads to the same file
  (`uv tool` and `pipx` keep `~/.local/bin/qswap` across a reinstall). A `qswap` first on
  `PATH` that belongs to another install is never used, and neither is a record in the
  interpreter's library directory that is not this package's (one with a package copy of
  its own, or an editable install of another checkout). Only an install with no console
  script gets `<python> -P -m ai_quota_swap`: `-P` keeps the session's project directory off
  `sys.path`, where the bare `-m` form would import a project file named like a
  standard-library module. Older `-m` lines are still read as qswap's, and doctor fails
  them. A bare word in the line holds only what qswap's own quoting leaves bare, so a line
  the shell would run as two commands (`;`, `$(`, a backtick) is never read as qswap's,
  shown as active, or run by doctor's `--check`. The menu bar's LaunchAgent uses the same resolver. On Linux the line always names
  the data directory (`--data-dir`), since the session that runs it may have another
  `XDG_DATA_HOME` than qswap, or none.
- **Who entered the key** (the user or the engine) and whether qswap created the settings
  file are kept in `<data dir>/key_helper.json`. Leaving a key keeps a tombstone there
  (number, time, who had entered it). A line of qswap's with no record but a tombstone for
  its number "came back": another program wrote an older copy of the settings. A tombstone
  counts for 24 hours from its time (one with no readable time is old); after that a line
  for that number is the user's. Claude Code
  is one (read in the 2.1.322 binary): a session that saves a setting within about a second
  of qswap's write merges into the copy it read before. The line keeps who had entered it,
  and every surface names the way out. A line with neither counts as the
  user's: it is only left at its budget or credit, or by the user. The record is written
  before the line, so a crash between the two leaves a record with no line, which the next
  switch clears, rather than a line nobody recorded. From one key to another the old record
  is kept beside the new one until the line is replaced, so a stop in between leaves the old
  key's line with its own record, not a line that seems to have come back. Removing a line
  that names an account that cannot give a key keeps the other keys' tombstones.
- **`activeAccountNumber`** in `sequence.json` is set to the key on entering, and back to the
  live login on every path that removes the line. `qswap add` while a key is active saves
  the new login but leaves it, and says the key is still active and how to switch.
- **One reconcile.** `reconcile_key_helper` compares the line, `key_helper.json`, the run
  profiles and `activeAccountNumber`. It points a line that runs a qswap no longer there at
  this qswap, removes a line naming an account that cannot give a key, turns a record
  whose line is gone into a tombstone, sets back an `activeAccountNumber` naming a key with
  no line, gives a still-linked run profile its copy, and puts back the links of copies left
  with no line. Every `qswap switch` runs it first, and so does every tick of the engine
  (never a dry run). List, status, doctor, the dashboard and the menu bar report the same
  findings without repairing. When `sequence.json` or the settings cannot be read the state
  is unknown, not missing: nothing is repaired or removed.
- **Another qswap's line.** A line reads its key from the data directory after its
  `--data-dir`, or, without one, from the default data directory (`~/.ai-quota-swap`;
  `~/.local/share/ai-quota-swap` on Linux). A line for another data directory than this
  qswap's is another program's line, as another helper is: not repaired, not removed, not
  counted as the active key, and reported (by list, status, the dashboard, the menu bar and
  doctor) with that data directory named. A switch to a key is refused in the words of the
  refusal for another helper, plus the data directory; a switch to a subscription leaves the
  line and says that sessions stay on the other qswap's key until the line is removed. A
  qswap whose own data directory is not the default always names it in its line, on every
  platform.
- **Settings that cannot be read while a key is recorded.** Every surface says the same
  sentence: `~/.claude/settings.json cannot be read (...); qswap last made #5 ci the active
  API key there, and its key line may still be in it. Fix the JSON in
  ~/.claude/settings.json, then retry.` (`Make ~/.claude/settings.json readable` when reading
  the file failed rather than parsing it). Commands that would change accounts stop with it;
  the engine reports `settings-unreadable` once per episode, and `auto --once` exits 3.
- **The repair command** for a line naming an account that cannot give a key, and for stale
  run-profile copies, is `qswap switch N` to the account underneath. A line that runs a
  qswap no longer there (a reinstall moved it) is read as qswap's, reported, and pointed at
  the current qswap by `qswap switch N` to the key. `qswap switch N` to the key already
  active first checks what outranks the line, as the first switch does (a variable in the
  `env` block is refused in the same words, and nothing is written; `ANTHROPIC_BASE_URL`
  warns), then runs the line's command with `--check` and repairs the line, the record and
  the run profiles, instead of answering only `Already on`. List, status, the dashboard and
  the menu bar carry a note for such a variable and for another `apiKeyHelper`, in the
  refusal's words and with its fix.
- **Order of the two steps.** Entering a key gives run profiles their copies first, then
  writes the record, then the line (a failure in between puts the profiles back); leaving
  removes the line and then restores the profiles. Either crash leaves copies with no line,
  which doctor reports. A profile that cannot be given its copy refuses the switch before
  the line is written. After the line is in, a second pass copies any profile a `qswap run`
  launch linked again in between; if that fails, the line is taken back and the switch
  refused. A rotation from a key that fails after the line was removed, Ctrl-C included,
  puts the line back.
- **The line's place in the file.** Always the first member, and in a file of several lines
  on a line of its own: where the first member shared the brace's line, that line is split
  after the brace (what followed the brace is recorded in `key_helper.json` and put back when
  the line comes out). A file of one line keeps the key inside that line. The round trip
  is byte for byte, a byte order mark included, and an empty object gets its own spelling
  back (`{ }` stays `{ }`).
- **`qswap run N` for the subscription underneath an active key** is refused: plain `claude`
  on that login would follow the line onto the key.
- **`--log-out-sessions`** always takes the saved-key path, also when the helper could be
  installed; with qswap's line installed it removes the line first. It is refused while
  another `apiKeyHelper` or a credential variable in the settings' `env` block is set. The
  part 1 session guard is not consulted any more: where the helper cannot be installed the
  switch is always refused, and the refusal still counts the running sessions.
- **Rotation** skips API-key accounts without a "Skipped" line (they are never in rotation).
  `--strategy best` on a key may pick a subscription with more room than the account
  underneath; the other rotations go back to it. With nothing to rotate to it stays on the
  key and names the next step.
- **The engine.** The thresholds are named constants in `autoswitch.py`: a spend figure is
  fresh for 20 minutes (`api_spend.SPEND_FRESH_S`); while a key with a budget or credit is
  active the engine scans the ledger every 5 minutes (`SPEND_SCAN_INTERVAL_S`), and before it
  enters a key, from the earlier of the start of the UTC month and the start of the key's
  credit (`scan_recent(start=...)`; others keep the 7-day default). A scan that raises, or
  that finds session files it cannot read, covers nothing: until one covers the period
  (`spendScan` in the state file: when, from when, whether it did, and how many files it could
  not read, the first named) the ledger's figure is `spend-unknown`, said with that cause and
  its step rather than an age. For 20 minutes after such a scan every reading of a ledger
  figure carries `scan_failed`, so the rows say `limit not enforced` and a dry run agrees. For a key linked to an organization only Anthropic's figure counts: the
  ledger's is shown (`this machine only`) but never fresh. So for a key whose link this pass
  could not decide (an active organization's keys could not be listed: `link_unknown` on the
  reading); a listing that answers without the key leaves the ledger's figure. With each of those scans the
  engine reads Anthropic's figure again, accepting an answer at most a minute old
  (`ADMIN_REFRESH_S`) whatever `admin_api.CACHE_TTL_S` says for the other readers; the way back needs 5 points of room in every window
  (`RETURN_ROOM_PCT`). The engine never enters a key while a subscription in the rotation
  has that much room in every watched window and is readable, not set aside and not
  disabled: that subscription is the target instead, over its own switch line and in any
  strategy, and the switch line says so (`taken over its switch line (5h 95%) before an API
  key`). While another subscription's usage is unreadable, key entry waits at most one
  cooldown from the first check that wanted the key (`keyWanted`;
  `usage-unreadable-hold`, said once). The wait carries on across checks up to 15 minutes or
  two check intervals apart; after a longer gap it starts again (an engine that stopped),
  unless the wait so far was one check under 6 hours old, so that checks far apart (a cron
  `--once`) go ahead on the second. Only a check that read the active account and wanted no
  key ends it. A key left at its limit (`keyLeft` in the state file:
  account, reason, when) means no other key until a check reads a subscription in the
  rotation with room (`key-left`, said once); the key left may come back once its own figure
  allows it, as a fresh figure after `spend-unknown` does. Among keys that qualify the engine
  takes the first in account order. A key judged at its budget or credit stays at it in the engine's
  state file, one latch per kind (`limitLatch[N]` holds `budget` and `credit` apart): the
  budget latch until the month rolls over or `qswap budget` sets it again, the credit latch
  until `qswap credit` records another; each command clears only its own, and the key stays
  out while either holds. A state file from before keeps its one latch as the kind its reason
  names. So a later reading that looks lower (an estimate in place of Anthropic's figure)
  does not make it a candidate again; the row says so (`budget reached earlier`, with the
  command that clears it) and the engine says once why it does not use the key
  (`limit-latched`). A reset the
  planner claimed whose refill no reading has shown yet holds key entry, for at most the
  30 minutes a successful claim keeps the account out of planning. A reset the planner wants
  but puts off because its account could not be read again holds key entry too, for at most
  one cooldown from the first check that wanted the key (`reset-pending`, naming the
  account). A reset kept while the active account's last claim settles is named with that
  reason (`settling`). A key the engine cannot
  use (its slot does not hold a key) is skipped and reported once, and leaving a key, a
  subscription whose switch fails and is rolled back is skipped for the next account
  (`switch-failed`), the account underneath last. A key the engine entered is left at once
  when `autoswitch.includeApiKeyAccounts` is turned off (as when its budget is removed; a
  running engine reads that setting again at each check, cached by the file's stamp, unless
  a command-line flag set it);
  a key the user entered is not touched. With no login under the key at its limit, the engine stays and
  notifies once. When it stops while sessions are on a key it entered, it says that nothing
  watches the key's limit and how to go back, and moves nothing; `enteredBy` stays
  `engine`, so the next engine carries on. An engine watches a key when its loop ticks and
  is not a dry run, or when its last `--once` tick (not a dry run) is under 15 minutes old
  (`autoswitch.key_watch`, read through `engine_watching()`); the surfaces say "only a dry
  run is running" for a dry-run loop and "last checked 4m ago" for a recent `--once`, and
  doctor warns only when nothing watches.
- **The dry run** writes nothing, the ledger included: it judges the stored figure and
  says that a real run measures the spend first and that its own figure may be older,
  and it checks read-only that a key's slot can give a key (A12). The rows say auto-switch
  measures the spend before it moves to the key, never that it stays off it.
- **The way back**, in status, list, the menu bar, the dashboard, doctor, the stopping
  engine and the switch onto a key itself (over a login qswap does not manage, or none): `qswap switch N` to the account underneath; `qswap add` over a login qswap does
  not manage; else `qswap switch N` to the first subscription; else log in to one and run
  `qswap add`. It is also the fix for a record whose line is gone (the line deleted by
  hand): the switch clears the record and `activeAccountNumber`, which doctor reports as
  one warning; `qswap switch 5`, to use the key again, is offered second and labelled so.
  No surface names re-entering the key as that fix.
- **Notes in JSON.** `list --json` and `status --json` carry `keyNotes`, one `{code, account,
  fix}` for each note the text view prints, in its order (`switcher.key_notes_json`; the
  codes are `key_activation.KEY_NOTE_CODES`). A recent `--once` tick's note is
  `watched-once`, with no fix; a row that says a budget or credit still counts as reached
  is `limit-latched` (status prints only the active account's row).
- **Credit and Anthropic's figures.** The usage report has daily buckets, so the day a credit
  is recorded counts in full against it; the ledger counts from the hour. The row and `qswap
  credit` say which (`credit counted from 14:00 UTC Nov 10`). A credit recorded before this
  month widens the report back to its day. Amounts are parsed strictly (`1187`, `1187.03`,
  `$1,187.03`, `1,187`), and an expiry date already past is refused.
- **`import --force`** over an account keeps its budget, credit and other qswap settings.
- **Swap and move** refuse to renumber the account the line names; **add** refuses to save a
  login into its slot; **add-token** may replace its key, says that running sessions pick up
  the new key within 5 minutes (at once after a rejected request), and never moves the key to
  another slot; **remove** takes the line out first and names `qswap run N` sessions on that
  key, which qswap records at launch.
