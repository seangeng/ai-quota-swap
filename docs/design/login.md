# Design: renewing a saved login (`qswap login`)

Status: proposed, 2026-11-23. Tested against a stand-in for `claude`; the first run against
the real `claude auth login` is still to come (see [Not verified](#not-verified)).

## The problem

Every Claude subscription login carries `refreshTokenExpiresAt`, a deadline its sign-in sets,
about a month out. Refreshing the access token does not move it; only a new sign-in does. The
way to renew a saved account used to be `/login` inside Claude Code as that account, which
replaces the live login and disturbs running sessions, then `qswap add`. With four accounts
that is four rounds a month.

## Facts the design rests on

1. `claude auth login --claudeai --email EMAIL` signs in from a shell: it opens the browser
   and waits. `--email` fills in the address on the login page; `--sso` exists too.
2. With `CLAUDE_CONFIG_DIR=DIR`, Claude Code keeps that profile's login apart from the default
   one: on macOS in the Keychain item `session.keychain_service_name(DIR)` (a hash of the exact
   string exported), elsewhere in `DIR/.credentials.json`. The identity it signed in as is in
   `DIR/.claude.json` under `oauthAccount`.
3. `CLAUDE_SECURESTORAGE_CONFIG_DIR`, when defined, moves the login store elsewhere; defined
   and empty, onto the default profile's store, which is the live login.
4. `claude auth logout` revokes the login at Anthropic, so every copy of it stops working.

## The design

`login.renew` takes the sign-in lock (`sign-in/.lock` in the data directory: one sign-in at
a time, across processes), removes whatever an earlier run left in `sign-in/`, and makes a
private profile there (`sign-in/profile-*`, mode 0700). Nothing else reads that directory;
it is not a `qswap run` profile (those are in `sessions/`) and is never reused.

`claude auth login --claudeai --email EMAIL [--sso]` runs with `CLAUDE_CONFIG_DIR` set to the
profile and the variables that would send the login elsewhere, or make it without the browser,
removed: the `session.AUTH_OVERRIDE_ENV_VARS`, `CLAUDE_SECURESTORAGE_CONFIG_DIR` (fact 3), and
`CLAUDE_CODE_OAUTH_REFRESH_TOKEN` (with it set, `claude auth login` 2.1.330 skips the browser
and exchanges that token, spending it), `CLAUDE_CODE_OAUTH_SCOPES`,
`CLAUDE_CODE_OAUTH_CLIENT_ID`, `ANTHROPIC_PROFILE` and `ANTHROPIC_FEDERATION_RULE_ID`; other
features keep their own lists. On a terminal it shares the terminal, so Claude Code's prompts
show and Ctrl-C reaches both processes. Claude Code handles SIGINT itself and exits 130, so an
exit of 130 (or -2) there counts as a cancel and stops a run of several sign-ins; the
terminal's settings are saved and restored around the child, for a child killed while it had
the terminal in raw mode. Off a terminal (the menu bar) its stdin is empty, its output is
captured, and a failure quotes the last line of stderr alone. Either way it is killed at the
timeout, 300 seconds by default. On Windows an email with a character `cmd.exe` reads (`&`,
`%`, `^`, `|` and the like) is refused when `claude` is a `.cmd` shim.

On exit 0 the profile's identity must be the account's: the same email (case aside) and
organization, and the same account uuid when both sides have one (another uuid is an account
deleted and made again). A mismatch saves nothing and names who signed in; when that is
another saved account, it names the command that renews that one. The credential is read with
the strict Keychain read, so a locked Keychain saves nothing rather than falling back to a
file. Then `ClaudeAccountSwitcher.save_renewed_login`:

- checks the slot's record against the sign-in's `oauthAccount`, before and again under the
  locks: the wait is minutes long (several, for a bare `qswap login`, which also checks each
  target before its sign-in starts), and a swap, a move or a remove and add meanwhile would
  otherwise put one account's login in another's slot;
- applies the capture guards `add` applies (no API key, no MCP-only item, a full token pair,
  the profile oracle before any lock, given the record's uuid), in this command's words; an
  oracle that cannot answer becomes a note the caller shows;
- under the locks a switch takes (qswap's, and Claude Code's credential and config locks),
  installs the new login live when the slot is the live login, by the switch's own steps
  (below). A busy lock raises before anything is written, and the caller retries for 45
  seconds rather than throw a finished sign-in away;
- replaces the slot's saved login (`.prev` kept), which invalidates its `qswap run` profile or
  marks a running one stale, as `add` does;
- keeps the saved config and updates only its `oauthAccount`. A switch splices only
  `oauthAccount` into `~/.claude.json`, and writes the whole saved config only when the live
  file cannot be read. The private profile's `.claude.json` is nearly empty, so it never
  replaces the saved one;
- keeps every setting on the record (alias, disabled flag, threshold, priority), fills in a
  missing uuid, and clears the usage store's dead-login state. The engine releases its
  set-aside by itself once the saved login's fingerprint changes.

The profile's Keychain item, then the directory, are deleted in every case: success, mismatch,
failure, timeout, Ctrl-C, an exception. When the item cannot be deleted the directory stays,
emptied, because its path is the only way to name the item again; the next `qswap login`
retries.

## The active account

Writing the new login into the slot alone would not last. The usage pass resyncs the active
account's saved login from the live one when their lineages differ and the profile endpoint
says the live one is the account's (`_resync_rotated_backup`): it would copy the old live login
back over the new one. So when the account is the live login, the new one goes live too:

- the live store is read as a switch reads it (`_read_active_credentials`); a failed read, one
  inside the Keychain's cooldown or one served from a fallback refuses before anything is
  written, since taking it for "no login live" renews the saved copy alone;
- before the locks, `_prefetch_live_identity`, as a switch does;
- under them, `_classify_outgoing_credential` attributes the live credential. When it is the
  account's (`own-bytes`, `own-family`, `own-rotated`) or wiped by Claude Code after a refused
  refresh, the new login is written with the live MCP sign-ins composed in
  (`_prepare_credentials_for_activation`) and `oauthAccount` spliced into `~/.claude.json`.
  An unresolved one is replaced after a safety copy (`qswap unclaimed`), as a switch keeps it;
- an empty store, an item with only MCP sign-ins, an API key and another account's credential
  are left alone, only the saved copy is renewed, and the result says so, with
  `qswap switch N --force` (a plain `switch N` to the active account changes nothing).

Then the copies that go the other way must not undo it. The resync and the expired-token
adopt branch both copy a live credential into the active account's slot; neither does so now
when the saved login's `refreshTokenExpiresAt` is later than the live one's. That date is set
at sign-in and never grows within one lineage, so a later one in the saved copy means a newer
sign-in. Each logs one line when it keeps the saved login.

The resync writes only a live credential that still carries the lineage it served, re-read
under the same locks, so a pass that read the old login just before the renewal finds the
new lineage and writes nothing (`tests/test_login.py::test_the_resync_does_not_undo_a_renewal`,
and its control, which shows the revert when only the saved copy is renewed).

Running Claude Code sessions pick the new login up as they do after a switch: within about 30
seconds on macOS (its Keychain cache), from their next message elsewhere. With an API key
active through the key line over the account, the login underneath is renewed, the key stays
the active account, and the result says what `add` says then.

## Choosing accounts

Only subscription logins qualify. An API-key account, a setup-token account and a Codex
account are refused with the command that renews those. Without an account, `qswap login`
renews each login that needs re-login (`relogin_reasons`, or no saved login) or runs out
within `oauth.LOGIN_DUE_WITHIN_S` (7 days). The deadline of the live login's account is the
later of its saved and live credentials', since a `/login` in Claude Code moves the live one
first.

## The menu bar

*Sign in again* lists the same accounts, due ones first, and runs `login.renew_account` with
no terminal in `menubar_engines.LoginRunner`: one at a time, with the app's action lock held
around the save only, so the menu's other actions go on while the browser is waited on. The
runner keeps the child's process: the waiting row cancels it, and so does quitting. A renewal
or a cancel is a notification; a failure is an alert, as other menu actions' are, and is
logged. `purge` sweeps `sign-in/` before it removes the data directory, so no leftover
profile's Keychain item outlives it. `menubar_notify.login_notices` posts one notice once a login is within
`login.notifyDays` days of its deadline and one in its last day, remembered per account and
deadline in `menubar_state.json`.

## Not verified

- What a login's end date follows. Confirmed from the token endpoint (2026-11-23): `refresh_token_expires_in`
  on every refresh points at the same absolute date, so a refresh never extends a login. Not
  confirmed: what sets the date at sign-in. Two sign-ins a minute apart came out at 27 and 29
  days, so it is not the sign-in time alone; the claude.ai session the browser signed in with is
  the likely source. A renewed login that ends in under 21 days gets a sentence that offers
  signing out of claude.ai first as something that may help (`short_login_note`).
- No run against the real `claude auth login` yet. Assumed: it exits 0 only once the login is
  written, writes `oauthAccount` into the profile's `.claude.json` as `/login` does, and on
  macOS keeps the credential in the hashed Keychain item.
- With no terminal (the menu bar), it is assumed to open the browser by itself and finish
  through its localhost callback, without reading stdin. If it needs a pasted code there, the
  sign-in fails after the timeout and saves nothing; `qswap login N` in a terminal works.
- It is assumed not to run Claude Code's first-run onboarding in a fresh profile.
- On Windows, a timeout kills the `claude.cmd` shim; a node process it started may outlive it
  and keep the profile's files open, and the next `qswap login` removes them.
