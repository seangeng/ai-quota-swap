# qswap

This package only installs [ai-quota-swap](https://pypi.org/project/ai-quota-swap/), the Claude Code and Codex account switcher behind the `qswap` command. It has no code of its own.

```sh
uv tool install qswap
uv tool install 'qswap[menubar]'   # on macOS, with the menu bar app
```

The project's own name is ai-quota-swap, and `uv tool install ai-quota-swap` installs it under that name.

If you installed it as qswap, upgrade with `uv tool upgrade qswap`, which upgrades ai-quota-swap too. `qswap upgrade` in ai-quota-swap 0.1.0 looks for a uv tool named ai-quota-swap, so it fails on an install made under this name.

The documentation is in the repository at https://github.com/seangeng/ai-quota-swap, and the package page is https://pypi.org/project/ai-quota-swap/.

Not affiliated with Anthropic or OpenAI.
