"""Write a scenario into the demo's temp home with the tool's own writers.

Accounts go in the way the test suite's ``EngineHarness.seed`` puts them in:
through the switcher's credential and config writers and its sequence file,
so the store on disk is exactly what a real ``qswap add`` leaves (stored
credentials land in the in-memory Keychain on macOS, in files elsewhere).
The live login is written through the same store. Nothing here is run
outside the sandbox: ``run.py`` installs and verifies the guards first.

Session history for ``qswap usage`` is written as Claude Code and Codex
write it (the same line shapes as ``tests/ledger_fixtures.py``).
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scenarios import PLANS, ClaudeAccount, CodexAccount, Ledger, Scenario

SCOPES = ["user:inference", "user:profile", "user:sessions:claude_code"]


def _credentials(a: ClaudeAccount, scenario: Scenario) -> str:
    if a.api_key:
        return a.api_key_secret  # a managed key is stored raw
    plan = PLANS[a.plan]
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": a.access_token(0),
            "refreshToken": a.refresh_token(0),
            "expiresAt": int((scenario.epoch + a.token_expires_in.total_seconds()) * 1000),
            "scopes": SCOPES,
            "subscriptionType": plan.organization_type.removeprefix("claude_"),
            "rateLimitTier": plan.tier,
        }
    })


def _oauth_account(a: ClaudeAccount) -> dict:
    if a.api_key:
        # What `qswap add-token` records for a key: no account metadata.
        return {
            "emailAddress": a.email,
            "accountUuid": "",
            "organizationUuid": None,
            "organizationName": None,
        }
    return {
        "accountUuid": a.account_uuid,
        "emailAddress": a.email,
        "organizationUuid": a.org_uuid,
        "organizationName": a.org_name,
        "organizationRole": "admin",
        "displayName": a.email.split("@", 1)[0],
    }


def _write_live_claude(switcher, a: ClaudeAccount, scenario: Scenario) -> None:
    from ai_quota_swap.paths import get_global_config_path

    switcher._store._write_credentials(_credentials(a, scenario))
    switcher._write_json(
        get_global_config_path(),
        {
            "numStartups": 212,
            "hasCompletedOnboarding": True,
            "oauthAccount": _oauth_account(a),
        },
    )


def seed_claude(switcher, scenario: Scenario) -> None:
    """Managed Claude accounts, the live login, and engine quarantines.

    A scenario with no managed Claude account writes no registry at all, as
    on a machine where ``qswap add`` never ran; its live login (if any) is the
    unmanaged ``scenario.live``.
    """
    accounts = scenario.claude_accounts
    if accounts:
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        added = (scenario.clock - timedelta(days=60)).isoformat().replace("+00:00", "Z")
        for a in accounts:
            n = str(a.slot)
            switcher._write_account_credentials(n, a.email, _credentials(a, scenario))
            switcher._write_account_config(n, a.email, json.dumps({"oauthAccount": _oauth_account(a)}))
            if a.api_key:
                record = {"email": a.email, "uuid": "", "organizationUuid": "",
                          "organizationName": "", "added": added, "kind": "api_key"}
            else:
                record = {
                    "email": a.email,
                    "uuid": a.account_uuid,
                    "organizationUuid": a.org_uuid,
                    "organizationName": a.org_name,
                    "added": added,
                }
            if a.alias:
                record["alias"] = a.alias
            if a.disabled:
                record["disabled"] = True
            if a.priority is not None:
                record["priority"] = a.priority
            if a.threshold is not None:
                record["threshold"] = a.threshold
            if a.budget is not None:
                record["budget"] = a.budget
            data["accounts"][n] = record
            if a.slot not in data["sequence"]:
                data["sequence"].append(a.slot)
        data["sequence"].sort()
        data["activeAccountNumber"] = scenario.active
        switcher._write_json(switcher.sequence_file, data)

    live = scenario.account(scenario.active) if scenario.active is not None else scenario.live
    if live is not None:
        _write_live_claude(switcher, live, scenario)

    quarantined = [a for a in accounts if a.quarantine]
    if quarantined:
        from ai_quota_swap.autoswitch import STATE_FILENAME, _mutate_state_file

        at = scenario.clock - timedelta(minutes=50)

        def add(state: dict) -> None:
            for a in quarantined:
                creds = switcher.read_account_credentials(str(a.slot), a.email)
                state.setdefault("quarantine", {})[str(a.slot)] = {
                    "email": a.email,
                    "reason": a.quarantine,
                    "at": at.isoformat().replace("+00:00", "Z"),
                    "refreshTokenFingerprint": switcher.credential_fingerprint(creds),
                }

        _mutate_state_file(switcher.backup_dir / STATE_FILENAME, add)


def seed_codex(switcher, scenario: Scenario) -> None:
    """Managed Codex accounts and Codex's live ``auth.json``, through the
    Codex switcher's own registry and saved-login writers."""
    accounts = scenario.codex_accounts
    live: CodexAccount | None = (
        scenario.codex_account(scenario.codex_active)
        if scenario.codex_active is not None else scenario.codex_live
    )
    if not accounts and live is None:
        return
    from ai_quota_swap.codex import auth as codex_auth
    from ai_quota_swap.codex.switcher import CodexSwitcher
    from ai_quota_swap.fsutil import atomic_write_bytes

    if accounts:
        cs = CodexSwitcher(data_root=switcher.backup_dir, configure_logging=False)
        data = cs._empty_registry()
        added = (scenario.clock - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        for a in accounts:
            n = str(a.slot)
            cs._write_saved(n, a.login_record(scenario.clock))
            record = {
                "email": a.email,
                "uuid": a.user_id,
                "organizationUuid": a.account_id,
                "organizationName": "",
                "added": added,
            }
            if a.alias:
                record["alias"] = a.alias
            if a.disabled:
                record["disabled"] = True
            if a.priority is not None:
                record["priority"] = a.priority
            if a.threshold is not None:
                record["threshold"] = a.threshold
            data["accounts"][n] = record
        data["sequence"] = sorted(a.slot for a in accounts)
        data["activeAccountNumber"] = scenario.codex_active
        cs._write_registry(data)
    if live is not None:
        home = codex_auth.codex_home()
        atomic_write_bytes(codex_auth.auth_path(home), live.login_record(scenario.clock), mode=0o600)


def seed_settings(backup_dir: Path, scenario: Scenario) -> None:
    from ai_quota_swap.settings import atomic_write_json, settings_path

    if not scenario.settings:
        return  # no settings.json, as before any `qswap config set`
    atomic_write_json(settings_path(backup_dir), scenario.settings)


SEEDERS = {"claude": seed_claude, "codex": seed_codex}


def seed(scenario: Scenario) -> object:
    """Seed everything and return the switcher used to do it."""
    from ai_quota_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    providers = {a.provider for a in scenario.accounts}
    unknown = providers - set(SEEDERS)
    if unknown:
        raise SystemExit(f"no seeder for provider(s): {', '.join(sorted(unknown))}")
    # Every seeder runs: each writes nothing when the scenario has none of its
    # accounts, and the live logins of a first run belong to no account.
    for provider in sorted(SEEDERS):
        SEEDERS[provider](switcher, scenario)
    seed_settings(switcher.backup_dir, scenario)
    if scenario.ledger is not None:
        seed_ledger(switcher.backup_dir, scenario)
    return switcher


# -- session history ----------------------------------------------------------


def _iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _claude_line(*, mid: str, ts: float, model: str, inp: int, out: int, cr: int, w5: int,
                 session: str, cwd: str) -> str:
    return json.dumps({
        "parentUuid": None,
        "isSidechain": False,
        "message": {
            "model": model,
            "id": mid,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "synthetic"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": w5,
                "cache_read_input_tokens": cr,
                "output_tokens": out,
                "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": 0},
                "output_tokens_details": {"thinking_tokens": 0},
                "service_tier": "standard",
                "speed": "standard",
            },
        },
        "type": "assistant",
        "uuid": f"u-{mid}",
        "timestamp": _iso(ts),
        "userType": "external",
        "entrypoint": "cli",
        "cwd": cwd,
        "sessionId": session,
        "version": "2.1.321",
        "requestId": f"req-{mid}",
    }, separators=(",", ":"))


