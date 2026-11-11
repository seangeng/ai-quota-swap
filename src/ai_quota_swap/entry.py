"""The ``qswap`` command's entry point.

``qswap key-helper N`` is answered here, before the command line module (and
the switcher, the usage client and the rest it imports) is loaded, and before
anything that could print: Claude Code reads that command's standard output
as the key. Every other command goes to :func:`ai_quota_swap.cli.main`.
"""

from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "key-helper":
        from ai_quota_swap.key_helper import main as key_helper_main

        sys.exit(key_helper_main(argv[1:]))
    from ai_quota_swap.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
