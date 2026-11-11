"""The auto-switch engine's log, in words.

Two consumers share the words in this module:

- every engine event's ``human()`` (``autoswitch.py``), which the menu bar
  shows as a notification and the dashboard as its log line, so it stays a
  complete one-line sentence;
- :class:`AutoLog`, the renderer ``qswap auto`` and ``qswap codex auto``
  print with: one line per tick, a per-account comparison only when it
  matters, and every line inside 80 columns.

The rules are docs/design/ux.md's: ``#2 work`` for an account, ``→`` for
"to", ``·`` between facts, whole percentages (a comparison shows both sides
at one precision), the reason for a switch in words, ``needs re-login`` and
``usage unavailable (HTTP 503)`` for the two ways an account drops out.
Reason codes (``below-threshold``) stay in JSON.

Everything here reads plain event data plus the event's non-JSON
``context`` (aliases, provider, how the engine saw each account this tick);
nothing here does I/O except :class:`AutoLog` calling its ``write``.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone

from ai_quota_swap import display
from ai_quota_swap.labels import account_label
from ai_quota_swap.lines import BindingWindow, binding_window

__all__ = [
    "AutoLog",
    "banner",
    "pct_pair",
    "pct_text",
]

#: Console width every line is written for.
WIDTH = display.WIDTH
#: ``HH:MM:SS`` plus two spaces.
STAMP_WIDTH = 10
#: Lines under a tick (the comparison, the next step) start here.
INDENT = " " * (STAMP_WIDTH + 2)
#: The widest an account label gets in the comparison's label column.
LABEL_COLUMN_MAX = 24

#: No-switch reasons that mean "the engine wants to move and cannot".
BLOCKED_REASONS = frozenset({
    "no-candidates", "no-comparison", "no-qualifying-candidate", "no-viable-target",
})

# Provider states (``UsageEntry.sentinel``) in a few words, per provider.
_SENTINEL_WORDS = {
    "token expired": {
        "claude": "token expired, the refresh retries",
        "codex": "token expired, Codex renews it on its next run",
    },
    "usage unknown until used": {"*": "usage unknown until used"},
    "keychain unavailable": {"*": "Keychain unavailable"},
    "foreign credential": {"*": "the live login belongs to another account"},
    "no credentials": {"*": "no saved login"},
    "re-login needed": {"*": display.NEEDS_RELOGIN},
    "api key": {"*": "API key"},
}

_UNQUARANTINE_WORDS = {
    "credentials-replaced": "login replaced",
    "account-replaced": "account replaced",
}


# -- percentages -------------------------------------------------------------------


def pct_decimals(line: float | None) -> int:
    """Decimals a comparison against ``line`` needs: 0 for 90, 1 for 99.9,
    at most 2."""
    if not isinstance(line, (int, float)):
        return 0
    for decimals in (0, 1, 2):
        if abs(round(line, decimals) - line) < 1e-9:
            return decimals
    return 2


def pct_text(value: float, decimals: int = 0, *, under: float | None = None) -> str:
    """``value`` at ``decimals`` places, never rounded up onto a line it has
    not reached: below ``under`` (its switch line) or below 100 it is cut,
    not rounded, so 89.6 against a 90 line reads 89 and 99.6 never reads as
    the limit."""
    text = f"{value:.{decimals}f}"
    ceilings: list[float] = []
    if value < 100.0:
        ceilings.append(100.0)
    if under is not None and value < under:
        ceilings.append(min(under, float(f"{under:.{decimals}f}")))
    if any(float(text) >= ceiling for ceiling in ceilings):
        scale = 10 ** decimals
        text = f"{math.floor(value * scale) / scale:.{decimals}f}"
    return text


def pct_pair(value: float, line: float) -> tuple[str, str]:
    """Both sides of a comparison at one precision: ``("86", "90")``;
    against 99.9, ``("99.5", "99.9")``."""
    decimals = pct_decimals(line)
    return pct_text(value, decimals, under=line), f"{line:.{decimals}f}"


# -- account labels ------------------------------------------------------------------


def _alias(context: object, number: object) -> str:
    names = getattr(context, "names", None) or {}
    return names.get(str(number), "") if isinstance(names, Mapping) else ""


def number_label(number: object, email: str = "", context: object = None) -> str:
    """``#2 work`` (alias from the event's context) or ``#2 maya@example.com``."""
    if number is None or str(number) == "":
        return account_label(None, email=email or "")
    return account_label(number, alias=_alias(context, number) or None, email=email or "")


def ref_label(ref: Mapping | None, context: object = None) -> str:
    """The label of an ``account_ref`` (``{"number", "email"}``)."""
    if not isinstance(ref, Mapping):
        return ""
    return number_label(ref.get("number"), ref.get("email") or "", context)


def view_label(view: object, context: object = None) -> str:
    """The label of an ``AccountView``; its own alias wins."""
    alias = getattr(view, "alias", "") or _alias(context, getattr(view, "number", None))
    return account_label(
        getattr(view, "number", None), alias=alias or None, email=getattr(view, "email", "") or ""
    )


def join_labels(labels: Sequence[str]) -> str:
    """``#2 work``, ``#2 work and #4 side``, ``#2, #3 and #4``."""
    labels = list(labels)
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


# -- times ---------------------------------------------------------------------------


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clock_at(when: object, at: object = None) -> tuple[str, str] | None:
    """``("15:32", "1h 12m")`` for an ISO instant, as seen from ``at`` (an
    ISO instant; default now): the clock in local time, with the date when it
    is not today, and the countdown in the shared grammar."""
    from ai_quota_swap.oauth import format_countdown, reset_clock_string

    target = _parse_iso(when)
    if target is None:
        return None
    now = _parse_iso(at) or datetime.now(timezone.utc)
    return reset_clock_string(target, now), format_countdown((target - now).total_seconds())


