"""Synthetic transcript builders for the ledger tests.

Every id, path and email here is invented: sessions are ``sess-*``, projects
are under ``/work/``, accounts are ``*@example.com``.
"""

from __future__ import annotations

import json
from functools import partial
from datetime import datetime, timezone
from pathlib import Path

# Compact, like the files Codex and Claude Code write.
_dumps = partial(json.dumps, separators=(",", ":"))


def iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def claude_line(
    *,
    mid: str,
    ts: float,
    rid: str | None = None,
    model: str = "claude-opus-5-5",
    inp: int = 10,
    out: int = 5,
    cr: int = 100,
    w5: int = 20,
    w1: int = 0,
    session: str = "sess-1",
    cwd: str = "/work/demo-app",
    entrypoint: str = "cli",
    sidechain: bool = False,
    stop: str | None = "end_turn",
    speed: str = "standard",
) -> str:
    return _dumps(
        {
            "parentUuid": None,
            "isSidechain": sidechain,
            "message": {
                "model": model,
                "id": mid,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "synthetic"}],
                "stop_reason": stop,
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": w5 + w1,
                    "cache_read_input_tokens": cr,
                    "output_tokens": out,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": w5,
                        "ephemeral_1h_input_tokens": w1,
                    },
                    "output_tokens_details": {"thinking_tokens": 0},
                    "service_tier": "standard",
                    "speed": speed,
                },
            },
            "type": "assistant",
            "uuid": f"u-{mid}-{out}",
            "timestamp": iso(ts),
            "userType": "external",
            "entrypoint": entrypoint,
            "cwd": cwd,
            "sessionId": session,
            "version": "2.1.322",
            "requestId": rid if rid is not None else f"req-{mid}",
        }
    )


def user_line(ts: float, session: str = "sess-1") -> str:
    return _dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": "synthetic prompt"},
            "timestamp": iso(ts),
            "sessionId": session,
            "cwd": "/work/demo-app",
        }
    )


def owner_line(session: str, account_uuid: str, org: str = "") -> str:
    return _dumps(
        {
            "type": "session-owner",
            "accountUuid": account_uuid,
            "organizationUuid": org,
            "sessionId": session,
        }
    )


def write_lines(path: Path, lines: list[str], *, append: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return path


# -- Codex -------------------------------------------------------------------


def codex_meta(
    ts: float,
    thread: str,
    *,
    session: str | None = None,
    cwd: str = "/work/demo-app",
    version: str = "0.166.0",
    ordinal: int | None = 0,
    start_ordinal: int | None = None,
) -> str:
    payload = {
        "id": thread,
        "session_id": session or thread,
        "timestamp": iso(ts),
        "cwd": cwd,
        "originator": "codex_cli_rs",
        "cli_version": version,
        "source": "cli",
        "model_provider": "openai",
        "base_instructions": {"text": "synthetic"},
    }
    if start_ordinal is not None:
        payload["subagent_history_start_ordinal"] = start_ordinal
    line: dict = {"timestamp": iso(ts)}
    if ordinal is not None:
        line["ordinal"] = ordinal
    line.update({"type": "session_meta", "payload": payload})
    return _dumps(line)


def codex_turn(ts: float, model: str = "gpt-6.1-sol", ordinal: int | None = None) -> str:
    line: dict = {"timestamp": iso(ts)}
    if ordinal is not None:
        line["ordinal"] = ordinal
    line.update({"type": "turn_context", "payload": {"model": model, "cwd": "/work/demo-app"}})
    return _dumps(line)


def _usage(inp: int, cached: int, out: int, reasoning: int = 0, write: int = 0) -> dict:
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": write,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": inp + out,
    }


def codex_record(
    ts: float,
    thread: str,
    response: str,
    *,
    inp: int = 1000,
    cached: int = 800,
    out: int = 50,
    reasoning: int = 20,
    session: str | None = None,
    ordinal: int | None = None,
) -> str:
    u = _usage(inp, cached, out, reasoning)
    line: dict = {"timestamp": iso(ts)}
    if ordinal is not None:
        line["ordinal"] = ordinal
    line.update(
        {
            "type": "token_usage_record",
            "payload": {
                "thread_id": thread,
                "turn_id": "turn-1",
                "session_id": session or thread,
                "root_turn_id": "turn-1",
                "response_id": response,
                "usage": u,
                "turn_token_usage": u,
                "thread_token_usage": u,
            },
        }
    )
    return _dumps(line)


def codex_count(
    ts: float,
    total: tuple[int, int, int],
    last: tuple[int, int, int],
    *,
    ordinal: int | None = None,
) -> str:
    """``total``/``last`` are (input, cached, output)."""
    line: dict = {"timestamp": iso(ts)}
    if ordinal is not None:
        line["ordinal"] = ordinal
    line.update(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": _usage(total[0], total[1], total[2]),
                    "last_token_usage": _usage(last[0], last[1], last[2]),
                    "model_context_window": 272000,
                },
                "rate_limits": None,
            },
        }
    )
    return _dumps(line)


def rollout_name(thread: str, stamp: str = "2026-11-09T10-00-00") -> str:
    return f"rollout-{stamp}-{thread}.jsonl"
