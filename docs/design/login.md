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
profile and the variables that would send the login elsewhere, or skip it, removed: the
`session.AUTH_OVERRIDE_ENV_VARS` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` (fact 3). On a
terminal it shares the terminal, so Claude Code's prompts show and Ctrl-C reaches both
processes. Off one (the menu bar) its stdin is empty and its output captured. Either way it is
killed at the timeout, 300 seconds by default.

On exit 0 the profile's identity must be the account's: the same email and organization, as
`_find_account_slot` compares them. A mismatch saves nothing and names who signed in; when
that is another saved account, it names the command that renews that one. The credential is
read with the strict Keychain read, so a locked Keychain saves nothing rather than falling
back to a file. Then `ClaudeAccountSwitcher.save_renewed_login`:

- applies the capture guards `add` applies (no API key, no MCP-only item, the profile oracle
  before any lock), and requires a full token pair;
- under the locks a switch takes (qswap's, and Claude Code's credential and config locks),
  checks that the slot is still the account, and when it is the live login installs the new
  login live by the switch's own steps (below);
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

- before the locks, `_prefetch_live_identity`, as a switch does;
- under them, `_classify_outgoing_credential` attributes the live credential. When it is the
  account's (`own-bytes`, `own-family`, `own-rotated`), unresolved, or wiped by Claude Code
  after a refused refresh, the new login is written with the live MCP sign-ins composed in
  (`_prepare_credentials_for_activation`) and `oauthAccount` spliced into `~/.claude.json`;
- another account's credential, an API key saved the older way and an item with no login are
  left alone, and only the saved copy is renewed. The resync never copies those into the slot.

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
result is a notification. `menubar_notify.login_notices` posts one notice once a login is within
`login.notifyDays` days of its deadline and one in its last day, remembered per account and
deadline in `menubar_state.json`.

## Not verified

- No run against the real `claude auth login` yet. Assumed: it exits 0 only once the login is
  written, writes `oauthAccount` into the profile's `.claude.json` as `/login` does, and on
  macOS keeps the credential in the hashed Keychain item.
- With no terminal (the menu bar), it is assumed to open the browser by itself and finish
  through its localhost callback, without reading stdin. If it needs a pasted code there, the
  sign-in fails after the timeout and saves nothing; `qswap login N` in a terminal works.
- It is assumed not to run Claude Code's first-run onboarding in a fresh profile.
- On Windows, a timeout kills the `claude.cmd` shim; a node process it started may outlive it
  and keep the profile's files open, and the next `qswap login` removes them.