def duration_text(seconds: float) -> str:
    """``45s``, ``12m``, ``1h 5m``."""
    if seconds < 60:
        return f"{max(0, round(seconds))}s"
    return display.age_text(seconds)


# -- one account, in words ---------------------------------------------------------


def windows_text(windows: Iterable[tuple[str, float]] | Mapping[str, float]) -> str:
    """``5h 12% · 7d 21%`` (whole percentages)."""
    pairs = windows.items() if isinstance(windows, Mapping) else windows
    return " · ".join(f"{name} {pct_text(pct)}%" for name, pct in pairs)


def sentinel_words(sentinel: str | None, provider: str | None = "claude") -> str:
    """A provider state in a few words (``"usage unknown until used"``)."""
    if not sentinel:
        return ""
    words = _SENTINEL_WORDS.get(sentinel)
    if not words:
        return sentinel
    return words.get(provider or "claude") or words.get("*") or next(iter(words.values()))


def _view_binding(view: object, weekly: float | None = None) -> BindingWindow | None:
    windows = list(getattr(view, "windows", ()) or ())
    line = getattr(view, "line", None)
    if not windows or not isinstance(line, (int, float)):
        return None
    weekly_line = getattr(view, "weekly_line", None)
    return binding_window(
        ((label, pct, None) for label, pct in windows),
        line,
        weekly_line if weekly_line is not None else weekly,
    )


def account_facts(
    view: object, *, provider: str | None = "claude", relogin: bool = False
) -> tuple[str, str]:
    """``(text, level)`` for one account in the comparison: its windows
    (``5h 12% · 7d 21%``, with "over its switch line" or "limit reached"
    when that is why it was passed over), or the words for why the engine
    cannot use it. ``level`` is a :func:`display.severity` level, or
    ``"relogin"`` / ``"dim"``; the words always carry the meaning."""
    state = "needs-relogin" if relogin else getattr(view, "state", "ok")
    windows = list(getattr(view, "windows", ()) or ())
    age = getattr(view, "age_s", None)
    error = getattr(view, "error", None)
    if state == "needs-relogin":
        return display.NEEDS_RELOGIN, "relogin"
    if state == "no-login":
        return "no saved login", "relogin"
    if state == "disabled":
        return "disabled", "dim"
    if state == "api-key":
        used = getattr(view, "used", False)
        return ("API key, used as a last resort" if used else "API key, not used"), "dim"
    if state == "unavailable":
        return display.usage_unavailable(error), "dim"
    if state == "not-read":
        return display.NOT_READ_YET, "dim"
    if state == "sentinel":
        return sentinel_words(getattr(view, "sentinel", None), provider), "dim"
    if state == "stale":
        parts = [windows_text(windows)] if windows else []
        old = display.as_of(age) or "an old reading"
        if error:
            old += f" ({display.error_label(error)})"
        parts.append(old)
        return " · ".join(parts), "dim"
    # A reading the engine decides on.
    if not windows:
        used = getattr(view, "used_pct", None)
        if isinstance(used, (int, float)):
            return f"{pct_text(used)}% used", display.severity(used)
        return "no usage windows reported", "dim"
    parts = [windows_text(windows)]
    level = "ok"
    if any(pct >= 100.0 for _, pct in windows):
        parts.append("limit reached")
        level = "limit"
    else:
        binding = _view_binding(view)
        if binding is not None and binding.over:
            parts.append("over its switch line")
            level = "warn"
    if isinstance(age, (int, float)):
        parts.append(display.as_of(age))
    return " · ".join(parts), level


def account_clause(view: object, *, context: object = None, provider: str | None = "claude") -> str:
    """One account in a sentence: ``#3 night-shift usage unavailable (HTTP
    503), retrying``, ``#5 spare as of 3h 5m ago (timed out)``, ``#2 work at
    5h 93% · 7d 81% · over its switch line``."""
    label = view_label(view, context)
    state = getattr(view, "state", "ok")
    if state == "stale":
        old = display.as_of(getattr(view, "age_s", None)) or "only an old reading"
        error = getattr(view, "error", None)
        return f"{label} {old}" + (f" ({display.error_label(error)})" if error else "")
    text, _ = account_facts(view, provider=provider)
    if state == "ok" and getattr(view, "windows", ()):
        return f"{label} at {text}"
    return f"{label} {text}"


def _views_from_poll(poll: object) -> list:
    """Stand-ins for a poll that carries no context (built by hand, or by an
    engine that predates it): number, windows, error, nothing else."""
    from types import SimpleNamespace

    headroom = getattr(poll, "headroom", None) or {}
    windows = getattr(poll, "windows", None) or {}
    errors = getattr(poll, "fetch_errors", None) or {}
    views = []
    for num in headroom:
        wins = windows.get(num)
        h = headroom.get(num)
        if wins:
            views.append(SimpleNamespace(number=num, state="ok", windows=tuple(wins.items()),
                                         line=getattr(poll, "threshold", None),
                                         weekly_line=getattr(poll, "weekly_threshold", None)))
        elif h is not None:
            views.append(SimpleNamespace(number=num, state="ok", windows=(),
                                         used_pct=100.0 - h, line=None))
        elif errors.get(num):
            views.append(SimpleNamespace(number=num, state="unavailable", error=errors[num]))
        else:
            views.append(SimpleNamespace(number=num, state="not-read"))
    return views


