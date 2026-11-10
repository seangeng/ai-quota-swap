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
    p.add_argument("--without-cli", action="append", choices=("claude", "codex"), default=[],
                   help="act as if this CLI is not installed (repeatable); by default "
                        "both are, whatever this machine has")
    p.add_argument("--menubar", action="store_true",
                   help="print the menu bar's menu for the scenario as an outline "
                        "(the menu bar itself needs macOS and rumps; this is its "
                        "description, built by ai_quota_swap.menubar.build_menu)")
    p.add_argument("--tui-child", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(head)
    if "--" not in argv and not args.serve and not args.menubar:
        p.error("give the qswap arguments after --, e.g. run.py fleet -- list "
                "(a bare `--` runs qswap with no arguments)")
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

    install_codex_process_fakes(scenario)

    import seed

    switcher = seed.seed(scenario)
    if scenario.warmup:
        switcher.usage_entries_by_account()  # the earlier read
        if scenario.codex_accounts:
            from ai_quota_swap.codex.switcher import CodexSwitcher

            CodexSwitcher(configure_logging=False).usage_entries_by_account()
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
    install_doctor_fakes()
    install_cli_fakes(args.without_cli)
    random.seed(scenario.name)  # the engine's poll jitter, run to run

    if args.menubar:
        print(menubar_outline(switcher))
        if sb.violations:
            print("run.py: the run crossed the sandbox:", file=sys.stderr)
            for v in sb.violations:
                print(f"  - {v}", file=sys.stderr)
            return EXIT_VIOLATION
        return 0

    code = 0
    blocks = []
    # A bare `--` is qswap with no arguments (its usage error off a terminal).
    for command in split_commands(qswap_args) or [[]]:
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

        shown = [" ".join(["qswap", *(shlex.quote(a) for a in c)]) for c, _ in blocks]
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


def menubar_outline(switcher) -> str:
    """The menu bar's menu for the seeded scenario, as the app builds it once
    its first read and its first ledger scan are done: both providers read
    through the same paths the app uses (``menubar_data``), engines off, the
    scenario's settings."""
    import time

    from ai_quota_swap import menubar, menubar_data
    from ai_quota_swap.settings import load_codex_settings, load_settings
    from ai_quota_swap.snapshot_source import SnapshotSource

    root = switcher.backup_dir
    # The app's background ledger scan, as it runs a minute after launch.
    menubar_data.scan_ledger(root, registry=switcher._get_sequence_data())
    now = time.time()
    claude = menubar_data.claude_state(
        switcher, SnapshotSource(switcher), full=False, store_only=False, now=now,
    )
    codex = menubar_data.CodexReader(root).state(full=False, store_only=False, now=now)
    now = time.time()  # the menu is drawn after the read, as in the app
    inp = menubar.MenuInput(
        claude=claude,
        codex=codex,
        settings=menubar.MenuBarSettings.load(root / "menubar_settings.json"),
        now=now,
        cost=menubar_data.read_cost(root),
        history=menubar_data.read_history(root / "ai-quota-swap.log"),
        thresholds={
            "claude": load_settings(root).threshold,
            "codex": load_codex_settings(root).threshold,
        },
    )
    return menubar.build_menu(inp).outline()


def split_commands(args: list[str]) -> list[list[str]]:
    """``list ++ reset`` is two commands run one after the other on one state."""
    out: list[list[str]] = [[]]
    for a in args:
        if a == "++":
            out.append([])
        else:
            out[-1].append(a)
    return [c for c in out if c]


def install_codex_process_fakes(scenario) -> None:
    """Fake the four functions through which Codex support reaches processes.

    ``ai_quota_swap.codex.processes`` lists running Codex processes with ``ps``,
    restarts Codex's background server and runs ``codex login``: all process
    spawns, which the sandbox refuses (and a refusal is not an ``OSError``, so
    the switch would die on it). The test suite replaces the same four
    functions in ``tests/conftest.py``; this does it the same way, in this
    process's memory only. Nothing runs: no Codex process is running here,
    no background server exists, and ``codex login`` writes the scenario's
    ``codex_login`` record into the temporary home it is given, as a real
    login would. A scenario without one answers as a machine without the
    codex CLI does.
    """
    from ai_quota_swap.codex import processes

    def no_restart(home, timeout=processes.RESTART_TIMEOUT_S):
        return processes.RestartResult(False, "no background server in the demo")

    def fake_login(home, args):
        if scenario.codex_login is None:
            return processes.LoginRun(None, "codex is not on PATH")
        path = home / "auth.json"
        path.write_bytes(scenario.codex_login.login_record(scenario.clock))
        path.chmod(0o600)
        return processes.LoginRun(0)

    processes.list_codex_processes = lambda: []
    processes.background_server_pid = lambda home: None
    processes.restart_background_server = no_restart
    processes.run_codex_login = fake_login


def install_cli_fakes(missing: list[str]) -> None:
    """Whether ``claude`` and ``codex`` are installed is the scenario's
    choice, not this machine's: ``ai_quota_swap.clis.find_cli`` (qswap's one
    PATH lookup for them) answers both found, except those named with
    ``--without-cli``. Output is then the same on a laptop with both CLIs and
    a CI runner with neither. Nothing found is ever run: the sandbox refuses
    every spawn."""
    from ai_quota_swap import clis

    clis.find_cli = lambda name: None if name in missing else f"/usr/local/bin/{name}"


def install_doctor_fakes() -> None:
    """``qswap doctor`` asks ``claude --version`` and ``codex --version`` and
    reads ``launchctl print``: process spawns the sandbox refuses. Like the
    Codex fakes above, these answer from memory: a fixed version for any CLI
    found on PATH, and a menu bar job launchd does not know. Its install line
    reads as a ``uv tool install`` on a fixed Python, the way a user's would,
    rather than this checkout and whatever Python runs the harness."""
    from ai_quota_swap import doctor

    real_install_info = doctor._install_info

    def install_info() -> dict:
        return {**real_install_info(), "python": "3.13.5", "method": "uv", "editable": False}

    doctor._cli_version = lambda path: "2.1.0 (demo)"
    doctor._launchctl_print = lambda: (113, "")
    doctor._install_info = install_info


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
