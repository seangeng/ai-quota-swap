# ai-quota-swap documentation

Start with the [quick start](quickstart.md). Each page below opens with what it lets you do. Every screenshot and recording in these pages shows made-up accounts from the [demo harness](../scripts/demo/README.md).

## Get started

| Page | Read it to |
|---|---|
| [quickstart.md](quickstart.md) | install qswap, save two accounts, see their usage and switch |
| [coming-from-claude-swap.md](coming-from-claude-swap.md) | move your accounts over from claude-swap |

## Watch your accounts

| Page | Read it to |
|---|---|
| [menubar.md](menubar.md) | run the macOS menu bar app: every part of its menu, its settings and auto-switch, starting at login, quitting and uninstalling |
| [dashboard.md](dashboard.md) | use the terminal dashboard: every screen and key |

## Switch accounts

| Page | Read it to |
|---|---|
| [auto-switch.md](auto-switch.md) | let qswap switch before the active account runs out, pick a strategy and a line, run it from cron or as a service |
| [codex.md](codex.md) | keep several Codex logins, add one without signing another out, and know what a switch does to running sessions |
| [sessions.md](sessions.md) | run two accounts at once in different terminals, and pick an account per directory |

## Quota and cost

| Page | Read it to |
|---|---|
| [resets.md](resets.md) | know what each kind of usage reset clears, when auto mode spends one, and how to spend or keep one yourself |
| [usage.md](usage.md) | see tokens and their estimated cost by day, account, model or project, and set your own prices |
| [api-accounts.md](api-accounts.md) | add API keys as accounts, move running sessions onto one and back, see what they spent, and hold them to a monthly budget or a prepaid credit |

## Settings, scripts and problems

| Page | Read it to |
|---|---|
| [settings.md](settings.md) | see and change every setting |
| [json-output.md](json-output.md) | script against `--json` output and exit codes |
| [troubleshooting.md](troubleshooting.md) | fix what `qswap doctor` reports, and the problems people hit |

## Security

[SECURITY.md](../SECURITY.md) lists what qswap stores, which endpoints it calls, how to report a vulnerability and how to remove everything.

## Contributing

| Page | What it covers |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | setup, running qswap against invented accounts, tests, lint |
| [architecture.md](architecture.md) | a map of the code and the data directory |
| [design/](design/) | the reasoning behind the larger decisions: [providers](design/providers.md), [Codex](design/codex.md), [the ledger](design/ledger.md), [API accounts](design/api-accounts.md), [output and wording rules](design/ux.md) |
| [../scripts/demo/README.md](../scripts/demo/README.md) | the demo harness that makes every screenshot and recording from invented accounts |
