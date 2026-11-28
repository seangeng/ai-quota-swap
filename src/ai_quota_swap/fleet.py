"""Fleet capacity snapshot, extra-provider tracking, and utilization-urgency routing.

Builds a single secret-free picture of every AI subscription and CLI on this
machine — Claude Code, Codex, OpenCode (e.g. DeepSeek API via ``opencode.db``),
Antigravity (``Antigravity.app`` / ``agy``), and Grok Bot (``Grok Bot.app`` /
``grok``) — and writes it atomically to ``~/.ai-quota-swap/fleet_status.json``
(mode ``0600``).

Other tools (Workmesh, ``studio-pixbar``, Harness wrappers, MCP tools) read
``fleet_status.json`` without touching credentials, Keychain items, or vendor
APIs. Nothing in the snapshot ever contains emails, organization UUIDs, API
keys, or tokens.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_quota_swap import fsutil

FLEET_STATUS_FILENAME = "fleet_status.json"
EXTRA_PROVIDERS_FILENAME = "providers_extra.json"
SCHEMA_VERSION = 1

TASK_KINDS = ("coding", "review", "long_context", "research", "bulk")

CORE_PROVIDERS = ("claude", "codex", "opencode", "gemini", "antigravity", "grok")
OPTIONAL_PROVIDERS = ("cursor", "copilot", "windsurf", "amp", "aider", "cline")
ALL_PROVIDERS = CORE_PROVIDERS + OPTIONAL_PROVIDERS

# Task-to-provider affinity weights (0..1). Combined with live headroom and
# reset/credit expiry urgency to pick the best subscription for a given task.
_TASK_AFFINITY: dict[str, dict[str, float]] = {
    "coding": {
        "claude": 1.00,
        "codex": 0.98,
        "cursor": 0.94,
        "antigravity": 0.90,
        "gemini": 0.89,
        "opencode": 0.88,
        "amp": 0.88,
        "windsurf": 0.86,
        "copilot": 0.84,
        "aider": 0.84,
        "cline": 0.82,
        "grok": 0.72,
    },
    "review": {
        "codex": 1.00,
        "antigravity": 0.95,
        "gemini": 0.94,
        "claude": 0.92,
        "amp": 0.88,
        "cursor": 0.86,
        "grok": 0.85,
        "opencode": 0.82,
        "copilot": 0.80,
        "windsurf": 0.78,
        "aider": 0.76,
        "cline": 0.76,
    },
    "long_context": {
        "gemini": 1.00,
        "antigravity": 1.00,
        "claude": 0.90,
        "grok": 0.82,
        "opencode": 0.80,
        "codex": 0.78,
        "cursor": 0.78,
        "amp": 0.76,
        "windsurf": 0.74,
        "copilot": 0.70,
        "aider": 0.72,
        "cline": 0.72,
    },
    "research": {
        "grok": 1.00,
        "gemini": 0.96,
        "antigravity": 0.95,
        "claude": 0.85,
        "opencode": 0.80,
        "amp": 0.78,
        "codex": 0.75,
        "cursor": 0.74,
        "copilot": 0.70,
        "windsurf": 0.70,
        "aider": 0.68,
        "cline": 0.68,
    },
    "bulk": {
        "opencode": 1.00,
        "codex": 0.95,
        "gemini": 0.92,
        "antigravity": 0.90,
        "aider": 0.88,
        "grok": 0.85,
        "copilot": 0.82,
        "cursor": 0.80,
        "amp": 0.80,
        "windsurf": 0.78,
        "cline": 0.78,
        "claude": 0.70,
    },
}

_PROVIDER_DISPLAY: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "gemini": "Gemini CLI",
    "antigravity": "Antigravity",
    "grok": "Grok Bot",
    "cursor": "Cursor",
    "copilot": "Copilot",
    "windsurf": "Windsurf",
    "amp": "Amp",
    "aider": "Aider",
    "cline": "Cline",
}

_PROVIDER_CLI: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "gemini": "gemini",
    "antigravity": "agy",
    "grok": "grok",
    "cursor": "cursor-agent",
    "copilot": "copilot",
    "windsurf": "windsurf",
    "amp": "amp",
    "aider": "aider",
    "cline": "cline",
}

_PROVIDER_HARNESS_AGENT: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "gemini": "gemini",
    "antigravity": "agy",
    "grok": "grok",
    "cursor": "cursor",
    "copilot": "copilot",
    "windsurf": "windsurf",
    "amp": "amp",
    "aider": "aider",
    "cline": "cline",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _parse_iso_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _human_countdown(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    secs = max(0, int(seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _bucket_for_headroom(headroom_pct: float | None, installed: bool) -> str:
    if not installed:
        return "unavailable"
    if headroom_pct is None:
        return "moderate"
    if headroom_pct >= 50.0:
        return "abundant"
    if headroom_pct >= 20.0:
        return "moderate"
    if headroom_pct > 5.0:
        return "tight"
    return "exhausted"


def _pct_used(usage: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    """Return ``(max_pct_used, earliest_reset_ts, earliest_reset_iso)`` for one reading."""
    pcts: list[float] = []
    resets: list[tuple[float, str]] = []
    for key in ("five_hour", "seven_day"):
        win = usage.get(key)
        if not isinstance(win, dict):
            continue
        p = win.get("pct")
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            pcts.append(float(p))
        r = win.get("resets_at")
        ts = _parse_iso_ts(r)
        if ts is not None and isinstance(r, str):
            resets.append((ts, r))
    if not pcts:
        return None, None, None
    max_pct = max(pcts)
    if resets:
        resets.sort(key=lambda pair: pair[0])
        return max_pct, resets[0][0], resets[0][1]
    return max_pct, None, None


def _expiring_grants(usage: dict[str, Any], now: float) -> list[dict[str, Any]]:
    resets = usage.get("resets")
    if not isinstance(resets, dict):
        return []
    grants = resets.get("grants")
    if not isinstance(grants, list):
        return []
    out: list[dict[str, Any]] = []
    for g in grants:
        if not isinstance(g, dict):
            continue
        left = g.get("resets_left")
        if left is None:
            left = g.get("resetsLeft")
        if not isinstance(left, int) or left <= 0:
            continue
        ends_at = g.get("ends_at") or g.get("endsAt")
        ends_ts = _parse_iso_ts(ends_at)
        if ends_ts is not None and ends_ts <= now:
            continue
        out.append({
            "kind": str(g.get("kind") or g.get("label") or "reset"),
            "scope": str(g.get("scope") or "unknown"),
            "count": left,
            "endsAt": ends_at if isinstance(ends_at, str) else None,
            "endsInSec": int(ends_ts - now) if ends_ts is not None else None,
        })
    return out


def _collect_managed_provider(
    provider_id: str,
    root: Path,
    *,
    now: float,
    cli_bin: str | None = None,
) -> dict[str, Any]:
    """Read Claude or Codex state from ``sequence.json`` and ``cache/usage.json``."""
    seq = _read_json(root / "sequence.json")
    accounts = seq.get("accounts") if isinstance(seq.get("accounts"), dict) else {}
    usage_doc = _read_json(root / "cache" / "usage.json")
    usage_accounts = usage_doc.get("accounts") if isinstance(usage_doc.get("accounts"), dict) else {}

    active_num = seq.get("activeAccountNumber")
    active_str = str(active_num) if active_num is not None else None

    sub_headrooms: list[tuple[str, float, float | None, str | None]] = []
    api_accounts = 0
    api_credit_left = 0.0
    sub_accounts = 0
    expiring_resets: list[dict[str, Any]] = []
    latest_attempt: float | None = None

    for num_str, meta in accounts.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("disabled"):
            continue
        kind = str(meta.get("kind") or "oauth")
        if kind == "api_key":
            api_accounts += 1
            credit = meta.get("credit")
            if isinstance(credit, dict):
                amt = credit.get("amount")
                if isinstance(amt, (int, float)):
                    api_credit_left += float(amt)
            continue
        sub_accounts += 1
        uentry = usage_accounts.get(str(num_str))
        if not isinstance(uentry, dict):
            continue
        attempt = uentry.get("lastAttemptAt")
        if isinstance(attempt, (int, float)):
            latest_attempt = attempt if latest_attempt is None else max(latest_attempt, float(attempt))
        last_good = uentry.get("lastGood")
        if not isinstance(last_good, dict):
            continue
        used_pct, reset_ts, reset_iso = _pct_used(last_good)
        if used_pct is not None:
            headroom = max(0.0, round(100.0 - used_pct, 1))
            sub_headrooms.append((str(num_str), headroom, reset_ts, reset_iso))
        for grant in _expiring_grants(last_good, now):
            expiring_resets.append(grant)

    installed = bool(shutil.which(cli_bin or _PROVIDER_CLI[provider_id])) or bool(accounts)
    best_slot: str | None = None
    best_headroom: float | None = None
    best_reset_ts: float | None = None
    best_reset_iso: str | None = None
    pool_headroom: float | None = None

    if sub_headrooms:
        # Best subscription account by highest remaining headroom; tie-break by earliest reset.
        sub_headrooms.sort(key=lambda item: (-item[1], item[2] if item[2] is not None else float("inf")))
        best_slot, best_headroom, best_reset_ts, best_reset_iso = sub_headrooms[0]
        pool_headroom = round(sum(item[1] for item in sub_headrooms) / len(sub_headrooms), 1)
        # Earliest reset across accounts that actually have quota (or all if exhausted).
        future_resets = [(ts, iso) for _, _, ts, iso in sub_headrooms if ts is not None and ts > now]
        if future_resets:
            future_resets.sort(key=lambda pair: pair[0])
            best_reset_ts, best_reset_iso = future_resets[0]

    resets_in_sec = int(best_reset_ts - now) if best_reset_ts is not None and best_reset_ts > now else None
    bucket = _bucket_for_headroom(best_headroom, installed=installed and (sub_accounts > 0 or api_accounts > 0))
    tight = best_headroom is not None and best_headroom <= 15.0

    # Aggregate expiring resets cleanly without exposing account numbers/emails.
    reset_summary: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for g in expiring_resets:
        k = (g["kind"], g["scope"], g.get("endsAt"))
        cur = reset_summary.get(k)
        if cur is None:
            reset_summary[k] = dict(g)
        else:
            cur["count"] += g["count"]

    return {
        "id": provider_id,
        "displayName": _PROVIDER_DISPLAY[provider_id],
        "cli": _PROVIDER_CLI[provider_id],
        "harnessAgent": _PROVIDER_HARNESS_AGENT[provider_id],
        "installed": installed,
        "quotaMode": "live",
        "subscriptionAccounts": sub_accounts,
        "apiKeyAccounts": api_accounts,
        "activeSlot": f"#{active_str}" if active_str else None,
        "bestSlot": f"#{best_slot}" if best_slot else None,
        "headroomPct": best_headroom,
        "poolHeadroomPct": pool_headroom,
        "bucket": bucket,
        "tight": tight,
        "nextResetAt": best_reset_iso,
        "resetsInSec": resets_in_sec,
        "resetCountdown": _human_countdown(resets_in_sec),
        "expiringResets": list(reset_summary.values()),
        "apiKeyFallbackAvailable": api_accounts > 0,
        "apiCreditHintUsd": round(api_credit_left, 2) if api_credit_left > 0 else None,
        "lastReadAt": _iso_utc(latest_attempt) if latest_attempt else None,
    }


def read_opencode_stats(db_path: Path, *, now: float | None = None) -> dict[str, Any]:
    """Read session counts, models, tokens, and exact recorded cost from OpenCode's SQLite DB.

    Opened strictly read-only (``mode=ro``) so it never blocks a live OpenCode session.
    """
    now_ts = time.time() if now is None else now
    out: dict[str, Any] = {
        "available": False,
        "dbPath": str(db_path),
        "sessionCount": 0,
        "activeSessions": 0,
        "todayCostUsd": 0.0,
        "sevenDayCostUsd": 0.0,
        "totalCostUsd": 0.0,
        "todayTokens": 0,
        "sevenDayTokens": 0,
        "models": [],
        "providers": [],
        "lastActiveAt": None,
    }
    if not db_path.exists():
        return out
    day_ago_ms = int((now_ts - 86400) * 1000)
    week_ago_ms = int((now_ts - 7 * 86400) * 1000)
    active_cutoff_ms = int((now_ts - 180) * 1000)
    uri = f"file:{db_path}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        rows = conn.execute(
            "SELECT id, model, cost, tokens_input, tokens_output, tokens_reasoning, "
            "tokens_cache_read, tokens_cache_write, time_updated, time_busy, time_idle "
            "FROM session_v2"
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        if conn is not None:
            conn.close()

    out["available"] = True
    out["sessionCount"] = len(rows)
    models: set[str] = set()
    providers: set[str] = set()
    latest_ms = 0

    for (
        _sid, model_raw, cost, t_in, t_out, t_reas, t_cr, t_cw,
        t_upd, t_busy, t_idle,
    ) in rows:
        cost_f = float(cost or 0.0)
        toks = int(t_in or 0) + int(t_out or 0) + int(t_reas or 0) + int(t_cr or 0) + int(t_cw or 0)
        upd = int(t_upd or 0)
        busy = int(t_busy or 0)
        idle = int(t_idle or 0)
        if upd > latest_ms:
            latest_ms = upd
        if busy > idle and busy >= active_cutoff_ms:
            out["activeSessions"] += 1
        out["totalCostUsd"] += cost_f
        if upd >= week_ago_ms:
            out["sevenDayCostUsd"] += cost_f
            out["sevenDayTokens"] += toks
        if upd >= day_ago_ms:
            out["todayCostUsd"] += cost_f
            out["todayTokens"] += toks
        if isinstance(model_raw, str) and model_raw:
            try:
                parsed = json.loads(model_raw)
                if isinstance(parsed, dict):
                    if parsed.get("id"):
                        models.add(str(parsed["id"]))
                    if parsed.get("providerID"):
                        providers.add(str(parsed["providerID"]))
                else:
                    models.add(model_raw)
            except ValueError:
                models.add(model_raw)

    out["todayCostUsd"] = round(out["todayCostUsd"], 4)
    out["sevenDayCostUsd"] = round(out["sevenDayCostUsd"], 4)
    out["totalCostUsd"] = round(out["totalCostUsd"], 4)
    out["models"] = sorted(models)
    out["providers"] = sorted(providers)
    if latest_ms > 0:
        out["lastActiveAt"] = _iso_utc(latest_ms / 1000.0)
    return out


def _collect_opencode(
    home: Path,
    extra_cfg: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    db_path = Path(extra_cfg.get("dbPath") or (home / ".local" / "share" / "opencode" / "opencode.db"))
    stats = read_opencode_stats(db_path, now=now)
    cli_found = bool(shutil.which("opencode")) or (home / ".opencode" / "bin" / "opencode").exists()
    installed = cli_found or stats["available"]

    budget_usd = extra_cfg.get("monthlyBudgetUsd")
    if not isinstance(budget_usd, (int, float)) or budget_usd <= 0:
        # Default generous soft budget for DeepSeek API usage unless the user sets one.
        budget_usd = 50.0
    used_usd = stats["sevenDayCostUsd"]
    override = extra_cfg.get("headroomPct")
    if isinstance(override, (int, float)):
        headroom: float | None = max(0.0, min(100.0, round(float(override), 1)))
    elif installed:
        headroom = max(0.0, round(100.0 * (1.0 - min(1.0, used_usd / float(budget_usd))), 1))
    else:
        headroom = None

    return {
        "id": "opencode",
        "displayName": _PROVIDER_DISPLAY["opencode"],
        "cli": "opencode",
        "harnessAgent": "opencode",
        "installed": installed,
        "quotaMode": "budget",
        "subscriptionAccounts": 0,
        "apiKeyAccounts": 1 if installed else 0,
        "activeSlot": stats["providers"][0] if stats["providers"] else ("deepseek" if installed else None),
        "bestSlot": stats["models"][0] if stats["models"] else None,
        "headroomPct": headroom,
        "poolHeadroomPct": headroom,
        "bucket": _bucket_for_headroom(headroom, installed),
        "tight": headroom is not None and headroom <= 15.0,
        "nextResetAt": extra_cfg.get("nextResetAt"),
        "resetsInSec": None,
        "resetCountdown": None,
        "expiringResets": [],
        "apiKeyFallbackAvailable": installed,
        "todayCostUsd": stats["todayCostUsd"],
        "sevenDayCostUsd": stats["sevenDayCostUsd"],
        "activeSessions": stats["activeSessions"],
        "models": stats["models"],
        "lastReadAt": stats["lastActiveAt"],
    }


def _app_exists(apps_dir: Path | None, *names: str) -> bool:
    if apps_dir is None:
        return False
    return any((apps_dir / name).exists() for name in names)


def _collect_gemini(
    home: Path,
    extra_cfg: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    cli_found = bool(shutil.which("gemini"))
    gemini_dir = home / ".gemini"
    has_creds = (gemini_dir / "oauth_creds.json").exists() or (gemini_dir / "settings.json").exists()
    installed = cli_found or has_creds or bool(extra_cfg.get("enabled"))

    last_active: str | None = None
    tmp_dir = gemini_dir / "tmp"
    if tmp_dir.is_dir():
        newest = 0.0
        try:
            for p in tmp_dir.glob("*/chats/session-*.json"):
                try:
                    m = p.stat().st_mtime
                    if m > newest:
                        newest = m
                except OSError:
                    pass
        except OSError:
            pass
        if newest > 0:
            last_active = _iso_utc(newest)

    override = extra_cfg.get("headroomPct")
    if isinstance(override, (int, float)):
        headroom: float | None = max(0.0, min(100.0, round(float(override), 1)))
    elif installed:
        headroom = 85.0
    else:
        headroom = None

    reset_iso = extra_cfg.get("nextResetAt") if isinstance(extra_cfg.get("nextResetAt"), str) else None
    reset_ts = _parse_iso_ts(reset_iso)
    resets_in_sec = int(reset_ts - now) if reset_ts is not None and reset_ts > now else None

    return {
        "id": "gemini",
        "displayName": _PROVIDER_DISPLAY["gemini"],
        "cli": "gemini",
        "harnessAgent": "gemini",
        "installed": installed,
        "quotaMode": "inferred",
        "subscriptionAccounts": 1 if installed else 0,
        "apiKeyAccounts": 0,
        "activeSlot": "Gemini" if installed else None,
        "bestSlot": "Gemini" if installed else None,
        "headroomPct": headroom,
        "poolHeadroomPct": headroom,
        "bucket": _bucket_for_headroom(headroom, installed),
        "tight": headroom is not None and headroom <= 15.0,
        "nextResetAt": reset_iso,
        "resetsInSec": resets_in_sec,
        "resetCountdown": _human_countdown(resets_in_sec),
        "expiringResets": [],
        "apiKeyFallbackAvailable": bool(os.environ.get("GEMINI_API_KEY")),
        "lastReadAt": last_active or (_iso_utc(now) if installed else None),
    }


def _collect_antigravity(
    home: Path,
    extra_cfg: dict[str, Any],
    *,
    now: float,
    apps_dir: Path | None = None,
) -> dict[str, Any]:
    app_exists = _app_exists(apps_dir, "Antigravity.app", "Antigravity IDE.app")
    cli_found = bool(shutil.which("agy"))
    state_dir = home / ".gemini" / "antigravity-ide"
    installed = app_exists or cli_found or state_dir.exists() or (home / ".gemini" / "antigravity").exists() or bool(extra_cfg.get("enabled"))

    override = extra_cfg.get("headroomPct")
    if isinstance(override, (int, float)):
        headroom: float | None = max(0.0, min(100.0, round(float(override), 1)))
    elif installed:
        # Subscription tier without a public quota API: default healthy headroom until overridden.
        headroom = 85.0
    else:
        headroom = None

    reset_iso = extra_cfg.get("nextResetAt") if isinstance(extra_cfg.get("nextResetAt"), str) else None
    reset_ts = _parse_iso_ts(reset_iso)
    resets_in_sec = int(reset_ts - now) if reset_ts is not None and reset_ts > now else None

    claude_gpt_override = extra_cfg.get("claudeGptHeadroomPct")
    claude_gpt_headroom: float | None = (
        max(0.0, min(100.0, round(float(claude_gpt_override), 1)))
        if isinstance(claude_gpt_override, (int, float))
        else (headroom if installed else None)
    )

    return {
        "id": "antigravity",
        "displayName": _PROVIDER_DISPLAY["antigravity"],
        "cli": "agy",
        "harnessAgent": "agy",
        "installed": installed,
        "quotaMode": "inferred",
        "subscriptionAccounts": 1 if installed else 0,
        "apiKeyAccounts": 0,
        "activeSlot": "Ultra" if installed else None,
        "bestSlot": "Ultra" if installed else None,
        "headroomPct": headroom,
        "poolHeadroomPct": headroom,
        "modelPools": ["gemini", "claude", "gpt"] if installed else [],
        "claudeGptHeadroomPct": claude_gpt_headroom,
        "bucket": _bucket_for_headroom(headroom, installed),
        "tight": headroom is not None and headroom <= 15.0,
        "nextResetAt": reset_iso,
        "resetsInSec": resets_in_sec,
        "resetCountdown": _human_countdown(resets_in_sec),
        "expiringResets": [],
        "apiKeyFallbackAvailable": False,
        "monthlyPlanUsd": extra_cfg.get("monthlyPlanUsd", 200.0 if installed else None),
        "lastReadAt": _iso_utc(now) if installed else None,
    }


def _collect_grok(
    home: Path,
    extra_cfg: dict[str, Any],
    *,
    now: float,
    apps_dir: Path | None = None,
) -> dict[str, Any]:
    app_exists = _app_exists(apps_dir, "Grok Bot.app", "Grok.app")
    grokbot_dir = home / ".grokbot"
    cli_found = bool(shutil.which("grok"))
    installed = app_exists or grokbot_dir.exists() or cli_found or bool(extra_cfg.get("enabled"))

    daemon_log = grokbot_dir / "local-exec-daemon.log"
    last_active: str | None = None
    try:
        if daemon_log.exists():
            last_active = _iso_utc(daemon_log.stat().st_mtime)
    except OSError:
        pass

    override = extra_cfg.get("headroomPct")
    if isinstance(override, (int, float)):
        headroom: float | None = max(0.0, min(100.0, round(float(override), 1)))
    elif installed:
        headroom = 80.0
    else:
        headroom = None

    reset_iso = extra_cfg.get("nextResetAt") if isinstance(extra_cfg.get("nextResetAt"), str) else None
    reset_ts = _parse_iso_ts(reset_iso)
    resets_in_sec = int(reset_ts - now) if reset_ts is not None and reset_ts > now else None

    return {
        "id": "grok",
        "displayName": _PROVIDER_DISPLAY["grok"],
        "cli": "grok",
        "harnessAgent": "grok",
        "installed": installed,
        "quotaMode": "inferred",
        "subscriptionAccounts": 1 if installed else 0,
        "apiKeyAccounts": 0,
        "activeSlot": "SuperGrok" if installed else None,
        "bestSlot": "SuperGrok" if installed else None,
        "headroomPct": headroom,
        "poolHeadroomPct": headroom,
        "bucket": _bucket_for_headroom(headroom, installed),
        "tight": headroom is not None and headroom <= 15.0,
        "nextResetAt": reset_iso,
        "resetsInSec": resets_in_sec,
        "resetCountdown": _human_countdown(resets_in_sec),
        "expiringResets": [],
        "apiKeyFallbackAvailable": bool(os.environ.get("XAI_API_KEY")),
        "monthlyPlanUsd": extra_cfg.get("monthlyPlanUsd", 200.0 if installed else None),
        "lastReadAt": last_active or (_iso_utc(now) if installed else None),
    }


_OPTIONAL_PROBES: dict[str, dict[str, Any]] = {
    "cursor": {
        "clis": ("cursor-agent", "cursor"),
        "apps": ("Cursor.app",),
        "paths": (".cursor",),
        "default_headroom": 75.0,
        "slot": "Pro",
    },
    "copilot": {
        "clis": ("copilot",),
        "apps": (),
        "paths": (".config/github-copilot",),
        "default_headroom": 80.0,
        "slot": "Copilot",
    },
    "windsurf": {
        "clis": ("windsurf",),
        "apps": ("Windsurf.app",),
        "paths": (".codeium/windsurf",),
        "default_headroom": 75.0,
        "slot": "Windsurf",
    },
    "amp": {
        "clis": ("amp",),
        "apps": (),
        "paths": (".config/amp",),
        "default_headroom": 80.0,
        "slot": "Amp",
    },
    "aider": {
        "clis": ("aider",),
        "apps": (),
        "paths": (".aider.conf.yml",),
        "default_headroom": 80.0,
        "slot": "Aider",
    },
    "cline": {
        "clis": ("cline",),
        "apps": (),
        "paths": (".cline",),
        "default_headroom": 75.0,
        "slot": "Cline",
    },
}


def _collect_optional_provider(
    provider_id: str,
    home: Path,
    extra_cfg: dict[str, Any],
    *,
    now: float,
    apps_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Detect popular coding agents (Cursor, Copilot, Windsurf, Amp, Aider, Cline).

    Returns ``None`` when neither installed nor explicitly configured in
    ``providers_extra.json`` so ``qswap where`` stays focused on active tools.
    """
    spec = _OPTIONAL_PROBES[provider_id]
    cli_found = any(bool(shutil.which(c)) for c in spec["clis"])
    app_found = _app_exists(apps_dir, *spec["apps"])
    path_found = any((home / rel).exists() for rel in spec["paths"])
    configured = bool(extra_cfg)
    installed = cli_found or app_found or path_found or bool(extra_cfg.get("enabled"))
    if not installed and not configured:
        return None

    override = extra_cfg.get("headroomPct")
    if isinstance(override, (int, float)):
        headroom: float | None = max(0.0, min(100.0, round(float(override), 1)))
    elif installed:
        headroom = float(spec["default_headroom"])
    else:
        headroom = None

    reset_iso = extra_cfg.get("nextResetAt") if isinstance(extra_cfg.get("nextResetAt"), str) else None
    reset_ts = _parse_iso_ts(reset_iso)
    resets_in_sec = int(reset_ts - now) if reset_ts is not None and reset_ts > now else None

    return {
        "id": provider_id,
        "displayName": _PROVIDER_DISPLAY[provider_id],
        "cli": _PROVIDER_CLI[provider_id],
        "harnessAgent": _PROVIDER_HARNESS_AGENT[provider_id],
        "installed": installed,
        "quotaMode": "inferred",
        "subscriptionAccounts": 1 if installed else 0,
        "apiKeyAccounts": 0,
        "activeSlot": spec["slot"] if installed else None,
        "bestSlot": spec["slot"] if installed else None,
        "headroomPct": headroom,
        "poolHeadroomPct": headroom,
        "bucket": _bucket_for_headroom(headroom, installed),
        "tight": headroom is not None and headroom <= 15.0,
        "nextResetAt": reset_iso,
        "resetsInSec": resets_in_sec,
        "resetCountdown": _human_countdown(resets_in_sec),
        "expiringResets": [],
        "apiKeyFallbackAvailable": False,
        "lastReadAt": _iso_utc(now) if installed else None,
    }