def other_views(poll: object) -> list:
    """The accounts besides the active one, as the engine saw them on this
    poll (its context), in account order; stand-ins without a context."""
    context = getattr(poll, "context", None)
    active = getattr(poll, "active", None)
    active_num = str(active.get("number")) if isinstance(active, Mapping) else None
    views = list(getattr(context, "accounts", ()) or ())
    if not views:
        views = _views_from_poll(poll)
    return [v for v in views if str(getattr(v, "number", "")) != active_num]


# -- the active account on a poll ----------------------------------------------------


def poll_binding(poll: object) -> BindingWindow | None:
    """The active account's binding window on this poll, under the lines
    the engine used (the same rule as its trigger)."""
    active = getattr(poll, "active", None)
    if not isinstance(active, Mapping):
        return None
    windows = (getattr(poll, "windows", None) or {}).get(str(active.get("number")))
    if not windows:
        return None
    return binding_window(
        ((label, pct, None) for label, pct in windows.items()),
        poll.threshold,
        getattr(poll, "weekly_threshold", None),
    )


def active_status(poll: object, *, provider: str | None = None) -> tuple[str, str, str]:
    """``(label, rest, level)`` for the active account on a poll:
    ``("#1 personal", "at 5h 86% (switch at 90%)", "ok")``. ``level`` is a
    :func:`display.severity` level (``"warn"`` at its switch line)."""
    context = getattr(poll, "context", None)
    provider = provider or getattr(context, "provider", None) or "claude"
    active = getattr(poll, "active", None)
    if not isinstance(active, Mapping):
        return "", "", "ok"
    num = str(active.get("number"))
    label = ref_label(active, context)
    binding = poll_binding(poll)
    if binding is not None:
        value, line = pct_pair(binding.pct, binding.line)
        if binding.pct >= 100.0:
            return label, f"at {binding.label} {value}% (limit reached)", "limit"
        level = "warn" if binding.over else "ok"
        return label, f"at {binding.label} {value}% (switch at {line}%)", level
    headroom = (getattr(poll, "headroom", None) or {}).get(num)
    if headroom is not None:
        used = 100.0 - headroom
        value, line = pct_pair(used, poll.threshold)
        if used >= 100.0:
            return label, f"at {value}% (limit reached)", "limit"
        return label, f"at {value}% (switch at {line}%)", display.severity(used, poll.threshold)
    view = next(
        (v for v in getattr(context, "accounts", ()) or () if str(getattr(v, "number", "")) == num),
        None,
    )
    # No reading to quote: what the engine sees instead, short (the tick's
    # decision says what happens next, so no "retrying" here).
    if view is not None:
        state = getattr(view, "state", "")
        if state == "unavailable":
            return label, display.usage_unavailable(getattr(view, "error", None), retrying=False), "ok"
        if state == "api-key":
            return label, "is an API key", "ok"
        if state == "sentinel" and getattr(view, "sentinel", None) == "token expired":
            return label, "token expired", "ok"
        text, level = account_facts(view, provider=provider)
        return label, text, ("warn" if level == "relogin" else "ok")
    error = (getattr(poll, "fetch_errors", None) or {}).get(num)
    if error:
        return label, display.usage_unavailable(error, retrying=False), "ok"
    return label, display.NOT_READ_YET, "ok"


def active_over(poll: object) -> bool:
    """Whether the active account is at or over its switch line on a poll."""
    binding = poll_binding(poll)
    if binding is not None:
        return binding.over
    active = getattr(poll, "active", None)
    if not isinstance(active, Mapping):
        return False
    headroom = (getattr(poll, "headroom", None) or {}).get(str(active.get("number")))
    return headroom is not None and 100.0 - headroom >= poll.threshold


def poll_sentence(poll: object) -> str:
    """``PollEvent.human()``: the active account, then the others."""
    context = getattr(poll, "context", None)
    provider = getattr(context, "provider", None) or "claude"
    label, rest, _ = active_status(poll, provider=provider)
    if not label:
        return "No active account"
    shown = [str(n) for n in (getattr(poll, "headroom", None) or {})]
    others = [
        account_clause(v, context=context, provider=provider)
        for v in other_views(poll)
        if not shown or str(getattr(v, "number", "")) in shown
    ]
    tail = f"; others: {'; '.join(others)}" if others else ""
    return f"{label} {rest}{tail}"


# -- why a switch, in words ------------------------------------------------------------


def switch_reason(event: object) -> str:
    """``5h reached 91%``, ``limit reached``, ``usage unreadable``, ..."""
    trigger = getattr(event, "trigger", "")
    binding = getattr(event, "binding", None)
    if trigger == "proactive":
        if binding is not None:
            value = pct_text(binding.pct, pct_decimals(binding.line))
            return f"{binding.label} reached {value}%"
        return "switch line reached"
    if trigger == "at-limit":
        if getattr(event, "over_budget", False):
            return "monthly budget reached"
        if binding is not None and binding.pct >= 100.0:
            return f"{binding.label} limit reached"
        return "limit reached"
    if trigger == "failover":
        return "usage unreadable"
    if trigger == "consume-first":
        return "its weekly window resets sooner"
    if trigger == "priority":
        if getattr(event, "priority_up", None) is False:
            return "same priority, its weekly window resets sooner"
        return "higher priority"
    return trigger.replace("-", " ")


