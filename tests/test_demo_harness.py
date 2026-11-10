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
* the scenarios use invented identities only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
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


class TestHarnessPieces:
    @pytest.fixture(autouse=True)
    def _demo_path(self, monkeypatch):
        monkeypatch.syspath_prepend(str(DEMO))

    def test_scenarios_use_invented_identities_only(self):
        import scenarios

        for s in scenarios.SCENARIOS.values():
            for a in s.accounts:
                assert a.email.rsplit("@", 1)[1].split(".")[-2:] in (
                    ["example", "com"], ["example", "org"], ["example", "net"],
                ), a.email
                assert a.org_name in ("", "Northwind Labs")
            if s.ledger:
                assert s.ledger.codex_email.endswith("@example.com")
                for p in s.ledger.projects + s.ledger.codex_projects:
                    assert p.path.startswith("/work/")

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
