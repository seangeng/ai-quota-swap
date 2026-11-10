"""The Admin API client (admin_api.py): request shapes, pagination, cache,
backoff, errors, the GET-only guard, and the key never leaking."""

from __future__ import annotations

import json
import logging
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_quota_swap import admin_api
from ai_quota_swap.admin_api import AdminApiError, AdminCache

from tests.admin_fakes import (
    ADMIN_KEY,
    KEY_A,
    FakeAnthropic,
    bucket,
    error_response,
    hint_for,
    json_response,
    key_item,
    keys_page,
    org_info,
    report,
    usage_result,
)

# 2026-11-10 12:00:00 UTC
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()


@dataclass
class FakeOrg:
    name: str = "acme"
    persistent: bool = True
    key: str = ADMIN_KEY
    loads: int = 0

    def admin_key(self) -> str:
        self.loads += 1
        return self.key

    def cache_id(self) -> str:
        return f"org:{self.name}" if self.persistent else f"env:{self.name}"


@pytest.fixture
def fake(monkeypatch):
    f = FakeAnthropic()
    monkeypatch.setattr(admin_api, "send", f)
    return f


@pytest.fixture(autouse=True)
def _fresh_memory():
    admin_api._memory.clear()
    yield
    admin_api._memory.clear()


class TestRequestShape:
    def test_org_info(self, fake):
        fake.routes["/v1/organizations/me"] = [json_response(org_info())]
        info = admin_api.organization_info(ADMIN_KEY)
        assert info == {"id": "11111111-2222-3333-4444-555555555555", "name": "Acme Corp"}
        (call,) = fake.calls
        assert call["method"] == "GET"
        assert call["url"] == "https://api.anthropic.com/v1/organizations/me"
        assert call["headers"]["x-api-key"] == ADMIN_KEY
        assert call["headers"]["anthropic-version"] == "2026-10-21"
        assert call["headers"]["user-agent"].startswith("ai-quota-swap/")
        assert call["timeout"] == admin_api.REQUEST_TIMEOUT_S

    def test_key_listing(self, fake):
        fake.routes["/v1/organizations/api_keys"] = [
            json_response(keys_page([key_item("apikey_01", "Dev", KEY_A)]))
        ]
        keys = admin_api.get_api_keys(ADMIN_KEY)
        assert keys == [{
            "id": "apikey_01", "name": "Dev", "hint": hint_for(KEY_A),
            "status": "active", "workspaceId": "wrkspc_01Fake",
        }]
        (call,) = fake.calls
        assert call["url"] == "https://api.anthropic.com/v1/organizations/api_keys?limit=1000"

    def test_month_usage_query(self, fake):
        fake.routes["/v1/organizations/usage_report/messages"] = [json_response(report([]))]
        org = FakeOrg()
        admin_api.month_usage_by_key(
            org, ["apikey_02", "apikey_01"], now=NOW, network=True, cache=AdminCache(None)
        )
        (call,) = fake.calls
        assert call["url"] == (
            "https://api.anthropic.com/v1/organizations/usage_report/messages"
            "?starting_at=2026-11-01T00:00:00Z&ending_at=2026-11-11T00:00:00Z"
            "&bucket_width=1d&group_by[]=api_key_id&group_by[]=model&group_by[]=speed"
            "&api_key_ids[]=apikey_01&api_key_ids[]=apikey_02&limit=31"
        )
        assert call["headers"]["x-api-key"] == ADMIN_KEY

    def test_cost_report_query_shape(self, fake):
        fake.routes["/v1/organizations/cost_report"] = [json_response(report([]))]
        params = [
            ("starting_at", "2026-11-04T00:00:00Z"), ("ending_at", "2026-11-11T00:00:00Z"),
            ("bucket_width", "1d"), ("group_by[]", "description"), ("limit", "31"),
        ]
        admin_api.get_report_pages(admin_api.COST_REPORT_PATH, params, ADMIN_KEY)
        assert fake.calls[0]["url"] == (
            "https://api.anthropic.com/v1/organizations/cost_report?starting_at="
            "2026-11-04T00:00:00Z&ending_at=2026-11-11T00:00:00Z&bucket_width=1d"
            "&group_by[]=description&limit=31"
        )

    def test_key_is_never_in_the_url(self, fake):
        fake.routes["/v1/organizations/me"] = [json_response(org_info())]
        admin_api.organization_info(ADMIN_KEY)
        assert ADMIN_KEY not in fake.calls[0]["url"]
        assert [k for k, v in fake.calls[0]["headers"].items() if v == ADMIN_KEY] == ["x-api-key"]


