"""The menu bar's notifications: which events get one, and their words.

Pure: no ``rumps``, no I/O. :class:`Notifier` turns the hosted engines'
events (one engine per provider) into at most one :class:`Notification` per
event that matters, and remembers what it has said so that nothing is said
twice, including by the two engines about one thing (a warning about a
setting both read, the same error from both).

The sentences are the engine's own (``event.human()``, whose words live in
:mod:`ai_quota_swap.autolog`), so a notification reads like the line ``qswap
auto`` logs. When both providers are managed the account in a sentence is
named with its provider (``Claude #1 personal``), as a mixed view must
(docs/design/ux.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ai_quota_swap import display
from ai_quota_swap.labels import PROVIDER_SHORT, account_label

#: Every notification's title.
NOTIFY_TITLE = "ai-quota-swap"

# How long an identical engine error stays quiet after being surfaced. The
# engine retries every tick, so an unconditional notification is a storm; an
# unconditional drop is how a broken auto-switch looked healthy for two days.
ENGINE_ERROR_REPEAT_S = 3600.0

#: The engine holds that a notification says once per episode: it will not
#: move onto an API key because sessions could not follow (no key line) or
#: because the key line cannot be added (docs/design/api-key-sessions.md), or
#: it cannot read the settings that hold the line of a key it made active.
_KEY_HOLDS = (
    "api-key-sessions", "key-helper-unavailable", "key-no-limit", "spend-unknown",
    "key-unusable", "stuck-on-key", "settings-unreadable", "limit-latched",
    "usage-unreadable-hold",
)


@dataclass(frozen=True)
class Notification:
    """One macOS notification: ``title`` (always :data:`NOTIFY_TITLE`), an
    optional ``subtitle`` and the sentence."""

    body: str
    subtitle: str = ""
    title: str = NOTIFY_TITLE


def should_surface_engine_error(event, surfaced: dict, now: float) -> bool:
    """Whether a hosted engine's error event should reach the log and the user.

    ``surfaced`` maps each message to when it was last surfaced (monotonic
    seconds) and is updated when this returns True. A message not seen within
    ``ENGINE_ERROR_REPEAT_S`` is surfaced; otherwise it stays quiet — per
    message, so two errors alternating tick by tick can't defeat the limit.
    """
    message = getattr(event, "message", None)
    if not message:
        return False
    # Digits stripped so a message carrying a changing id, count or time is
    # still one error, and stale entries pruned so the table stays bounded.
    key = re.sub(r"\d+", "#", message)
    for k in [k for k, t in surfaced.items() if now - t >= ENGINE_ERROR_REPEAT_S]:
        del surfaced[k]
    if key in surfaced:
        return False
    surfaced[key] = now
    return True


def engine_name(provider: str, *, both: bool) -> str:
    """``Auto-switch``; ``Codex auto-switch`` when both providers are managed."""
    if not both:
        return "Auto-switch"
    return f"{display.provider_name(provider)} auto-switch"


def _prefix(text: str, label: str, provider: str, both: bool) -> str:
    """``text`` with its first mention of ``label`` provider-prefixed."""
    if not both or not label or label not in text:
        return text
    return text.replace(label, f"{PROVIDER_SHORT.get(provider, provider)} {label}", 1)


def _ref_label(ref: object, context: object) -> str:
    from ai_quota_swap.autolog import ref_label

    return ref_label(ref if isinstance(ref, Mapping) else None, context)


def switch_text(provider: str, event, *, both: bool) -> str:
    """``Switched Claude #1 personal → #2 work · 5h reached 91%``."""
    from ai_quota_swap.autolog import switch_sentence

    context = getattr(event, "context", None)
    src = _ref_label(getattr(event, "from_ref", None), context)
    dst = _ref_label(getattr(event, "to_ref", None), context)
    short = PROVIDER_SHORT.get(provider, provider)
    if both:
        if src:
            src = f"{short} {src}"
        elif dst:
            dst = f"{short} {dst}"
    return switch_sentence(event, src=src, dst=dst or None)


def exhausted_text(provider: str, event, *, both: bool) -> str:
    """``All accounts are at their limit; the first resets 15:32 · in 1h
    12m``; ``All Codex accounts …`` when both providers are managed."""
    text = event.human()
    if both:
        text = text.replace("All accounts", f"All {display.provider_name(provider)} accounts", 1)
    return text


def manual_switch_text(
    provider: str,
    result: Mapping | None,
    *,
    both: bool,
    label: Callable[[Mapping | None], str],
    followup: str = "",
    warnings: tuple[str, ...] = (),
) -> str | None:
    """After a switch from the menu: ``Switched #1 personal → #2 work``, then
    when it takes effect and any warning. None when nothing changed."""
    if not isinstance(result, Mapping) or not result.get("switched", True):
        return None
    src, dst = label(result.get("from")), label(result.get("to"))
    short = PROVIDER_SHORT.get(provider, provider)
    if both:
        if src:
            src = f"{short} {src}"
        else:
            dst = f"{short} {dst}"
    move = f"{src} → {dst}" if src else f"to {dst}"
    lines = [f"Switched {move}."]
    if followup:
        lines.append(followup)
    lines.extend(w for w in warnings if w)
    lines.extend(w for w in (result.get("warnings") or ()) if isinstance(w, str) and w)
    return " ".join(lines)


