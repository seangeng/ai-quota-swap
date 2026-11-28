"""Tests for ai_quota_swap.fleet (multi-provider snapshot, OpenCode reader, urgency routing)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_quota_swap import fleet


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _make_opencode_db(db_path: Path, now: float) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY,
            model TEXT,
            cost REAL DEFAULT 0,
            tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0,
            tokens_reasoning INTEGER DEFAULT 0,
            tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0,
            time_updated INTEGER DEFAULT 0,
            time_busy INTEGER DEFAULT 0,
            time_idle INTEGER DEFAULT 0
        )
        """
    )
    now_ms = int(now * 1000)
    conn.execute(
        "INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses_test_1",
            json.dumps({"id": "deepseek-v4.1-flash", "providerID": "deepseek"}),
            1.25,
            500_000,
            80_000,
            20_000,
            1_000_000,
            0,
            now_ms - 10_000,
            now_ms - 5_000,
            now_ms - 30_000,
        ),
    )
    conn.commit()
    conn.close()


def _seed_data_root(tmp_path: Path, now: float) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    home = tmp_path / "home"
    (data_root / "cache").mkdir(parents=True)
    (data_root / "codex" / "cache").mkdir(parents=True)
    home.mkdir(parents=True)

    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    # Claude: #1 exhausted, #2 89% used (11% headroom)
    (data_root / "sequence.json").write_text(
        json.dumps({
            "activeAccountNumber": 2,
            "accounts": {
                "1": {"email": "secret1@example.com", "organizationUuid": "org-secret-1"},
                "2": {"email": "secret2@example.com", "organizationUuid": "org-secret-2"},
                "5": {"email": "api-key-5@token.local", "kind": "api_key", "credit": {"amount": 250.0}},
            },
        }),
        encoding="utf-8",
    )
    (data_root / "cache" / "usage.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "accounts": {
                "1": {
                    "email": "secret1@example.com",
                    "lastAttemptAt": now - 60,
                    "lastGood": {
                        "seven_day": {"pct": 100.0, "resets_at": _iso(now_dt + timedelta(days=2))},
                    },
                },
                "2": {
                    "email": "secret2@example.com",
                    "lastAttemptAt": now - 30,
                    "lastGood": {
                        "seven_day": {"pct": 89.0, "resets_at": _iso(now_dt + timedelta(days=5))},
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    # Codex: #1 at 41% used (59% headroom), resets in 30m, 3 expiring reset credits!
    (data_root / "codex" / "sequence.json").write_text(
        json.dumps({
            "activeAccountNumber": 1,
            "accounts": {
                "1": {"email": "codex-secret@example.com"},
            },
        }),
        encoding="utf-8",
    )
    (data_root / "codex" / "cache" / "usage.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "accounts": {
                "1": {
                    "email": "codex-secret@example.com",
                    "lastAttemptAt": now - 20,
                    "lastGood": {
                        "seven_day": {"pct": 41.0, "resets_at": _iso(now_dt + timedelta(minutes=30))},
                        "resets": {
                            "grants": [
                                {
                                    "id": "credit-1",
                                    "kind": "full",
                                    "scope": "full",
                                    "resets_left": 3,
                                    "ends_at": _iso(now_dt + timedelta(days=3)),
                                }
                            ]
                        },
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    _make_opencode_db(home / ".local" / "share" / "opencode" / "opencode.db", now)
    return data_root, home


def test_fleet_snapshot_is_secret_free_and_ranks_expiring_codex_first(tmp_path: Path) -> None:
    now = time.time()
    data_root, home = _seed_data_root(tmp_path, now)
    snap = fleet.write_fleet_status(data_root, home=home, now=now)

    raw = (data_root / fleet.FLEET_STATUS_FILENAME).read_text(encoding="utf-8")
    assert "secret1@example.com" not in raw
    assert "secret2@example.com" not in raw
    assert "codex-secret@example.com" not in raw
    assert "org-secret-1" not in raw

    assert snap["providers"]["claude"]["headroomPct"] == 11.0
    assert snap["providers"]["claude"]["tight"] is True
    assert snap["providers"]["codex"]["headroomPct"] == 59.0
    assert snap["providers"]["codex"]["expiringResets"][0]["count"] == 3
    assert snap["providers"]["opencode"]["installed"] is True
    assert snap["providers"]["opencode"]["todayCostUsd"] == 1.25
    assert snap["providers"]["opencode"]["activeSessions"] == 1
    assert "deepseek-v4.1-flash" in snap["providers"]["opencode"]["models"]

    # Coding & review should both rank codex #1 because of 59% free + 30m reset + 3 expiring credits.
    coding_top = snap["recommendations"]["coding"][0]
    assert coding_top["provider"] == "codex"
    assert coding_top["expiringCredits"] == 3

    alert_kinds = {a["kind"] for a in snap["alerts"]}
    assert "expiring_quota" in alert_kinds
    assert "expiring_credits" in alert_kinds


def test_route_prefers_cross_harness_for_reviews(tmp_path: Path) -> None:
    providers = {
        "claude": {
            "installed": True,
            "headroomPct": 70.0,
            "bucket": "abundant",
            "subscriptionAccounts": 2,
            "cli": "claude",
            "harnessAgent": "claude",
            "expiringResets": [],
        },
        "codex": {
            "installed": True,
            "headroomPct": 70.0,
            "bucket": "abundant",
            "subscriptionAccounts": 1,
            "cli": "codex",
            "harnessAgent": "codex",
            "expiringResets": [],
        },
        "antigravity": {
            "installed": True,
            "headroomPct": 85.0,
            "bucket": "abundant",
            "subscriptionAccounts": 1,
            "cli": "agy",
            "harnessAgent": "agy",
            "expiringResets": [],
        },
    }
    ranked = fleet.recommend_providers(providers, task_kind="review", caller_provider="claude")
    assert ranked[0].provider != "claude"
    assert "cross-harness" in ranked[0].reason

    ranked_long = fleet.recommend_providers(providers, task_kind="long_context")
    assert ranked_long[0].provider == "antigravity"


def test_format_where_text(tmp_path: Path) -> None:
    now = time.time()
    data_root, home = _seed_data_root(tmp_path, now)
    snap = fleet.collect_fleet_snapshot(data_root, home=home, now=now, apps_dir=None)
    text = fleet.format_where_text(snap)
    assert "Fleet capacity" in text
    assert "Codex" in text
    assert "OpenCode" in text
    assert "Best right now (coding): codex" in text


def test_detects_gemini_and_popular_optional_providers(tmp_path: Path) -> None:
    now = time.time()
    data_root, home = _seed_data_root(tmp_path, now)
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text("{}", encoding="utf-8")
    (home / ".cursor").mkdir(parents=True)
    (home / ".config" / "amp").mkdir(parents=True)

    snap = fleet.collect_fleet_snapshot(data_root, home=home, now=now, apps_dir=None)
    assert snap["providers"]["gemini"]["installed"] is True
    assert snap["providers"]["cursor"]["installed"] is True
    assert snap["providers"]["amp"]["installed"] is True
    assert "windsurf" not in snap["providers"] or snap["providers"]["windsurf"]["installed"] is True

    ranked_long = fleet.recommend_providers(snap["providers"], task_kind="long_context")
    assert any(c.provider == "gemini" for c in ranked_long)


def test_menubar_shows_other_ai_tools_and_workmesh_pool(tmp_path: Path) -> None:
    from ai_quota_swap.menubar import MenuBarSettings, MenuInput, ProviderState, build_menu

    now = time.time()
    data_root, home = _seed_data_root(tmp_path, now)
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    snap = fleet.collect_fleet_snapshot(data_root, home=home, now=now, apps_dir=None)

    menu = build_menu(
        MenuInput(
            claude=ProviderState("claude"),
            codex=ProviderState("codex"),
            settings=MenuBarSettings(),
            now=now,
            fleet=snap,
            workmesh={
                "configured": True,
                "capacityShare": "detailed",
                "poolEnabled": True,
                "rulesCount": 1,
                "boardUrl": "https://workmesh.b3os.org/usage",
            },
        )
    )
    texts = [r.text for r in menu.rows() if not r.separator]
    assert any("Other AI tools" in t for t in texts)
    assert any("OpenCode" in t and "deepseek-v4.1-flash" in t for t in texts)
    assert any("Antigravity" in t and "Gemini + Claude/GPT" in t for t in texts)
    assert any("Also installed:" in t and "Cursor" in t for t in texts)
    assert any("Best right now: Codex" in t for t in texts)
    assert any("Workmesh · Pool ON (1 rule) · sharing: detailed" in t for t in texts)
