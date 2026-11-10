"""Run the real ``qswap`` against a synthetic scenario, inside the demo sandbox.

    uv run python scripts/demo/run.py fleet -- list
    uv run python scripts/demo/run.py usage --width 104 -- usage --by model
    uv run python scripts/demo/run.py fleet --html docs/shots/list.html -- list
    uv run python scripts/demo/run.py fleet --serve --port 8765      # the TUI, in a browser

Every run gets a fresh temp home, the guards in ``guards.py`` (installed and
verified before anything that touches accounts is imported), the scenario's
clock, and the canned API. The output is the tool's own rendering of the
scenario. See scripts/demo/README.md.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before any import that could write a .pyc

import argparse  # noqa: E402
import io  # noqa: E402
import shlex  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import guards  # noqa: E402  (imports nothing from ai_quota_swap)

EXIT_GUARD_FAILED = 3
EXIT_VIOLATION = 4


class _Capture(io.StringIO):
    """A non-tty stdout that keeps everything written to it."""

    def isatty(self) -> bool:
        return False


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    if "--" in argv:
        i = argv.index("--")
        head, tail = argv[:i], argv[i + 1 :]
    else:
        head, tail = argv, []
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Run qswap against a synthetic scenario inside the demo sandbox.",
    )
    p.add_argument("scenario", help="scenario name (see scenarios.py)")
    p.add_argument("--theme", choices=("dark", "light"), help="override ui.theme")
    p.add_argument("--width", type=int, default=100, help="terminal columns (default 100)")
    p.add_argument("--height", type=int, default=32, help="terminal rows (default 32)")
    p.add_argument("--html", type=Path, help="render the output to this terminal-window page")
    p.add_argument("--title", help="window title for --html (default: the command)")
    p.add_argument("--ansi", type=Path, help="also write the raw ANSI output here")
    p.add_argument("--audit-log", type=Path, help="record every file, process and network event")
    p.add_argument("--serve", action="store_true", help="serve the TUI in a browser (loopback)")
    p.add_argument("--port", type=int, default=8765, help="port for --serve (default 8765)")
    p.add_argument("--font-size", type=int, default=18, help="terminal font size for --serve")
    p.add_argument("--view", choices=("dashboard", "watch"), default="dashboard",
                   help="which TUI page --serve opens on")
    p.add_argument("--compression", type=float,
                   help="override the scenario's auto-switch time compression (see AutoRun)")
    p.add_argument("--tui-child", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(head)
    if not tail and not args.serve:
        p.error("give the qswap arguments after --, e.g. run.py fleet -- list")
    return args, tail


def main(argv: list[str] | None = None) -> int:
    args, qswap_args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.serve:
        from serve import serve

        return serve(args)
    return run_cli(args, qswap_args)


def run_cli(args: argparse.Namespace, qswap_args: list[str]) -> int:
    audit_fd = guards.open_audit_log(str(args.audit_log)) if args.audit_log else None
    outputs = tuple(p.resolve() for p in (args.html, args.ansi) if p is not None)
    for out in outputs:  # before the guards: afterwards only these files are writable
        out.parent.mkdir(parents=True, exist_ok=True)
    capture = args.html is not None or args.ansi is not None
    # A TUI child keeps the terminal size textual-serve hands it.
    extra_env = {} if args.tui_child else {"COLUMNS": str(args.width), "LINES": str(args.height)}
    if capture:
        extra_env["FORCE_COLOR"] = "1"
        extra_env["TERM"] = "xterm-256color"
    try:
        sb = guards.install(extra_env=extra_env, write_roots=outputs, audit=audit_fd is not None)
    except Exception as e:  # noqa: BLE001
        print(f"run.py: the demo sandbox could not be installed: {e}", file=sys.stderr)
        return EXIT_GUARD_FAILED

    # Everything below runs inside the sandbox.
    import time

    import time_machine

    import fakeapi
    import scenarios

    scenario = scenarios.get(args.scenario)
    if args.theme:
        scenario = scenarios.replace(
            scenario, settings={**scenario.settings, "ui": {"theme": args.theme}}
        )
    start = scenario.epoch - (scenario.warmup.total_seconds() if scenario.warmup else 0)
    traveller = time_machine.travel(start, tick=True)
    coordinates = traveller.start()
    state = scenarios.LiveState(scenario, clock=time.time)
    api = fakeapi.FakeAnthropic(state, on_unknown=sb.record_violation)
    guards.install_fake_api(api)
    try:
        guards.verify(sb, clock_epoch=start)
    except guards.DemoGuardError as e:
        print(f"run.py: refusing to start.\n{e}", file=sys.stderr)
        return EXIT_GUARD_FAILED

    import seed

    switcher = seed.seed(scenario)
    if scenario.warmup:
        switcher.usage_entries_by_account()  # the earlier read
        coordinates.move_to(scenario.epoch)
    if scenario.auto is not None:
        auto = scenario.auto
        if args.compression:
            auto = scenarios.replace(auto, compression=args.compression)
        install_time_compression(auto, coordinates, stop_after=not args.tui_child)

    import random

    from ai_quota_swap import cli

    # `main` injects truststore, whose import loads the Security framework;
    # the sandbox refuses that framework outright (it is also the Keychain
    # API), and a process with no network needs no TLS trust store.
    cli._use_native_tls = lambda: None
    random.seed(scenario.name)  # the engine's poll jitter, run to run

    code = 0
    blocks = []
    for command in split_commands(qswap_args):
        saved = sys.stdout, sys.stderr
        buf = _Capture() if capture else None
        if buf is not None:
            sys.stdout = sys.stderr = buf
        sys.argv = ["qswap", *command]
        try:
            cli.main()
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        finally:
            sys.stdout, sys.stderr = saved
        if buf is not None:
            blocks.append((command, buf.getvalue()))

    if capture:
        import render

        shown = ["qswap " + " ".join(shlex.quote(a) for a in c) for c, _ in blocks]
        if args.ansi:
            args.ansi.write_text("".join(f"$ {s}\n{out}" for s, (_, out) in zip(shown, blocks)),
                                 encoding="utf-8")
        if args.html:
            render.write_page(
                args.html,
                [render.Block(s, out) for s, (_, out) in zip(shown, blocks)],
                title=args.title or shown[0],
                theme=scenario.settings.get("ui", {}).get("theme", "dark"),
                columns=args.width,
            )

    if audit_fd is not None:
        guards.write_audit_log(audit_fd, sb)
    if sb.violations:
        print("run.py: the run crossed the sandbox:", file=sys.stderr)
        for v in sb.violations:
            print(f"  - {v}", file=sys.stderr)
        return EXIT_VIOLATION
    return code


def split_commands(args: list[str]) -> list[list[str]]:
    """``list ++ reset`` is two commands run one after the other on one state."""
    out: list[list[str]] = [[]]
    for a in args:
        if a == "++":
            out.append([])
        else:
            out[-1].append(a)
    return [c for c in out if c]


def install_time_compression(auto, coordinates, *, stop_after: bool) -> None:
    """Make the auto-switch engine's sleeps short in real time.

    Each sleep the engine asks for jumps the scenario clock forward by that
    much instead of waiting for it, minus the short real wait it keeps (so the
    loop still yields). Log timestamps and countdowns stay true to scenario
    time. With ``stop_after`` the loop ends after ``auto.ticks`` ticks, for
    the CLI log capture. Demo process only: this patches the engine class in
    this process and nothing on disk.
    """
    from ai_quota_swap import poll_policy
    from ai_quota_swap.autoswitch import AutoSwitchEngine

    poll_policy.JITTER_FRAC = 0.0  # as the test suite pins it: polls on the minute
    original = AutoSwitchEngine._next_delay
    counts: dict[int, int] = {}

    def compressed(self, outcome):
        delay = original(self, outcome)
        n = counts[id(self)] = counts.get(id(self), 0) + 1
        if stop_after and n >= auto.ticks:
            self.stop()
            return 0.0
        # A normal-cadence sleep lands on the configured interval (the
        # engine's +-10% jitter exists to keep machines apart; one demo has
        # nobody to keep apart), so ticks read 14:20:00, 14:21:00, ...
        interval = self.settings.interval_seconds
        step = interval if delay <= interval * 1.1 else delay
        real = step / auto.compression
        coordinates.shift(step - real)
        return real

    AutoSwitchEngine._next_delay = compressed


if __name__ == "__main__":
    sys.exit(main())