class TestGetOnly:
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "get"])
    def test_request_refuses_other_methods_before_any_io(self, fake, method):
        with pytest.raises(ValueError, match="only sends GET"):
            admin_api.request(method, admin_api.API_KEYS_PATH, None, ADMIN_KEY)
        assert fake.calls == []

    @pytest.mark.parametrize("method", ["POST", "DELETE", "PUT"])
    def test_transport_refuses_other_methods_before_urlopen(self, monkeypatch, method):
        def boom(*a, **k):
            raise AssertionError("urlopen reached")

        monkeypatch.setattr(admin_api.urllib.request, "urlopen", boom)
        with pytest.raises(ValueError, match="only sends GET"):
            admin_api._urllib_send(method, "https://api.anthropic.com/x", {}, 1.0)

    def test_transport_builds_a_get_request(self, monkeypatch):
        seen = {}

        class Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self, n=-1):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            seen["method"] = req.get_method()
            seen["data"] = req.data
            return Resp()

        monkeypatch.setattr(admin_api.urllib.request, "urlopen", fake_urlopen)
        admin_api._urllib_send("GET", "https://api.anthropic.com/x", {"x-api-key": "k"}, 1.0)
        assert seen == {"method": "GET", "data": None}


class TestPagination:
    def test_report_follows_next_page(self, fake):
        pages = [
            json_response(report([bucket("2026-11-01", [])], next_page="page_2")),
            json_response(report([bucket("2026-11-02", [])], next_page="page_3")),
            json_response(report([bucket("2026-11-03", [])])),
        ]
        fake.routes["/v1/organizations/usage_report/messages"] = pages
        buckets = admin_api.get_report_pages(
            admin_api.USAGE_REPORT_PATH, [("starting_at", "2026-11-01T00:00:00Z")], ADMIN_KEY
        )
        assert [b["starting_at"][:10] for b in buckets] == ["2026-11-01", "2026-11-02", "2026-11-03"]
        assert [dict(c["query"]).get("page") for c in fake.calls] == [None, "page_2", "page_3"]

    def test_key_listing_follows_last_id(self, fake):
        fake.routes["/v1/organizations/api_keys"] = [
            json_response(keys_page([key_item("apikey_01", "a", KEY_A)], has_more=True)),
            json_response(keys_page([key_item("apikey_02", "b", None)])),
        ]
        keys = admin_api.get_api_keys(ADMIN_KEY)
        assert [k["id"] for k in keys] == ["apikey_01", "apikey_02"]
        assert keys[1]["hint"] is None
        assert [dict(c["query"]).get("after_id") for c in fake.calls] == [None, "apikey_01"]

    def test_has_more_without_cursor_is_malformed(self, fake):
        fake.routes["/v1/organizations/usage_report/messages"] = [
            json_response({"data": [], "has_more": True, "next_page": None})
        ]
        with pytest.raises(AdminApiError) as exc:
            admin_api.get_report_pages(admin_api.USAGE_REPORT_PATH, [], ADMIN_KEY)
        assert exc.value.kind == "malformed"

    def test_page_cap(self, fake, monkeypatch):
        monkeypatch.setattr(admin_api, "MAX_PAGES", 3)
        fake.routes["/v1/organizations/usage_report/messages"] = lambda q: json_response(
            report([], next_page="again")
        )
        with pytest.raises(AdminApiError) as exc:
            admin_api.get_report_pages(admin_api.USAGE_REPORT_PATH, [], ADMIN_KEY)
        assert exc.value.kind == "malformed"
        assert len(fake.calls) == 3


