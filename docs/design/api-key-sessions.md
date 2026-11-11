# Design: API-key accounts and running sessions

Status: part 1 accepted, 2026-11-10. Part 2 accepted in principle, pending one end-to-end
check. Written after the first real switch to an API-key account logged out every running
Claude Code session on the machine.

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

Pending a check that one process with a claude.ai login present moves to the key when
`apiKeyHelper` appears and back to the login when it is removed.

- **One session.** `qswap run N` for an API-key account starts Claude Code on the shared
  profile with a settings override whose `apiKeyHelper` is `qswap key-helper N`. Only that
  process uses the key.
- **Every session, live.** Activating an API-key account installs `apiKeyHelper` in Claude
  Code's user settings and leaves the login alone; switching away removes it. Running
  sessions change over in seconds, in both directions. `qswap run` profiles must stop
  sharing that settings file while a helper is installed.
- **Credit.** A balance entered once per API-key account, counted down by measured spend.
  At zero, auto-switch leaves the account, which with the live switch returns every session
  to a subscription.
- **Ledger.** Usage is attributed by the credential a request actually used, not by which
  account was active at the time.
