"""Where the Claude Code and Codex commands are: the one place qswap asks.

Every "is the CLI installed" question (the empty-state next step in `list`
and `status`, `qswap doctor`, finding `claude` to launch or to read its
version, finding `codex` to run or restart) goes through :func:`find_cli`,
so a test, or the demo harness, can decide the answer in one place instead
of depending on the PATH of the machine it runs on. ``tests/conftest.py``
gives every test a fixed answer (both found); a test that needs "not
installed" patches this module's ``find_cli``.

Callers look the function up on the module when they call it
(``clis.find_cli("claude")``), never ``from ai_quota_swap.clis import
find_cli``, so a patch reaches them.
"""

from __future__ import annotations

import shutil

#: Provider id → the command it runs.
COMMANDS = {"claude": "claude", "codex": "codex"}


def find_cli(name: str) -> str | None:
    """The full path of the ``claude`` or ``codex`` command on PATH, or None."""
    return shutil.which(name)


def cli_found(provider: str) -> bool:
    """Whether a provider's command (``"claude"``, ``"codex"``) is on PATH."""
    return find_cli(COMMANDS.get(provider, provider)) is not None