class TestErrors:
    @pytest.mark.parametrize(
        "status,kind",
        [(401, "auth"), (403, "forbidden"), (429, "rate-limited"), (500, "server"),
         (529, "server"), (504, "server"), (400, "http"), (404, "http")],
    )
    def test_status_kinds(self, fake, status, kind):
        fake.routes["/v1/organizations/me"] = [error_response(status, "x_error", "bad")]
        with pytest.raises(AdminApiError) as exc:
            admin_api.organization_info(ADMIN_KEY)
        assert exc.value.kind == kind
        assert exc.value.status == status
        assert f"HTTP {status}" in str(exc.value)

    def test_retry_after_is_read(self, fake):
        fake.routes["/v1/organizations/me"] = [
            error_response(429, "rate_limit_error", "slow down", {"retry-after": "42"})
        ]
        with pytest.raises(AdminApiError) as exc:
            admin_api.organization_info(ADMIN_KEY)
        assert exc.value.retry_after == 42.0

    @pytest.mark.parametrize(
        "raised,kind",
        [
            (TimeoutError("timed out"), "timeout"),
            (urllib.error.URLError(TimeoutError("t")), "timeout"),
            (urllib.error.URLError("Name or service not known"), "network"),
            (ConnectionResetError("reset"), "network"),
        ],
    )
    def test_transport_failures(self, fake, raised, kind):
        fake.routes["/v1/organizations/me"] = [raised]
        with pytest.raises(AdminApiError) as exc:
            admin_api.organization_info(ADMIN_KEY)
        assert exc.value.kind == kind

    @pytest.mark.parametrize(
        "body",
        [b"not json", b"[1, 2]", b'{"data": "nope", "has_more": false}', b"\xff\xfe"],
    )
    def test_malformed_bodies(self, fake, body):
        fake.routes["/v1/organizations/usage_report/messages"] = [
            admin_api.Response(200, {}, body)
        ]
        with pytest.raises(AdminApiError) as exc:
            admin_api.get_report_pages(admin_api.USAGE_REPORT_PATH, [], ADMIN_KEY)
        assert exc.value.kind == "malformed"

    @pytest.mark.parametrize(
        "result",
        [
            {"api_key_id": "k", "uncached_input_tokens": "12"},
            {"api_key_id": "k", "output_tokens": -1},
            {"api_key_id": "k", "cache_creation": [1]},
        ],
    )
    def test_malformed_token_counts(self, result):
        with pytest.raises(AdminApiError) as exc:
            admin_api.tokens_by_key_day([bucket("2026-11-01", [result])])
        assert exc.value.kind == "malformed"

    def test_cost_amount_is_cents(self):
        assert admin_api.amount_usd({"amount": "123.45", "currency": "USD"}) == pytest.approx(1.2345)
        assert admin_api.amount_usd({"amount": "41280.000000", "currency": "USD"}) == pytest.approx(412.80)
        for bad in ({"amount": None}, {"amount": "lots"}, {"amount": "1", "currency": "EUR"}):
            with pytest.raises(AdminApiError):
                admin_api.amount_usd(bad)


class TestKeyNeverLeaks:
    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
    def test_not_in_errors_or_logs(self, fake, caplog, status, tmp_path):
        # A hostile or buggy server echoing the key back must not get it into
        # an exception message or a log line.
        fake.routes["/v1/organizations/usage_report/messages"] = [
            error_response(status, "x_error", f"bad key {ADMIN_KEY}")
        ]
        caplog.set_level(logging.DEBUG, logger="ai-quota-swap")
        org = FakeOrg()
        result = admin_api.cached_report(
            org, admin_api.USAGE_REPORT_PATH, [("starting_at", "2026-11-01T00:00:00Z")],
            now=NOW, network=True, cache=AdminCache(tmp_path),
        )
        assert result.buckets is None and result.error is not None
        assert ADMIN_KEY not in str(result.error)
        assert ADMIN_KEY not in repr(result.error.args)
        assert ADMIN_KEY not in caplog.text
        assert ADMIN_KEY not in (tmp_path / "cache" / "admin_api.json").read_text()

    def test_not_in_the_cache_after_success(self, fake, tmp_path):
        fake.routes["/v1/organizations/usage_report/messages"] = [
            json_response(report([bucket("2026-11-01", [usage_result("apikey_01", "claude-opus-5-5", inp=5)])]))
        ]
        fake.routes["/v1/organizations/api_keys"] = [
            json_response(keys_page([key_item("apikey_01", "Dev", KEY_A)]))
        ]
        org = FakeOrg()
        cache = AdminCache(tmp_path)
        admin_api.month_usage_by_key(org, ["apikey_01"], now=NOW, network=True, cache=cache)
        admin_api.api_key_list(org, now=NOW, network=True, cache=cache, unseen={"fp"})
        text = (tmp_path / "cache" / "admin_api.json").read_text()
        assert ADMIN_KEY not in text
        assert KEY_A not in text  # nor a managed key: the listing holds hints only

    def test_redact(self):
        assert admin_api.redact(f"x {ADMIN_KEY} y") == "x sk-ant-[redacted] y"
        assert admin_api.redact(f"{KEY_A}") == "sk-ant-[redacted]"