def switch_sentence(event: object, *, src: str | None = None, dst: str | None = None) -> str:
    """``Switched #1 personal → #2 work · 5h reached 91%``; a dry run
    ``Would switch … (dry run)``. ``src``/``dst`` replace the labels (the
    renderer passes fitted ones)."""
    context = getattr(event, "context", None)
    src = ref_label(getattr(event, "from_ref", None), context) if src is None else src
    dst = ref_label(getattr(event, "to_ref", None), context) if dst is None else dst
    dst = dst or "another account"
    verb = "Would switch" if getattr(event, "dry_run", False) else "Switched"
    move = f"{src} → {dst}" if src else f"to {dst}"
    tail = " (dry run)" if getattr(event, "dry_run", False) else ""
    return f"{verb} {move} · {switch_reason(event)}{tail}"


# -- no switch, in words ---------------------------------------------------------------


def _relogin_views(views: Iterable) -> list:
    return [v for v in views if getattr(v, "state", "") in ("needs-relogin", "no-login")]


def blocked_reasons(views: Sequence, *, context: object = None, provider: str | None = "claude") -> str:
    """Why no other account can take over, per account: ``#2 work and #4
    side need re-login; #3 night-shift usage unavailable (HTTP 503),
    retrying; #5 spare as of 3h 5m ago``."""
    relogin = [view_label(v, context) for v in views if getattr(v, "state", "") == "needs-relogin"]
    nologin = [view_label(v, context) for v in views if getattr(v, "state", "") == "no-login"]
    disabled = [view_label(v, context) for v in views if getattr(v, "state", "") == "disabled"]
    unused_keys = [
        view_label(v, context) for v in views
        if getattr(v, "state", "") == "api-key" and not getattr(v, "used", False)
    ]
    grouped = ("needs-relogin", "no-login", "disabled")
    clauses = []
    if relogin:
        clauses.append(f"{join_labels(relogin)} {'needs' if len(relogin) == 1 else 'need'} re-login")
    for view in views:
        state = getattr(view, "state", "")
        if state in grouped or (state == "api-key" and not getattr(view, "used", False)):
            continue
        clauses.append(account_clause(view, context=context, provider=provider))
    if nologin:
        clauses.append(f"{join_labels(nologin)} {'has' if len(nologin) == 1 else 'have'} no saved login")
    if disabled:
        clauses.append(f"{join_labels(disabled)} {'is' if len(disabled) == 1 else 'are'} disabled")
    if unused_keys:
        noun = "is an API key" if len(unused_keys) == 1 else "are API keys"
        clauses.append(f"{join_labels(unused_keys)} {noun}, not used")
    return "; ".join(clauses)


def blocked_advice(views: Sequence, *, context: object = None, provider: str | None = "claude") -> str:
    """The next step when no account can take over, or ``""`` when there is
    nothing to do but wait (usage that cannot be read is retried every check)."""
    provider = provider or "claude"
    if not views:
        return display.ANOTHER_ACCOUNT_STEPS.get(provider, display.ANOTHER_ACCOUNT_STEPS["claude"])
    relogin = _relogin_views(views)
    if relogin:
        who = join_labels([view_label(v, context) for v in relogin])
        it = "it" if len(relogin) == 1 else "each"
        if provider == "codex":
            return f"To use {who} again, log in to {it} again: qswap codex login"
        return f"To use {who} again, log in to {it} in Claude Code (/login), then run: qswap add"
    unread = [
        v for v in views
        if getattr(v, "state", "") == "sentinel"
        and getattr(v, "sentinel", None) == "usage unknown until used"
    ]
    if unread:
        # A saved Codex login whose usage only shows once Codex runs on it:
        # waiting never reads it, a hand switch does (with a warning).
        first = unread[0]
        switch = "qswap codex switch" if provider == "codex" else "qswap switch"
        return (
            f"To use {view_label(first, context)} anyway, switch to it: "
            f"{switch} {getattr(first, 'number', '')}"
        )
    usable = [
        v for v in views
        if getattr(v, "state", "") not in ("disabled",)
        and not (getattr(v, "state", "") == "api-key" and not getattr(v, "used", False))
    ]
    if usable:
        return ""
    disabled = [v for v in views if getattr(v, "state", "") == "disabled"]
    enable = "qswap codex enable" if provider == "codex" else "qswap enable"
    if disabled:
        first = disabled[0]
        who = view_label(first, context) if len(disabled) == 1 else "one"
        return f"To put {who} back in rotation, run: {enable} {getattr(first, 'number', '')}"
    if provider != "codex":
        return "To allow API keys as a last resort, run: qswap auto --include-api-key-accounts"
    return ""


def _blocked_views(event: object) -> list:
    context = getattr(event, "context", None)
    active = getattr(context, "active", None)
    return [
        v for v in getattr(context, "accounts", ()) or ()
        if str(getattr(v, "number", "")) != str(active)
    ]


_BLOCKED_FALLBACK = {
    "no-candidates": "no other account is in rotation",
    "no-comparison": "no other account's usage can be read",
    "no-qualifying-candidate": "none is under its switch lines with room to spare",
    "no-viable-target": "every candidate was turned down this check",
}


def _cooldown_left(event: object) -> str:
    wait = getattr(event, "wait_s", None)
    return f", {duration_text(wait)} left" if isinstance(wait, (int, float)) and wait > 0 else ""


def _failover_counts(event: object) -> tuple[int, int] | None:
    """``(done, needed)`` from an active-usage-unknown detail (``"1/3 …"``)."""
    counts = re.match(r"\s*(\d+)\s*/\s*(\d+)", getattr(event, "detail", "") or "")
    return (int(counts.group(1)), int(counts.group(2))) if counts else None