@dataclass(frozen=True)
class RouteChoice:
    provider: str
    score: float
    headroom_pct: float | None
    bucket: str
    reason: str
    cli: str
    harness_agent: str
    expiring_credits: int = 0
    resets_in_sec: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "score": round(self.score, 3),
            "headroomPct": self.headroom_pct,
            "bucket": self.bucket,
            "reason": self.reason,
            "cli": self.cli,
            "harnessAgent": self.harness_agent,
            "expiringCredits": self.expiring_credits,
            "resetsInSec": self.resets_in_sec,
        }


def recommend_providers(
    providers_map: dict[str, dict[str, Any]],
    *,
    task_kind: str = "coding",
    caller_provider: str | None = None,
    exclude: tuple[str, ...] = (),
    require_cli: bool = False,
) -> list[RouteChoice]:
    """Rank installed providers by task affinity, headroom, and expiry urgency."""
    kind = task_kind if task_kind in _TASK_AFFINITY else "coding"
    affinities = _TASK_AFFINITY[kind]
    excluded = {e.lower() for e in exclude}
    caller = caller_provider.lower() if caller_provider else None

    choices: list[RouteChoice] = []
    for pid, info in providers_map.items():
        if pid in excluded:
            continue
        if not info.get("installed"):
            continue
        cli = str(info.get("cli") or _PROVIDER_CLI.get(pid, pid))
        if require_cli and not shutil.which(cli):
            continue
        headroom = info.get("headroomPct")
        bucket = str(info.get("bucket") or "unavailable")
        if bucket == "unavailable":
            continue

        # Base headroom score (0.0 .. 1.0); exhausted subscriptions with API fallback get a tiny floor.
        if isinstance(headroom, (int, float)):
            headroom_factor = max(0.0, min(1.0, float(headroom) / 100.0))
        else:
            headroom_factor = 0.5
        if headroom_factor <= 0.02:
            if info.get("apiKeyFallbackAvailable"):
                headroom_factor = 0.05  # prefer any free flat-rate subscription over metered fallback
            else:
                continue

        affinity = affinities.get(pid, 0.7)
        reasons: list[str] = []
        if isinstance(headroom, (int, float)):
            reasons.append(f"{headroom:.0f}% free")
        else:
            reasons.append(bucket)

        # Flat-rate subscriptions get a preference over pure metered APIs when headroom is good.
        sub_boost = 1.15 if (info.get("subscriptionAccounts") or 0) > 0 else 0.92

        # Expiry urgency: use-it-or-lose-it boost when unused quota resets soon!
        expiry_boost = 1.0
        resets_in_sec = info.get("resetsInSec")
        if isinstance(resets_in_sec, (int, float)) and resets_in_sec > 0 and headroom_factor >= 0.20:
            hours_left = float(resets_in_sec) / 3600.0
            if hours_left <= 2.0:
                expiry_boost = 2.0
                reasons.append(f"resets in {_human_countdown(resets_in_sec)}")
            elif hours_left <= 12.0:
                expiry_boost = 1.55
                reasons.append(f"resets in {_human_countdown(resets_in_sec)}")
            elif hours_left <= 24.0:
                expiry_boost = 1.25
                reasons.append(f"resets in {_human_countdown(resets_in_sec)}")

        # Expiring reset credits are huge unused value (e.g. Codex reset credits expiring in <5d).
        exp_credits = 0
        soonest_credit_days: float | None = None
        for g in info.get("expiringResets") or []:
            c = int(g.get("count") or 0)
            exp_credits += c
            e_sec = g.get("endsInSec")
            if isinstance(e_sec, (int, float)) and e_sec > 0:
                d = float(e_sec) / 86400.0
                soonest_credit_days = d if soonest_credit_days is None else min(soonest_credit_days, d)
        if exp_credits > 0:
            if soonest_credit_days is not None and soonest_credit_days <= 5.0:
                expiry_boost = max(expiry_boost, 2.2)
            else:
                expiry_boost = max(expiry_boost, 1.35)
            reasons.append(f"{exp_credits} reset credit{'s' if exp_credits != 1 else ''}")

        # Cross-harness diversity bonus for reviews (e.g. Claude PR → Codex/Antigravity review).
        diversity_boost = 1.0
        if kind == "review" and caller and pid != caller:
            diversity_boost = 1.25
            reasons.append("cross-harness")
        elif caller and pid == caller and info.get("tight"):
            diversity_boost = 0.65
            reasons.append("caller tight")

        score = affinity * headroom_factor * sub_boost * expiry_boost * diversity_boost
        choices.append(
            RouteChoice(
                provider=pid,
                score=score,
                headroom_pct=float(headroom) if isinstance(headroom, (int, float)) else None,
                bucket=bucket,
                reason=", ".join(reasons),
                cli=cli,
                harness_agent=str(info.get("harnessAgent") or pid),
                expiring_credits=exp_credits,
                resets_in_sec=int(resets_in_sec) if isinstance(resets_in_sec, (int, float)) else None,
            )
        )

    choices.sort(key=lambda c: (-c.score, c.provider))
    return choices


