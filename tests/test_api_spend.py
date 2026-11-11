"""Spend readings for API-key accounts (api_spend.py): linking by partial hint,
the Admin and ledger sources, budgets, month rollover, and every surface that
shows them (list text, list --json, TUI card and mini row, menu bar row)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ai_quota_swap import admin_api, api_spend
from ai_quota_swap.admin_orgs import ENV_VAR, OrgRegistry
from ai_quota_swap.ledger.activations import identity_key
from ai_quota_swap.ledger.store import DB_FILENAME, LAST_SCAN_META, LedgerStore
from ai_quota_swap.models import Platform
from ai_quota_swap.switcher import ClaudeAccountSwitcher

from tests.admin_fakes import (
    ADMIN_KEY,
    KEY_A,
    KEY_B,
    KEY_C,
    OTHER_ADMIN_KEY,
    FakeAnthropic,
    bucket,
    error_response,
    hint_for,
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
    record,
    run_main,
    seed_api_key,
    seed_oauth,
    set_record,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()
OPUS = "claude-opus-5-5"  # $4 in, $20 out, $8 fast in, per million (pricing.py)


@pytest.fixture
def env(temp_home, monkeypatch):
    pin_platform(monkeypatch, Platform.LINUX)
    monkeypatch.delenv(ENV_VAR, raising=False)
    admin_api._memory.clear()
    return temp_home


@pytest.fixture
def anthropic(monkeypatch):
    fake = FakeAnthropic({
        "/v1/organizations/api_keys": [json_response(keys_page([
            key_item("apikey_A", "Dev laptop", KEY_A),
            key_item("apikey_B", "CI", KEY_B),
        ]))],
        "/v1/organizations/usage_report/messages": lambda q: json_response(report([
            bucket("2026-11-03", [usage_result("apikey_A", OPUS, inp=1_000_000)]),
            bucket("2026-11-10", [
                usage_result("apikey_A", OPUS, out=100_000),
                usage_result("apikey_A", OPUS, inp=1_000_000, speed="fast"),
                usage_result("apikey_B", OPUS, inp=500_000),
            ]),
        ])),
    })
    monkeypatch.setattr(admin_api, "send", fake)
    return fake


def _register(name="acme", key=ADMIN_KEY, org_id="org-1"):
    OrgRegistry(data_root(), platform=Platform.LINUX).add(name, key, org_id, name.title())


def _infos(s: ClaudeAccountSwitcher) -> dict:
    return {
        str(i[0]): i for i in s._build_accounts_info()
        if s._account_kind(str(i[0])) == "api_key"
    }


def _readings(s, fetch=None, now=NOW, env_vars=None):
    return api_spend.readings(s, _infos(s), fetch=fetch, now=now, env=env_vars or {})


def _ledger(email: str, rows: list[tuple[float, str, int, int]], *, scanned_at=NOW):
    """Seed ledger.db: ``(epoch, model, input, output)`` per bucket."""
    store = LedgerStore(data_root() / DB_FILENAME)
    store.add_buckets([
        ("claude", int(ts // 3600), identity_key(email, ""), model, "/p", f"s{i}",
         inp, out, 0, 0, 0, 0, 1)
        for i, (ts, model, inp, out) in enumerate(rows)
    ])
    if scanned_at is not None:
        store.set_meta(LAST_SCAN_META, str(scanned_at))
    store.close()


class TestHints:
    def test_matching_rule(self):
        hint = hint_for(KEY_A)
        assert hint.startswith("sk-ant-api03-") and "..." in hint
        assert api_spend.hint_matches(hint, KEY_A)
        assert not api_spend.hint_matches(hint, KEY_B)
        assert not api_spend.hint_matches(hint, hint.replace("...", ""))
        for bad in (None, "", "sk-ant-api03-R2D", "sk-ant-api03-R2D...ig",
                    "abc...igAA", "sk-ant-...igAA", "sk-ant-a...b...igAA"):
            assert not api_spend.hint_matches(bad, KEY_A)
        assert api_spend.hint_matches(KEY_A[:16] + "…" + KEY_A[-4:], KEY_A)


class TestLinking:
    def test_match_is_stored_on_the_record(self, env, anthropic):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        seed_api_key(s, 4, KEY_B, email="ci@example.com")
        _register()
        out = _readings(s)
        assert out["3"]["api_key_id"] == "apikey_A" and out["3"]["org"] == "acme"
        assert out["4"]["api_key_id"] == "apikey_B"
        rec = record(make_switcher(), 3)
        assert (rec["apiKeyId"], rec["apiOrg"], rec["apiKeyHint"]) == ("apikey_A", "acme", hint_for(KEY_A))
        assert KEY_A not in json.dumps(make_switcher()._get_sequence_data())
        # A second pass trusts the stored link: no new listing.
        calls = anthropic.paths().count("/v1/organizations/api_keys")
        _readings(s, now=NOW + 400)
        assert anthropic.paths().count("/v1/organizations/api_keys") == calls

    def test_no_match_falls_back_with_a_note(self, env, anthropic):
        s = make_switcher()
        seed_api_key(s, 5, KEY_C)
        _register()
        out = _readings(s)
        assert out["5"]["source"] is None
        assert out["5"]["note"] == "key not found in a registered organization"
        assert "apiKeyId" not in record(make_switcher(), 5)
        # The unmatched key is remembered, so the next pass does not list again.
        calls = len(anthropic.calls)
        _readings(s, now=NOW + 60)
        assert len(anthropic.calls) == calls

    def test_two_listed_keys_with_the_same_hint_link_nothing(self, env, anthropic):
        twin = KEY_A[:16] + "f" * 80 + KEY_A[-4:]
        anthropic.routes["/v1/organizations/api_keys"] = [json_response(keys_page([
            key_item("apikey_A", "one", KEY_A), key_item("apikey_T", "two", twin),
        ]))]
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _register()
        out = _readings(s)
        assert "share this key's hint" in out["3"]["note"]
        assert "api_key_id" not in out["3"]
        assert "apiKeyId" not in record(make_switcher(), 3)

    def test_ambiguous_across_organizations(self, env, anthropic, monkeypatch):
        # Both organizations list a key with KEY_A's hint.
        anthropic.routes["/v1/organizations/api_keys"] = lambda q: json_response(
            keys_page([key_item("apikey_X", "x", KEY_A)])
        )
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _register("acme", ADMIN_KEY, "org-1")
        _register("beta", OTHER_ADMIN_KEY, "org-2")
        out = _readings(s)
        assert "2 listed API keys share this key's hint" in out["3"]["note"]

    def test_two_managed_accounts_on_one_listed_key_link_neither(self, env, anthropic):
        twin = KEY_A[:16] + "g" * 80 + KEY_A[-4:]
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        seed_api_key(s, 4, twin, email="twin@example.com")
        _register()
        out = _readings(s)
        for num in ("3", "4"):
            assert out[num]["note"] == "another managed account matches the same API key; not linked"

    def test_a_replaced_key_is_relinked(self, env, anthropic):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _register()
        _readings(s)
        assert record(make_switcher(), 3)["apiKeyId"] == "apikey_A"
        # The same slot now holds key B: the stored hint no longer matches it.
        s._write_account_credentials("3", email, KEY_B)
        out = _readings(make_switcher(), now=NOW + admin_api.KEY_LIST_MIN_REFRESH_S + 5)
        assert out["3"]["api_key_id"] == "apikey_B"
        assert record(make_switcher(), 3)["apiKeyId"] == "apikey_B"

    def test_listing_failure_says_so(self, env, anthropic):
        anthropic.routes["/v1/organizations/api_keys"] = [error_response(403, "permission_error")]
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _register()
        out = _readings(s)
        assert out["3"]["note"] == "Admin key may not read usage reports"


class TestAdminSource:
    def test_reading(self, env, anthropic):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _register()
        set_record(s, 3, budget=20)
        reading = _readings(s)["3"]
        # Nov 3: 1M in ($4); today: 100k out ($2) + 1M fast in ($8).
        assert reading["source"] == "admin-api"
        assert reading["spend"] == {
            "used": 14.0, "limit": 20.0, "pct": 70.0, "currency": "USD",
            "resets_at": "2026-12-01T00:00:00Z",
        }
        assert reading["today"] == 10.0
        assert reading["month"] == "2026-11"
        assert reading["as_of"] == NOW
        assert reading["api_key_name"] == "Dev laptop"
        assert "note" not in reading
        assert api_spend.spend_text(reading, now=NOW) == (
            "API key · $10.00 today · $14.00 this month of $20 (70%)"
        )
        assert api_spend.detail_text(reading, NOW) is None
        # One usage-report request, filtered to the linked key.
        (usage_call,) = [c for c in anthropic.calls if c["path"].endswith("/messages")]
        assert ("api_key_ids[]", "apikey_A") in usage_call["query"]

    def test_one_request_serves_every_key_of_the_org(self, env, anthropic):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        seed_api_key(s, 4, KEY_B, email="ci@example.com")
        _register()
        out = _readings(s)
        assert out["4"]["spend"]["used"] == 2.0
        assert anthropic.paths().count("/v1/organizations/usage_report/messages") == 1
        _readings(s, now=NOW + 100)
        assert anthropic.paths().count("/v1/organizations/usage_report/messages") == 1

    def test_unknown_model_is_counted_not_priced(self, env, anthropic):
        anthropic.routes["/v1/organizations/usage_report/messages"] = [json_response(report([
            bucket("2026-11-10", [
                usage_result("apikey_A", OPUS, inp=1_000_000),
                usage_result("apikey_A", "claude-future-9", inp=123),
            ])
        ]))]
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _register()
        reading = _readings(s)["3"]
        assert reading["spend"]["used"] == 4.0 and reading["unpriced_tokens"] == 123
        assert "$4.00+ this month" in api_spend.spend_text(reading)

    def test_failure_falls_back_to_the_ledger_with_a_note(self, env, anthropic):
        anthropic.routes["/v1/organizations/usage_report/messages"] = [error_response(429)]
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _register()
        _ledger(email, [(NOW - 3600, OPUS, 1_000_000, 0)])
        reading = _readings(s)["3"]
        assert reading["source"] == "ledger"
        assert reading["spend"]["used"] == 4.0
        assert reading["note"] == "Admin API rate limited"
        assert api_spend.spend_text(reading).endswith("· estimate")
        assert api_spend.detail_text(reading, NOW) == (
            "Admin API rate limited; estimate from this machine's transcripts"
        )

    def test_no_network_pass_never_requests(self, env, anthropic):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _register()
        out = _readings(s, fetch=set())
        assert anthropic.calls == []
        assert out["3"]["source"] is None

    def test_environment_key_links_without_writing(self, env, anthropic):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        out = _readings(s, env_vars={ENV_VAR: ADMIN_KEY})
        assert out["3"]["source"] == "admin-api" and out["3"]["org"] == "env"
        assert "apiKeyId" not in record(make_switcher(), 3)
        assert not (data_root() / "api_orgs.json").exists()
        cache = data_root() / "cache" / "admin_api.json"
        assert not cache.exists() or ADMIN_KEY not in cache.read_text()
        assert all(c["headers"]["x-api-key"] == ADMIN_KEY for c in anthropic.calls)

    def test_rollover_at_utc_midnight(self, env, anthropic):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _register()
        anthropic.routes["/v1/organizations/usage_report/messages"] = lambda q: json_response(report(
            [bucket("2026-11-30", [usage_result("apikey_A", OPUS, inp=1_000_000)])]
            if dict(q)["starting_at"] == "2026-11-01T00:00:00Z"
            else [bucket("2026-12-01", [usage_result("apikey_A", OPUS, out=50_000)])]
        ))
        before = datetime(2026, 11, 30, 23, 59, 30, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 12, 1, 0, 0, 5, tzinfo=timezone.utc).timestamp()
        nov = _readings(s, now=before)["3"]
        dec = _readings(s, now=after)["3"]
        assert (nov["month"], nov["spend"]["used"], nov["today"]) == ("2026-11", 4.0, 4.0)
        assert (dec["month"], dec["spend"]["used"], dec["today"]) == ("2026-12", 1.0, 1.0)
        assert dec["spend"]["resets_at"] == "2027-01-01T00:00:00Z"


class TestLedgerSource:
    def test_month_and_today_are_utc(self, env):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _ledger(email, [
            (datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc).timestamp(), OPUS, 1_000_000, 0),
            (datetime(2026, 11, 1, 0, 10, tzinfo=timezone.utc).timestamp(), OPUS, 1_000_000, 0),
            (datetime(2026, 11, 10, 0, 5, tzinfo=timezone.utc).timestamp(), OPUS, 0, 100_000),
        ])
        reading = _readings(s)["3"]
        assert reading["source"] == "ledger"
        assert reading["spend"]["used"] == 6.0  # Oct 31 is last month
        assert reading["today"] == 2.0
        assert reading["as_of"] == NOW

    def test_rollover_at_utc_midnight(self, env):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _ledger(email, [
            (datetime(2026, 11, 30, 23, 10, tzinfo=timezone.utc).timestamp(), OPUS, 1_000_000, 0),
            (datetime(2026, 12, 1, 0, 10, tzinfo=timezone.utc).timestamp(), OPUS, 0, 50_000),
        ])
        nov = _readings(s, now=datetime(2026, 11, 30, 23, 50, tzinfo=timezone.utc).timestamp())["3"]
        dec = _readings(s, now=datetime(2026, 12, 1, 0, 30, tzinfo=timezone.utc).timestamp())["3"]
        assert nov["spend"]["used"] == 4.0 + 1.0  # the Dec bucket is not yet "later"
        assert dec["spend"]["used"] == 1.0 and dec["today"] == 1.0

    def test_ledger_is_opened_read_only(self, env):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _ledger(email, [(NOW - 60, OPUS, 1_000_000, 0)])
        db = data_root() / DB_FILENAME
        before = db.read_bytes()
        _readings(s)
        assert db.read_bytes() == before

    def test_missing_ledger_is_not_created(self, env):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        assert _readings(s) == {}
        assert not (data_root() / DB_FILENAME).exists()

    def test_old_estimate_says_how_old(self, env):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _ledger(email, [(NOW - 7200, OPUS, 1_000_000, 0)], scanned_at=NOW - 3 * 3600)
        reading = _readings(s)["3"]
        assert api_spend.detail_text(reading, NOW) == "estimate as of the last qswap usage, 3h ago"


class TestNothingRecorded:
    """An unused key reads the same with no ledger and after the first scan."""

    def test_zero_estimate_without_a_budget(self, env):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _ledger("someone-else@example.com", [(NOW - 60, OPUS, 1_000_000, 0)])
        reading = _readings(s)["3"]
        assert reading["source"] == "ledger" and reading["spend"]["used"] == 0
        assert api_spend.nothing_recorded(reading)
        assert api_spend.spend_text(reading) == "API key · no usage recorded on this machine"
        assert api_spend.detail_text(reading, NOW) is None
        assert api_spend.menu_text(reading) == "no usage recorded"

    def test_zero_estimate_with_a_budget(self, env):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        set_record(s, 3, budget=500)
        _ledger("someone-else@example.com", [])
        reading = _readings(s)["3"]
        assert api_spend.spend_text(reading, now=NOW) == (
            "API key · $0 of $500 this month · no usage recorded on this machine"
        )
        assert api_spend.menu_text(reading, now=NOW) == "$0 of $500 this month"
        assert api_spend.budget_state(reading) == (0.0, 500.0)  # still enforced as before

    def test_an_old_zero_estimate_says_how_old_without_estimate(self, env):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        _ledger("someone-else@example.com", [], scanned_at=NOW - 3 * 3600)
        reading = _readings(s)["3"]
        assert api_spend.detail_text(reading, NOW) == "as of the last qswap usage, 3h ago"

    def test_spend_uses_the_existing_wording(self, env):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _ledger(email, [(NOW - 60, OPUS, 1_000_000, 0)])
        reading = _readings(s)["3"]
        assert not api_spend.nothing_recorded(reading)
        assert api_spend.spend_text(reading) == "API key · $4.00 today · $4.00 this month · estimate"

    def test_unpriced_tokens_are_usage(self):
        reading = {"spend": {"used": 0.0, "limit": None}, "today": 0.0, "source": "ledger",
                   "unpriced_tokens": 120}
        assert not api_spend.nothing_recorded(reading)
        assert api_spend.spend_text(reading).endswith("$0.00+ this month · estimate")

    def test_admin_api_zeros_are_shown_as_they_are(self):
        reading = {"spend": {"used": 0.0, "limit": None}, "today": 0.0,
                   "source": "admin-api", "unpriced_tokens": 0}
        assert not api_spend.nothing_recorded(reading)
        assert api_spend.spend_text(reading) == "API key · $0.00 today · $0.00 this month"

    def test_an_organization_problem_is_not_nothing_recorded(self):
        reading = {"spend": {"used": None, "limit": None}, "today": None, "source": None,
                   "unpriced_tokens": 0, "note": "key not found in a registered organization"}
        assert not api_spend.nothing_recorded(reading)
        assert api_spend.spend_text(reading) == "API key · no spend data yet"
        assert api_spend.detail_text(reading, NOW) == "key not found in a registered organization"


def _break_ledger(how: str, email: str) -> None:
    """The ledger states that are not "nothing recorded"."""
    import sqlite3

    db = data_root() / DB_FILENAME
    if how == "no-ledger":
        return
    if how == "no-meta":
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE other (x)")
        conn.commit()
        conn.close()
        return
    _ledger(email, [(NOW - 60, OPUS, 120_000_000, 0)])  # $480 this month, readable
    if how == "schema":
        conn = sqlite3.connect(db)
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
    elif how == "corrupt":
        for extra in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            extra.unlink(missing_ok=True)
        db.write_bytes(b"not a sqlite database" * 64)


class TestUnreadableLedger:
    """Only "no ledger file yet" reads as nothing recorded; a ledger that
    exists but cannot be read is spend unknown, never "$0 of …"."""

    UNKNOWN = "spend unknown: this machine's usage ledger could not be read; run: qswap usage"

    def _fleet(self):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        set_record(s, 3, budget=500)
        return s, email

    @pytest.mark.parametrize("how", ["no-meta", "schema", "corrupt"])
    def test_spend_unknown_on_every_surface(self, env, capsys, how):
        from ai_quota_swap import menubar, menubar_data

        s, email = self._fleet()
        _break_ledger(how, email)
        reading = _readings(s)["3"]
        assert reading["spend"]["used"] is None and api_spend.spend_unknown(reading)
        assert not api_spend.nothing_recorded(reading)
        assert api_spend.spend_text(reading) == f"API key · {self.UNKNOWN}"
        assert api_spend.detail_text(reading, NOW) is None
        assert api_spend.budget_state(reading) is None  # the brake has no figure
        capsys.readouterr()
        # qswap list: the sentence, wrapped with the command whole.
        make_switcher().list_accounts()
        out = capsys.readouterr().out
        assert "$0" not in out and "no usage recorded" not in out
        joined = " ".join(out.split())
        assert self.UNKNOWN in joined
        assert "\n     qswap usage\n" in out or "run: qswap usage\n" in out
        # qswap budget.
        run_main(["budget"])
        budget = capsys.readouterr().out
        assert f"3: api-key-3  budget $500 · {self.UNKNOWN}" in budget and "$0" not in budget
        # The dashboard's one-line row and the menu bar's row: the state.
        snap = s.accounts_snapshot()
        acc = {a.number: a for a in snap.accounts}["3"]
        row = TestTuiAndMenuBar._drawn(acc, row=True)
        assert row.endswith("   spend unknown") and "$0" not in row
        accs = {a.number: a for a in menubar_data.account_states(snap, "claude", {}, now=NOW)}
        menu_row = menubar.account_row(accs["3"], now=NOW, stale_after_s=900)
        assert menu_row.text == "#3 api-key-3 · API key · spend unknown"
        assert self.UNKNOWN in " ".join(menu_row.tooltip.split())

    def test_no_ledger_file_yet_is_nothing_recorded(self, env, capsys):
        """With a budget there is no figure to hold it to: the rows say the
        limit is not enforced (what auto-switch does), never "$0 of $500"."""
        from ai_quota_swap import menubar, menubar_data

        s, email = self._fleet()
        _break_ledger("no-ledger", email)
        reading = _readings(s)["3"]
        assert api_spend.nothing_recorded(reading) and not api_spend.spend_unknown(reading)
        capsys.readouterr()
        make_switcher().list_accounts()
        out = capsys.readouterr().out
        assert "budget $500 a month · limit not enforced: no spend figure" in out
        assert "$0 of $500" not in out
        run_main(["budget"])
        assert "3: api-key-3  budget $500 · limit not enforced: no spend figure" in (
            capsys.readouterr().out)
        snap = s.accounts_snapshot()
        acc = {a.number: a for a in snap.accounts}["3"]
        assert "limit not enforced" in TestTuiAndMenuBar._drawn(acc, row=True)
        accs = {a.number: a for a in menubar_data.account_states(snap, "claude", {}, now=NOW)}
        assert menubar.account_row(accs["3"], now=NOW, stale_after_s=900).text == (
            "#3 api-key-3 · API key · budget $500 · limit not enforced")

    @pytest.mark.parametrize("keep", [0, 100, "half"])
    def test_running_qswap_usage_fixes_every_unreadable_state(self, env, capsys, keep):
        # The advice holds even for a file that is not a database: qswap
        # usage rebuilds it (the ledger is derived from the session files).
        s, email = self._fleet()
        _break_ledger("schema", email)
        db = data_root() / DB_FILENAME
        conn_data = db.read_bytes()
        for extra in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            extra.unlink(missing_ok=True)
        size = len(conn_data) // 2 if keep == "half" else keep
        db.write_bytes(conn_data[:size] if size else b"\0" * 64)  # truncated or zeroed
        assert api_spend.spend_unknown(_readings(s)["3"])
        capsys.readouterr()
        run_main(["usage"])
        out = capsys.readouterr().out
        assert "could not be read as a database, so it was rebuilt from the session files." in (
            " ".join(out.split()))
        after = _readings(make_switcher())["3"]
        assert not api_spend.spend_unknown(after) and api_spend.nothing_recorded(after)
        # The no-meta and schema states recover through the same command.
        run_main(["usage"])
        assert "rebuilt" not in capsys.readouterr().out

    @pytest.mark.parametrize("how", ["no-meta", "schema"])
    def test_running_qswap_usage_fixes_a_ledger_without_its_schema(self, env, capsys, how):
        s, email = self._fleet()
        _break_ledger(how, email)
        assert api_spend.spend_unknown(_readings(s)["3"])
        run_main(["usage"])
        assert not api_spend.spend_unknown(_readings(make_switcher())["3"])

    def test_without_a_budget_an_unreadable_ledger_still_says_so(self, env, capsys):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _break_ledger("schema", email)
        reading = _readings(s)["3"]  # a reading now, where "no ledger" gives none
        assert api_spend.spend_text(reading) == f"API key · {self.UNKNOWN}"


class TestNoDataAndBudgets:
    def test_nothing_known_keeps_the_old_row(self, env):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        assert _readings(s) == {}

    def test_budget_without_data(self, env):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        set_record(s, 3, budget=500)
        reading = _readings(s)["3"]
        assert reading["spend"]["used"] is None and reading["spend"]["limit"] == 500.0
        assert reading["spend"]["pct"] is None
        # No figure for a budget: the limit is not enforced (A4), and the
        # detail line names what auto-switch does and how to get one.
        assert api_spend.spend_text(reading, now=NOW) == (
            "API key · budget $500 a month · limit not enforced: no spend figure"
        )
        assert api_spend.detail_text(reading, NOW) == api_spend.UNENFORCED_NEXT
        assert api_spend.budget_state(reading) is None

    def test_budget_set_and_unset(self, env):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _ledger(email, [(NOW - 60, OPUS, 1_000_000, 0)])
        set_record(s, 3, budget=8)
        reading = _readings(s)["3"]
        assert reading["spend"]["limit"] == 8.0 and reading["spend"]["pct"] == 50.0
        assert api_spend.budget_state(reading) == (4.0, 8.0)
        data = s._get_sequence_data()
        data["accounts"]["3"].pop("budget")
        s._write_json(s.sequence_file, data)
        reading = _readings(make_switcher())["3"]
        assert reading["spend"]["limit"] is None and reading["spend"]["pct"] is None
        assert api_spend.budget_state(reading) is None

    def test_over_budget_text(self, env):
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _ledger(email, [(NOW - 60, OPUS, 2_000_000, 0)])
        set_record(s, 3, budget=5)
        reading = _readings(s)["3"]
        assert api_spend.over_budget(reading)
        assert "of $5 (160%, over budget)" in api_spend.spend_text(reading)

    @pytest.mark.parametrize("value,expected", [
        ("500", 500.0), ("$1,200.50", 1200.5), (49.999, 50.0), ("0.01", 0.01),
    ])
    def test_normalize(self, value, expected):
        assert api_spend.normalize_budget(value) == expected

    @pytest.mark.parametrize("value", ["0", "-5", "abc", "", "nan", "inf", True, None, "20000000"])
    def test_normalize_refuses(self, value):
        with pytest.raises(ValueError):
            api_spend.normalize_budget(value)

    def test_garbage_in_the_record_reads_as_no_budget(self):
        assert api_spend.budget_from_record({"budget": "lots"}) is None
        assert api_spend.budget_from_record({"budget": -3}) is None
        assert api_spend.budget_from_record({}) is None


# -- surfaces -------------------------------------------------------------------


@pytest.fixture
def fleet(env, anthropic, monkeypatch):
    """Account 1 (subscription), 3 (API key linked in acme, budget $20)."""
    from ai_quota_swap import oauth

    monkeypatch.setattr(
        oauth, "try_fetch_usage_for_account",
        lambda *a, **k: oauth.UsageOutcome(usage={"five_hour": {"pct": 10.0}, "seven_day": {"pct": 20.0}}),
    )
    s = make_switcher()
    seed_oauth(s, 1, "sub@example.com")
    seed_api_key(s, 3, KEY_A)
    set_record(s, 3, budget=20)
    _register()
    return s


@pytest.fixture
def at_now(monkeypatch):
    monkeypatch.setattr(api_spend.time, "time", lambda: NOW)


class TestListText:
    def test_api_key_row(self, fleet, at_now, capsys):
        fleet.list_accounts()
        out = capsys.readouterr().out
        # The header says "API key"; the line under it is the spend.
        assert "· API key" in out
        assert "$10.00 today · $14.00 this month of $20 (70%)" in out
        assert "no usage recorded" not in out

    def test_without_a_reading_nothing_is_recorded(self, env, capsys):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        s.list_accounts()
        out = capsys.readouterr().out
        assert "  #3 api-key-3 · API key\n     no usage recorded on this machine\n" in out


class TestListJson:
    def test_additive(self, fleet, at_now, monkeypatch):
        payload = fleet.list_accounts(json_output=True)
        rows = {r["number"]: r for r in payload["accounts"]}
        row = rows[3]
        assert row["usageStatus"] == "api_key" and row["usage"] is None
        assert row["budget"] == 20.0
        assert row["apiSpend"] == {
            "used": 14.0, "limit": 20.0, "pct": 70.0, "currency": "USD", "today": 10.0,
            "source": "admin-api", "month": "2026-11", "resetsAt": "2026-12-01T00:00:00Z",
            "asOf": "2026-11-10T12:00:00Z", "organization": "acme", "apiKeyId": "apikey_A",
        }
        # Every key that existed before is unchanged; the new ones only add.
        monkeypatch.setattr(ClaudeAccountSwitcher, "_api_spend_readings", lambda self, i, f: {})
        data = fleet._get_sequence_data()
        data["accounts"]["3"].pop("budget")
        fleet._write_json(fleet.sequence_file, data)
        before = {r["number"]: r for r in fleet.list_accounts(json_output=True)["accounts"]}
        assert {k: v for k, v in row.items() if k not in ("apiSpend", "budget")} == before[3]

        # The reading's age is wall-clock time between the two calls, so it can
        # tick over on a slow machine; everything else must be identical.
        def stable(r):
            return {k: v for k, v in r.items() if k != "usageAgeSeconds"}

        assert stable(rows[1]) == stable(before[1])
        assert "apiSpend" not in rows[1] and "budget" not in rows[1]

    def test_status_json_for_an_active_api_key(self, fleet, at_now, temp_home):
        # Make account 3 the live login, as a switch to it would: the managed
        # key in primaryApiKey, the synthesized oauthAccount beside it.
        email = record(fleet, 3)["email"]
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "organizationUuid": None},
            "primaryApiKey": KEY_A,
        }))
        active = make_switcher().status(json_output=True)["active"]
        assert active["number"] == 3
        assert active["usageStatus"] == "api_key" and active["usage"] is None
        assert active["apiSpend"]["source"] == "admin-api"
        assert active["apiSpend"]["used"] == 14.0
        assert active["budget"] == 20.0


class TestTuiAndMenuBar:
    def _snapshot(self, s):
        snap = s.accounts_snapshot()
        return snap, {a.number: a for a in snap.accounts}

    @staticmethod
    def _drawn(acc, *, row: bool = False) -> str:
        """The dashboard's card (or one-line row) for one account."""
        from ai_quota_swap.models import AccountsSnapshot
        from ai_quota_swap.tui import data
        from ai_quota_swap.tui.theme import Palette
        from ai_quota_swap.tui.widgets import card_text, row_text

        view = data.ProviderView("claude", AccountsSnapshot(None, (acc,), NOW))
        block = data.block_for(view, acc, now=NOW)
        if row:
            return row_text(block, 110, Palette.DARK, over_budget=data.over_budget(acc)).plain
        return card_text(block, 110, Palette.DARK).plain

    def test_card_and_row(self, fleet, at_now):
        _snap, accs = self._snapshot(fleet)
        card = self._drawn(accs["3"])
        assert "· API key" in card.splitlines()[0]
        assert "$10.00 today · $14.00 this month of $20 (70%)" in card
        assert "API key (no quota)" not in card
        assert self._drawn(accs["3"], row=True).endswith("$10.00 today · $14.00 this month of $20 (70%)")
        # The subscription row renders as it did.
        assert "API key" not in self._drawn(accs["1"])

    def test_card_shows_the_fallback_note(self, env, anthropic, at_now):
        anthropic.routes["/v1/organizations/usage_report/messages"] = [error_response(401)]
        s = make_switcher()
        email = seed_api_key(s, 3, KEY_A)
        _register()
        _ledger(email, [(NOW - 60, OPUS, 1_000_000, 0)])
        acc = {a.number: a for a in s.accounts_snapshot().accounts}["3"]
        card = self._drawn(acc)
        assert "$4.00 this month · estimate" in card
        assert "Admin key rejected; estimate from this machine's transcripts" in card

    def test_menu_bar_row(self, fleet, at_now):
        # The menu bar says what qswap list says (docs/design/ux.md).
        from ai_quota_swap import menubar, menubar_data

        snap, _ = self._snapshot(fleet)
        accs = {a.number: a for a in menubar_data.account_states(snap, "claude", {}, now=NOW)}
        row = menubar.account_row(accs["3"], now=NOW, stale_after_s=900)
        # qswap list's words, cut to the row limit; the tooltip has them whole.
        assert row.text.startswith("#3 api-key-3 · API key · $10.00 today · $14.00")
        assert len(row.text) <= menubar.ROW_LIMIT
        assert "$10.00 today · $14.00 this month of $20 (70%)" in row.tooltip
        assert not row.attention  # under budget


