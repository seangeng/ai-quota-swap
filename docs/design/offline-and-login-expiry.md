# Design: offline, and logins that end

Status: accepted, 2026-11-23.

Two things the auto-switch engine used to ignore: a machine without a network, and the date each saved login ends. The user-facing rules are in [auto-switch.md](../auto-switch.md#when-something-fails) and [auto-switch.md](../auto-switch.md#logins-that-end); this page says why they are the way they are.

## What went wrong

One owner's log, 13 days:

- The machine had no working network for 12 hours. The engine wrote "Active token expired and owned for over 30 minutes; resuming unhealthy counting (dead refresh token?)" once a minute, 1,650 times in all. Nothing was wrong with the token.
- It wrote "Usage fetch failed for account N: network" 1,106 times, and the Codex line 354 times, and counted those failures toward failover.
- Every failure read `network`, so the log could not tell a DNS failure from a refused connection or a TLS error.
- Two accounts stopped being read one afternoon and the log never said why.

## Offline

A check is offline when no usage read worked and one failed without an answer from the server: `network` or `timeout`. A failure the server answered (an HTTP status, a body it sent) proves the network works, whatever it says about the account. A failed token refresh is not a usage read, and the token host is not the usage host, so it says nothing either way.

One failed read is not enough. A lone timeout is common; an engine that wrote "Offline" and "Back online" a minute apart for it, or held back failover for it, would be worse than one that ignored the network. So reads that fail are checked against the usage host itself:

- **The probe** is a TCP connection to the usage host (through the HTTPS proxy when one is set), bounded as a whole, name lookup included, and its answer reused for 55 seconds: each 60-second check asks once at most. It is a connection, not a read, so it never ends an episode on its own while a read it failed is still waiting.
- Reads fail and the probe fails: offline at once.
- Reads fail and the probe succeeds (a captive portal, a proxy that accepts and stalls): offline from the second check in a row.
- No read was due: the probe decides, but a read that worked in the last 5 minutes outranks it. A probe that says "down" while reads work would otherwise hold back failover on a dead account.

While offline the engine counts nothing toward failover, and the idle hold's 30-minute clock does not run, since no refresh could have worked meanwhile. The unhealthy count is kept rather than reset, so a probe that is wrong only delays failover. The check emits a `no-switch` event with reason `offline`.

The log gets one line when an episode starts and one when it ends. In between, a logging filter holds back each account's repeated network failure lines; an answer from the server, such as a 429 with its Retry-After, always gets through. The episode, and the accounts whose line is written, are kept in the state file, so `qswap auto --once` from cron shares one episode and installs the filter before its own reads. A record not touched for an hour is dropped, never reported as "back online after 7 days".

### The backoff, and the lift

Each account's failure backoff (30 seconds, doubling, up to 10 minutes) already keeps the engine from reading an account more than about once a minute while offline. What it does not do is recover: at its cap, the first read after the network returns can wait 10 minutes.

So when the probe succeeds after it failed during the episode, the reads that found no network at all are let through at once, one time per such transition, and only for rows last tried at least 50 seconds ago. Never a timeout: a host that takes the connection and does not answer is not back, and lifting its rows every check turned two hours of a half-open network into 88 reads per account, against 15 on the backoff alone. Never a 429 or any answer from the server. The failure count stays, so a read that fails again backs off from where it was.

## Accounts that stop being read

One log line when an account stops being readable for a reason that will not clear by itself (a refused refresh with the server's code, a 401 on a token renewed moments before, a login past its date that the server refuses, the Keychain, a foreign live login), with the step that fixes it, and one line once a read works again. Network trouble, rate limits and an idle expired token are not such reasons. The log names accounts by number, as its fetch lines do.

## Logins that end

A saved login records the date it ends (`refreshTokenExpiresAt`). The engine treats it as a preference, never an exclusion, for two reasons:

- The server has not been seen to hold to the date. qswap logs the server's `refresh_token_expires_in` after each refresh it makes, next to the saved date, to find out whether the date is fixed or moves.
- A refresh never rewrites the saved date, so it goes stale on logins that keep working. A fleet where every date has passed is normal, and must behave as if the setting were off.

The rules:

- **Choosing.** A candidate whose login ends within 2 hours, or whose date has passed, ranks behind every other, in every strategy: a layer above the strategy's own order. It is still chosen when nothing else qualifies.
- **Leaving.** In the 15 minutes before its date, the active account is left for an account with room under its lines whose login does not end within 2 hours (trigger `login-expiry`), after the cooldown. When there is none, or the move cannot land, the check does what usage alone would have done: stays (saying why, reason `login-ending`) or makes the switch usage calls for. No reads are spent on a move that cannot happen. A date already passed is not a reason to leave: the account either works, or the failover path handles it.
- **No ping-pong.** A move by choice (at the line, to a sooner reset, to a higher tier) never lands on a login inside those 15 minutes, which the next check would leave again. Escapes (at the limit, unreadable) may.

The deadline comes from the credentials the collector already reads, so no Keychain read is added; a degraded read of the live login (a fallback copy that may predate a renewal) gives no date. `autoswitch.watchLoginExpiry` turns both rules off.
