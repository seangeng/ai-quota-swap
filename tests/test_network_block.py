"""The global network block in tests/conftest.py.

Every destination here reaches nothing even if the block were broken: a host
under the reserved ``.invalid`` TLD never resolves, and 192.0.2.0/24 is a
documentation range that is never routed.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests import conftest

NOWHERE_HOST = "qswap-network-block.invalid"
NOWHERE_IP = "192.0.2.1"  # TEST-NET-1


@pytest.fixture
def take_attempts():
    """The attempts this test made so far, cleared as they are taken, so the
    block's own hook does not fail the test that is checking the block."""
    assert conftest._NETWORK.drain() == []
    yield conftest._NETWORK.drain
    conftest._NETWORK.drain()


def test_create_connection_is_refused_before_any_lookup(take_attempts, monkeypatch, request):
    def no_lookup(*args, **kwargs):
        raise AssertionError("resolved a host the block should have refused")

    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)
    with pytest.raises(conftest.NetworkAccessBlocked):
        socket.create_connection((NOWHERE_HOST, 443), timeout=0.5)
    assert take_attempts() == [
        ("socket.create_connection", (NOWHERE_HOST, 443), request.node.nodeid)
    ]


def test_urlopen_is_refused(take_attempts):
    with pytest.raises(urllib.error.URLError) as exc:
        urllib.request.urlopen(f"https://{NOWHERE_HOST}/", timeout=0.5)
    assert isinstance(exc.value.reason, conftest.NetworkAccessBlocked)
    assert [address for _via, address, _during in take_attempts()] == [(NOWHERE_HOST, 443)]


def test_a_package_caller_that_swallows_errors_is_still_recorded(take_attempts, monkeypatch):
    """The block does not depend on where the urlopen lives: a module of the
    package that catches every exception still leaves a record."""
    from ai_quota_swap import update_check

    monkeypatch.setattr(update_check, "PYPI_URL", f"https://{NOWHERE_HOST}/pypi/json")
    assert update_check._pypi_latest_version() is None
    assert [address for _via, address, _during in take_attempts()] == [(NOWHERE_HOST, 443)]


def test_raw_sockets_to_remote_addresses_are_refused(take_attempts):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        tcp.settimeout(0.5)
        with pytest.raises(conftest.NetworkAccessBlocked):
            tcp.connect((NOWHERE_IP, 80))
        assert tcp.connect_ex((NOWHERE_IP, 80)) == errno.ECONNREFUSED
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        with pytest.raises(conftest.NetworkAccessBlocked):
            udp.connect((NOWHERE_IP, 53))
    assert [via for via, _address, _during in take_attempts()] == [
        "socket.connect", "socket.connect_ex", "socket.connect",
    ]


def test_a_loopback_proxy_cannot_carry_a_request_out(take_attempts, monkeypatch):
    """urllib would send the request to the proxy, on loopback, which the block
    allows; with no proxy it goes to the real host, which is refused."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    assert urllib.request.getproxies() == {}
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"https://{NOWHERE_HOST}/", timeout=0.5)
    assert [address for _via, address, _during in take_attempts()] == [(NOWHERE_HOST, 443)]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_connections_are_allowed(take_attempts, host):
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection((host, port), timeout=2):
            pass
    assert take_attempts() == []


def test_ipv6_loopback_is_allowed(take_attempts):
    try:
        server = socket.create_server(("::1", 0), family=socket.AF_INET6)
    except OSError:
        pytest.skip("no IPv6 loopback on this machine")
    with server:
        port = server.getsockname()[1]
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(("::1", port))
    assert take_attempts() == []


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="no Unix sockets"
)
def test_unix_sockets_are_allowed(take_attempts):
    # A short directory: AF_UNIX paths are capped near 104 bytes on macOS.
    directory = tempfile.mkdtemp(prefix="qsnb")
    try:
        path = os.path.join(directory, "s")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    assert take_attempts() == []


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.8.9.10", "::1", "[::1]", "::ffff:127.0.0.1", "::1%lo0",
     "localhost", "LOCALHOST", "api.localhost", "0.0.0.0", "::", "", b"127.0.0.1"],
)
def test_hosts_on_this_machine(host):
    assert conftest._is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    ["192.0.2.1", "10.0.0.1", "192.168.1.1", "169.254.1.1", "::ffff:192.0.2.1",
     "2001:db8::1", "api.anthropic.com", "localhost.example", "notlocalhost", None, 7],
)
def test_hosts_off_this_machine(host):
    assert not conftest._is_loopback_host(host)


@pytest.mark.allow_network
def test_the_marker_lifts_the_block():
    """No connection is made: the policy alone is checked."""
    assert conftest._NETWORK.allowed is True
    assert conftest._destination_allowed(socket.AF_INET, (NOWHERE_IP, 80))


def test_the_block_is_on_without_the_marker():
    assert conftest._NETWORK.allowed is False
    assert not conftest._destination_allowed(socket.AF_INET, (NOWHERE_IP, 80))
    assert conftest._destination_allowed(getattr(socket, "AF_UNIX", -1), "/tmp/s")


_INNER_TESTS = '''
import socket
import urllib.request

import pytest

NOWHERE = "qswap-network-block.invalid"


def test_swallowed():
    try:
        urllib.request.urlopen(f"https://{NOWHERE}/", timeout=0.5)
    except Exception:
        pass


def test_propagated():
    socket.create_connection((NOWHERE, 443), timeout=0.5)


def test_loopback():
    with socket.create_server(("127.0.0.1", 0)) as server:
        with socket.create_connection(server.getsockname(), timeout=2):
            pass


@pytest.fixture
def connects_at_teardown():
    yield
    try:
        socket.create_connection((NOWHERE, 80), timeout=0.5)
    except OSError:
        pass


def test_fixture_teardown(connects_at_teardown):
    pass
'''


def test_an_attempt_fails_its_test_even_when_swallowed(tmp_path):
    """End to end, in a child pytest running this same conftest: the code
    under test catching the error does not save the test, and an attempt in
    fixture teardown fails the test at teardown."""
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shutil.copy(conftest.__file__, inner / "conftest.py")
    (inner / "test_inner.py").write_text(textwrap.dedent(_INNER_TESTS), encoding="utf-8")

    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PYTEST_") and k != "PYTHONDONTWRITEBYTECODE"
    }
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-rfE",
            "-p", "no:cacheprovider", "-p", "no:xdist",
            "-c", str(inner / "pytest.ini"), "--rootdir", str(inner),
            "--basetemp", str(tmp_path / "basetemp"),
            str(inner / "test_inner.py"),
        ],
        capture_output=True, text=True, env=env, cwd=inner, timeout=120,
    )
    out = result.stdout + result.stderr

    assert "2 failed, 2 passed, 1 error" in out, out
    assert "FAILED test_inner.py::test_swallowed" in out, out
    assert "FAILED test_inner.py::test_propagated" in out, out
    assert "ERROR test_inner.py::test_fixture_teardown" in out, out
    assert "A test tried to open a real outbound connection" in out, out
    assert f"socket.create_connection to ('{NOWHERE_HOST}', 443)" in out, out
