"""The demo harness (scripts/demo) never reaches real accounts.

Every guarded run happens in a subprocess: the harness installs a permanent
audit hook, which must never land in the pytest process. The tests prove:

* a full demo run (four commands on one seeded state) opens no file outside
  its temp home, the repository and the interpreter's own install, writes
  nothing outside its temp home, spawns nothing, makes no network call, and
  never touches a real account store;
* the guards refuse a real-store read, an outside write, a process spawn,
  an off-machine connection and a DNS lookup, and let loopback through;
* ``verify`` notices a guard that is missing, and the launcher then refuses
  to start instead of running unguarded;
* the Codex verbs (switch, spending a credit, ``codex login``) run in the
  sandbox with nothing spawned and no network, through the same process fakes
  the suite's conftest installs;
* each scenario shows the state it is named for (first run, a mixed fleet,
  Codex attribution in ``qswap usage``, failure states);
* the menu bar drawing (``--menubar --html``) draws every row ``build_menu``
  returns, in both themes, opens the submenu it is asked to and refuses one
  that does not exist, references nothing outside the page, and its frames
  follow the menu bar's own engine;
* the scenarios use invented identities only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

if sys.platform == "win32":
    # The harness is a macOS/Linux development tool: its sandbox identifies the
    # real home through the password database and blocks POSIX paths.
    pytest.skip("the demo harness's sandbox guards are POSIX-only", allow_module_level=True)

import pwd  # noqa: E402  (POSIX-only; imported after the platform check)

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "scripts" / "demo"
RUN = DEMO / "run.py"

# Files outside the sandbox a demo process may read: the interpreter's own
# installation (lazy stdlib imports) and the macOS version file
# (platform.mac_ver). Nothing under a home directory except the repository.
SYSTEM_READS = ("/System/Library/CoreServices/SystemVersion.plist",)
DEVICES = ("/dev/null", "/dev/urandom", "/dev/tty")


def _real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _run(args: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN), *args],
        capture_output=True, text=True, timeout=timeout, cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _snippet(code: str, *, timeout: float = 60) -> subprocess.CompletedProcess:
    prelude = f"import sys; sys.path.insert(0, {str(DEMO)!r})\n"
    return subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(code)],
        capture_output=True, text=True, timeout=timeout, cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


@pytest.fixture(scope="module")
def audited_run(tmp_path_factory) -> dict:
    """One guarded demo run with the audit log on: list, reset, usage, status."""
    log = tmp_path_factory.mktemp("demo-audit") / "audit.json"
    r = _run([
        "usage", "--audit-log", str(log), "--",
        "list", "++", "reset", "++", "usage", "--by", "model", "++", "status",
    ])
    assert r.returncode == 0, r.stderr
    data = json.loads(log.read_text())
    data["stdout"], data["stderr"] = r.stdout, r.stderr
    return data


class TestDemoRunStaysInItsSandbox:
    def test_the_run_shows_the_scenario(self, audited_run):
        out = audited_run["stdout"]
        assert "maya@example.com" in out
        assert "Northwind Labs" in out
        assert "Usage resets" in out
        assert "claude-opus-5-5" in out  # the usage report ran on seeded sessions

    def test_the_audit_hook_saw_the_run(self, audited_run):
        opened = [e for e in audited_run["events"] if e["event"] == "open"]
        assert len(opened) > 50, "the hook recorded almost nothing; the check proves nothing"
        assert any(e.get("write") and _under(os.path.realpath(e["path"]), os.path.realpath(audited_run["home"]))
                   for e in opened), "no write into the temp home was recorded"

    def test_no_file_outside_temp_home_repo_and_interpreter(self, audited_run):
        home = os.path.realpath(audited_run["home"])
        roots = [home, str(REPO), os.path.realpath(sys.base_prefix), os.path.realpath(sys.prefix)]
        outside = sorted({
            e["path"] for e in audited_run["events"]
            if e.get("path")
            and not any(_under(os.path.realpath(e["path"]), r) or _under(e["path"], r) for r in roots)
            and e["path"] not in SYSTEM_READS + DEVICES
        })
        assert outside == []

    def test_no_write_outside_temp_home(self, audited_run):
        home = os.path.realpath(audited_run["home"])
        writes = sorted({
            e["path"] for e in audited_run["events"]
            if e.get("write") and e.get("path")
            and not _under(os.path.realpath(e["path"]), home)
            and e["path"] not in DEVICES
        })
        assert writes == []

    def test_no_real_account_store_was_touched(self, audited_run):
        stores = []
        for home in {str(_real_home()), os.environ.get("HOME", "")} - {""}:
            stores += [os.path.join(home, n) for n in
                       (".claude", ".claude.json", ".codex", ".ai-quota-swap", ".claude-swap-backup")]
            stores += [os.path.join(home, ".local", "share", "ai-quota-swap"),
                       os.path.join(home, "Library", "Keychains")]
        hits = [e for e in audited_run["events"] if e.get("path")
                and any(_under(e["path"], s) or e["path"].startswith(s) for s in stores)]
        assert hits == []

    def test_no_process_spawned_and_no_network(self, audited_run):
        kinds = {e["event"] for e in audited_run["events"]}
        assert not kinds & {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system", "os.fork"}
        net = [e for e in audited_run["events"] if e["event"].startswith(("socket.", "http.", "urllib."))]
        assert net == []
        assert audited_run["violations"] == []

    def test_output_carries_no_machine_identity(self, audited_run):
        out = audited_run["stdout"] + audited_run["stderr"]
        assert str(_real_home()) not in out
        assert audited_run["home"] not in out
        assert os.path.realpath(audited_run["home"]) not in out


class TestGuardsRefuse:
    def test_each_guard_refuses_and_loopback_passes(self, tmp_path):
        outside = tmp_path / "outside.txt"
        probe = _real_home() / ".claude" / "qswap-demo-test-probe-never-created"
        r = _snippet(f"""
            import json, socket, subprocess, sys
            import guards
            sb = guards.install()
            results = {{}}

            def expect(name, kind, fn):
                try:
                    fn()
                except kind:
                    results[name] = "refused"
                except Exception as e:
                    results[name] = f"wrong: {{type(e).__name__}}: {{e}}"
                else:
                    results[name] = "allowed"

            # The hook raises before the syscall: nothing reaches the filesystem.
            expect("store-read", guards.DemoGuardViolation, lambda: sys.audit("open", {str(probe)!r}, "r", 0))
            expect("store-listdir", guards.DemoGuardViolation,
                   lambda: sys.audit("os.listdir", {str(probe.parent)!r}))
            expect("outside-write", guards.DemoGuardViolation, lambda: open({str(outside)!r}, "w"))
            expect("spawn", guards.DemoGuardViolation, lambda: subprocess.run(["/usr/bin/true"]))
            expect("connect", guards.NetworkBlocked,
                   lambda: socket.create_connection(("192.0.2.1", 9), timeout=0.5))
            expect("dns", guards.NetworkBlocked, lambda: socket.getaddrinfo("api.anthropic.com", 443))

            srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
            c = socket.create_connection(srv.getsockname(), timeout=2); c.close(); srv.close()
            results["loopback"] = "allowed"
            open(sb.home / "inside.txt", "w").write("ok")
            results["inside-write"] = "allowed"
            print(json.dumps(results))
        """)
        assert r.returncode == 0, r.stderr
        results = json.loads(r.stdout.strip().splitlines()[-1])
        assert results == {
            "store-read": "refused",
            "store-listdir": "refused",
            "outside-write": "refused",
            "spawn": "refused",
            "connect": "refused",
            "dns": "refused",
            "loopback": "allowed",
            "inside-write": "allowed",
        }
        assert not outside.exists()

    def test_verify_notices_a_missing_keychain_fake(self):
        r = _snippet("""
            import time
            import guards, fakeapi, scenarios
            sb = guards.install()
            guards.install_fake_api(fakeapi.FakeAnthropic(scenarios.LiveState(scenarios.FLEET, time.time)))
            guards.verify(sb)  # all guards in place: passes
            from ai_quota_swap import macos_keychain
            macos_keychain.get_password = lambda service, account: None  # not the fake
            try:
                guards.verify(sb)
            except guards.DemoGuardError as e:
                print("REFUSED", e)
            else:
                print("PASSED")
        """)
        assert r.returncode == 0, r.stderr
        assert "REFUSED" in r.stdout
        assert "macos_keychain.get_password is not the in-memory fake" in r.stdout

    def test_verify_notices_a_missing_api_fake(self):
        r = _snippet("""
            import guards
            sb = guards.install()
            try:
                guards.verify(sb)
            except guards.DemoGuardError as e:
                print("REFUSED", e)
        """)
        assert "urllib.request.urlopen is not the canned API" in r.stdout

    def test_launcher_refuses_to_start_when_a_guard_is_off(self):
        """CODEX_HOME left pointing outside the temp home: exit 3, nothing run."""
        r = _snippet("""
            import os, sys
            import guards, run
            real = guards._set_env
            def leaky(home, extra):
                real(home, extra)
                os.environ["CODEX_HOME"] = "/nonexistent/real-codex-home"
            guards._set_env = leaky
            sys.exit(run.main(["fleet", "--", "list"]))
        """)
        assert r.returncode == 3, r.stderr
        assert "refusing to start" in r.stderr
        assert "$CODEX_HOME" in r.stderr
        assert "maya@example.com" not in r.stdout


@pytest.fixture(scope="module")
def audited_codex_run(tmp_path_factory) -> dict:
    """One guarded run over the Codex verbs that reach processes and the
    ChatGPT backend: list, switch, spend a credit, log a new account in."""
    log = tmp_path_factory.mktemp("demo-codex-audit") / "audit.json"
    r = _run([
        "fleet", "--audit-log", str(log), "--",
        "codex", "list", "++", "codex", "switch", "2", "++", "codex", "reset", "2", "--yes",
        "++", "codex", "login", "--alias", "ops", "++", "codex", "auto", "--once", "--dry-run",
        "++", "list",
    ])
    data = json.loads(log.read_text())
    data["stdout"], data["stderr"], data["returncode"] = r.stdout, r.stderr, r.returncode
    return data


class TestCodexInTheSandbox:
    """Codex support runs whole in the sandbox: its process seams are faked
    the way tests/conftest.py fakes them, so nothing is spawned."""

    def test_the_codex_verbs_ran(self, audited_codex_run):
        out = audited_codex_run["stdout"]
        # The exit status is the last command's (list); the output shows the rest ran.
        assert audited_codex_run["returncode"] == 0, audited_codex_run["stderr"]
        assert "Switched #1 maya@example.com → #2 work (maya@northwind.example.com)" in out
        # codex auto --once --dry-run: the reason in words, never the code
        # (the per-event line says "No switch needed", the tick log "· no switch needed").
        assert "no switch needed" in out.lower()
        assert "below-threshold" not in out
        assert "Used a reset credit on #2 work" in out
        assert "Added #3" in out and "ops@example.net" in out
        assert "\nCodex accounts\n" in out  # the unified list's Codex section

    def test_nothing_spawned_no_network_no_violation(self, audited_codex_run):
        kinds = {e["event"] for e in audited_codex_run["events"]}
        assert not kinds & {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system", "os.fork"}
        net = [e for e in audited_codex_run["events"] if e["event"].startswith(("socket.", "http.", "urllib."))]
        assert net == []
        assert audited_codex_run["violations"] == []

    def test_no_file_outside_temp_home_repo_and_interpreter(self, audited_codex_run):
        home = os.path.realpath(audited_codex_run["home"])
        roots = [home, str(REPO), os.path.realpath(sys.base_prefix), os.path.realpath(sys.prefix)]
        outside = sorted({
            e["path"] for e in audited_codex_run["events"]
            if e.get("path")
            and not any(_under(os.path.realpath(e["path"]), r) or _under(e["path"], r) for r in roots)
            and e["path"] not in SYSTEM_READS + DEVICES
        })
        assert outside == []

    def test_codex_login_without_a_scenario_login_reads_as_no_codex_cli(self):
        r = _run(["empty", "--", "codex", "login"])
        assert r.returncode == 1, r.stderr
        assert "codex is not on PATH" in r.stdout + r.stderr


class TestScenarioStates:
    """Each scenario shows the state it is named for."""

    def test_fleet_has_an_api_key_account_and_two_codex_accounts(self):
        r = _run(["fleet", "--", "list"])
        assert r.returncode == 0, r.stderr
        # The placeholder email is a name: never shown as an address.
        assert "#5 ci · API key" in r.stdout and "token.local" not in r.stdout
        # No ledger yet: nothing recorded, as after the menu bar's first scan.
        assert "no usage recorded on this machine" in r.stdout
        codex = r.stdout.split("\nCodex accounts\n", 1)[1]
        assert "#1 maya@example.com · active · Plus" in codex
        assert "47%" in codex  # the live account's weekly figure
        assert "#2 work (maya@northwind.example.com) · Team" in codex
        assert "Holds 1 reset credit" in codex  # the saved account's credit
        assert all(len(line) <= 80 for line in r.stdout.splitlines())

    # The harness decides whether the CLIs are installed (both, unless
    # --without-cli), so these do not depend on this machine's PATH.
    @pytest.mark.parametrize(
        ("scenario", "claude", "codex"),
        [
            ("empty", ("Claude Code is not logged in. Log in with claude (/login), then "
                       "run: qswap add",),
             ("Codex is not logged in. Run codex login, then: qswap codex add",)),
            ("empty-claude", ("Claude Code is logged in as maya@example.com. Add it with: qswap add",),
             ("Codex is not logged in. Run codex login, then: qswap codex add",)),
            ("empty-claude-codex",
             ("Claude Code is logged in as maya@example.com. Add it with: qswap add",),
             ("Codex is logged in as maya@example.com. Add it with: qswap codex add",)),
        ],
    )
    def test_first_run_variants(self, scenario, claude, codex):
        r = _run([scenario, "--", "status"])
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("No accounts are managed yet.\n")  # with a next step
        assert any(text in r.stdout for text in claude)
        r = _run([scenario, "--", "codex", "status"])
        assert r.returncode == 0, r.stderr
        assert any(text in r.stdout for text in codex)
        r = _run([scenario, "--", "list"])
        assert r.returncode == 0, r.stderr  # read-only: never asks, never a traceback
        assert r.stdout.startswith("No accounts are managed yet.\n")
        assert any(text in r.stdout for text in claude)
        r = _run([scenario, "--", "list", "--json"])
        assert json.loads(r.stdout)["accounts"] == []  # nothing managed

    def test_without_the_clis_installed(self):
        r = _run(["--without-cli", "claude", "--without-cli", "codex", "empty", "--",
                  "status", "++", "codex", "status"])
        assert r.returncode == 0, r.stderr
        assert r.stdout.splitlines() == [
            "No accounts are managed yet.",
            "Claude Code was not found on PATH. Install it and log in, then run: qswap add",
            "No Codex accounts are managed yet.",
            "Codex was not found on PATH. Install it and log in, then run: qswap codex add",
        ]
        assert all(len(line) <= 80 for line in r.stdout.splitlines())

    def test_usage_attributes_codex_sessions_to_codex_accounts(self):
        r = _run(["usage", "--", "usage", "--by", "account", "--provider", "codex"])
        assert r.returncode == 0, r.stderr
        # A Codex-only view: account labels without the provider's name.
        assert "  #1 maya@example.com " in r.stdout
        assert "  #2 work " in r.stdout
        assert "not managed" not in r.stdout
        assert "unattributed" not in r.stdout

    def test_trouble_shows_each_failure_state(self):
        r = _run(["trouble", "--", "list"])
        assert r.returncode == 0, r.stderr
        claude = r.stdout.split("\nCodex accounts\n", 1)[0]
        rows = {}
        for block in claude.split("\n\n"):
            number = re.search(r"^\s+#(\d+) ", block, re.M)
            if number:
                rows[number.group(1)] = block
        assert re.search(r"5h\s+100%.*limit reached", rows["1"])
        assert "needs re-login" in rows["2"]
        assert "usage unavailable (HTTP 503), retrying" in rows["3"]
        assert "needs re-login" in rows["4"]  # set aside by the engine
        assert "as of 3h" in rows["5"]
        assert "usage unknown until used" in r.stdout  # the Codex saved login
        assert all(len(line) <= 80 for line in r.stdout.splitlines())

    @staticmethod
    def _account_rows(outline: str) -> list[str]:
        """The account rows of a --menubar outline: top-level rows under a
        provider heading, without the check mark and attention marks."""
        rows, inside = [], False
        for line in outline.splitlines():
            if line.endswith("accounts  (heading)"):
                inside = True
            elif line.startswith("────"):
                inside = False
            elif inside and not line.startswith("    "):
                rows.append(line[2:].removeprefix("⚠ "))
        assert rows
        return rows

    def test_the_menu_bar_says_what_list_says(self):
        # --menubar prints ai_quota_swap.menubar.build_menu's description of
        # the seeded fleet: both providers, the API-key account, held resets.
        r = _run(["fleet", "--menubar"])
        assert r.returncode == 0, r.stderr
        out = r.stdout
        assert out.startswith("title: ⇄ personal · 46% · 38%\n")
        assert "Claude Code accounts  (heading)" in out and "Codex accounts  (heading)" in out
        assert "✓ #1 personal · 5h 46% · 7d 38% · Fable 52% · 1 Fable reset\n" in out
        assert "#3 night-shift · 5h 93% · 7d 81% · Fable 97% · 1 five-hour reset\n" in out
        assert "#2 work · 5h 8% · 7d 15% · 1 reset credit\n" in out
        # After the menu bar's ledger scan the unused API-key account has a
        # zero estimate, which reads as `qswap list` reads with no ledger yet.
        assert "#5 ci · API key · no usage recorded\n" in out
        assert "Codex #2 work (maya@northwind.example.com)" in out  # submenus keep long labels
        assert all(len(row) <= 72 for row in self._account_rows(out))
        r = _run(["trouble", "--menubar"])
        assert r.returncode == 0, r.stderr
        assert "#1 personal · 5h 100% (resets in 1h 12m) · 7d 64%" in r.stdout
        assert "#2 work · needs re-login" in r.stdout
        assert "#4 side · needs re-login" in r.stdout  # set aside
        assert "#3 night-shift · usage unavailable (HTTP 503), retrying" in r.stdout
        assert "#5 spare · 5h 40% · 7d 55% · as of 3h" in r.stdout
        assert all(len(row) <= 72 for row in self._account_rows(r.stdout))
        r = _run(["usage", "--menubar"])
        assert r.returncode == 0, r.stderr
        # qswap usage's total; Haiku 5.5 prompts over 100,000 tokens at
        # their own price (pricing.py, ANTHROPIC_LONG).
        assert "7 days $180.55 (estimated)" in r.stdout
        r = _run(["empty-claude", "--menubar"])
        assert "Claude Code is logged in as maya@example.com. Add it with: qswap add" in r.stdout

    def test_an_active_api_key_renders_in_the_sandbox(self):
        # The documents quote the states under an active key: the switch,
        # doctor (which runs the key line with --check) and the switch to the
        # key already active (which runs it too). The line names the demo's
        # own ~/.local/bin/qswap, never this checkout, and nothing spawns.
        r = _run(["fleet", "--width", "80", "--", "switch", "5", "++", "doctor",
                  "++", "switch", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "crossed the sandbox" not in r.stderr
        assert "Go back with: qswap switch 1" in r.stdout
        assert (
            "  ok    active API key: #5 ci, through qswap's key line in\n"
            "        ~/.claude/settings.json, which runs ~/.local/bin/qswap\n"
        ) in r.stdout
        assert "Already on #5 ci (API key)\n" in r.stdout
        assert str(REPO) not in r.stdout
        # --menubar after commands: the menu for the state they leave.
        r = _run(["fleet", "--menubar", "--", "switch", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "✓ #5 ci · API key · over #1 personal · no usage recorded\n" in r.stdout
        assert "#1 personal · underneath · 5h 46% · 7d 38%" in r.stdout

    def test_last_resort_moves_to_the_key_with_a_budget(self):
        # Every subscription at its limit, the key with a budget and a fresh
        # spend figure: auto-switch's last resort, and what the surfaces say
        # about a key it entered: the --once tick that entered it watches it
        # for 15 minutes (decision DS4).
        r = _run(["last-resort", "--width", "80", "--", "auto", "--once", "++", "status"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Switched #1 personal → #5 ci · 5h limit reached\n" in r.stdout
        assert "#5 ci · active · API key · over #1 personal · by auto-switch" in r.stdout
        assert "Auto-switch entered #5 ci and last checked it just now (qswap auto --once)." in (
            r.stdout
        )
        assert "nothing watches its limit" not in r.stdout
        r = _run(["last-resort", "--", "budget", "5", "off", "++", "auto", "--once"])
        assert r.returncode == 3, r.stdout + r.stderr
        assert "#5 ci has no budget or credit; set one" in r.stdout

    def test_trouble_seeds_a_quarantine_the_engine_honours(self):
        # After `list` reads it, the quarantined account (4) has a fresh,
        # readable reading with headroom, and auto still finds no candidate:
        # the seeded quarantine keeps it out.
        r = _run(["trouble", "--", "list", "++", "auto", "--once", "--json"])
        assert r.returncode == 3, r.stderr
        poll, no_switch = (json.loads(line) for line in r.stdout.strip().splitlines()[-2:])
        assert poll["headroomPct"]["4"] == 70.0
        assert no_switch["reason"] == "no-comparison"


class _Drawing(HTMLParser):
    """A menu bar page read back: the status item's title and each menu's
    rows in drawing order (None for a separator)."""

    def __init__(self, page: str) -> None:
        super().__init__()
        self.menus: list[dict] = []
        self.status = ""
        self.stage_classes: set[str] = set()
        self.sources: list[str] = []
        self._text: dict | None = None
        self._in_status = False
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        for key in ("src", "href"):
            if a.get(key):
                self.sources.append(a[key])
        if a.get("id") == "stage":
            self.stage_classes = classes
        if a.get("id") == "status":
            self._in_status = True
        elif "menu" in classes:
            self.menus.append({"level": int(a["data-level"]), "title": a.get("data-title"), "rows": []})
        elif "item" in classes:
            self.menus[-1]["rows"].append({"classes": classes, "text": ""})
        elif "sep" in classes:
            self.menus[-1]["rows"].append(None)
        elif tag == "span" and classes == {"t"}:
            self._text = self.menus[-1]["rows"][-1]

    def handle_endtag(self, tag):
        if tag == "span":
            self._text = None
            self._in_status = False

    def handle_data(self, data):
        if self._text is not None:
            self._text["text"] += data
        elif self._in_status:
            self.status += data

    def outline(self, level: int) -> list[str]:
        """One drawn menu in ``Menu.outline``'s words, without the indent."""
        menu = next(m for m in self.menus if m["level"] == level)
        lines = []
        for row in menu["rows"]:
            if row is None:
                lines.append("────")
                continue
            c = row["classes"]
            text = ("✓ " if "checked" in c else "  ") + ("⚠ " if "attention" in c else "") + row["text"]
            if "submenu" in c:
                text += " ▸"
            elif "heading" in c:
                text += "  (heading)"
            elif "disabled" in c:
                text += "  (greyed)"
            lines.append(text)
        return lines

    def selected(self, level: int) -> list[str]:
        menu = next(m for m in self.menus if m["level"] == level)
        return [r["text"] for r in menu["rows"] if r is not None and "selected" in r["classes"]]


