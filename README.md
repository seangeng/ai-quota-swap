# ai-quota-swap

If you have more than one Claude Code or Codex account, `qswap` keeps them all on one machine, shows how much of each one's 5-hour and weekly quota is left, and switches to another before the one you are on runs out.

<picture>
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/screenshots/raw/hero-light.png">
  <img src="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/screenshots/raw/hero.png" alt="The qswap menu bar with its menu open, beside the terminal dashboard: Claude Code and Codex accounts with their 5-hour and weekly usage, held usage resets, an API key's spend against its budget, and today's estimated cost" width="900">
</picture>

Every screenshot and recording on this page shows made-up accounts and comes from the [demo harness](https://github.com/seangeng/ai-quota-swap/blob/main/scripts/demo/README.md).

## What you get

### The menu bar (macOS)

The menu bar shows the active account and how much of its 5-hour and weekly windows it has used, as `⇄ personal · 46% · 38%`. Its menu lists every Claude Code and Codex account with usage, held resets and today's cost. Click an account to switch to it, or turn on auto-switch and let it switch for you.

<img src="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/demos/menubar.gif" alt="Recording: the menu bar's title shows the active account's weekly figure as it climbs; auto-switch moves to the next account and the title changes; then the menu opens and shows why" width="800">

Every row of the menu, its settings and how to start it at login: [docs/menubar.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/menubar.md).

### Switching before the limit

`qswap auto` checks the active account every minute. When a window reaches 90%, it moves to the account with the most room left, and running Claude Code sessions carry on with the new account. `qswap auto --dry-run` says what it would do and changes nothing.

<picture>
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/screenshots/raw/auto-light.png">
  <img src="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/screenshots/raw/auto.png" alt="qswap auto: two checks of #1 personal below its 90% line, then a switch to #2 work when its 5h window reaches 91%" width="800">
</picture>

Strategies, switch lines, cron and running it as a service: [docs/auto-switch.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/auto-switch.md).

### Usage resets

Some plans come with banked usage resets (Claude Code's `/limit-reset`), and each kind clears different windows: a Fable reset clears Fable's weekly window only, a weekly reset the all-models weekly window. `qswap reset` lists what every account holds and spends one. While auto-switch runs, it spends the narrowest reset that helps once no other account can take over, at most 3 a day for each provider.

<picture>
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/screenshots/raw/reset-light.png">
  <img src="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/screenshots/raw/reset.png" alt="qswap reset: #1 personal holds a Fable reset and a weekly reset; qswap reset 1 --yes spends the Fable one and Fable drops to 0%" width="800">
</picture>

The kinds, when auto mode spends each, and the guards: [docs/resets.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/resets.md).

### What it costs

`qswap usage` prices the tokens in this machine's Claude Code and Codex session files at API list prices, by day, account, model or project; a subscription pays a flat price, so for one the figure is the API value of what you used. A Console API key can be an account too, held to a monthly budget (`qswap budget 5 40`) or a prepaid credit. With `autoswitch.includeApiKeyAccounts` on, auto-switch moves to such a key only when no subscription can take over.

<img src="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/screenshots/raw/menubar-key.png" alt="The menu bar's menu with an API-key account active: what it spent today and this month against its budget, and the subscription logged in underneath it" width="800">

Reports and prices: [docs/usage.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/usage.md). Keys, budgets and credits: [docs/api-accounts.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/api-accounts.md).

### The dashboard and the command line

Bare `qswap` in a terminal opens a dashboard of every account, on macOS, Linux and Windows: `s` switches, `w` watches every account, `g` opens auto-switch and `u` shows tokens and cost. Each view is also a command (`qswap list`, `qswap status`, `qswap usage`), with `--json` for scripts.

<img src="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/demos/dashboard.gif" alt="Recording: the dashboard's main view; w opens the watch page, g the auto view, u the usage screen, and esc goes back to the main view" width="800">

Every screen and key: [docs/dashboard.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/dashboard.md).

## Install

ai-quota-swap is not on PyPI yet, so install it from GitHub with [uv](https://docs.astral.sh/uv/). On a Mac, install it with the menu bar:

```bash
uv tool install 'ai-quota-swap[menubar] @ git+https://github.com/seangeng/ai-quota-swap'
qswap menubar    # start the menu bar now and at every login
```

On Linux (WSL included) or Windows:

```bash
uv tool install git+https://github.com/seangeng/ai-quota-swap
```

pipx takes the same arguments (`pipx install git+https://github.com/seangeng/ai-quota-swap`). You need Python 3.12 or newer, and Claude Code logged in; Codex is optional. `qswap upgrade` updates qswap with whichever tool installed it (on Windows it prints the command to run).

## Quick start

<img src="https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/assets/demos/quickstart.gif" alt="Recording: a terminal in which qswap add saves the Claude Code login, qswap list shows every account and its usage, qswap switch 2 makes #2 the active account, and qswap status shows the active accounts" width="800">

Log in to Claude Code with your first account, then save that login:

```text
$ qswap add --alias personal
Added #1 personal (maya@example.com)
To add another account, log in to it in Claude Code (/login), then: qswap add
```

In Claude Code, run `/login` with your next account, then `qswap add --alias work`. Go straight to `/login`: a `/logout` first may revoke the saved login of the account you leave.

`qswap list` shows every account's usage. Switch by number, email or alias:

```text
$ qswap switch 2
Switched #1 personal → #2 work (maya@northwind.example.com)
...
Claude Code picks up the new account within 30 seconds; restart it to apply now.
```

Running Claude Code sessions keep working and move to the new account: within about 30 seconds on macOS, from their next message on Linux and Windows. `qswap status` shows the active account of each provider and whether auto-switch is running.

To let qswap switch for you, run `qswap auto`, or turn on the menu bar's auto-switch, which starts off: `qswap menubar --auto-switch`. When something looks wrong, run `qswap doctor`: it checks everything qswap needs and prints the fix for each problem. The [quick start guide](https://github.com/seangeng/ai-quota-swap/blob/main/docs/quickstart.md) has each step in more detail.

## Everything else

| To | Run | Read |
|---|---|---|
| keep several Codex logins | `qswap codex add`, then `qswap codex login` (not plain `codex login`) for each further account | [codex.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/codex.md) |
| run Claude Code as another account in one terminal only | `qswap run 2` | [sessions.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/sessions.md) |
| pick the account for a directory | `qswap map 2 ~/work/client-app` | [sessions.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/sessions.md#pick-the-account-per-directory) |
| script it | `--json`, and `qswap auto --once` for cron, with exit codes | [json-output.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/json-output.md) |
| see or change a setting | `qswap config`, `qswap config set autoswitch.threshold 85` | [settings.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/settings.md) |
| fix a problem | `qswap doctor` | [troubleshooting.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/troubleshooting.md) |
| move over from claude-swap | `cswap export FILE`, then `qswap import FILE`; never run both on the same accounts | [coming-from-claude-swap.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/coming-from-claude-swap.md) |
| remove everything qswap stored | `qswap purge` | [SECURITY.md](https://github.com/seangeng/ai-quota-swap/blob/main/SECURITY.md#remove-everything) |
| find any command | `qswap help`, `qswap help COMMAND` | [all the docs](https://github.com/seangeng/ai-quota-swap/blob/main/docs/README.md) |

## What works and what is new

- The menu bar is macOS only. It has been in daily use on the author's Mac since 2026-11-10.
- Codex support is experimental. It has been tested against fakes and with one real account; switching between two real Codex accounts has not been tried yet. Read [docs/codex.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/codex.md) before your first Codex switch.
- `qswap run` is experimental, and covers Claude Code only.
- Switching to an API key is new. It moves every running Claude Code session onto the key and keeps the subscription logged in underneath:

```text
$ qswap switch 5
Switched #1 personal → #5 ci (API key)
Running Claude Code sessions move to the key within a few seconds. #1 personal
stays logged in underneath. claude.ai connectors and Remote Control are
unavailable while a key is in use. Go back with: qswap switch 1
```

The switch adds one line to Claude Code's settings (`"apiKeyHelper": ".../qswap key-helper 5"`), and `qswap switch 1` takes it out. It has been tested on Claude Code 2.1.322 with sessions in print mode (`claude -p`), not yet with an interactive session or with a claude.ai login underneath ([what has not been tested](https://github.com/seangeng/ai-quota-swap/blob/main/docs/api-accounts.md#limits)).

## What it stores and what it talks to

Saved logins go in the macOS login Keychain, or in files in qswap's data directory on Linux and Windows. Everything else lives in that directory: `~/.ai-quota-swap`, or `~/.local/share/ai-quota-swap` on Linux.

qswap talks to Anthropic and OpenAI with each account's own token, and once a day asks PyPI whether a newer version exists. It makes no other network calls and has no server of its own. Usage readings and reset spends go through endpoints the vendors do not document, which can change without notice. Reading reset status identifies as Claude Code, because Anthropic reports resets only to Claude Code.

[SECURITY.md](https://github.com/seangeng/ai-quota-swap/blob/main/SECURITY.md) lists every file and endpoint, and how `qswap purge` removes everything.

## Contributing

[CONTRIBUTING.md](https://github.com/seangeng/ai-quota-swap/blob/main/CONTRIBUTING.md) covers setup, running qswap against invented accounts, the tests and the lint. The code map is in [docs/architecture.md](https://github.com/seangeng/ai-quota-swap/blob/main/docs/architecture.md). What changed is in the [changelog](https://github.com/seangeng/ai-quota-swap/blob/main/CHANGELOG.md), what comes next in the [roadmap](https://github.com/seangeng/ai-quota-swap/blob/main/ROADMAP.md). Report security problems privately, as [SECURITY.md](https://github.com/seangeng/ai-quota-swap/blob/main/SECURITY.md) describes.

## License

MIT. See [LICENSE](https://github.com/seangeng/ai-quota-swap/blob/main/LICENSE).

Built on [claude-swap](https://github.com/realiti4/claude-swap) by Onur Cetinkol.