class TestCache:
    def _route(self, fake, n=1):
        fake.routes["/v1/organizations/usage_report/messages"] = lambda q: json_response(
            report([bucket("2026-11-10", [usage_result("apikey_01", "claude-opus-5-5", inp=n)])])
        )

    def _read(self, org, cache, now, network=True):
        return admin_api.month_usage_by_key(org, ["apikey_01"], now=now, network=network, cache=cache)

    def test_answer_is_reused_within_the_ttl(self, fake, tmp_path):
        self._route(fake)
        org, cache = FakeOrg(), AdminCache(tmp_path)
        first = self._read(org, cache, NOW)
        again = self._read(org, cache, NOW + admin_api.CACHE_TTL_S - 1)
        assert len(fake.calls) == 1
        assert first.buckets == again.buckets and again.fetched_at == NOW
        # The key is not even loaded while the cache serves.
        assert org.loads == 1

    def test_refetched_after_the_ttl(self, fake, tmp_path):
        self._route(fake)
        org, cache = FakeOrg(), AdminCache(tmp_path)
        self._read(org, cache, NOW)
        later = self._read(org, cache, NOW + admin_api.CACHE_TTL_S + 1)
        assert len(fake.calls) == 2
        assert later.fetched_at == NOW + admin_api.CACHE_TTL_S + 1

    def test_no_network_serves_stale_or_nothing(self, fake, tmp_path):
        self._route(fake)
        org, cache = FakeOrg(), AdminCache(tmp_path)
        assert self._read(org, cache, NOW, network=False).buckets is None
        assert fake.calls == []
        self._read(org, cache, NOW)
        stale = self._read(org, cache, NOW + 600, network=False)
        assert stale.stale and stale.buckets is not None
        assert stale.error.kind == "not-read"
        too_old = self._read(org, cache, NOW + admin_api.STALE_MAX_S + 1, network=False)
        assert too_old.buckets is None

    def test_failure_backs_off_and_serves_stale(self, fake, tmp_path):
        org, cache = FakeOrg(), AdminCache(tmp_path)
        self._route(fake)
        self._read(org, cache, NOW)
        fake.routes["/v1/organizations/usage_report/messages"] = [error_response(503)]
        t = NOW + admin_api.CACHE_TTL_S + 1
        failed = self._read(org, cache, t)
        assert failed.stale and failed.error.kind == "server"
        calls = len(fake.calls)
        # Backing off: no request within the backoff, even past the TTL.
        again = self._read(org, cache, t + 30)
        assert len(fake.calls) == calls and again.stale
        # After it, a request again.
        self._route(fake)
        fresh = self._read(org, cache, t + admin_api.BACKOFF_BASE_S + 1)
        assert not fresh.stale and len(fake.calls) == calls + 1

    def test_backoff_doubles(self, fake, tmp_path):
        org, cache = FakeOrg(), AdminCache(tmp_path)
        fake.routes["/v1/organizations/usage_report/messages"] = [error_response(500)]
        self._read(org, cache, NOW)
        until1 = cache.read(org)["backoff"]["until"]
        self._read(org, cache, until1 + 1)
        until2 = cache.read(org)["backoff"]["until"]
        assert until1 - NOW == admin_api.BACKOFF_BASE_S
        assert until2 - (until1 + 1) == 2 * admin_api.BACKOFF_BASE_S

    def test_429_honours_retry_after(self, fake, tmp_path):
        org, cache = FakeOrg(), AdminCache(tmp_path)
        fake.routes["/v1/organizations/usage_report/messages"] = [
            error_response(429, headers={"retry-after": "900"})
        ]
        self._read(org, cache, NOW)
        backoff = cache.read(org)["backoff"]
        assert backoff["until"] == NOW + 900 and backoff["kind"] == "rate-limited"

    def test_rejected_key_waits_long(self, fake, tmp_path):
        org, cache = FakeOrg(), AdminCache(tmp_path)
        fake.routes["/v1/organizations/usage_report/messages"] = [error_response(401)]
        self._read(org, cache, NOW)
        assert cache.read(org)["backoff"]["until"] == NOW + admin_api.AUTH_BACKOFF_S

    def test_month_rollover_misses_the_cache(self, fake, tmp_path):
        self._route(fake)
        org, cache = FakeOrg(), AdminCache(tmp_path)
        before = datetime(2026, 11, 30, 23, 59, 30, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 12, 1, 0, 0, 5, tzinfo=timezone.utc).timestamp()
        self._read(org, cache, before)
        self._read(org, cache, after)
        assert len(fake.calls) == 2
        assert dict(fake.calls[1]["query"])["starting_at"] == "2026-12-01T00:00:00Z"
        assert dict(fake.calls[1]["query"])["ending_at"] == "2026-12-02T00:00:00Z"

    def test_environment_org_is_never_written_to_disk(self, fake, tmp_path):
        self._route(fake)
        org, cache = FakeOrg(name="env", persistent=False), AdminCache(tmp_path)
        self._read(org, cache, NOW)
        self._read(org, cache, NOW + 10)
        assert len(fake.calls) == 1
        assert not (tmp_path / "cache" / "admin_api.json").exists()

    def test_unreadable_key_is_an_error_not_a_crash(self, fake, tmp_path):
        class Broken(FakeOrg):
            def admin_key(self):
                raise RuntimeError("keychain locked")

        result = self._read(Broken(), AdminCache(tmp_path), NOW)
        assert result.buckets is None and result.error.kind == "key-unreadable"
        assert fake.calls == []

    def test_corrupt_cache_file_is_ignored(self, fake, tmp_path):
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "admin_api.json").write_text("{not json")
        self._route(fake)
        result = self._read(FakeOrg(), AdminCache(tmp_path), NOW)
        assert result.buckets is not None
        assert json.loads((tmp_path / "cache" / "admin_api.json").read_text())["schemaVersion"] == 1


