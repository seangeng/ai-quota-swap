"""``qswap usage --billed``: the organization's usage and billed cost as
Anthropic reports them, per grouping, in text and JSON, and the one-line
outcomes when there is nothing to show."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ai_quota_swap import admin_api, api_cli
from ai_quota_swap.admin_orgs import ENV_VAR, OrgRegistry
from ai_quota_swap.models import Platform

from tests.admin_fakes import (
    ADMIN_KEY,
    KEY_A,
    FakeAnthropic,
    bucket,
    error_response,
    json_response,
    key_item,
    keys_page,
    report,
    usage_result,
)
from tests.api_helpers import (
    data_root,
    make_switcher,
    pin_platform,
    run_main,
    seed_api_key,
    set_record,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()
WINDOW = ["--since", "2026-11-08", "--until", "2026-11-10"]
OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"


def cost(amount_cents: str, **extra) -> dict:
    return {"amount": amount_cents, "currency": "USD", "description": None, "model": None,
            "cost_type": None, "token_type": None, "workspace_id": None, **extra}


def _usage_route(query):
    groups = [v for k, v in query if k == "group_by[]"]
    if groups == ["model"]:
        return json_response(report([
            bucket("2026-11-09", [usage_result(None, OPUS, inp=1_000_000, out=200_000, read=3_000_000),
                                  usage_result(None, HAIKU, inp=50_000)]),
            bucket("2026-11-10", [usage_result(None, OPUS, inp=500_000, w5=100_000)]),
        ]))
    if groups == ["api_key_id", "model", "speed"]:
        return json_response(report([
            bucket("2026-11-09", [usage_result("apikey_A", OPUS, inp=1_000_000),
                                  usage_result(None, HAIKU, inp=10_000)]),
        ]))
    if groups == ["workspace_id"]:
        return json_response(report([
            bucket("2026-11-09", [usage_result(None, OPUS, inp=10, workspace="wrkspc_01X"),
                                  usage_result(None, OPUS, inp=5)]),
        ]))
    return json_response(report([
        bucket("2026-11-08", []),
        bucket("2026-11-09", [usage_result(None, OPUS, inp=1_000_000)]),
        bucket("2026-11-10", [usage_result(None, OPUS, out=100_000)]),
    ]))


def _cost_route(query):
    groups = [v for k, v in query if k == "group_by[]"]
    if groups == ["description"]:
        return json_response(report([
            bucket("2026-11-09", [
                cost("21240.00", model=OPUS, cost_type="tokens", description="Claude Opus 5.5 Usage - Input Tokens"),
                cost("102.00", model=HAIKU, cost_type="tokens"),
                cost("50.00", cost_type="web_search", description="Web Search"),
            ]),
        ]))
    if groups == ["workspace_id"]:
        return json_response(report([
            bucket("2026-11-09", [cost("300.00", workspace_id="wrkspc_01X"), cost("100.00")]),
        ]))
    return json_response(report([
        bucket("2026-11-09", [cost("400.00")]),
        bucket("2026-11-10", [cost("200.00")]),
    ]))


@pytest.fixture
def env(temp_home, monkeypatch):
    pin_platform(monkeypatch, Platform.LINUX)
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(api_cli.time, "time", lambda: NOW)
    admin_api._memory.clear()
    make_switcher()
    return temp_home


@pytest.fixture
def anthropic(monkeypatch):
    fake = FakeAnthropic({
        "/v1/organizations/usage_report/messages": _usage_route,
        "/v1/organizations/cost_report": _cost_route,
        "/v1/organizations/api_keys": [json_response(keys_page([key_item("apikey_A", "Dev laptop", KEY_A)]))],
    })
    monkeypatch.setattr(admin_api, "send", fake)
    return fake


def _register():
    OrgRegistry(data_root(), platform=Platform.LINUX).add("acme", ADMIN_KEY, "org-1", "Acme Corp")


def _billed_text(capsys, *extra):
    run_main(["usage", *WINDOW, "--billed", *extra])
    out = capsys.readouterr().out
    return out[out.index("Billed by Anthropic"):]


class TestOutcomes:
    def test_no_organization(self, env, anthropic, capsys):
        text = _billed_text(capsys)
        assert text.splitlines() == [
            "Billed by Anthropic, 2026-11-08 to 2026-11-10 (UTC days), by day",
            "No organization registered: add one with qswap api org add,",
            "or set ANTHROPIC_ADMIN_KEY for one command.",
        ]
        assert anthropic.calls == []

    def test_rejected_key(self, env, anthropic, capsys):
        _register()
        anthropic.routes["/v1/organizations/usage_report/messages"] = [error_response(401, "authentication_error")]
        text = _billed_text(capsys)
        assert ("acme: Anthropic rejected the Admin key (HTTP 401); replace it: qswap api "
                "org remove acme, then qswap api org add acme.") in text
        assert ADMIN_KEY not in text

    def test_rate_limited(self, env, anthropic, capsys):
        _register()
        anthropic.routes["/v1/organizations/usage_report/messages"] = [error_response(429)]
        assert "rate limiting requests; try again in a few minutes" in _billed_text(capsys)

    def test_nothing_reported_yet(self, env, anthropic, capsys):
        _register()
        anthropic.routes["/v1/organizations/usage_report/messages"] = [json_response(report([bucket("2026-11-10", [])]))]
        anthropic.routes["/v1/organizations/cost_report"] = [json_response(report([bucket("2026-11-10", [])]))]
        text = _billed_text(capsys)
        assert "No usage reported for this window yet (Anthropic's reports lag by a few minutes)." in text
        assert "Usage from the last few minutes may not be in Anthropic's reports yet." in text

    def test_key_and_workspace_need_billed(self, env, capsys):
        with pytest.raises(SystemExit) as exc:
            run_main(["usage", "--by", "key"])
        assert exc.value.code == 64
        assert "--by key needs --billed" in capsys.readouterr().err

    def test_unsupported_grouping_falls_back_to_days(self, env, anthropic, capsys):
        _register()
        text = _billed_text(capsys, "--by", "project")
        assert "by day" in text.splitlines()[0]
        assert "Anthropic's reports cannot group by project; the billed section is by day." in text


class TestGroupings:
    def test_by_day(self, env, anthropic, capsys):
        _register()
        text = _billed_text(capsys)
        lines = [l for l in text.splitlines() if l.startswith("  2026-11-")]
        assert [l.split()[0] for l in lines] == ["2026-11-08", "2026-11-09", "2026-11-10"]
        assert lines[0].split()[-1] == "n/a" and lines[1].split()[-1] == "$4.00"
        assert "$6.00" in [l for l in text.splitlines() if l.strip().startswith("Total")][0]
        usage_call, cost_call = anthropic.calls
        assert usage_call["url"] == (
            "https://api.anthropic.com/v1/organizations/usage_report/messages?starting_at="
            "2026-11-08T00:00:00Z&ending_at=2026-11-11T00:00:00Z&bucket_width=1d&limit=31"
        )
        assert cost_call["url"] == (
            "https://api.anthropic.com/v1/organizations/cost_report?starting_at="
            "2026-11-08T00:00:00Z&ending_at=2026-11-11T00:00:00Z&bucket_width=1d&limit=31"
        )
        assert {c["method"] for c in anthropic.calls} == {"GET"}

    def test_by_model(self, env, anthropic, capsys):
        _register()
        text = _billed_text(capsys, "--by", "model")
        rows = {l.split()[0]: l for l in text.splitlines() if l.startswith("  claude-") or l.startswith("  (")}
        assert rows[OPUS].split()[-1] == "$212.40"
        assert rows[OPUS].split()[1:3] == ["1.5M", "200K"]
        assert rows[HAIKU].split()[-1] == "$1.02"
        assert rows["(web"].split()[-1] == "$0.50"
        assert "$213.92" in [l for l in text.splitlines() if l.strip().startswith("Total")][0]
        usage_q = dict(anthropic.calls[0]["query"])
        cost_groups = [v for k, v in anthropic.calls[1]["query"] if k == "group_by[]"]
        assert usage_q["group_by[]"] == "model" and cost_groups == ["description"]

    def test_by_key_estimates_per_key_and_bills_the_total(self, env, anthropic, capsys):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        set_record(s, 3, apiKeyId="apikey_A", apiOrg="acme", apiKeyHint=KEY_A[:16] + "..." + KEY_A[-4:])
        _register()
        text = _billed_text(capsys, "--by", "key")
        assert "Est. cost" in text
        row = [l for l in text.splitlines() if "Dev laptop" in l][0]
        assert "Dev laptop (#3)" in row and row.split()[-1] == "$4.00"
        assert [l for l in text.splitlines() if "Console usage" in l]
        assert "$6.00" in [l for l in text.splitlines() if l.strip().startswith("Total")][0]
        assert "Per-key cost is estimated from tokens" in text

    def test_by_account_reads_as_key(self, env, anthropic, capsys):
        _register()
        text = _billed_text(capsys, "--by", "account")
        assert "by key" in text.splitlines()[0]

    def test_by_workspace(self, env, anthropic, capsys):
        _register()
        text = _billed_text(capsys, "--by", "workspace")
        assert [l for l in text.splitlines() if "wrkspc_01X" in l][0].split()[-1] == "$3.00"
        assert [l for l in text.splitlines() if "default workspace" in l][0].split()[-1] == "$1.00"

    def test_cached_for_five_minutes(self, env, anthropic, capsys):
        _register()
        _billed_text(capsys)
        _billed_text(capsys)
        assert len(anthropic.calls) == 2


class TestJson:
    def test_shape(self, env, anthropic, capsys):
        _register()
        run_main(["usage", *WINDOW, "--billed", "--by", "model", "--json"])
        doc = json.loads(capsys.readouterr().out)
        assert doc["schemaVersion"] == 1 and "rows" in doc  # the ledger report is unchanged
        billed = doc["billed"]
        assert billed["timezone"] == "UTC" and billed["by"] == "model"
        assert billed["startingAt"] == "2026-11-08T00:00:00Z"
        assert billed["endingAt"] == "2026-11-11T00:00:00Z"
        (org,) = billed["organizations"]
        assert org["name"] == "acme" and org["error"] is None
        opus = [r for r in org["rows"] if r["key"] == OPUS][0]
        assert opus == {
            "key": OPUS, "label": OPUS, "input": 1_500_000, "output": 200_000,
            "cacheRead": 3_000_000, "cacheWrite5m": 100_000, "cacheWrite1h": 0,
            "costUsd": 212.4, "costSource": "cost-report",
        }
        assert org["total"]["billedCostUsd"] == 213.92
        assert ADMIN_KEY not in json.dumps(doc)

    def test_without_billed_there_is_no_key(self, env, capsys):
        run_main(["usage", *WINDOW, "--json"])
        assert "billed" not in json.loads(capsys.readouterr().out)

    def test_error_and_empty(self, env, anthropic, capsys):
        run_main(["usage", *WINDOW, "--billed", "--json"])
        billed = json.loads(capsys.readouterr().out)["billed"]
        assert billed["organizations"] == []
        assert billed["notes"][0].startswith("No organization registered")
        _register()
        anthropic.routes["/v1/organizations/usage_report/messages"] = [error_response(403)]
        run_main(["usage", *WINDOW, "--billed", "--json"])
        (org,) = json.loads(capsys.readouterr().out)["billed"]["organizations"]
        assert org["error"]["type"] == "forbidden"
        assert "may not read usage reports" in org["error"]["message"]
