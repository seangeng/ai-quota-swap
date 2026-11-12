# qswap

This package only installs [ai-quota-swap](https://pypi.org/project/ai-quota-swap/), the Claude Code and Codex account switcher behind the `qswap` command. It has no code of its own.

```sh
uv tool install qswap
uv tool install 'qswap[menubar]'   # on macOS, with the menu bar app
```

The project's own name is ai-quota-swap, and `uv tool install ai-quota-swap` installs it under that name.

If you installed it as qswap, `qswap upgrade` upgrades it from ai-quota-swap 0.1.1 on. In 0.1.0 it looks for a uv tool named ai-quota-swap and fails on an install made under this name, so there use `uv tool upgrade qswap`, which upgrades ai-quota-swap too.

The documentation is in the repository at https://github.com/seangeng/ai-quota-swap, and the package page is https://pypi.org/project/ai-quota-swap/.

Not affiliated with Anthropic or OpenAI.
