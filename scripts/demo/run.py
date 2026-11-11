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
                        "description, built by ai_quota_swap.menubar.build_menu), "
                        "after the qswap commands given after --, if any; with "
                        "--html, also draw it as the menu bar with its menu open")
    mb = p.add_argument_group("menu bar drawing (with --menubar --html)")
    mb.add_argument("--open", metavar="TITLE",
                    help="open this submenu beside the menu (its exact row text); "
                         "'Settings > Use resets automatically' opens one inside another")
    mb.add_argument("--highlight", metavar="TEXT",
                    help="draw this row (its text, or the start of it) of the innermost "
                         "open menu under the pointer")
    mb.add_argument("--closed", action="store_true",
                    help="draw the menu bar with the menu closed (laid out, not shown, so "
                         "the picture is the same size as an open one)")
    mb.add_argument("--beside", type=Path, metavar="PNG",
                    help="put this 2x screenshot on the desktop as a window behind the menu "
                         "(the README's hero: the dashboard)")
    mb.add_argument("--size", metavar="WxH", help="fix the picture's size in CSS px")
    mb.add_argument("--auto-switch", nargs="?", const="both", choices=("claude", "codex", "both"),
                    help="turn the menu bar's auto-switch on (default: both providers) and "
                         "host its engines as the app does; the menu is drawn after their "
                         "first check")
    mb.add_argument("--frames", type=Path, metavar="DIR",
                    help="with --auto-switch: draw the menu bar after each of Claude Code's "
                         "engine checks into DIR (frame-NN.html, frame-NN-closed.html, "
                         "frame-NN.txt)")
    mb.add_argument("--ticks", type=int, default=4,
                    help="how many engine checks --frames draws (default 4)")
    p.add_argument("--tui-child", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(head)
    if "--" not in argv and not args.serve and not args.menubar:
        p.error("give the qswap arguments after --, e.g. run.py fleet -- list "
                "(a bare `--` runs qswap with no arguments)")
    drawing = ("open", "highlight", "closed", "beside", "size", "auto_switch", "frames")
    if not args.menubar and any(getattr(args, k) not in (None, False) for k in drawing):
        p.error("--open, --highlight, --closed, --beside, --size, --auto-switch and "
                "--frames go with --menubar")
    if args.frames is not None and not args.auto_switch:
        p.error("--frames draws one picture per engine check: add --auto-switch")
    if args.size is not None:
        try:
            w, h = (int(x) for x in args.size.lower().split("x"))
            args.size = (w, h)
        except ValueError:
            p.error(f"--size takes WIDTHxHEIGHT in CSS px, not {args.size!r}")
    args.open = [t.strip() for t in args.open.split(" > ")] if args.open else []
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
    if args.frames is not None:
        args.frames = args.frames.resolve()
        args.frames.mkdir(parents=True, exist_ok=True)
        outputs += (args.frames,)
    # The window behind the menu is read before the guards, like the outputs.
    beside = args.beside.read_bytes() if args.beside is not None else None
    # With --menubar the commands only set up the state: their output is
    # printed as it is, and --html draws the menu bar instead.
    capture = (args.html is not None or args.ansi is not None) and not args.menubar
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
        # The menu bar's own engines run until the drawing is done.
        install_time_compression(auto, coordinates, stop_after=not (args.tui_child or args.auto_switch))

    import random

    from ai_quota_swap import cli

    # `main` injects truststore, whose import loads the Security framework;
    # the sandbox refuses that framework outright (it is also the Keychain
    # API), and a process with no network needs no TLS trust store.
    cli._use_native_tls = lambda: None
    install_doctor_fakes()
    install_key_line_fakes()
    install_cli_fakes(args.without_cli)
    random.seed(scenario.name)  # the engine's poll jitter, run to run

    code = 0
    blocks = []
    # A bare `--` is qswap with no arguments (its usage error off a terminal).
    # With --menubar the commands, if any, set up the state the menu shows.
    commands = split_commands(qswap_args) or ([] if args.menubar else [[]])
    for command in commands:
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

    if args.menubar:
        code = run_menubar(args, switcher, beside=beside)

    if audit_fd is not None:
        guards.write_audit_log(audit_fd, sb)
    if sb.violations:
        print("run.py: the run crossed the sandbox:", file=sys.stderr)
        for v in sb.violations:
            print(f"  - {v}", file=sys.stderr)
        return EXIT_VIOLATION
    return code


def run_menubar(args: argparse.Namespace, switcher, *, beside: bytes | None) -> int:
    """``--menubar``: print the menu's outline and, with ``--html``, draw it.
    With ``--auto-switch`` the menu bar's engines run first, as the app hosts
    them; ``--frames`` draws after each of Claude Code's engine checks."""
    import menubar_page

    host = MenuBarHost(switcher, auto_switch=args.auto_switch)
    backdrop = None
    if beside is not None:
        backdrop = menubar_page.Backdrop(beside, alt="The qswap dashboard")

    def draw(menu, target: Path, *, closed: bool) -> None:
        target.write_text(menubar_page.page(
            menu, theme=args.theme or "dark", open_path=args.open, highlight=args.highlight,
            clock=host.clock_text(), closed=closed, backdrop=backdrop, size=args.size,
        ), encoding="utf-8")

    try:
        host.start()
        if args.frames is not None:
            for i in range(args.ticks):
                with host.after_tick("claude"):
                    menu = host.menu()
                    draw(menu, args.frames / f"frame-{i:02d}.html", closed=args.closed)
                    draw(menu, args.frames / f"frame-{i:02d}-closed.html", closed=True)
                    (args.frames / f"frame-{i:02d}.txt").write_text(menu.outline() + "\n", encoding="utf-8")
        else:
            menu = host.menu()
        print(menu.outline())
        if args.html is not None:
            draw(menu, args.html, closed=args.closed)
    except (menubar_page.UnknownSubmenu, menubar_page.UnknownRow) as e:
        print(f"run.py: {e}", file=sys.stderr)
        return 2
    finally:
        host.close()
    return 0


class MenuBarHost:
    """What the menu bar app does to decide its menu, without the app.

    The reads are the app's (``menubar_data``: both providers through their
    paced read paths, the ledger, the switch log, each engine's heartbeat),
    and so is the input to ``build_menu`` (``MenuBarApp._menu_input``). The
    ledger scan that the app starts a minute after launch has run.

    With ``auto_switch``, the menu bar's auto-switch toggles are turned on in
    ``menubar_settings.json``, as the menu's own toggle does, and the
    engines run as the app hosts them: an ``EngineSupervisor`` starting an
    ``AutoSwitchEngine`` per provider (``dry_run=False``, host label ``qswap
    menubar``) in a thread of its own, against the scenario's canned API.
    Each engine's check is followed by a pause until the menu has been drawn
    from what it left, so a picture never shows half a tick.
    """

    def __init__(self, switcher, *, auto_switch: str | None) -> None:
        import queue

        from ai_quota_swap import menubar, menubar_data
        from ai_quota_swap.snapshot_source import SnapshotSource

        self.switcher = switcher
        self.root = switcher.backup_dir
        self.claude_source = SnapshotSource(switcher)  # reused, as the app keeps its pacing
        self.codex = menubar_data.CodexReader(self.root)
        self.settings_path = self.root / "menubar_settings.json"
        self.settings = menubar.MenuBarSettings.load(self.settings_path)
        if auto_switch:
            self.settings.auto_switch_enabled = auto_switch in ("claude", "both")
            self.settings.codex_auto_switch_enabled = auto_switch in ("codex", "both")
            self.settings.save(self.settings_path)
        self.supervisor = None
        self._ticks: queue.Queue = queue.Queue()
        self._closing = False

    # -- engines --------------------------------------------------------------

    def wanted(self) -> dict[str, bool]:
        from ai_quota_swap import menubar, menubar_data

        return menubar.wanted_engines(
            self.settings, codex_accounts=menubar_data.codex_registry_count(self.root),
        )

    def _make_engine(self, provider, emit):
        """``MenuBarApp._make_engine``, with each check reported to the drawing."""
        from ai_quota_swap import menubar
        from ai_quota_swap.autoswitch import AutoSwitchEngine
        from ai_quota_swap.settings import load_engine_settings

        sw = self.codex.ensure() if provider == "codex" else self.switcher
        if provider == "codex":
            sw.require_accounts()
        engine = AutoSwitchEngine(sw, load_engine_settings(self.root, provider), emit, dry_run=False)
        engine.host_label = menubar.ENGINE_HOST_LABEL
        tick = engine.tick

        def reported_tick():
            outcome = tick()
            self._checked(provider)
            return outcome

        engine.tick = reported_tick
        return engine

    def _checked(self, provider: str) -> None:
        # Engine thread: wait until the main thread has drawn this state.
        import threading

        if self._closing:
            return
        drawn = threading.Event()
        self._ticks.put((provider, drawn))
        drawn.wait(timeout=120)

    def start(self) -> None:
        """The ledger scan, then the engines the toggles want; returns once
        each has made its first check (and holds them there)."""
        from ai_quota_swap import menubar_data
        from ai_quota_swap.menubar_engines import EngineSupervisor

        menubar_data.scan_ledger(self.root, registry=self.switcher._get_sequence_data())
        self._held: list = []
        wanted = self.wanted()
        if not any(wanted.values()):
            return
        self.supervisor = EngineSupervisor(self._make_engine, lambda provider, event: None)
        failed = self.supervisor.apply(wanted)
        pending = {p for p, want in wanted.items() if want and p not in failed}
        while pending:
            provider, drawn = self._ticks.get(timeout=120)
            pending.discard(provider)
            self._held.append(drawn)

    def _release(self) -> None:
        for drawn in self._held:
            drawn.set()
        self._held = []

    def after_tick(self, provider: str):
        """``with host.after_tick("claude"):`` runs its body once that
        engine's next check is done (the first time: its first check), while
        the engines wait."""
        import contextlib

        @contextlib.contextmanager
        def held():
            if self._held:  # the first checks, held by start()
                pass
            else:
                while True:
                    p, drawn = self._ticks.get(timeout=120)
                    self._held.append(drawn)
                    if p == provider:
                        break
                    self._release()  # another provider's check: let it go on
            try:
                yield
            finally:
                self._release()

        return held()

    def close(self) -> None:
        self._closing = True
        self._release()
        while not self._ticks.empty():
            self._ticks.get_nowait()[1].set()
        if self.supervisor is not None:
            self.supervisor.stop_all(join_timeout=10)

    # -- the menu -------------------------------------------------------------

    def menu(self):
        """``build_menu`` over a fresh read, as ``MenuBarApp.rebuild_menu``
        does after its read worker: a provider whose engine runs is read from
        the store the engine fills (``store_only``)."""
        import time

        from ai_quota_swap import menubar, menubar_data
        from ai_quota_swap.settings import load_codex_settings, load_reset_settings, load_settings

        root = self.root
        sup = self.supervisor
        running = {p: sup is not None and sup.running(p) for p in ("claude", "codex")}
        now = time.time()
        claude = menubar_data.claude_state(
            self.switcher, self.claude_source, full=False, store_only=running["claude"], now=now,
        )
        codex = self.codex.state(full=False, store_only=running["codex"], now=now)
        beats = menubar_data.heartbeats(root)
        now = time.time()  # the menu is drawn after the read, as in the app
        wanted = self.wanted()
        engines = {
            p: menubar.engine_state(
                p, enabled=wanted[p], running=running[p],
                failure=sup.failure(p) if sup is not None else None,
                heartbeat=beats.get(p), now=now,
            )
            for p in ("claude", "codex")
        }
        inp = menubar.MenuInput(
            claude=claude,
            codex=codex,
            settings=self.settings,
            now=now,
            engines=engines,
            cost=menubar_data.read_cost(root),
            history=menubar_data.read_history(root / "ai-quota-swap.log"),
            thresholds={
                "claude": load_settings(root).threshold,
                "codex": load_codex_settings(root).threshold,
            },
            can_add_token=hasattr(self.switcher, "add_account_from_token"),
            resets=load_reset_settings(root),
        )
        return menubar.build_menu(inp)

    @staticmethod
    def clock_text() -> str:
        """The menu bar clock at the scenario's time: ``Tue 10 Nov 14:20``."""
        import time

        t = time.localtime()
        return f"{time.strftime('%a', t)} {t.tm_mday} {time.strftime('%b %H:%M', t)}"


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
    # The machine's own managed settings file is not the scenario's.
    doctor._managed_settings_path = lambda: None


def install_key_line_fakes() -> None:
    """The key line a switch to an API key writes names this install's
    ``qswap``, which here would be the checkout's own path. The demo pins it
    to ``~/.local/bin/qswap`` in the temp home, where ``uv tool`` and
    ``pipx`` put the command (a stand-in file that is never run), so output
    is the same on every machine.

    ``qswap doctor`` and ``qswap switch N`` to the active key run the key
    line with ``--check`` through the shell, a spawn the sandbox refuses.
    Here the check runs ``qswap key-helper N --check`` in this process
    instead, against the temp home and the in-memory Keychain: the same
    answer the line's command would give, with no process started. A line
    that is not qswap's, or whose program is missing, is still judged by the
    real ``claude_settings.command_problem`` before this is reached."""
    import contextlib

    from ai_quota_swap import claude_settings, installed, key_helper

    program = Path.home() / ".local" / "bin" / "qswap"
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text("#!/bin/sh\n# the demo's stand-in for qswap; never run\nexit 1\n",
                       encoding="utf-8")
    program.chmod(0o755)
    installed.command = lambda: [str(program)]

    def run_check(command: str, *, cwd: str, timeout: float) -> tuple[int, str]:
        line = claude_settings.read_line(command)
        argv = [line.number, "--check"] if line is not None else []
        if line is not None and line.data_dir:
            argv += ["--data-dir", line.data_dir]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = key_helper.main(argv)
        return code, err.getvalue()

    claude_settings._run_check = run_check


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