def no_switch_fragment(event: object) -> tuple[str, str]:
    """``(text, tone)`` that follows the active account on a tick line:
    ``("no switch needed", "routine")``. Short enough that the whole line
    fits 80 columns with an ordinary label; the full sentence is
    :func:`no_switch_sentence`. ``tone`` is ``"routine"``, ``"wait"``,
    ``"blocked"`` or ``"sentence"`` (the text stands alone)."""
    reason = getattr(event, "reason", "")
    context = getattr(event, "context", None)
    if reason == "below-threshold":
        return "no switch needed", "routine"
    if reason == "cooldown":
        return f"cooling down{_cooldown_left(event)}", "wait"
    if reason == "already-consuming-soonest":
        return "no sooner reset with room", "routine"
    if reason == "reset-unknown":
        return "no weekly reset known yet", "routine"
    if reason == "already-consuming-priority":
        return "already on the top priority", "routine"
    if reason in BLOCKED_REASONS:
        return "nothing to switch to", "blocked"
    if reason == "stale-usage":
        account = getattr(event, "account", None)
        who = number_label(account, "", context) if account else "a candidate"
        return f"re-checking {who}", "wait"
    if reason == "active-api-key":
        return "no quota to watch", "routine"
    if reason == "api-key-sessions":
        running = getattr(event, "sessions", None)
        if running and running > 0:
            held = f"{running} sessions running"
        elif running:
            held = "sessions could not be counted"
        else:
            held = "sessions running"
        return f"not moving to an API key, {held}", "wait"
    if reason == "active-idle":
        name = display.provider_name(getattr(context, "provider", None))
        return f"{name} is idle, resumes on next use", "routine"
    if reason == "active-usage-unknown":
        counts = _failover_counts(event)
        if counts:
            left = max(counts[1] - counts[0], 0)
            return f"failover in {left} more check{'' if left == 1 else 's'}", "wait"
        return "failover pending", "wait"
    if reason == "switch-deferred":
        return "switch deferred, retrying", "wait"
    if reason == "already-active":
        return "already active", "routine"
    return no_switch_sentence(event), "sentence"


def no_switch_sentence(event: object) -> str:
    """``NoSwitchEvent.human()``: why the engine stayed, as a sentence that
    stands alone (a notification, the dashboard's log)."""
    reason = getattr(event, "reason", "")
    detail = getattr(event, "detail", "") or ""
    context = getattr(event, "context", None)
    provider = getattr(context, "provider", None) or "claude"
    binding = getattr(event, "binding", None)
    if reason == "below-threshold":
        if binding is not None:
            value, line = pct_pair(binding.pct, binding.line)
            return f"No switch needed: {binding.label} at {value}% (switch at {line}%)"
        return f"No switch needed ({detail})" if detail else "No switch needed"
    if reason == "cooldown":
        return f"No switch yet: cooling down after the last switch{_cooldown_left(event)}"
    if reason == "already-consuming-soonest":
        return "No switch: no account whose weekly window resets sooner has room to spare"
    if reason == "reset-unknown":
        return (
            "No switch: the active account's weekly reset time is unknown; "
            "consume-first waits until it is reported"
        )
    if reason == "already-consuming-priority":
        return "No switch: no usable account has a higher priority or an earlier weekly reset"
    if reason in BLOCKED_REASONS:
        views = _blocked_views(event)
        why = blocked_reasons(views, context=context, provider=provider) if views else ""
        text = f"No account to switch to: {why or _BLOCKED_FALLBACK[reason]}"
        advice = blocked_advice(views, context=context, provider=provider) if (
            views or reason == "no-candidates"
        ) else ""
        return f"{text}. {advice}" if advice else text
    if reason == "stale-usage":
        account = getattr(event, "account", None)
        if account:
            return (
                f"No switch yet: {number_label(account, '', context)}'s usage could not be "
                "re-read this check; retrying"
            )
        return f"No switch yet: {detail}" if detail else "No switch yet: usage could not be re-read"
    if reason == "active-api-key":
        return "No switch: the active account is an API key, which has no quota to watch"
    if reason == "api-key-sessions":
        account = getattr(event, "account", None)
        who = number_label(account, "", context) if account else "an API-key account"
        return display.api_key_engine_sentence(
            who, getattr(event, "sessions", None) or 0, getattr(event, "records", None) or (),
        )
    if reason == "active-idle":
        return f"No switch: the active account's {detail}" if detail else "No switch: the active login is idle"
    if reason == "active-usage-unknown":
        counts = _failover_counts(event)
        if counts:
            done, needed = counts
            return (
                "No switch yet: the active account's usage is unreadable; failover after "
                f"{needed} unreadable checks ({done} so far)"
            )
        return "No switch yet: the active account's usage is unreadable"
    if reason == "unmanaged-active-account":
        name = display.provider_name(provider)
        add = display.ADD_COMMANDS.get(provider, "qswap add")
        return f"No switch: the live {name} login is not managed yet. Add it with: {add}"
    if reason == "no-active-account":
        name = display.provider_name(provider)
        add = display.ADD_COMMANDS.get(provider, "qswap add")
        how = "codex login" if provider == "codex" else "claude, then /login"
        return f"No active account: log in to {name} ({how}), then run: {add}"
    if reason == "switch-deferred":
        return f"Switch deferred: {detail}; trying again next check" if detail else (
            "Switch deferred; trying again next check"
        )
    if reason == "already-active":
        return "No switch: that account is already active" + (f" ({detail})" if detail else "")
    words = reason.replace("-", " ") or "no switch"
    return f"No switch: {words}" + (f" ({detail})" if detail else "")


# -- the other events --------------------------------------------------------------------


