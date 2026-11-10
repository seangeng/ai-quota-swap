"""Line readers: turn transcript lines into token events.

Both readers see raw ``bytes`` lines and reject most of them with a substring
check before any JSON parsing; the parse is the expensive part of a scan.

Claude Code (``<config home>/projects/**/*.jsonl``)
    One ``assistant`` line per content block of a response, each carrying the
    response's ``message.usage``. The lines of one response share
    ``message.id`` and ``requestId``; their input and cache counts are equal,
    and ``output_tokens`` grows to its final value on the last line (the
    earlier lines carry the count streamed so far). So the dedupe key is
    ``message.id`` + ``requestId`` and the highest output count wins. A
    resumed session replays earlier responses into its new file under the
    same keys, which the same rule absorbs. Sub-agent transcripts
    (``isSidechain``, ``subagents/``) are separate API requests with their own
    ids: they are counted. ``session-owner`` lines name the account that
    started the session; they are kept for attribution (see report.py).

Codex (``$CODEX_HOME/sessions`` and ``archived_sessions``)
    ``token_usage_record`` lines, one per completed response, keyed by
    ``thread_id`` + ``response_id``; forks never copy them. Files written
    before those records existed carry only cumulative ``token_count``
    events, which forks replay; those are counted as deltas of the file's
    own events only, keyed by the cumulative totals so a replay in another
    file of the same session lands on the same key. The model comes from the
    latest ``turn_context``; the project from the first ``session_meta``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from ai_quota_swap.ledger.pricing import FAST_SUFFIX
from ai_quota_swap.ledger.store import key_hash

# Claude Code entrypoints that run on a login the desktop app supplies, not on
# the CLI's stored login that qswap switches.
DESKTOP_ENTRYPOINTS = frozenset({"claude-desktop", "claude-desktop-3p", "local-agent"})
DESKTOP_ACCOUNT = "desktop"

_NOT_A_MODEL = "unknown"


@dataclass(slots=True)
class Event:
    """One response's tokens, ready for dedupe and bucketing."""

    key: int
    ts: float
    model: str
    project: str
    session: str
    input: int
    output: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    reasoning: int
    account: str | None = None  # preset attribution; None = by activation log


@dataclass(slots=True)
class OwnerNote:
    """Claude Code's ``session-owner`` line: the account that started a session."""

    session: str
    owner: str  # "<accountUuid>|<organizationUuid>"