class TestKeyList:
    def test_refresh_rules(self, fake, tmp_path):
        fake.routes["/v1/organizations/api_keys"] = lambda q: json_response(
            keys_page([key_item("apikey_01", "Dev", KEY_A)])
        )
        org, cache = FakeOrg(), AdminCache(tmp_path)
        first = admin_api.api_key_list(org, now=NOW, network=True, cache=cache, unseen={"a"})
        assert first.refreshed and first.seen == {"a"}
        # Nothing new: served from the cache.
        admin_api.api_key_list(org, now=NOW + 3600, network=True, cache=cache, unseen={"a"})
        assert len(fake.calls) == 1
        # An unseen managed key, but within the floor: still cached.
        admin_api.api_key_list(org, now=NOW + 60, network=True, cache=cache, unseen={"b"})
        assert len(fake.calls) == 1
        # Past the floor: listed again, and b is remembered.
        again = admin_api.api_key_list(
            org, now=NOW + admin_api.KEY_LIST_MIN_REFRESH_S + 1, network=True, cache=cache, unseen={"b"}
        )
        assert again.refreshed and again.seen == {"a", "b"} and len(fake.calls) == 2
        # Daily refresh.
        admin_api.api_key_list(org, now=NOW + admin_api.KEY_LIST_TTL_S + 700, network=True, cache=cache)
        assert len(fake.calls) == 3
