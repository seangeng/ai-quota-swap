# Design: a setup-token as a login's backup (`qswap add-token --backup-for`)

Status: experimental, 2026-11-23, from 0.1.3. Tested against stand-ins for the probe and the
usage endpoint; never run against a real setup-token (see [Not verified](#not-verified)).

## The problem

A Claude subscription login ends about a month after its sign-in; then the account needs
`qswap login N`, and until that runs it is out of rotation. `claude setup-token` prints a
token for the same subscription that lasts about a year. qswap already took such a token as an
account of its own (`qswap add-token`), but a token added for an account that is also saved as
a login gave two rows on one quota, and auto-switch counted them as two accounts.

## Facts the design rests on

1. A setup-token has only the `user:inference` scope and no refresh token
   (`oauth.is_setup_token`). The usage endpoint and the profile endpoint do not answer it.
2. Its usage can be read from the rate-limit headers of a request for one output token
   (`oauth.request_usage_via_probe`): the 5-hour and weekly utilization and reset times.
3. One account has one 5-hour and one weekly window. A window starts when the account's first
   request after the last one ended lands, so two accounts share a reset time only by chance.
4. A setup-token carries no identity: nothing in it says which account it belongs to.

## The link

The token is stored as an ordinary setup-token account (no new kind, no other store). Its
record adds four keys: `backupFor`, the login's account number; `backupForEmail` and
`backupForOrganization`, who that login is; and `backupAdded`, when the token was added (ISO
8601). `backup_token.py` is the pure model:

- `links` keeps a link only when the number holds a managed subscription login (not an API
  key; with an organization, which every subscription login has and a token's account never
  does; not a backup itself) and that login is the account the link was made for. After a
  swap or a remove and add on another build, the number may hold another account: the link
  is dropped and the token works as an ordinary setup-token account.
- One backup per login. When two records name one login, `links` keeps the one added last;
  `duplicates` names the others, which `qswap doctor` warns about. `dangling` names links
  whose login is gone or changed.
- The dates: `ends_at` is a year after `backupAdded`; `due` is the last 30 days.

A second `--backup-for N` asks, then writes the new token into the first one's slot; a token
already saved as its own account is linked where it is (the old backup, if any, is removed).
`--slot` is refused with `--backup-for`. A backup that is the active account, or the login
underneath an active key, is not replaced: switch away first, or the live copy would later be
saved back over the new token. Plain `add-token` (with `--slot`, or the backup's email) and
`add` (with `--slot`, or with the backup live) over a backup's record are refused and name
`qswap add-token --backup-for N`: they would drop the link, or keep it on a token nobody
checked.

The link follows its login: `swap` and `move` renumber it, `add --slot` over the login's slot
unlinks it, and `remove` of the login unlinks it and says so. Export writes the link with the
login's identity; import links the token to the account with that email and organization
wherever it is managed here (imported or not), and drops the link with a note otherwise.

## The identity check

The login is read first: afresh when it works, through the usual read path; its last stored
reading when it has ended. A match needs the weekly window (`nothing_to_compare`): running
(its reset ahead) and used (over 1%). When the reading cannot give one, the command refuses
before any request is sent: a probe would start the other account's windows, and a retry
would then agree on resets alone.

Then one probe with the token (`oauth.probe_token_account`, which also returns the
`anthropic-organization-id` header when the response has one; a 429 with rate-limit headers
answers too). `compare` takes, for each of the 5-hour and weekly windows whose reset on the
login's side is ahead and which is not unused on both sides (1% or less), the reset times
(within 10 s: the endpoint stamps resets with sub-second jitter, the headers carry whole
seconds) and the figures (within 5 points). Windows seem to start on the hour, so two
accounts first used in the same hour share their resets; the figures are what tells them
apart.

- `same` needs the weekly window to agree and nothing to disagree.
- A disagreement is `other` (refused, `--unverified` or not) only against a reading of the
  login taken for this check. Against an older one, a usage reset spent since or a slow clock
  could explain it, so it is `unknown`.
- An organization header shaped like an organization id that differs from the login's makes
  a match `unknown`, with a sentence saying so, and a disagreement `other`. One that matches
  proves nothing alone (a team's members share an organization). A header of another shape is
  not compared: its form is not known from a real response.
- `unknown` refuses unless `--unverified`, which accepts it with a warning that names the
  risk (another account's quota spent under this login's name). With `--unverified` the probe
  is still sent, even when the login has nothing to compare: a 401 or 403 refuses ("Anthropic
  refused the token"). A failed request counts as `unknown`.

The check guards against a mistake (the wrong browser profile at `claude setup-token`), not
against a forger.

## States

`backup_token.state`: `standing-by` (the login works, or is disabled, and the token is not the
active account), `standing-in` (the login needs re-login and is not disabled), `in-use` (the
token is the active account while the login works). The login has ended when it needs
re-login for an auth reason: the engine set it aside (`set_aside_accounts`) or its saved login
was refused (the usage store's dead-login state, checked against the stored credential:
`_ended_logins`). A passed end date alone does not count: an account that still reads is
still used, as everywhere else. A disabled login's token is held out with it. `Link.covers`
(standing in, not disabled itself, its token saved) is what lets the login's row say the
token covers it.

## The collector

`ClaudeAccountSwitcher._place_backup_tokens` runs in every collect pass, after the static and
dead-login sentinels. A token standing by gets the `standing by` sentinel
(`json_output.USAGE_STANDING_BY`), so it is never reserved, fetched or probed; its plan is never
asked for either (`_collect_subscription_plans` skips every linked token). Any other token is
read with `header_probe=True`, threaded through `_run_usage_fetches` to the three fetch paths,
whatever `usage.headerProbe` says. A login the pass reads is judged by the pass's own "re-login
needed"; another by the usage store. The lockstep warning skips a token and its login.

When Claude Code meets the refusal first, it empties both tokens of the live login in place;
the active login then reads "no credentials", which ended nothing. For a login with a backup,
`_poll_saved_logins` reads its saved copy in the same pass, as an idle account (refreshed
through the consume gate when expired), provided `~/.claude.json` still names it (it is the
active account) and the saved copy has a refresh token. A refusal there records the dead-login
state, so the login reads "needs re-login" from then on and its backup stands in. A saved copy
that works proves nothing about the emptied live login: the account keeps "no credentials" and
the usual failover applies. Without a backup nothing of this runs.

The pass in which a login first turns dead (a fetch that just got `invalid_grant`) leaves its
token standing by; the next pass reads it. The views follow the token's own sentinel, so one
pass never shows a state its reading disagrees with.

## The engine

`switchable_account_numbers` leaves out a token standing by, which is the one rule every
census uses: the engine's candidates and its scheduled reads, the all-exhausted decision,
`switch` with no account (all three rotations: from a login, off a key, and with no live
login), `--strategy best`, and the dashboard's "Next best" (which also checks the snapshot's
`backup_state`). A token without its own threshold or priority takes its login's
(`_policy_from_data`).

The engine wraps each tick in the switcher's `check_scope`: the links, the set-aside list and
which logins have ended are worked out once per check, and with no backup no question about
backups reads `sequence.json` again (`switchable_account_numbers` hands over the registry it
loaded). A login that ends during the check counts from the next one.

Two moves are added in `autoswitch.py`, both inert without `backup_links` (Codex) or without
a link:

- `_backup_first`: when the active login needs re-login and its token is among the ranked
  targets and under its own switch lines, the token goes first. Over a line it keeps its place
  in the ranking.
- `_return_from_backup`, right after the key-line branch of `_tick_inner`: when the active
  account is a token whose login works again and is in rotation, the engine needs a
  successful read of the login taken after the move onto the token (`lastSwitchAt`; this
  check's reads when the engine did not make the move). Without one it reads the login now
  (`refresh_usage`) and decides on that. Under every switch line, it freshens the login and
  switches with trigger `backup-return`, which `_perform` does not hold for the cooldown. Over
  a line, unread, disabled or set aside, the normal rules decide.

The auto log names both sides: `backup for #3 work, standing by`, and for the login
`needs re-login, its backup #7 setup-token-7 covers it`; the switch reason is `its login works
again`.

## What every view says

`display.BackupFacts` carries the link into `display.block_from_entry`, which `list`, `status`,
the dashboard (`tui/data.block_for`) and the menu bar (`menubar.account_block`) all use. A token
standing by is a block of its own state, `standing-by`: no windows, no "usage unavailable",
only `token ends about Nov 23, 2027` (in its last month, with the way to a new one). A token
that cannot be used (no saved token, a locked Keychain) shows that state instead, never
"standing by". The header adds `backup for #3 work` and the state in words (`standing by`,
`standing in`, `in use`). A login that needs re-login and has a token that covers it says
`its backup #7 setup-token-7 covers it` and `Bring the login back with: qswap login 3`. The
menu bar's rows name the login by number and alias only (`backup for #3`), to fit. `--json`
rows gain `backupFor` (`accountNumber`, `state`, `added`, `endsAbout`) on the token and
`backupAccountNumber` on the login, and `usageStatus` `standing_by`.

`qswap switch N` to a login that needs re-login names its token and `qswap switch 7`. `qswap
switch 7` to a token standing by says that auto-switch moves back to the login (at its next
check when an engine is running, once one runs otherwise). `qswap login N` that succeeds while
the token stands in adds a note: the token goes back to standing by; when it is still in use
(the active account, or the login underneath a key), that auto-switch moves back at its next
check if it runs, else the switch that does. The menu bar posts one notice when a token starts
standing in (`menubar_notify.backup_notices`, kept with the login notices in
`menubar_state.json` until the login works again, so a state that flaps meanwhile says
nothing twice). `qswap doctor` warns about a link to a login that is gone or changed, a second
backup for one login, and a token in its last month.

## Not verified

- No run against a real setup-token: whether the probe's response carries both windows'
  headers for it, whether its reset times agree with the usage endpoint's to within 10 s and
  its figures to within 5 points, and whether it sends `anthropic-organization-id`, in what
  form.
- That windows start on the hour; the figures are compared because they might.
- That `claude setup-token` makes a token that lasts a year from when it is added here; the
  date shown is an estimate.
- Usage resets, Remote Control and claude.ai connectors with a setup-token as the active
  account.