def quarantine_sentence(event: object) -> str:
    """``#4 side needs re-login: its saved login was refused. Log in to it
    in Claude Code (/login), then run: qswap add``."""
    context = getattr(event, "context", None)
    label = number_label(getattr(event, "number", None), getattr(event, "email", ""), context)
    why = display.set_aside_reason(getattr(event, "reason", None))
    step = getattr(event, "recover", "") or ""
    if not step:
        provider = getattr(context, "provider", None) or "claude"
        step = display.relogin_step(provider)
        step = step[:1].upper() + step[1:]
    return f"{label} {display.NEEDS_RELOGIN}: {why}. {step}"


def unquarantine_sentence(event: object) -> str:
    """``#4 side is back in rotation (login replaced)``."""
    context = getattr(event, "context", None)
    label = number_label(getattr(event, "number", None), getattr(event, "email", ""), context)
    reason = getattr(event, "reason", "") or ""
    words = _UNQUARANTINE_WORDS.get(reason, reason.replace("-", " "))
    return f"{label} is back in rotation" + (f" ({words})" if words else "")


def exhausted_sentence(event: object) -> str:
    """``All accounts are at their limit; the first resets 15:32 · in 1h 12m``."""
    when = clock_at(getattr(event, "earliest_reset_at", None), getattr(event, "ts", None))
    if when is None:
        return "All accounts are at their limit; no reset time is known yet"
    clock, countdown = when
    return f"All accounts are at their limit; the first resets {clock} · in {countdown}"


def sleep_sentence(event: object) -> str:
    """``Sleeping 12m, until 14:32``."""
    seconds = getattr(event, "seconds", 0.0) or 0.0
    when = clock_at(getattr(event, "until", None), getattr(event, "ts", None))
    until = f", until {when[0]}" if when else ""
    return f"Sleeping {duration_text(seconds)}{until}"


def budget_sentence(event: object) -> str:
    """``#5 ci has spent $410.00 of its $500.00 monthly budget (82%)``."""
    context = getattr(event, "context", None)
    who = ref_label(getattr(event, "account", None), context) or "An API-key account"
    estimate = " (estimate)" if getattr(event, "source", None) == "ledger" else ""
    used, limit = event.used, event.limit
    return (
        f"{who} has spent ${used:,.2f} of its ${limit:,.2f} monthly budget "
        f"({pct_text(event.pct)}%){estimate}"
    )


def _hours_label(hours: float) -> str:
    if hours >= 48:
        return f"{hours / 24:.0f}d"
    if hours >= 1:
        return f"{hours:.0f}h"
    return f"{max(hours * 60, 0):.0f}m"


def reset_why(plan: Mapping) -> str:
    """Why a usage reset is worth spending, in words: ``Fable reached 100%,
    3d before it resets on its own``."""
    from ai_quota_swap import resets

    reason = plan.get("reason")
    window = plan.get("window") or "a window"
    pct = plan.get("pct")
    reached = f"{window} reached {pct_text(pct)}%" if isinstance(pct, (int, float)) else f"{window} at its line"
    if reason == "expiring":
        ends = resets.parse_ts(plan.get("grantEndsAt"))
        by = resets.use_by(ends) if ends is not None else None
        why = f"it must be used by {by}" if by else "it expires soon"
        return f"{why} ({reached})" if isinstance(pct, (int, float)) else why
    if reason == "last-resort":
        reached += ", no account to switch to"
    hours = plan.get("hoursLeft")
    if isinstance(hours, (int, float)):
        reached += f", {_hours_label(hours)} before it resets on its own"
    return reached


def reset_sentence(event: object) -> str:
    """``Used a Fable reset on #1 personal · Fable reached 100%, 3d before
    it resets on its own · 1 left on this grant``."""
    from ai_quota_swap.resets import scope_noun

    context = getattr(event, "context", None)
    plan = getattr(event, "plan", None) or {}
    who = ref_label(getattr(event, "account", None), context) or "an account"
    what = f"a {scope_noun(plan.get('scope'), plan.get('grantKind'))}"
    why = reset_why(plan)
    if getattr(event, "dry_run", False):
        return f"Would use {what} on {who} · {why} (dry run)"
    result = getattr(event, "result", None) or {}
    if result.get("result") == "reset":
        left = result.get("resetsLeft")
        tail = f" · {left} left on this grant" if isinstance(left, int) else ""
        return f"Used {what} on {who} · {why}{tail}"
    outcome = str(result.get("result", "error")).replace("_", " ")
    detail = str(result.get("reason") or result.get("detail") or "").replace("_", " ")
    return f"Did not use {what} on {who}: {outcome}" + (
        f" ({detail})" if detail and detail != outcome else ""
    )


# -- the start banner --------------------------------------------------------------------


def banner(settings: object, *, dry_run: bool = False, provider: str = "claude") -> str:
    """The line ``qswap auto`` prints when its loop starts::

        Auto-switch running: switch at 90% · checks every 60s · Ctrl-C to stop

    Shortened to fit 80 columns when a weekly line, a strategy or Codex
    makes it long."""
    from ai_quota_swap.autoswitch import pct_label

    who = "Codex auto-switch" if provider == "codex" else "Auto-switch"
    line = f"switch at {pct_label(settings.threshold)}%"
    weekly = getattr(settings, "weekly_threshold", None)
    if weekly is not None:
        line += f" (weekly {pct_label(weekly)}%)"
    interval = float(getattr(settings, "interval_seconds", 60.0) or 60.0)
    every = f"{interval:.0f}s" if interval < 120 else duration_text(interval)
    head = f"{who} dry run: would {line}" if dry_run else f"{who} running: {line}"
    strategy = getattr(settings, "strategy", "best")
    extra = f" · {strategy}" if strategy and strategy != "best" else ""
    for text in (
        f"{head}{extra} · checks every {every} · Ctrl-C to stop",
        f"{head}{extra} · every {every} · Ctrl-C to stop",
        f"{head} · every {every} · Ctrl-C to stop",
        f"{head} · every {every}",
    ):
        if len(text) <= WIDTH:
            return text
    return display.fit(f"{head} · every {every}", WIDTH)


