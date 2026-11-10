"""Every question qswap asks goes through here.

docs/design/ux.md: a prompt is only shown on a terminal. Without one, the
command stops with one line naming the flag that confirms it, and no command
ever ends in a traceback. Cancelling (no, Ctrl-D, Ctrl-C at the prompt)
changes nothing and returns ``False``; the caller says "Cancelled" and stops.

``interactive`` and ``read_line`` are module attributes so tests can swap
them for a fake terminal without touching ``sys.stdin``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from ai_quota_swap.exceptions import ConfirmationRequiredError, ValidationError


def _stdin_is_terminal() -> bool:
    stream = sys.stdin
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


#: Whether a question can be asked (stdin is a terminal).
interactive: Callable[[], bool] = _stdin_is_terminal
#: Reads one answer; ``input`` unless a test replaces it.
read_line: Callable[[str], str] = input


def needs_confirmation(command: str, flag: str = "--yes") -> ConfirmationRequiredError:
    """The error a command raises when it cannot ask: one line naming ``flag``."""
    return ConfirmationRequiredError(
        f"{command} needs {flag} when there is no terminal to ask on"
    )


def confirm(
    question: str,
    *,
    command: str,
    assume_yes: bool = False,
    flag: str = "--yes",
    default: bool = False,
) -> bool:
    """Ask a yes/no ``question``; True only for a yes.

    ``assume_yes`` (the caller's ``--yes``) answers without asking. Off a
    terminal it raises :class:`ConfirmationRequiredError` naming ``flag``,
    with ``command`` as the thing that needs it (``"qswap remove"``).
    End of input or Ctrl-C at the prompt is a no.
    """
    if assume_yes:
        return True
    if not interactive():
        raise needs_confirmation(command, flag)
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = read_line(f"{question} {suffix} ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def choose(
    question: str,
    choices: Sequence[str],
    *,
    unasked: str,
) -> str | None:
    """Ask for one of ``choices`` (account numbers); None on anything else.

    Off a terminal it raises with ``unasked``, the line that says how to
    name one directly (for example "maya@example.com matches #2 and #5;
    name one by number").
    """
    if not interactive():
        raise ConfirmationRequiredError(unasked)
    try:
        answer = read_line(f"{question} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer if answer in choices else None


def secret(question: str, *, unasked: str) -> str:
    """A hidden answer on a terminal; off one, the first line of stdin.

    Empty input raises with ``unasked`` (how to pass the value instead).
    """
    if interactive():
        import getpass

        try:
            value = getpass.getpass(question)
        except (EOFError, KeyboardInterrupt):
            print()
            value = ""
    else:
        stream = sys.stdin
        value = stream.readline() if stream is not None else ""
    value = value.strip()
    if not value:
        raise ValidationError(unasked)
    return value