def _outline_rows(stdout: str, parents: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """``(title, rows)`` from a printed outline: the menu's own rows, or with
    ``parents`` the rows of that submenu, each without its indent."""
    lines = stdout.rstrip("\n").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("title: "))
    title, body = lines[start][len("title: "):], lines[start + 1:]

    def depth(line: str) -> int:
        n = len(line) - len(line.lstrip(" "))
        rest = line[n:]
        return n // 4 if rest.startswith(("✓", "────")) else (n - 2) // 4

    rows = [(depth(line), line) for line in body]
    lo, hi, level = 0, len(rows), 0
    for name in parents:
        lo = next(i for i in range(lo, hi)
                  if rows[i][0] == level and rows[i][1][4 * level:].endswith(f"{name} ▸")
                  and rows[i][1][4 * level + 2:].startswith(name))
        end = next((i for i in range(lo + 1, hi) if rows[i][0] <= level), hi)
        lo, hi, level = lo + 1, end, level + 1
    return title, [line[4 * level:] for d, line in rows[lo:hi] if d == level]


@pytest.fixture(scope="module")
def drawn(tmp_path_factory) -> dict:
    """The menu bar drawn six ways: both themes, a submenu open, two open,
    a key chosen by hand, and the hero's window behind the menu."""
    out = tmp_path_factory.mktemp("menubar-drawing")
    runs = {
        "dark": ["showcase", "--menubar", "--auto-switch"],
        "light": ["showcase", "--menubar", "--theme", "light"],
        "resets": ["showcase", "--menubar", "--open", "Use a reset", "--highlight", "Codex #2"],
        "nested": ["showcase", "--menubar", "--open", "Settings > Use resets automatically"],
        "key": ["fleet", "--menubar", "--", "switch", "5"],
        "hero": ["showcase", "--menubar", "--beside", str(REPO / "assets" / "screenshots" / "raw" / "dashboard.png")],
    }
    results = {}
    for name, args in runs.items():
        page = out / f"{name}.html"
        head, tail = (args[:args.index("--")], args[args.index("--"):]) if "--" in args else (args, [])
        r = _run([*head, "--html", str(page), *tail])
        assert r.returncode == 0, (name, r.stderr)
        assert "crossed the sandbox" not in r.stderr, name
        results[name] = (r.stdout, page.read_text(encoding="utf-8"))
    return results