# -- the renderer --------------------------------------------------------------------------


def _style(name: str) -> Callable[[str], str]:
    from ai_quota_swap import printer

    return {
        "accent": printer.accent,
        "yellow": printer.yellowed,
        "red": printer.reddened,
        "dim": printer.dimmed,
        "muted": printer.muted,
    }.get(name, lambda text: text)


def _level_style(level: str) -> Callable[[str], str]:
    return {
        "limit": _style("red"),
        "warn": _style("yellow"),
        "relogin": _style("yellow"),
        "dim": _style("dim"),
    }.get(level, _style("muted"))


def _fit_labels(compose: Callable[[list[str]], str], labels: list[str], room: int, *, floor: int = 8) -> str:
    """``compose(labels)`` cut to ``room`` by shortening the longest label
    first (with an ellipsis), never below ``floor`` columns each. When even
    that cannot fit, the labels stay whole and the caller wraps the line."""
    labels = list(labels)
    text = compose(labels)
    if len(text) > room and len(compose([display.fit(lb, floor) for lb in labels])) > room:
        return text
    while len(text) > room:
        index = max(range(len(labels)), key=lambda k: len(labels[k]), default=None)
        if index is None or len(labels[index]) <= floor:
            break
        over = len(text) - room
        labels[index] = display.fit(labels[index], max(floor, len(labels[index]) - over))
        text = compose(labels)
    return text


