"""argparse, the way every qswap command uses it.

docs/design/ux.md: a usage error (unknown verb, bad flag, missing argument)
exits 64, never 2, because ``auto --once`` already gives 2 its own meaning
("nothing to do"). Under ``--json`` the error is the JSON error envelope on
stdout, like every other failure. Help is wrapped to 80 columns whatever the
terminal's width.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

#: Exit status for a usage error (BSD sysexits ``EX_USAGE``).
EXIT_USAGE = 64
#: Help text never runs wider than this.
HELP_WIDTH = 80

# Whether the command line being parsed asked for --json; set by the
# outermost parse_args so subparsers (``config get``) answer the same way.
_json_requested = False


def help_width() -> int:
    """The width help is wrapped to: the terminal's, at most 80 columns."""
    try:
        columns = shutil.get_terminal_size((HELP_WIDTH, 24)).columns
    except (OSError, ValueError):
        columns = HELP_WIDTH
    return max(40, min(HELP_WIDTH, columns))


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Descriptions and examples as written; option help wrapped to 80."""

    def __init__(self, prog: str, indent_increment: int = 2, max_help_position: int = 24,
                 width: int | None = None) -> None:
        super().__init__(
            prog, indent_increment=indent_increment,
            max_help_position=max_help_position, width=width or help_width() - 2,
        )


class ArgumentParser(argparse.ArgumentParser):
    """``argparse.ArgumentParser`` with qswap's exit code, JSON errors and width."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        global _json_requested
        argv = list(sys.argv[1:] if args is None else args)
        outer = not _json_requested
        if outer:
            _json_requested = "--json" in argv
        try:
            return super().parse_args(argv, namespace)
        finally:
            if outer:
                _json_requested = False

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[override]
        global _json_requested
        argv = list(sys.argv[1:] if args is None else args)
        outer = not _json_requested
        if outer:
            _json_requested = "--json" in argv
        try:
            return super().parse_known_args(argv, namespace)
        finally:
            if outer:
                _json_requested = False

    def error(self, message: str):  # type: ignore[override]
        usage_error(self.prog, message, usage=self.format_usage(), as_json=_json_requested)


def usage_error(prog: str, message: str, *, usage: str | None = None, as_json: bool = False):
    """Report a usage error and exit 64: the usage line and ``prog: error:
    message`` on stderr, or the JSON error envelope on stdout."""
    if as_json:
        from ai_quota_swap.json_output import SCHEMA_VERSION

        print(json.dumps(
            {"schemaVersion": SCHEMA_VERSION,
             "error": {"type": "UsageError", "message": message}},
            indent=2,
        ))
    else:
        if usage:
            sys.stderr.write(usage)
        sys.stderr.write(f"{prog}: error: {message}\n")
    sys.exit(EXIT_USAGE)
