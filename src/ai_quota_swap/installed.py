"""The command that runs this install of qswap, for what outlives this process.

Two things name qswap by command and are run later by someone else: the
``apiKeyHelper`` line in Claude Code's settings (every Claude Code session
runs it, from its own project directory, with its own PATH) and the menu
bar's LaunchAgent plist (launchd runs it at login). Both get the same
answer from :func:`command`:

1. This install's console script (``qswap``, else ``ai-quota-swap``), found
   from the package's own environment rather than from PATH: the
   distribution record (``RECORD``) the installer wrote next to the package
   names the scripts it made. The package's location is resolved first, so
   from the menu bar's bundle (whose ``Contents/lib`` links into the
   install) the record is the install's own. An editable checkout keeps
   the package in ``src/`` and the record in its environment, so the
   running interpreter's library directories are looked at next.
2. Of that script, the link a shell finds first on PATH (or the path this
   process was started by), when it leads to the same file: ``uv tool``
   and ``pipx`` put such a link in ``~/.local/bin`` and keep it across a
   reinstall. The link is made absolute but not resolved. A ``qswap``
   first on PATH that belongs to another install is never used.
3. Only when this install has no console script: this interpreter with
   ``-P -m ai_quota_swap``. ``-P`` keeps the working directory off
   ``sys.path``; Claude Code runs the helper from the session's project
   directory, and the bare ``-m`` form would import a project file named
   like a standard-library module (``base64.py``) in its place.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

#: Names the installed command goes by, preferred first.
COMMAND_NAMES = ("qswap", "ai-quota-swap")
#: The distribution's metadata directory, as installers name it.
DIST_INFO_GLOB = "ai_quota_swap-*.dist-info"
#: The module the fallback runs.
MODULE = "ai_quota_swap"


def command_name(file_name: str) -> str | None:
    """``qswap`` or ``ai-quota-swap`` when ``file_name`` is that command
    (``qswap.exe`` on Windows), else None."""
    name = file_name.lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name if name in COMMAND_NAMES else None


def _package_dir() -> Path:
    """This package's directory, links followed."""
    return Path(__file__).resolve().parent


def _interpreter_site_dirs() -> list[Path]:
    """The running interpreter's library directories."""
    import sysconfig

    paths = sysconfig.get_paths()
    return [Path(paths[k]) for k in ("purelib", "platlib") if paths.get(k)]


def _recorded_scripts(site: Path) -> list[Path]:
    """The console scripts a distribution record in ``site`` names and that
    are there."""
    found: list[Path] = []
    try:
        infos = sorted(site.glob(DIST_INFO_GLOB))
    except OSError:
        return found
    for info in infos:
        try:
            text = (info / "RECORD").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for row in csv.reader(text.splitlines()):
            if not row or not row[0]:
                continue
            leaf = row[0].replace("\\", "/").rsplit("/", 1)[-1]
            if command_name(leaf) is None:
                continue
            script = Path(os.path.normpath(site / row[0]))
            if script.is_file() and script not in found:
                found.append(script)
    return found


def own_scripts(
    *, package_dir: Path | None = None, site_dirs: Iterable[Path] | None = None,
) -> list[Path]:
    """This install's console scripts, ``qswap`` first; empty when it has
    none. ``package_dir`` and ``site_dirs`` stand in for this package's
    directory and the interpreter's library directories (tests)."""
    package = _package_dir() if package_dir is None else package_dir
    sites = [package.parent, *(_interpreter_site_dirs() if site_dirs is None else site_dirs)]
    seen: set[str] = set()
    for site in sites:
        try:
            real = os.path.realpath(site)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        scripts = _recorded_scripts(Path(real))
        if scripts:
            return sorted(scripts, key=lambda p: COMMAND_NAMES.index(command_name(p.name)))
    return []


def _same_file(a: str, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def command(
    *,
    package_dir: Path | None = None,
    site_dirs: Iterable[Path] | None = None,
    path: str | None = None,
    argv0: str | None = None,
    executable: str | None = None,
) -> list[str]:
    """The argv that runs this install of qswap (see the module): its
    console script, as the PATH link to it when there is one, else
    ``[interpreter, "-P", "-m", "ai_quota_swap"]``. The keyword arguments
    stand in for this process's own facts (tests): the package's directory,
    the interpreter's library directories, PATH, ``sys.argv[0]`` and
    ``sys.executable``."""
    scripts = own_scripts(package_dir=package_dir, site_dirs=site_dirs)
    if not scripts:
        return [sys.executable if executable is None else executable, "-P", "-m", MODULE]
    candidates = [shutil.which(name, path=path) for name in COMMAND_NAMES]
    started = (sys.argv[0] if sys.argv else "") if argv0 is None else argv0
    if started and command_name(os.path.basename(started)) and (
        os.sep in started or "/" in started
    ):
        candidates.append(started)  # a path, not a name the shell looked up
    for found in candidates:
        if found and any(_same_file(found, s) for s in scripts):
            return [os.path.abspath(found)]
    return [str(scripts[0])]