class AutoLog:
    """``qswap auto``'s log: one line per tick, in words.

    Feed it every engine event (``engine = AutoSwitchEngine(..., log)``);
    call :meth:`flush` when the stream ends. A poll is held until the
    decision that follows it in the same tick, then both print as one line::

        14:20:00  #1 personal at 5h 86% (switch at 90%) · no switch needed
        14:22:00  Switched #1 personal → #2 work · 5h reached 91%
                    #2 work         5h 12% · 7d 21%
                    #3 night-shift  5h 93% · 7d 81% · over its switch line

    The indented comparison (one line per other account) prints on the first
    tick, on a tick where a switch is being decided (the active account is
    at or over its switch line, a switch happens, or the engine is blocked),
    and on every tick with ``verbose``. Events outside a tick (errors,
    sleeps, accounts set aside or back, usage resets, warnings, budgets)
    print as their own line. Colour comes from :mod:`ai_quota_swap.printer`
    (accent for switches and resets taken, yellow for errors, re-logins and
    warnings, dim for routine ticks) and never carries meaning alone.

    ``clock`` returns the ``HH:MM:SS`` stamp; by default each line is
    stamped with its event's own time, in local time. ``provider`` words the
    next steps (``qswap add`` or ``qswap codex login``) when an event does
    not say which provider it came from.
    """

    def __init__(
        self,
        write: Callable[[str], None] = print,
        *,
        verbose: bool = False,
        clock: Callable[[], str] | None = None,
        provider: str = "claude",
        width: int = WIDTH,
    ) -> None:
        self._write = write
        self.verbose = verbose
        self._clock = clock
        self.provider = provider
        self.width = width
        self._poll: object | None = None
        self._poll_stamp = ""
        self._ticks = 0
        # Accounts set aside while a poll waits for its decision: the
        # comparison says "needs re-login" for them, not the poll's numbers.
        self._set_aside_now: set[str] = set()

    # -- feeding ----------------------------------------------------------------------

    def __call__(self, event: object) -> None:
        kind = getattr(event, "kind", "")
        if kind == "poll":
            self.flush()
            self._poll = event
            self._poll_stamp = self._stamp(event)
            self._set_aside_now = set()
            return
        if kind in ("switch", "no-switch", "all-exhausted"):
            self._tick(event)
            return
        if kind == "error" and self._poll is not None:
            self.flush()  # the tick's status, then what went wrong
        if kind == "account-quarantined" and self._poll is not None:
            self._set_aside_now.add(str(getattr(event, "number", "")))
        self._event_line(event)

    def flush(self) -> None:
        """Print a poll still waiting for its decision (the stream ended)."""
        if self._poll is not None:
            self._tick(None)

    # -- output -----------------------------------------------------------------------

    def _stamp(self, event: object) -> str:
        if self._clock is not None:
            return self._clock()
        parsed = _parse_iso(getattr(event, "ts", None))
        if parsed is not None:
            return parsed.astimezone().strftime("%H:%M:%S")
        return time.strftime("%H:%M:%S")

    def _room(self) -> int:
        return self.width - STAMP_WIDTH

    def _emit(self, stamp: str, text: str, style: Callable[[str], str]) -> None:
        """A stamped line; text longer than the room wraps under itself."""
        room = self._room()
        dim = _style("dim")
        if len(text) <= room:
            self._write(f"{dim(stamp)}  {style(text)}")
            return
        lines = display.wrap(text, room)
        for i, line in enumerate(lines):
            line = display.fit(line, room)
            prefix = f"{dim(stamp)}  " if i == 0 else " " * STAMP_WIDTH
            self._write(f"{prefix}{style(line)}")

    def _indented(self, text: str, style: Callable[[str], str]) -> None:
        room = self.width - len(INDENT)
        for line in display.wrap(text, room):
            self._write(f"{INDENT}{style(display.fit(line, room))}")

    def _event_line(self, event: object) -> None:
        kind = getattr(event, "kind", "")
        text = event.human() if hasattr(event, "human") else str(event)
        if kind in ("error", "config-warning", "budget-warning", "account-quarantined"):
            style = _style("yellow")
        elif kind == "sleep":
            style = _style("dim")
        elif kind == "limit-reset":
            if getattr(event, "dry_run", False):
                style = _style("")
            elif getattr(event, "used", False):
                style = _style("accent")
            else:
                style = _style("yellow")
        else:
            style = _style("")
        self._emit(self._stamp(event), text, style)

    # -- a tick -------------------------------------------------------------------------

    def _tick(self, decision: object | None) -> None:
        poll = self._poll
        stamp = self._poll_stamp if poll is not None else self._stamp(decision)
        self._poll = None
        first = self._ticks == 0
        self._ticks += 1
        room = self._room()
        context = getattr(poll, "context", None) or getattr(decision, "context", None)
        provider = getattr(context, "provider", None) or self.provider

        label, rest, level = active_status(poll, provider=provider) if poll is not None else ("", "", "ok")
        kind = getattr(decision, "kind", None)
        decided = getattr(decision, "binding", None) if kind == "no-switch" else None
        if label and decided is not None:
            # The window the engine decided on, which a usage reset spent
            # between the poll and the decision can have refilled.
            value, line = pct_pair(decided.pct, decided.line)
            rest = f"at {decided.label} {value}% (switch at {line}%)"
            level = "warn" if decided.over else "ok"
        blocked = False
        if kind == "switch":
            src = ref_label(getattr(decision, "from_ref", None), getattr(decision, "context", None))
            dst = ref_label(getattr(decision, "to_ref", None), getattr(decision, "context", None)) or "another account"
            labels = [src, dst] if src else [dst]

            def compose(ls: list[str]) -> str:
                if src:
                    return switch_sentence(decision, src=ls[0], dst=ls[1])
                return switch_sentence(decision, src="", dst=ls[0])

            text = _fit_labels(compose, labels, room)
            style = _style("" if getattr(decision, "dry_run", False) else "accent")
        elif kind == "all-exhausted":
            text = exhausted_sentence(decision)
            style = _style("red")
            blocked = True
        else:
            if kind == "no-switch":
                fragment, tone = no_switch_fragment(decision)
            else:
                fragment, tone = "", "routine"
            blocked = tone == "blocked"
            if kind == "no-switch" and (tone == "sentence" or not label):
                # The first line; the lines naming unreadable session records
                # follow the tick below, unwrapped.
                text = no_switch_sentence(decision).split("\n", 1)[0]
            elif label:
                tail = f" · {fragment}" if fragment else ""
                text = _fit_labels(lambda ls: f"{ls[0]} {rest}{tail}", [label], room)
            else:
                text = "No active account"
            if blocked:
                style = _style("yellow")
            elif tone == "sentence" and getattr(decision, "reason", "") in (
                "no-active-account", "unmanaged-active-account",
            ):
                style = _style("yellow")
            elif tone == "routine" and level == "ok":
                style = _style("dim")
            elif level == "limit":
                style = _style("red")
            elif level == "warn":
                style = _style("yellow")
            else:
                style = _style("")
        self._emit(stamp, text, style)

        deciding = blocked or kind in ("switch", "all-exhausted") or (
            kind == "no-switch" and getattr(decision, "reason", "") == "active-usage-unknown"
        ) or (poll is not None and active_over(poll))
        if poll is not None and (self.verbose or first or deciding):
            self._comparison(poll, provider)
        if blocked:
            views = other_views(poll) if poll is not None else _blocked_views(decision)
            views = [self._with_set_aside(v) for v in views]
            advice = blocked_advice(views, context=context, provider=provider)
            if advice:
                self._indented(advice, _style("yellow") if _relogin_views(views) else _style("dim"))
        if kind == "switch":
            for warning in getattr(decision, "warnings", None) or ():
                self._indented(str(warning), _style("yellow"))
        if kind == "no-switch" and getattr(decision, "records", None):
            # Session records that could not be read: each path whole on its
            # own line, never cut to the width (ux.md), then what to do.
            room = self.width - len(INDENT)
            for line in display.unreadable_records_lines(decision.records, room):
                self._write(f"{INDENT}{_style('yellow')(line)}")

    def _with_set_aside(self, view: object) -> object:
        """``view`` as "needs re-login" when the engine set it aside after
        the poll it came from."""
        if str(getattr(view, "number", "")) not in self._set_aside_now:
            return view
        import dataclasses
        from types import SimpleNamespace

        if dataclasses.is_dataclass(view) and not isinstance(view, type):
            return dataclasses.replace(view, state="needs-relogin")
        return SimpleNamespace(**{**vars(view), "state": "needs-relogin"})

    def _comparison(self, poll: object, provider: str) -> None:
        """One indented line per other account: its windows, or why the
        engine cannot use it."""
        context = getattr(poll, "context", None)
        views = other_views(poll)
        if not views:
            return
        labels = [view_label(v, context) for v in views]
        column = min(max(len(label) for label in labels), LABEL_COLUMN_MAX)
        room = self.width - len(INDENT) - column - 2
        muted = _style("muted")
        for view, label in zip(views, labels):
            relogin = str(getattr(view, "number", "")) in self._set_aside_now
            text, level = account_facts(view, provider=provider, relogin=relogin)
            label = display.fit(label, column)
            self._write(
                f"{INDENT}{muted(f'{label:<{column}}')}  {_level_style(level)(display.fit(text, room))}"
            )