def parse_iso(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


# -- Claude Code -----------------------------------------------------------


def parse_claude_line(raw: bytes) -> Event | OwnerNote | None:
    """An event, an owner note, or None for the many lines that carry neither."""
    if b'"session-owner"' in raw[:64]:
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if obj.get("type") != "session-owner":
            return None
        sid, acct = obj.get("sessionId"), obj.get("accountUuid")
        if isinstance(sid, str) and sid and isinstance(acct, str) and acct:
            return OwnerNote(sid, f"{acct}|{obj.get('organizationUuid') or ''}")
        return None
    if b'"input_tokens"' not in raw or b'"assistant"' not in raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    model = msg.get("model")
    if not isinstance(model, str) or not model or model == "<synthetic>":
        return None  # API error placeholders carry zero usage
    ts = parse_iso(obj.get("timestamp"))
    if ts is None:
        return None
    inp = _int(usage.get("input_tokens"))
    out = _int(usage.get("output_tokens"))
    read = _int(usage.get("cache_read_input_tokens"))
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        w5 = _int(cc.get("ephemeral_5m_input_tokens"))
        w1 = _int(cc.get("ephemeral_1h_input_tokens"))
    else:
        w5, w1 = _int(usage.get("cache_creation_input_tokens")), 0
    if not (inp or out or read or w5 or w1):
        return None
    details = usage.get("output_tokens_details")
    reasoning = _int(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    mid, rid = msg.get("id"), obj.get("requestId")
    if isinstance(mid, str) and mid:
        key = key_hash("claude", mid, rid if isinstance(rid, str) else "")
    else:
        uid = obj.get("uuid")
        if not isinstance(uid, str) or not uid:
            return None
        key = key_hash("claude-line", uid)
    if usage.get("speed") == "fast":
        model += FAST_SUFFIX
    entry = obj.get("entrypoint")
    return Event(
        key=key,
        ts=ts,
        model=model,
        project=obj.get("cwd") if isinstance(obj.get("cwd"), str) else "",
        session=obj.get("sessionId") if isinstance(obj.get("sessionId"), str) else "",
        input=inp,
        output=out,
        cache_read=read,
        cache_write_5m=w5,
        cache_write_1h=w1,
        reasoning=reasoning,
        account=DESKTOP_ACCOUNT if entry in DESKTOP_ENTRYPOINTS else None,
    )


# -- Codex -------------------------------------------------------------------

_HEAD = 240


def new_codex_state() -> dict:
    return {
        "thread": None,
        "session": None,
        "cwd": "",
        "version": None,
        "start_ord": None,
        "model": None,
        "metas": 0,
        "inherited": False,
        "records": False,
        "prev": None,
    }


def _codex_kind(raw: bytes) -> str | None:
    """A cheap guess at a rollout line's kind from its first bytes (the
    envelope's ``type`` and the payload's own ``type`` come first). Callers
    confirm the guess after parsing."""
    head = raw[:_HEAD]
    if b'"payload"' not in head:
        head = raw  # an unexpected layout: fall back to the whole line
    if b'"token_usage_record"' in head:
        return "record"
    if b'"event_msg"' in head:
        if b'"token_count"' in head:
            return "count"
        if b'"task_started"' in head:
            return "task"
        return None
    if b'"turn_context"' in head:
        return "turn"
    if b'"session_meta"' in head:
        return "meta"
    return None


_ENVELOPE = {
    "record": "token_usage_record",
    "count": "event_msg",
    "task": "event_msg",
    "turn": "turn_context",
    "meta": "session_meta",
}


def _usage_tokens(u: object) -> tuple[int, int, int, int, int] | None:
    """(fresh input, cache read, cache write, output, reasoning) from a Codex
    usage object. OpenAI counts cached and cache-write tokens inside
    ``input_tokens`` and reasoning inside ``output_tokens``."""
    if not isinstance(u, dict):
        return None
    total_in = _int(u.get("input_tokens"))
    cached = _int(u.get("cached_input_tokens"))
    write = _int(u.get("cache_write_input_tokens"))
    out = _int(u.get("output_tokens"))
    reasoning = _int(u.get("reasoning_output_tokens"))
    return max(0, total_in - cached - write), cached, write, out, reasoning


def _sig(u: dict) -> list[int]:
    return [
        _int(u.get("total_tokens")),
        _int(u.get("input_tokens")),
        _int(u.get("cached_input_tokens")),
        _int(u.get("output_tokens")),
        _int(u.get("reasoning_output_tokens")),
    ]


def _counts_wanted(state: dict) -> bool:
    """Whether this thread is still counted from ``token_count`` events: only
    until its first ``token_usage_record``. Codex writes a response's record
    before that response's ``token_count``, so a thread that writes records
    never reaches an own ``token_count`` first. The CLI version is no guide:
    on real files, threads from 0.153 to 0.162 exist both with and without
    records."""
    return not state.get("records")


def parse_codex_line(raw: bytes, state: dict) -> Event | None:
    """Update ``state`` from one rollout line; return an event when it carries one."""
    kind = _codex_kind(raw)
    if kind is None:
        return None
    if kind == "count" and not _counts_wanted(state):
        return None  # skip the parse: this thread is counted from records
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != _ENVELOPE[kind]:
        return None
    p = obj.get("payload")
    if not isinstance(p, dict):
        return None
    if kind in ("count", "task") and p.get("type") != ("token_count" if kind == "count" else "task_started"):
        return None
    if kind == "meta":
        state["metas"] = state.get("metas", 0) + 1
        if state["metas"] > 1:
            # A fork's inherited prefix starts with the parent's session_meta.
            state["inherited"] = True
            return None
        if isinstance(p.get("id"), str):
            state["thread"] = p["id"]
        sid = p.get("session_id")
        state["session"] = sid if isinstance(sid, str) and sid else state["thread"]
        if isinstance(p.get("cwd"), str):
            state["cwd"] = p["cwd"]
        state["version"] = p.get("cli_version") if isinstance(p.get("cli_version"), str) else None
        so = p.get("subagent_history_start_ordinal")
        state["start_ord"] = so if isinstance(so, int) and not isinstance(so, bool) else None
        return None
    if kind in ("turn", "task"):
        if kind == "turn" and isinstance(p.get("model"), str) and p["model"]:
            state["model"] = p["model"]
        if state.get("start_ord") is None:
            state["inherited"] = False  # the child's own turn begins
        return None
    ts = parse_iso(obj.get("timestamp"))
    if ts is None:
        return None
    if kind == "record":
        state["records"] = True
        thread, resp = p.get("thread_id"), p.get("response_id")
        toks = _usage_tokens(p.get("usage"))
        if not isinstance(resp, str) or not resp or toks is None:
            return None
        thread = thread if isinstance(thread, str) and thread else (state.get("thread") or "")
        session = p.get("session_id")
        session = session if isinstance(session, str) and session else (state.get("session") or thread)
        key = key_hash("codex", thread, resp)
        return _codex_event(key, ts, state, session, toks)
    # kind == "count": the fallback for threads without records.
    info = p.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("total_token_usage"), dict):
        return None
    total = info["total_token_usage"]
    cur = _sig(total)
    prev = state.get("prev")
    state["prev"] = cur
    ordinal = obj.get("ordinal")
    start = state.get("start_ord")
    if isinstance(ordinal, int) and isinstance(start, int):
        if ordinal < start:
            return None  # inherited from the parent thread
    elif state.get("inherited"):
        return None
    if prev == cur:
        return None  # the same totals re-announced
    last = info.get("last_token_usage")
    if prev is None or cur[0] < prev[0]:
        # The file's first own event, or a total that went down (a new
        # context): the response's own usage, never a negative delta.
        toks = _usage_tokens(last if isinstance(last, dict) else total)
    else:
        delta = {
            "input_tokens": cur[1] - prev[1],
            "cached_input_tokens": cur[2] - prev[2],
            "output_tokens": cur[3] - prev[3],
            "reasoning_output_tokens": cur[4] - prev[4],
        }
        toks = _usage_tokens(delta)
    if toks is None or not any(toks):
        return None
    session = state.get("session") or state.get("thread") or ""
    key = key_hash("codex-count", session, json.dumps(prev), json.dumps(cur))
    return _codex_event(key, ts, state, session, toks)


def _codex_event(
    key: int, ts: float, state: dict, session: str, toks: tuple[int, int, int, int, int]
) -> Event:
    fresh, cached, write, out, reasoning = toks
    return Event(
        key=key,
        ts=ts,
        model=state.get("model") or _NOT_A_MODEL,
        project=state.get("cwd") or "",
        session=session,
        input=fresh,
        output=out,
        cache_read=cached,
        cache_write_5m=write,
        cache_write_1h=0,
        reasoning=reasoning,
    )