_UNSET = object()


def collect_fleet_snapshot(
    data_root: Path,
    *,
    home: Path | None = None,
    cost_summary: Any = None,
    now: float | None = None,
    apps_dir: Any = _UNSET,
) -> dict[str, Any]:
    """Assemble the complete, secret-free fleet capacity snapshot."""
    now_ts = time.time() if now is None else now
    home_dir = Path(home) if home is not None else Path.home()
    root = Path(data_root)
    extra_all = _read_json(root / EXTRA_PROVIDERS_FILENAME)
    resolved_apps: Path | None = (
        (Path("/Applications") if sys.platform == "darwin" else None)
        if apps_dir is _UNSET
        else (Path(apps_dir) if apps_dir is not None else None)
    )

    providers_map: dict[str, dict[str, Any]] = {
        "claude": _collect_managed_provider("claude", root, now=now_ts),
        "codex": _collect_managed_provider("codex", root / "codex", now=now_ts),
        "opencode": _collect_opencode(home_dir, extra_all.get("opencode") or {}, now=now_ts),
        "gemini": _collect_gemini(home_dir, extra_all.get("gemini") or {}, now=now_ts),
        "antigravity": _collect_antigravity(
            home_dir, extra_all.get("antigravity") or {}, now=now_ts, apps_dir=resolved_apps
        ),
        "grok": _collect_grok(
            home_dir, extra_all.get("grok") or {}, now=now_ts, apps_dir=resolved_apps
        ),
    }
    for pid in OPTIONAL_PROVIDERS:
        entry = _collect_optional_provider(
            pid, home_dir, extra_all.get(pid) or {}, now=now_ts, apps_dir=resolved_apps
        )
        if entry is not None:
            providers_map[pid] = entry

    today_usd: float | None = None
    seven_day_usd: float | None = None
    if cost_summary is not None:
        today_usd = round(float(getattr(cost_summary, "today", 0.0)), 2)
        seven_day_usd = round(float(getattr(cost_summary, "week", 0.0)), 2)
    else:
        try:
            from ai_quota_swap.menubar_data import read_cost

            cs = read_cost(root, days=False)
            if cs is not None:
                today_usd = round(float(cs.today), 2)
                seven_day_usd = round(float(cs.week), 2)
        except Exception:
            pass

    oc_today = float(providers_map["opencode"].get("todayCostUsd") or 0.0)
    oc_week = float(providers_map["opencode"].get("sevenDayCostUsd") or 0.0)
    if today_usd is not None:
        today_usd = round(today_usd + oc_today, 2)
    elif oc_today > 0:
        today_usd = round(oc_today, 2)
    if seven_day_usd is not None:
        seven_day_usd = round(seven_day_usd + oc_week, 2)
    elif oc_week > 0:
        seven_day_usd = round(oc_week, 2)

    recommendations = {
        kind: [c.to_json() for c in recommend_providers(providers_map, task_kind=kind)]
        for kind in TASK_KINDS
    }

    # Build top-level urgent nudges (e.g. expiring quota / expiring reset credits).
    alerts: list[dict[str, Any]] = []
    for pid, info in providers_map.items():
        headroom = info.get("headroomPct")
        resets_in = info.get("resetsInSec")
        if isinstance(headroom, (int, float)) and headroom >= 30.0 and isinstance(resets_in, (int, float)) and 0 < resets_in <= 6 * 3600:
            alerts.append({
                "kind": "expiring_quota",
                "provider": pid,
                "headroomPct": headroom,
                "resetsInSec": int(resets_in),
                "message": f"{info['displayName']} has {headroom:.0f}% free, resets in {_human_countdown(resets_in)}",
            })
        exp_count = sum(int(g.get("count") or 0) for g in (info.get("expiringResets") or []))
        if exp_count > 0:
            alerts.append({
                "kind": "expiring_credits",
                "provider": pid,
                "count": exp_count,
                "message": f"{info['displayName']} holds {exp_count} unused reset credit{'s' if exp_count != 1 else ''}",
            })

    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": _iso_utc(now_ts),
        "updatedAtMs": int(now_ts * 1000),
        "todayUsd": today_usd,
        "sevenDayUsd": seven_day_usd,
        "providers": providers_map,
        "recommendations": recommendations,
        "alerts": alerts,
    }