class TestBudgetCommand:
    def test_set_and_remove(self, env, capsys):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        run_main(["budget", "3", "500"])
        assert record(make_switcher(), 3)["budget"] == 500.0
        out = capsys.readouterr().out
        assert "Set a monthly budget of $500 for #3 api-key-3." in out
        assert "not a cap at Anthropic" in out
        run_main(["budget", "3", "$1,200.50"])
        assert record(make_switcher(), 3)["budget"] == 1200.5
        run_main(["budget", "3", "off"])
        assert "budget" not in record(make_switcher(), 3)
        assert "Removed the budget for #3 api-key-3." in capsys.readouterr().out
        run_main(["budget", "3", "off"])
        assert "has no budget" in capsys.readouterr().out

    @pytest.mark.parametrize("amount", ["0", "-5", "abc", "nan"])
    def test_bad_amount_writes_nothing(self, env, capsys, amount):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        before = s.sequence_file.read_bytes()
        with pytest.raises(SystemExit) as exc:
            run_main(["budget", "3", amount])
        assert exc.value.code == 1
        assert "a budget is an amount in US dollars" in capsys.readouterr().err
        assert s.sequence_file.read_bytes() == before

    def test_refused_on_a_subscription_account(self, env, capsys):
        s = make_switcher()
        seed_oauth(s, 1, "sub@example.com")
        before = s.sequence_file.read_bytes()
        with pytest.raises(SystemExit):
            run_main(["budget", "1", "100"])
        assert "is a subscription account" in capsys.readouterr().err
        assert s.sequence_file.read_bytes() == before

    def test_list(self, env, capsys):
        s = make_switcher()
        seed_oauth(s, 1, "sub@example.com")
        email = seed_api_key(s, 3, KEY_A)
        seed_api_key(s, 4, KEY_B, email="ci@example.com")
        _ledger(email, [(NOW - 60, OPUS, 1_000_000, 0)], scanned_at=None)
        set_record(s, 3, budget=8)
        capsys.readouterr()
        import ai_quota_swap.api_spend as mod

        orig = mod.time.time
        mod.time.time = lambda: NOW
        try:
            run_main(["budget"])
            out = capsys.readouterr().out
            run_main(["budget", "--json"])
            doc = json.loads(capsys.readouterr().out)
        finally:
            mod.time.time = orig
        assert "Monthly budgets for API-key accounts" in out
        assert email == "api-key-3@token.local"
        assert "3: api-key-3  $4.00 of $8 this month (50%) (estimate)" in out
        assert "4: ci@example.com  no budget · no usage recorded on this machine" in out
        assert "sub@example.com" not in out
        assert [a["number"] for a in doc["accounts"]] == [3, 4]
        assert doc["accounts"][0]["budget"] == 8.0
        assert doc["accounts"][0]["email"] == "api-key-3@token.local"  # JSON keeps the stored value
        assert doc["accounts"][0]["apiSpend"]["used"] == 4.0

    def test_list_with_no_api_key_accounts(self, env, capsys):
        s = make_switcher()
        seed_oauth(s, 1, "sub@example.com")
        run_main(["budget"])
        assert "No API-key accounts are managed" in capsys.readouterr().out

    def test_budget_survives_a_slot_readd(self, env):
        s = make_switcher()
        seed_api_key(s, 3, KEY_A)
        run_main(["budget", "3", "75"])
        seed_api_key(make_switcher(), 3, KEY_A)
        assert record(make_switcher(), 3)["budget"] == 75.0