class TestMenuBarDrawing:
    """``run.py --menubar --html`` draws the rows ``build_menu`` returns (the
    outline it prints is the same ``Menu``), in both themes, with a named
    submenu open, and references nothing outside the page."""

    @pytest.mark.parametrize("name", ["dark", "light", "key"])
    def test_every_row_of_the_menu_is_drawn(self, drawn, name):
        stdout, page = drawn[name]
        title, rows = _outline_rows(stdout)
        d = _Drawing(page)
        assert d.status == title
        assert [m["level"] for m in d.menus] == [0]  # no submenu open
        assert d.outline(0) == rows
        assert d.selected(0) == []

    def test_both_themes(self, drawn):
        _, dark = drawn["dark"]
        _, light = drawn["light"]
        assert _Drawing(dark).stage_classes >= {"stage", "dark"}
        assert _Drawing(light).stage_classes >= {"stage", "light"}
        assert '<meta name="color-scheme" content="light">' in light
        # The same rows whatever the theme (auto-switch aside).
        assert _Drawing(dark).outline(0)[2:] == _Drawing(light).outline(0)[2:]

    def test_the_drawing_shows_what_the_menu_says(self, drawn):
        stdout, page = drawn["dark"]
        assert "Auto-switch running for Claude Code and Codex" in stdout
        rows = _Drawing(page).outline(0)
        assert "✓ #1 personal · 5h 46% · 7d 38% · Fable 52% · 1 Fable reset" in rows
        assert "  ⚠ #3 night-shift · 5h 93% · 7d 81% · Fable 97% · 1 five-hour reset" in rows
        assert "  Claude Code accounts  (heading)" in rows
        assert "  Use a reset ▸" in rows
        # A key chosen by hand: its row checked, the login underneath named.
        key = _Drawing(drawn["key"][1])
        assert key.status == "⇄ ci"
        assert any(r.startswith("✓ #5 ci · API key · over #1 personal") for r in key.outline(0))
        assert any(r.startswith("  #1 personal · underneath") for r in key.outline(0))

    def test_a_named_submenu_opens_beside_the_menu(self, drawn):
        stdout, page = drawn["resets"]
        d = _Drawing(page)
        assert [(m["level"], m["title"]) for m in d.menus] == [(1, "Use a reset"), (0, None)]
        assert d.outline(0) == _outline_rows(stdout)[1]
        assert d.outline(1) == _outline_rows(stdout, ("Use a reset",))[1]
        assert d.selected(0) == ["Use a reset"]  # the open submenu's parent row
        assert d.selected(1) == ["Codex #2 work · reset credit · clears all usage"]
        stdout, page = drawn["nested"]
        d = _Drawing(page)
        assert [m["title"] for m in d.menus] == ["Use resets automatically", "Settings", None]
        assert d.outline(1) == _outline_rows(stdout, ("Settings",))[1]
        assert d.outline(2) == _outline_rows(stdout, ("Settings", "Use resets automatically"))[1]
        assert d.selected(0) == ["Settings"] and d.selected(1) == ["Use resets automatically"]

    def test_an_unknown_submenu_or_row_is_refused(self, tmp_path):
        page = tmp_path / "menu.html"
        r = _run(["showcase", "--menubar", "--html", str(page), "--open", "Use a rest"])
        assert r.returncode == 2
        assert "no submenu 'Use a rest' in the menu" in r.stderr
        assert "'Use a reset'" in r.stderr  # it names the ones there are
        assert not page.exists()
        r = _run(["showcase", "--menubar", "--html", str(page), "--open", "Settings > Nope"])
        assert r.returncode == 2 and "no submenu 'Nope' in submenu 'Settings'" in r.stderr
        # An information row cannot be under the pointer.
        r = _run(["showcase", "--menubar", "--html", str(page), "--highlight", "Today $"])
        assert r.returncode == 2 and "no clickable row starting 'Today $'" in r.stderr
        r = _run(["showcase", "--open", "Settings", "--", "list"])
        assert r.returncode == 2 and "go with --menubar" in r.stderr

    @pytest.mark.parametrize("name", ["dark", "light", "resets", "nested", "key", "hero"])
    def test_the_page_references_nothing_outside_itself(self, drawn, name):
        _, page = drawn[name]
        assert "http://" not in page and "https://" not in page
        assert "<script" not in page and "<link" not in page
        assert "@import" not in page and "url(" not in page
        assert all(s.startswith("data:image/png;base64,") for s in _Drawing(page).sources)
        assert bool(_Drawing(page).sources) == (name == "hero")  # the window behind the menu
        assert str(_real_home()) not in page

    def test_frames_follow_the_engine(self, tmp_path):
        # The menu bar's own engine, hosted as the app hosts it, climbs past
        # its line and switches; a frame is drawn after each check.
        r = _run(["autoswitch", "--menubar", "--auto-switch", "--frames", str(tmp_path),
                  "--ticks", "3"])
        assert r.returncode == 0, r.stderr
        assert "crossed the sandbox" not in r.stderr
        titles = [(tmp_path / f"frame-{i:02d}.txt").read_text().splitlines()[0] for i in range(3)]
        assert titles == ["title: ⇄ personal · 86% · 38%", "title: ⇄ personal · 88% · 38%",
                          "title: ⇄ work · 12% · 21%"]
        last = (tmp_path / "frame-02.txt").read_text()
        assert "Auto-switch running · usage read" in last
        assert "  ⚠ #1 personal · 5h 91%" in last
        assert "#1 personal → #2 work · 2026-11-10 14:22" in last
        for i in range(3):
            closed = _Drawing((tmp_path / f"frame-{i:02d}-closed.html").read_text())
            assert "closed" in closed.stage_classes
            assert closed.status == titles[i].removeprefix("title: ")


