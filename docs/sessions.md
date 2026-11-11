# Run several accounts at once

`qswap run` starts Claude Code as one of your saved accounts in this terminal only, while your active login, every other terminal and the VS Code extension keep theirs. This page covers starting a session, choosing an account per directory with `qswap map`, and what a session shares with your normal `~/.claude` setup.

`qswap run` is experimental, and its own help says so. It covers Claude Code accounts only: Codex has no `qswap codex run` yet (milestone M2c in [../ROADMAP.md](../ROADMAP.md)).

## Start a session

```
$ qswap help run
usage: qswap run [ACCOUNT] [options] [-- CLAUDE_ARGS]

Run Claude Code as a saved account in this terminal only; the active login and
other terminals keep theirs. Experimental.

positional arguments:
  ACCOUNT               number, email or alias; omit for this directory's
                        account (qswap map)

options:
  -h, --help            show this help
  --no-share            do not share settings, CLAUDE.md, skills, commands,
                        agents and rules from ~/.claude with the session
  --share-history, --no-share-history
                        share conversation history with ~/.claude (the default;
                        run.shareHistory sets it for every launch)
  --require-session     refuse when the account is already the active login
  --debug               log debug detail

examples:
  qswap run 2
  qswap run work -- --resume      # arguments after -- go to claude
  qswap run                       # the account mapped to this directory
```

`qswap run 2` starts Claude Code as `#2 work`. ACCOUNT is a number, an email or an alias. Everything after `--` goes to `claude` unchanged, so `qswap run work -- --resume` resumes a conversation as `#2 work`. A `qswap switch` or `qswap auto` in another terminal changes the active login and leaves a running session where it is.

