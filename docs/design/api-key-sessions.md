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
6. `apiKeyHelper` in a settings file is the one key source a running session picks up: within
   about three seconds of the settings change. It turns the claude.ai login off for every
   process that reads that settings file. Removing the setting turns the login back on.
   Emptying the helper's output does not: the last key keeps being sent.
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

Status: accepted, 2026-11-10. Checked on one process: started on a claude.ai login, it moved
to the key on its next request after an `apiKeyHelper` appeared in a settings file it
watches (about one second), and back to the login after the setting was removed. Not yet
run on the user settings file with interactive sessions; the first such run is the test.

### What Claude Code does with `apiKeyHelper`

- While the setting is present, the helper's output is the API key and the claude.ai login
  is not used. Every process that reads that settings file follows it, running or new.
- User settings (`~/.claude/settings.json`) and project settings are watched. The `--settings`
  flag is read once at start and never again.
- The helper runs once when a session moves to the key and again when its cached value
  expires (five minutes by default). Removing the setting is the only way off: a helper that
  prints nothing keeps the last key in use.
- While a key is in use, Claude Code turns off its claude.ai connectors (the MCP servers that
  come with the claude.ai login) in that session.
- No approval prompt or trust step applies to a helper in user settings or the flag layer.

### Key helper

`qswap key-helper N` prints account N's saved key and nothing else. It makes no network
call, takes no lock it could wait on, and exits non-zero with a message on standard error
when N is not an API-key account or has no key. It is what every helper setting below runs,
by absolute path to the installed `qswap`.

### Live activation (the default for API-key accounts)

Activating an API-key account adds `"apiKeyHelper": "<qswap> key-helper N"` to Claude Code's
user settings. Leaving it removes that line. Nothing else changes: not the login item, not
`~/.claude.json`. The subscription that was active stays logged in underneath, and sessions
return to it when the key is left.

- **Who is active.** An API-key account is active exactly when qswap's helper line for it is
  in the user settings. That is the first rule of working out the active account; the rest
  is unchanged. `status` and `list` show `#5 ci · active · API key · over #1 personal`.
- **Switching.** To a key: add the line (replace it when moving from another key). To a
  subscription: remove the line, then the ordinary login switch if the target is not the
  account underneath. Rotation and the engine go through the same two steps.
- **Messages.** To a key: running sessions move to it within a few seconds, the account
  underneath stays logged in, and claude.ai connectors are unavailable while a key is in use.
  Away from a key: sessions are back on the named account within a few seconds.
- **The settings file.** Read, change one key, write atomically, keep everything else as it
  was (key order, indentation, file mode). If the file is missing it is created with only
  that key and removed again when the key is left. If it cannot be parsed, or already has an
  `apiKeyHelper` that is not qswap's, qswap changes nothing and says why.
- **Run profiles.** A `qswap run` profile shares the user settings file through a link, so it
  would follow the helper. While qswap's helper line is installed, each run profile's link is
  replaced by a copy of the settings without the line; when the line is removed the links
  come back. A profile created in between gets the copy.
- **The engine.** With `autoswitch.includeApiKeyAccounts` on, the engine may move to a key
  when no subscription has room, whether or not sessions are running, because they can
  follow. On a key it returns to a subscription as soon as one has room, and leaves the key
  at its budget or when its credit is spent.
- **The fallback.** Where the helper cannot be installed (another helper is set, the file
  cannot be written), activation is refused with the reason. `--log-out-sessions` then takes
  the part 1 path: the saved key, which only sessions started afterwards will use.
- **Doctor.** qswap's own helper line is reported as the active key, not as a warning. A
  line for an account that no longer exists, or copies left in run profiles after the line
  is gone, are failures with the repair command.

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

### Ledger

Each activation record says how the account was activated (`helper` or `saved-key`). Under
the helper every session changes over within seconds, so attributing usage by time is right.
For a saved-key period only sessions started after the activation used the key; usage in
such a period is attributed to the key only for sessions whose first event is after the
activation, and the report says the split is approximate.