class TestHarnessPieces:
    @pytest.fixture(autouse=True)
    def _demo_path(self, monkeypatch):
        monkeypatch.syspath_prepend(str(DEMO))

    def test_scenarios_use_invented_identities_only(self):
        import scenarios

        for s in scenarios.SCENARIOS.values():
            logins = (*s.accounts, *(a for a in (s.live, s.codex_live, s.codex_login) if a is not None))
            for a in logins:
                domain = a.email.rsplit("@", 1)[1]
                # token.local is the tool's own placeholder for an API key added
                # without --email.
                assert domain == "token.local" or domain.split(".")[-2:] in (
                    ["example", "com"], ["example", "org"], ["example", "net"],
                ), a.email
                assert a.org_name in ("", "Northwind Labs")
            if s.ledger:
                for p in s.ledger.projects + s.ledger.codex_projects:
                    assert p.path.startswith("/work/")

    def test_codex_logins_decode_as_chatgpt_logins(self):
        import scenarios

        from ai_quota_swap.codex import auth as codex_auth

        for a in scenarios.FLEET.codex_logins():
            login = codex_auth.parse_login(a.login_record(scenarios.CLOCK))
            assert (login.email, login.user_id, login.account_id) == (a.email, a.user_id, a.account_id)
            assert login.access_expires_at == scenarios.CLOCK.timestamp() + a.token_expires_in.total_seconds()

    def test_loopback_rule(self):
        import guards

        for host in ("127.0.0.1", "::1", "[::1]", "localhost", "127.8.9.10", "::ffff:127.0.0.1"):
            assert guards.is_loopback_host(host), host
        for host in ("0.0.0.0", "192.0.2.1", "api.anthropic.com", "localhost.example.com", None, ""):
            assert not guards.is_loopback_host(host), host

    def test_commands_split_on_plus_plus(self):
        import run

        assert run.split_commands(["reset", "++", "reset", "1", "--yes"]) == [
            ["reset"], ["reset", "1", "--yes"],
        ]
        assert run.split_commands(["list"]) == [["list"]]

    def test_a_bare_double_dash_runs_qswap_with_no_arguments(self):
        r = _run(["empty", "--"])
        assert r.returncode == 0  # off a terminal, bare qswap prints a hint
        assert "qswap help" in r.stdout and "qswap list" in r.stdout
        r = _run(["empty"])  # no `--` at all is still the launcher's own usage error
        assert r.returncode == 2
        assert "give the qswap arguments after --" in r.stderr

    def test_tour_stops_are_well_formed(self):
        import scenarios
        import tour

        names = [s.name for s in tour.STOPS]
        assert len(names) == len(set(names))
        named_keys = {"Enter", "Escape", "Tab", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"}
        for stop in tour.STOPS:
            assert stop.scenario in scenarios.SCENARIOS, stop
            for kind, value in stop.steps:
                assert kind in ("key", "type", "wait"), stop
                if kind == "key":
                    assert value in named_keys, stop
                if kind == "type":
                    assert isinstance(value, str) and value, stop
        # The menu helper follows the dashboard's root menu order.
        from ai_quota_swap.tui.dashboard import ROOT_MENU

        root = [action for _label, action in ROOT_MENU]
        assert [a.removesuffix("-menu") for a in root] == list(tour.MENU)
        assert tour.parse_size("100x30") == (100, 30)
        for stop in tour.STOPS:
            assert all(isinstance(z, tuple) and len(z) == 2 for z in stop.sizes), stop

    def test_littledemo_steps_are_valid_and_keyboard_only(self, tmp_path):
        import stories

        allowed = {"pause", "click", "type", "key", "scroll", "wait", "navigate",
                   "hover", "select", "moveTo", "drag", "screenshot", "callout"}
        for story in stories.STORIES.values():
            config = json.loads(stories.to_littledemo(story, tmp_path / "s.json", viewport=(1280, 760)).read_text())
            assert {s["action"] for s in config["steps"]} <= allowed
            assert not any(s["action"] == "click" for s in config["steps"])
            assert all("selector" not in s for s in config["steps"] if s["action"] == "type")

    def test_html_page_is_self_contained(self, tmp_path):
        out = tmp_path / "list.html"
        r = _run(["fleet", "--html", str(out), "--", "list"])
        assert r.returncode == 0, r.stderr
        page = out.read_text()
        assert "maya@example.com" in page
        assert "http://" not in page and "https://" not in page
        assert "<script" not in page
