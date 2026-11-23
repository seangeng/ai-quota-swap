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
record adds two keys: `backupFor`, the login's account number, and `backupAdded`, when it was
added (ISO 8601). `backup_token.py` is the pure model: `links` (token to login, only when the
login is a managed subscription login, not an API key and not a token), `dangling`,
`backups_by_parent`, `renumber`, `unlink`, the dates (`ends_at` is a year after
`backupAdded`; `due` is the last 30 days) and the identity check (`compare`).

One backup per login. A second `--backup-for N` asks, then writes the new token into the first
one's slot; a token already saved as its own account is linked where it is (the old backup, if
any, is removed). `--slot` is refused with `--backup-for`. A backup that is the active account
is not replaced: switch away first, or the live copy would later be saved back over the new
token.

The link follows its login: `swap` and `move` renumber it, `add --slot` over the login's slot
unlinks it, and `remove` of the login unlinks it and says so. Export writes `backupFor` (the
login's number in the file) and `backupAdded`; import maps it to wherever that login landed
(imported, overwritten, or already here) and drops it with a note otherwise. An export
without the field keeps a link the overwritten slot had.

## The identity check

Before anything is saved, one probe with the token (`oauth.probe_token_account`, which also
returns the `anthropic-organization-id` header when the response has one) is compared with the
login's reading: a fresh read when the login works, its last stored reading when it has ended.
A window counts on the login's side only while its reset is ahead (an ended window says nothing
about the next, and the probe may have started one); two resets agree within
`RESET_TOLERANCE_S` (120 s: the endpoint stamps resets with sub-second jitter, the headers carry
whole seconds). Then:

- an organization header that differs from the login's organization refuses ("another
  organization"); one that matches proves nothing by itself, since a team's members share an
  organization;
- any window that disagrees refuses ("its weekly window resets at another time");
- at least one window agreeing, and none disagreeing, accepts;
- nothing to compare refuses unless `--unverified`, which saves it with a warning that names
  the risk: another account's quota spent under this login's name. `--unverified` does not
  override a disagreement;
- a 401 or 403 refuses ("Anthropic refused the token"); a network failure counts as nothing to
  compare.

The 5-hour window alone is a weaker match than both together. The check guards against a
mistake (the wrong browser profile at `claude setup-token`), not against a forger.

## States

`backup_token.state`: `standing-by` (the login works and the token is not the active account),
`standing-in` (the login needs re-login), `in-use` (the token is the active account while the
login works). The login has ended when it needs re-login for an auth reason: the engine set it
aside (`set_aside_accounts`) or its saved login was refused (the usage store's dead-login
state, checked against the stored credential: `_ended_logins`). A passed end date alone does
not count: an account that still reads is still used, as everywhere else.

## The collector

`ClaudeAccountSwitcher._place_backup_tokens` runs in every collect pass, after the static and
dead-login sentinels. A token standing by gets the `standing by` sentinel
(`json_output.USAGE_STANDING_BY`), so it is never reserved, fetched or probed; its plan is never
asked for either (`_collect_subscription_plans` skips every linked token). Any other token is
read with `header_probe=True`, threaded through `_run_usage_fetches` to the three fetch paths,
whatever `usage.headerProbe` says. A login the pass reads is judged by the pass's own "re-login
needed"; another by the usage store. The lockstep warning skips a token and its login.

The pass in which a login first turns dead (a fetch that just got `invalid_grant`) leaves its
token standing by; the next pass reads it. The views follow the token's own sentinel, so one
pass never shows a state its reading disagrees with.

## The engine

`switchable_account_numbers` leaves out a token standing by, which is the one rule every
census uses: the engine's candidates and its scheduled reads, the all-exhausted decision,
`switch` with no account, `--strategy best`, and the dashboard's "Next best" (which also checks
the snapshot's `backup_state`). A token without its own threshold or priority takes its
login's (`_policy_from_data`).

Two moves are added in `autoswitch.py`, both inert without `backup_links` (Codex) or without
a link:

- `_backup_first`: when the active login needs re-login and its token is among the ranked
  targets, the token goes first. Ranking still decides whether it qualifies (it must have a
  reading; the pass that finds the login dead escalates and reads every candidate, the token
  included).
- `_return_from_backup`, right after the key-line branch of `_tick_inner`: when the active
  account is a token whose login works again, is in rotation and reads under every switch
  line, the engine freshens the login and switches to it with trigger `backup-return`, which
  `_perform` does not hold for the cooldown. Over a line, unread, disabled or set aside, the
  normal rules decide. This also applies to a token switched to by hand while its login works.

The auto log names both sides: `backup for #3 work, standing by`, and for the login
`needs re-login, its backup #7 setup-token-7 covers it`; the switch reason is `its login works
again`.

## What every view says

`display.BackupFacts` carries the link into `display.block_from_entry`, which `list`, `status`,
the dashboard (`tui/data.block_for`) and the menu bar (`menubar.account_block`) all use. A token
standing by is a block of its own state, `standing-by`: no windows, no "usage unavailable",
only `token ends about Nov 23, 2027` (in its last month, with the way to a new one). The
header adds `backup for #3 work` and the state in words (`standing by`, `standing in`,
`in use`). A login that needs re-login and has a token says `its backup #7 setup-token-7 covers
it` and `Bring the login back with: qswap login 3`. The menu bar's rows name the login by
number and alias only (`backup for #3`), to fit. `--json` rows gain `backupFor` (`accountNumber`,
`state`, `added`, `endsAbout`) on the token and `backupAccountNumber` on the login, and
`usageStatus` `standing_by`.

`qswap switch N` to a login that needs re-login names its token and `qswap switch 7`. `qswap
login N` that succeeds while the token stands in adds a note: the token goes back to standing
by, or, when it is the active account, that auto-switch moves back at its next check. The menu
bar posts one notice when a token starts standing in (`menubar_notify.backup_notices`, kept
with the login notices in `menubar_state.json` and dropped when the token stands by again).
`qswap doctor` warns about a link to a login that is gone and about a token in its last month.

## Not verified

- No run against a real setup-token: whether the probe's response carries both windows' reset
  headers for it, whether they agree with the usage endpoint's reset times to within two
  minutes, and whether it sends `anthropic-organization-id`.
- That `claude setup-token` makes a token that lasts a year from when it is added here; the
  date shown is an estimate.
- Usage resets, Remote Control and claude.ai connectors with a setup-token as the active
  account.
