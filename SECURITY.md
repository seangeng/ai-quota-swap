# Security

qswap holds working logins for your AI accounts, so this page says what it stores, what it sends where, how to report a problem, and how to remove everything. Everything it keeps stays on your machine. qswap runs no server of its own and collects no telemetry.

## What qswap stores

The data directory is `~/.ai-quota-swap` on macOS and Windows, and `$XDG_DATA_HOME/ai-quota-swap` (default `~/.local/share/ai-quota-swap`) on Linux and WSL. On macOS and Linux the directory is owner-only (0700), and every file that holds a login is owner-only (0600). Windows has no such modes, so there the files are protected by your user profile's permissions.

- **Saved Claude Code logins**, including Console API keys added with `qswap add-token`. On macOS they are Keychain items under the service `ai-quota-swap`, written with the system `security` tool. The secret goes to `security` on standard input; a login too long for that goes on its command line, hex-encoded. When the Keychain is unusable (locked, or over SSH), and always on Linux, WSL and Windows, each login is a file in `credentials/`. These files are base64-encoded, not encrypted.
- **`qswap run` profiles.** Each profile in `sessions/` is seeded with a plaintext `.credentials.json` holding that account's login, a file Claude Code reads on every platform. On macOS, Claude Code migrates it into its own Keychain entry the first time it writes.
- **Codex logins**: byte-for-byte copies of Codex's `auth.json` in `codex/credentials/`, one 0600 file per account, on every platform. qswap never refreshes a Codex login and never runs `codex logout`.
- **Admin API keys**, only if you register an organization with `qswap api org add`. They are stored like saved logins (the Keychain item `admin-org-<name>` on macOS, otherwise a base64 file in `credentials/`). `api_orgs.json` lists the organizations and holds no keys. qswap sends only GET requests with an Admin key and refuses any other method before a request is made. `ANTHROPIC_ADMIN_KEY` uses a key for one command without storing it.
- **The login in use** stays where Claude Code and Codex keep it: Claude Code's Keychain item or `~/.claude/.credentials.json`, and Codex's `auth.json`. A switch writes the chosen account's login there.
- **Exports.** `qswap export` writes logins as plain JSON to the file you name. Encrypt it or delete it.

Anyone who can read your user account's files, or use your unlocked Keychain, can use these logins. qswap's storage is as strong as the user account it runs under, and no stronger.

## What qswap sends, and where

qswap talks to Anthropic, to OpenAI for Codex accounts, and to PyPI. Every request to Anthropic or OpenAI carries the token of the account it is about.

To read usage and spend usage resets, qswap calls the private endpoints the official clients use. None of them is part of a published API:

- Anthropic: `GET api.anthropic.com/api/oauth/usage` (usage, and which resets an account holds), `GET api.anthropic.com/api/oauth/profile` (identity and plan), `POST platform.claude.com/v1/oauth/token` (renewing a saved login that is not in use), and `POST api.anthropic.com/api/organizations/{org}/reset_rate_limits` (spending a reset).
- OpenAI: `GET chatgpt.com/backend-api/wham/usage`, `GET chatgpt.com/backend-api/wham/rate-limit-reset-credits` and `POST chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume`.

What that means for you:

- These endpoints can change or disappear without notice. When one does, the readings it supplied show as unavailable until qswap is updated.
- Anthropic reports resets only to Claude Code, so qswap reads them with Claude Code's own client agent string. Anthropic sees those reads as coming from Claude Code.
- Spending a reset cannot be undone. Automatic spending waits for a fresh reading, spends at most 3 a day for each provider (Claude Code and Codex count separately), and sends an idempotency key so a retry cannot spend a second one.
- Whether this use fits the terms of your plan is your decision. Read them.

Two documented Anthropic APIs are used only when you turn them on: the Admin API (GET requests under `api.anthropic.com/v1/organizations/`) for organizations you registered, and, with `usage.headerProbe true`, a one-token Haiku request to `api.anthropic.com/v1/messages` that reads a setup-token's usage from the response headers. Each probe is a real request on that account.

At most once a day, after a command such as `qswap list`, qswap asks PyPI (or uv, for uv installs) whether a newer version exists. A failed check is retried after an hour. It never runs with `--json`, and `qswap config set updates.check false` turns it off.

qswap makes no other network calls. `qswap usage` reads local session files only. The programs qswap starts make their own: `claude` (for `qswap run`), `codex login` and Codex's background-server restart, and uv or pipx for `qswap upgrade`.

## Report a vulnerability

Report it privately through GitHub: open the [Security tab](https://github.com/seangeng/ai-quota-swap/security) and choose "Report a vulnerability", or go straight to [a new advisory](https://github.com/seangeng/ai-quota-swap/security/advisories/new). Do not open a public issue. Include `qswap --version`, your platform, and the steps that show the problem. Never include a real token or key; a redacted one shows the shape well enough.

Fixes are made on `main` and ship in the next release.

## Remove everything

```bash
qswap purge
```

It lists what it will delete and asks first. It removes every saved login (Keychain items and files), Codex logins, `qswap run` profiles and their Keychain entries, stored Admin keys, and the data directory. On macOS it also removes the menu bar's LaunchAgent, wrapper app and logs. It refuses while a `qswap run` session is still running.

It leaves the logins Claude Code and Codex are using now (log out there if you want those gone), any export files you wrote, and one empty lock file in `~/.claude`. Then uninstall the tool with `uv tool uninstall ai-quota-swap` or `pipx uninstall ai-quota-swap`.