def _codex_lines(*, thread: str, start: float, model: str, cwd: str, turns: list[tuple[float, int, int, int, int]]) -> list[str]:
    def wrap(ts: float, kind: str, payload: dict) -> str:
        return json.dumps({"timestamp": _iso(ts), "type": kind, "payload": payload}, separators=(",", ":"))

    lines = [
        wrap(start, "session_meta", {
            "id": thread, "session_id": thread, "timestamp": _iso(start), "cwd": cwd,
            "originator": "codex_cli_rs", "cli_version": "0.166.0", "source": "cli",
            "model_provider": "openai", "base_instructions": {"text": "synthetic"},
        }),
        wrap(start, "turn_context", {"model": model, "cwd": cwd}),
    ]
    for i, (ts, inp, cached, out, reasoning) in enumerate(turns):
        usage = {
            "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out,
        }
        lines.append(wrap(ts, "token_usage_record", {
            "thread_id": thread, "turn_id": f"turn-{i}", "session_id": thread,
            "root_turn_id": f"turn-{i}", "response_id": f"resp-{thread[:8]}-{i}",
            "usage": usage, "turn_token_usage": usage, "thread_token_usage": usage,
        }))
    return lines


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pick(rng: random.Random, weighted: tuple[tuple[str, float], ...]) -> str:
    r = rng.random() * sum(w for _, w in weighted)
    for name, w in weighted:
        r -= w
        if r <= 0:
            return name
    return weighted[-1][0]