Each account gets a profile of its own (see [Where profiles live](#where-profiles-live)), and qswap starts `claude` with `CLAUDE_CONFIG_DIR` pointing at it. The first launch for an account refreshes its saved login once, copies it into the profile and checks the profile with `claude auth status`. Later launches reuse the profile while it passes that check, and seed it again when it does not. A freshly seeded profile that fails the check is deleted, and qswap tells you to log in with that account and run `qswap add --slot N`.

Before it starts `claude`, a session launch:

- ignores, with a warning, any variable that would replace the account inside Claude Code: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR` and `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`;
- overrides a `CLAUDE_CONFIG_DIR` that is already set, also with a warning.

### When the account is already the active login

Running the account that is already the active login starts plain `claude` on that login, as if you had typed `claude`, and says `... is already the active default login — launching claude directly.` A session would hold a second copy of the same login, and the two copies drift apart as soon as one of them refreshes the token. Scripts that need the separate profile can pass `--require-session`, which refuses instead.

### API-key accounts

Sessions need a subscription login, so an API-key account is refused:

```
$ qswap run 5
Error: #5 ci is an API-key account; qswap run does not support API-key accounts
  yet.
Make it the active login instead: qswap switch 5
```

## Pick the account per directory

`qswap map` binds a directory to an account. A bare `qswap run` there starts that account.

```
$ qswap map 2 /work/client-app
Warning: /work/client-app is not an existing directory (mapping it anyway)
Mapped /work/client-app → #2 work (maya@northwind.example.com)
```

The directory does not have to exist yet; qswap warns and maps it anyway. Without PATH, `qswap map ACCOUNT` maps the directory you are in. With no arguments it lists the mappings:

```
$ qswap map
Directory mappings
  /work → #1 personal (maya@example.com)
  /work/client-app → #2 work (maya@northwind.example.com)
```

A bare `qswap run` uses the nearest mapped directory at or above the one you are in: with these two mappings it starts `#2 work` in `/work/client-app/src` and `#1 personal` in `/work/notes`. In a directory with no mapping it prints `No account mapped for DIR — launching the default account.` and starts plain `claude` on the active login, with your environment unchanged. It does the same, with a warning, when the mapped account is no longer managed.

```
$ qswap unmap /work/client-app
Unmapped /work/client-app
```

`qswap unmap` without a path removes the mapping for the directory you are in. It removes an exact match only, never a parent's mapping.

Paths are stored absolute, with `~` expanded and symlinks resolved (case-folded on Windows). A mapping names its account by email and organization rather than by number, so `qswap swap` and `qswap move` leave it on the same account, and `qswap remove` deletes the account's mappings and says how many. Mappings live in `mappings.json` in the data directory and are not part of `qswap export`.

## What a session shares

| From `~/.claude` | In a session | Turned off by |
|---|---|---|
| `settings.json`, `keybindings.json`, `CLAUDE.md`, `skills/`, `commands/`, `agents/`, `rules/` | linked to yours | `--no-share` |
| `projects/` (conversations) and `history.jsonl` (prompt history) | linked to yours | `--no-share-history`, or `run.shareHistory false` |
| User-scope MCP servers (`mcpServers` in `~/.claude.json`) | copied in on every launch | `--no-share` |
| Logins, MCP OAuth logins, `plugins/`, the rest of `~/.claude.json` | not shared | |

Sharing always reads from `~/.claude`, the default profile, even when `CLAUDE_CONFIG_DIR` is set in your shell.

### Settings, CLAUDE.md, skills, commands, agents and rules

These are links, so a session uses your setup as it is, and a change made inside the session (a `/config` change, say) lands in `~/.claude`. Windows differs, see [On Windows](#on-windows). `--no-share` removes these links and the copied MCP servers; its help names neither `keybindings.json` nor the MCP servers, but both are covered. The next launch without the flag puts them back. If a profile already has its own copy of one of these, qswap leaves it alone and says `Not sharing NAME: the session profile already has its own copy.`

### Chat history

Shared by default. Every session, and plain `claude` on the active login, reads and writes the one history in `~/.claude`, so a conversation started as one account shows up in `claude --resume` under the others. History is shared by link only, never by copy, because a copy would split it in two.

To give each account its own history, run `qswap config set run.shareHistory false`, or pass `--no-share-history` for one launch (`--share-history` turns sharing on for one launch). Each account then keeps its history in its profile. Turning sharing off removes the links; the conversations already in `~/.claude` stay there.

When a profile holds history of its own (from launches with sharing off), the next launch with sharing on merges it into `~/.claude` before linking it:

- A file only the profile has moves to `~/.claude`.
- A file that `~/.claude` already holds in full is dropped from the profile.
- When the two sides hold different versions of a file, one stays in `~/.claude` (for a conversation, usually the one with the later last message; for any other file, the one already there) and the other moves to `history-conflicts/` in the data directory, in a folder named for the time and the profile. The launch says how many files it kept aside. Nothing is deleted.
- `~/.claude/history.jsonl` gains the profile's prompt lines it did not have.

The merge waits while Claude Code runs in that profile: the launch says the account keeps its own history for now, lists the running sessions, and prints the command to relaunch once they are closed. It also runs when `qswap run` starts plain `claude` because the account is the active login.

### MCP servers

User-scope MCP servers (`claude mcp add -s user`) are copied from `~/.claude.json` into the profile on every launch. Your default profile is the source: additions, edits and removals there reach a session on its next launch, and a change made inside a session is replaced on the next launch and never flows back. Per-project entries are left alone on both sides. Definitions are copied as they are, inline `env` and `headers` values included. MCP OAuth logins are not copied from your default profile, so a server that uses one may ask you to log in through `/mcp` once per profile. The first time a profile gets the copy, servers it had defined itself are saved to `.qswap-mcp-displaced.json` in the profile. `--no-share` removes the copied servers.

### On Windows

Directories (`skills`, `commands`, `agents`, `rules`, `projects`) are shared through directory junctions, which need no special rights. A file needs a symbolic link, which Windows allows only with Developer Mode on (Settings > System > For developers) or in an elevated terminal. Without one:

- `settings.json`, `keybindings.json` and `CLAUDE.md` are copied into the profile on every launch, so a change made inside a session does not reach `~/.claude` and is replaced on the next launch.
- `history.jsonl` is never copied. Prompt history stays per account, and the launch says `share-history: history.jsonl stays per-account on this Windows machine.` Conversations (`projects`) are still shared.

A link made once, in an elevated terminal for example, keeps working afterwards. On a volume without junctions (not NTFS), `projects` stays per account too. On Windows qswap keeps running while Claude Code runs and exits with its exit code; on macOS and Linux it hands the process over to `claude`.

### Tokens

A session holds its own copy of the account's login, and Claude Code refreshes it there. A refresh replaces the token, so the copy qswap saved falls behind. Once the session has exited, qswap takes the newer login from the profile into its saved copy before a switch, `swap`, `move` or usage read uses it.

While the session is still running, `qswap switch` to that account refuses if the saved copy has already fallen behind, because activating it would fail on its first refresh: exit the session first, or switch to another account. If the saved copy has not fallen behind, the switch goes ahead with a warning that the account now runs twice and one copy can stop working after the next refresh; if the session then fails to log in, exit it and run `qswap run N` again.

The other way round, when qswap's saved copy changes (a new `qswap add`, an import, a refresh), a profile with no session running drops its login, and the next `qswap run` copies the new one in. A running session keeps its own until it exits.

### Usage while a session runs

qswap reads the account's usage with the session's own token and never refreshes that token. When the server refuses it, the account shows `token expired`, and qswap does not ask again until the session has renewed the token on its next request.

## Where profiles live

Each profile is a folder in `sessions/` in the data directory, named for the account's number and email, for example `sessions/2-maya_northwind.example.com/` (characters other than letters, digits, `.`, `_` and `-` become `_`). The data directory is `~/.ai-quota-swap` on macOS and Windows, and `${XDG_DATA_HOME:-~/.local/share}/ai-quota-swap` on Linux and WSL.

A profile starts with a plaintext `.credentials.json` holding the account's login; on macOS, Claude Code moves it into a Keychain entry of its own for that profile the first time it writes. The profile also holds its `.claude.json`, the links above, `.qswap-shared.json` (the record of what qswap linked or copied, so it never removes anything else), and whatever Claude Code writes there. With history sharing off, that includes the account's conversations, and they go with the profile when it is deleted.

- `qswap remove` deletes the account's profile; `swap` and `move` carry it to the account's new number. All three refuse while a session runs on that account.
- `qswap purge` deletes every profile, and refuses while any session runs.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `run.shareHistory` | `true` | Sessions share one chat history in `~/.claude`. `false` gives each account its own, kept in its profile. `--share-history` and `--no-share-history` override it for one launch. |

Change it with `qswap config set run.shareHistory false`. Every setting is in [settings.md](settings.md).

## Limits and caveats

- `qswap run` is experimental.
- Claude Code only. API-key accounts are refused, and Codex has no `qswap codex run` yet.
- `run` has to be the first word: `qswap run 2 --debug` works, `qswap --debug run 2` does not.
- Commands that change accounts (`switch`, `add`, `add-token`, `remove`, `alias`, `swap`, `move`, `purge`) refuse in a shell inside a session, where `CLAUDE_CONFIG_DIR` points at a profile. That includes commands Claude Code runs for you in the session. Run them from a normal terminal.
- The `claude auth status` check is local. A login that was revoked but has not expired passes it and fails on first use.