class Notifier:
    """Engine events in, notifications out, each said once.

    - a switch: every time (a switch also ends that provider's "all at their
      limit" episode);
    - a usage reset used: every time, by kind; one that was not used: once
      per provider, account and result;
    - an account that needs re-login: once per provider, account and reason,
      with the next step;
    - every account at its limit: once per provider and first reset time;
    - an API-key budget warning: once per provider, account and month;
    - a configuration warning: once per message, whichever engine said it;
    - an engine error: once per message an hour, whichever engine said it;
    - not moving onto an API-key account because Claude Code sessions are
      running: once per episode (until a switch or another decision);
    - an engine that stopped or did not start: every time.

    Dry-run events never notify; anything else (polls, sleeps, other
    no-switch decisions) is the log's.
    """

    def __init__(self) -> None:
        self._seen: set[tuple] = set()
        self._errors: dict[str, float] = {}

    def _once(self, key: tuple) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def forget(self, provider: str, kind: str) -> None:
        """Let an episode be said again (``exhausted`` after a switch)."""
        self._seen = {k for k in self._seen if not (k[0] == kind and k[1] == provider)}

    def event(self, provider: str, event, *, both: bool, now: float) -> Notification | None:
        """The notification for one engine event, or None. ``now`` is
        monotonic seconds (the error repeat window)."""
        kind = getattr(event, "kind", "")
        if getattr(event, "dry_run", False):
            return None
        context = getattr(event, "context", None)
        if kind == "switch":
            self.forget(provider, "exhausted")
            for held in _KEY_HOLDS:
                self.forget(provider, held)
            return Notification(switch_text(provider, event, both=both))
        if kind == "no-switch":
            reason = getattr(event, "reason", "")
            if reason not in _KEY_HOLDS:
                # The engine decided something else: that episode is over.
                for held in _KEY_HOLDS:
                    self.forget(provider, held)
                return None
            from ai_quota_swap.autolog import number_label

            number = getattr(event, "account", None)
            if not self._once((reason, provider, str(number))):
                return None
            label = number_label(number, "", context) if number else ""
            return Notification(_prefix(event.human(), label, provider, both))
        if kind == "limit-reset":
            label = _ref_label(getattr(event, "account", None), context)
            text = _prefix(event.human(), label, provider, both)
            if getattr(event, "used", False):
                return Notification(text)
            # A refusal or failure retries by itself (10-60 min apart): say
            # each kind once per run, not every retry.
            account = getattr(event, "account", None) or {}
            result = getattr(event, "result", None) or {}
            key = ("reset-failed", provider, str(account.get("number")), str(result.get("result")))
            return Notification(text) if self._once(key) else None
        if kind == "account-quarantined":
            from ai_quota_swap.autolog import number_label

            number = getattr(event, "number", None)
            key = ("relogin", provider, str(number), str(getattr(event, "reason", "")))
            if not self._once(key):
                return None
            label = number_label(number, getattr(event, "email", ""), context)
            return Notification(_prefix(event.human(), label, provider, both))
        if kind == "all-exhausted":
            key = ("exhausted", provider, str(getattr(event, "earliest_reset_at", None)))
            return Notification(exhausted_text(provider, event, both=both)) if self._once(key) else None
        if kind == "budget-warning":
            account = getattr(event, "account", None) or {}
            key = ("budget", provider, str(account.get("number")), str(getattr(event, "month", "")))
            if not self._once(key):
                return None
            label = _ref_label(account, context)
            return Notification(_prefix(event.human(), label, provider, both))
        if kind == "key-unwatched":
            # An engine stopped while sessions are on a key it entered.
            return Notification(_prefix(event.human(), "", provider, both))
        if kind == "config-warning":
            # Both engines read the same autoswitch.* settings and may warn
            # about one value: say it once.
            key = ("config", str(getattr(event, "message", "")))
            return Notification(event.human()) if self._once(key) else None
        if kind == "error":
            if not should_surface_engine_error(event, self._errors, now):
                return None
            message = str(getattr(event, "message", "")).rstrip(".")
            tail = " It retries by itself." if getattr(event, "transient", True) else ""
            return Notification(f"{engine_name(provider, both=both)} error: {message}.{tail}")
        return None

    def engine_stopped(self, provider: str, reason: str, *, both: bool) -> Notification:
        """An engine whose thread ended without the menu bar stopping it."""
        return Notification(
            f"{engine_name(provider, both=both)} stopped: {reason}. Open the menu to restart it."
        )

    def engine_failed(self, provider: str, reason: str, *, both: bool) -> Notification:
        """An engine that could not start."""
        return Notification(f"{engine_name(provider, both=both)} did not start: {reason}")


def ref_labeler(accounts: Mapping[str, Mapping[str, str]]) -> Callable[[Mapping | None], str]:
    """A labeller for switch results' ``{number, email}`` refs, with the
    aliases the menu knows (``accounts``: number → ``{"alias", "email"}``)."""

    def label(ref: Mapping | None) -> str:
        if not isinstance(ref, Mapping) or ref.get("number") is None:
            email = (ref or {}).get("email") if isinstance(ref, Mapping) else ""
            return account_label(None, email=email or "")
        number = str(ref.get("number"))
        known = accounts.get(number) or {}
        return account_label(
            number, alias=known.get("alias") or None, email=ref.get("email") or known.get("email", ""),
        )

    return label