def seed_ledger(backup_dir: Path, scenario: Scenario) -> None:
    from ai_quota_swap.paths import get_claude_config_home, get_codex_home

    ledger: Ledger = scenario.ledger  # type: ignore[assignment]
    rng = random.Random(ledger.seed)
    now = scenario.epoch
    day0 = scenario.clock.replace(hour=0, minute=0, second=0, microsecond=0)
    projects_root = get_claude_config_home() / "projects"
    msg = 0
    for days_ago in range(ledger.days - 1, -1, -1):
        day = day0 - timedelta(days=days_ago)
        weekend = day.weekday() >= 5
        day_scale = ledger.daily_scale * (0.35 if weekend else rng.uniform(0.75, 1.3))
        for p in ledger.projects:
            sessions = max(0, round(rng.gauss(2.4 * p.weight / 0.25 * day_scale, 0.6)))
            for s in range(sessions):
                start_h = rng.uniform(8.5, 21.0)
                start = day.timestamp() + start_h * 3600
                if start > now - 600:
                    continue
                session = f"{rng.getrandbits(32):08x}-{rng.getrandbits(16):04x}-4{rng.getrandbits(12):03x}-8{rng.getrandbits(12):03x}-{rng.getrandbits(48):012x}"
                lines = []
                ts = start
                for _ in range(rng.randint(14, 40)):
                    ts += rng.uniform(25, 160)
                    if ts > now - 60:
                        break
                    msg += 1
                    model = _pick(rng, p.models)
                    lines.append(_claude_line(
                        mid=f"msg_demo_{msg:06d}", ts=ts, model=model,
                        inp=rng.randint(40, 2400), out=rng.randint(250, 3800),
                        cr=rng.randint(30_000, 160_000), w5=rng.randint(800, 9_000),
                        session=session, cwd=p.path,
                    ))
                if lines:
                    _write(projects_root / p.path.replace("/", "-") / f"{session}.jsonl", lines)

    codex_root = get_codex_home() / "sessions"
    thread_n = 0
    for days_ago in range(ledger.days - 1, -1, -1):
        day = day0 - timedelta(days=days_ago)
        for p in ledger.codex_projects:
            if rng.random() > p.weight * 8:
                continue
            start = day.timestamp() + rng.uniform(9, 20) * 3600
            if start > now - 600:
                continue
            thread_n += 1
            thread = f"0000{thread_n:04d}-0000-4000-8000-{rng.getrandbits(48):012x}"
            model = _pick(rng, p.models)
            turns = []
            ts = start
            for _ in range(rng.randint(6, 18)):
                ts += rng.uniform(40, 200)
                if ts > now - 60:
                    break
                inp = rng.randint(8_000, 60_000)
                turns.append((ts, inp, int(inp * rng.uniform(0.6, 0.9)), rng.randint(300, 3000), rng.randint(100, 1500)))
            stamp = datetime.fromtimestamp(start, tz=timezone.utc)
            name = f"rollout-{stamp.strftime('%Y-%m-%dT%H-%M-%S')}-{thread}.jsonl"
            _write(codex_root / stamp.strftime("%Y/%m/%d") / name,
                   _codex_lines(thread=thread, start=start, model=model, cwd=p.path, turns=turns))

    acts = []
    for days_ago, hour, slot in ledger.switches:
        ts = (day0 - timedelta(days=days_ago)).timestamp() + hour * 3600
        if ts > now:
            continue
        a = scenario.account(slot)
        acts.append(json.dumps({
            "ts": _iso(ts), "number": a.slot, "email": a.email, "org": _org(a), "source": "switch",
        }, separators=(",", ":")))
    # The live login at the scenario clock is the active account.
    active = scenario.account(scenario.active)
    acts.append(json.dumps({
        "ts": _iso(now - 1800), "number": active.slot, "email": active.email,
        "org": _org(active), "source": "switch",
    }, separators=(",", ":")))
    _write(backup_dir / "activations.jsonl", acts)

    # Codex's own activation log, in its data root: account ids are the org.
    codex_acts = []
    for days_ago, hour, slot in ledger.codex_switches:
        ts = (day0 - timedelta(days=days_ago)).timestamp() + hour * 3600
        if ts > now:
            continue
        c = scenario.codex_account(slot)
        codex_acts.append(json.dumps({
            "ts": _iso(ts), "number": c.slot, "email": c.email, "org": c.account_id, "source": "switch",
        }, separators=(",", ":")))
    if codex_acts and scenario.codex_active is not None:
        c = scenario.codex_account(scenario.codex_active)
        codex_acts.append(json.dumps({
            "ts": _iso(now - 1800), "number": c.slot, "email": c.email, "org": c.account_id,
            "source": "switch",
        }, separators=(",", ":")))
    if codex_acts:
        _write(backup_dir / "codex" / "activations.jsonl", codex_acts)

    if ledger.prescan:
        from ai_quota_swap.ledger.usage import resolve_range, run_usage

        since, until = resolve_range(days=ledger.days, since=None, until=None,
                                     today=scenario.clock.date())
        run_usage(backup_dir, since=since, until=until, by="day", scan=True,
                  registry=_registry(backup_dir), progress=False)


def _org(a: ClaudeAccount) -> str:
    """The organization the activation log records: an API key has none, as
    its registry record says."""
    return "" if a.api_key else a.org_uuid


def _registry(backup_dir: Path) -> dict:
    path = backup_dir / "sequence.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