def write_fleet_status(
    data_root: Path,
    *,
    home: Path | None = None,
    cost_summary: Any = None,
    now: float | None = None,
    apps_dir: Any = _UNSET,
) -> dict[str, Any]:
    """Compute and atomically write ``<data_root>/fleet_status.json`` (mode 0600)."""
    snapshot = collect_fleet_snapshot(
        data_root, home=home, cost_summary=cost_summary, now=now, apps_dir=apps_dir
    )
    target = Path(data_root) / FLEET_STATUS_FILENAME
    try:
        fsutil.atomic_write_text(target, json.dumps(snapshot, indent=2) + "\n", mode=0o600)
    except OSError:
        pass
    return snapshot


def format_where_text(snapshot: dict[str, Any]) -> str:
    """Human-readable multi-provider fleet summary for ``qswap where``."""
    lines: list[str] = ["Fleet capacity"]
    providers_map = snapshot.get("providers") or {}
    ordered = [pid for pid in ALL_PROVIDERS if pid in providers_map] + [
        pid for pid in providers_map if pid not in ALL_PROVIDERS
    ]
    for pid in ordered:
        info = providers_map.get(pid)
        if not isinstance(info, dict):
            continue
        name = info.get("displayName") or pid
        if not info.get("installed"):
            if pid in ("claude", "codex"):
                lines.append(f"  {name:<12} not detected")
            continue
        hr = info.get("headroomPct")
        hr_str = f"{hr:5.1f}% free" if isinstance(hr, (int, float)) else f"{info.get('bucket', 'unknown'):>10}"
        parts = [f"{info.get('bucket', 'unknown'):<9}", hr_str]
        if info.get("resetCountdown"):
            parts.append(f"resets in {info['resetCountdown']}")
        exp = sum(int(g.get("count") or 0) for g in (info.get("expiringResets") or []))
        if exp:
            parts.append(f"{exp} reset credit{'s' if exp != 1 else ''}")
        if pid == "opencode" and info.get("models"):
            parts.append(f"model: {info['models'][0]}")
        if pid == "antigravity" and info.get("modelPools"):
            parts.append("pools: Gemini + Claude/GPT")
        if info.get("quotaMode") and info["quotaMode"] != "live":
            parts.append(f"({info['quotaMode']})")
        lines.append(f"  {name:<12} " + " · ".join(parts))

    today = snapshot.get("todayUsd")
    week = snapshot.get("sevenDayUsd")
    if today is not None or week is not None:
        t_s = f"${today:,.2f}" if today is not None else "n/a"
        w_s = f"${week:,.2f}" if week is not None else "n/a"
        lines.append(f"\nEstimated value: today {t_s} · 7 days {w_s}")

    recs = (snapshot.get("recommendations") or {}).get("coding") or []
    if recs:
        top = recs[0]
        lines.append(f"Best right now (coding): {top['provider']} ({top['reason']})")
    alerts = snapshot.get("alerts") or []
    if alerts:
        lines.append("Heads up:")
        for a in alerts:
            lines.append(f"  ! {a['message']}")
    return "\n".join(lines) + "\n"
