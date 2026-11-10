"""Serve the qswap dashboard in a browser, for screenshots and recordings.

``run.py <scenario> --serve`` lands here. textual-serve runs a small web
server on 127.0.0.1 and starts one app process per browser session; that app
process is ``run.py <scenario> --tui-child -- tui``, a fully guarded demo run
of the real dashboard with its own fresh temp home. This server process gets
the guards too (no account-store reads, no outbound connections, loopback
binds only) and may spawn exactly that one command, nothing else.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import guards

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "web"


def child_command(args) -> str:
    argv = [sys.executable, str(HERE / "run.py"), args.scenario, "--tui-child"]
    if args.theme:
        argv += ["--theme", args.theme]
    if args.compression:
        argv += ["--compression", f"{args.compression:g}"]
    argv += ["--", "watch" if args.view == "watch" else "tui"]
    return shlex.join(argv)


def serve(args) -> int:
    command = child_command(args)

    def allow_spawn(event: str, spawn_args: tuple) -> bool:
        # asyncio.create_subprocess_shell -> Popen(['/bin/sh', '-c', command])
        if event != "subprocess.Popen" or len(spawn_args) < 2:
            return False
        argv = list(spawn_args[1]) if isinstance(spawn_args[1], (list, tuple)) else []
        return len(argv) == 3 and argv[1] == "-c" and argv[2] == command

    try:
        sb = guards.install(allow_spawn=allow_spawn, import_package=False)
        guards.verify(sb, package=False)
    except guards.DemoGuardError as e:
        print(f"run.py --serve: refusing to start.\n{e}", file=sys.stderr)
        return 3

    import textual_serve
    from textual_serve.server import Server

    statics = Path(textual_serve.__file__).resolve().parent / "static"
    server = Server(
        command,
        host="127.0.0.1",
        port=args.port,
        title="qswap",
        statics_path=statics,
        templates_path=TEMPLATES,
    )
    print(
        f"Serving the {args.scenario!r} dashboard on http://127.0.0.1:{args.port}/"
        f"?fontsize={args.font_size} (Ctrl-C to stop)",
        file=sys.stderr,
    )
    server.serve()
    return 0
