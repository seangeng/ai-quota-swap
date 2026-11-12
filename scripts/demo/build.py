"""Regenerate every demo asset: one command after a UI change.

    uv run python scripts/demo/build.py                  # pages, PNGs, index (+ framing if configured)
    uv run python scripts/demo/build.py --only list,watch
    uv run python scripts/demo/build.py --record local   # also re-make the GIFs without LittleDemo
    uv run python scripts/demo/build.py --record littledemo --upload

Steps: CLI shots to HTML (docs/shots/), raw PNGs (assets/screenshots/raw/),
the social preview cut from the hero (assets/social-preview.png), ScreenFramed framing (assets/screenshots/, skipped with a message unless a
public base URL and a login are configured), the index page, and with
``--record`` the recordings (assets/demos/). Recordings are opt-in
because LittleDemo ones use the maintainer's account.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture  # noqa: E402
import stories  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="build.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--only", help="comma-separated shot names (default: all)")
    p.add_argument("--no-frame", action="store_true", help="skip the ScreenFramed step")
    p.add_argument("--base-url", help="public URL serving docs/shots/, for ScreenFramed")
    p.add_argument("--record", choices=("littledemo", "local"),
                   help="also make the recordings this way")
    p.add_argument("--upload", action="store_true", help="host LittleDemo recordings (with --record littledemo)")
    args = p.parse_args(argv)
    only = set(args.only.split(",")) if args.only else None
    started = time.monotonic()

    capture.render_html(only)
    capture.capture_shots(only)
    if not only or only & {"hero", "social-preview"}:
        capture.social_preview()
    status = 0
    if not args.no_frame:
        status = capture.frame(args.base_url, background="midnight", device=None, only=only)
    capture.write_index()
    if args.record:
        names = [n for n in stories.STORIES if not only or n in only]
        capture.save_recordings([capture.record(n, mode=args.record, upload=args.upload) for n in names])
    capture.log(f"done in {time.monotonic() - started:.0f}s")
    return status


if __name__ == "__main__":
    sys.exit(main())
