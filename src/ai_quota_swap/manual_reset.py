"""Spending one usage reset (or Codex reset credit) by hand.

One sequence for every surface that spends one on request: ``qswap reset
ACCOUNT [--grant ID] [--yes]``, ``qswap codex reset …`` and the menu bar's
*Use a reset*. :func:`spend` is that sequence:

1. read the account's usage again (a reset taken in Claude Code, another
   surface or an engine meanwhile is invisible in the stored reading) and
   pick the reset from the fresh numbers (:func:`choose`);
2. ask, when the caller asks (``confirm``);
3. claim it (:func:`claim`): a pending record holding the request id first,
   reserved under the state lock as the engine reserves its own
   (``autoswitch.reserve_reset_claim``): refused while another claim on the
   account (the engine's, another window's) is in flight for a different
   grant, and reusing the stored request id when an earlier attempt on this
   grant never got an answer, so a retry cannot spend a second one. Then the
   claim, which re-reads the account's usage once the server decided, then
   the final record, which also enters a decided claim in the reset log as a
   manual use. A final record that cannot be written is logged, never allowed
   to hide the server's answer: the pending record still holds the request id.

The texts that describe a reset before it is spent (:func:`describe`) are the
command line's, so the menu bar's confirmation says what ``qswap reset``
says. Nothing here prints.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ai_quota_swap.exceptions import ValidationError
from ai_quota_swap.printer import bolded, dimmed, yellowed

_logger = logging.getLogger("ai-quota-swap")

#: Claim results that do not say whether the reset was spent (a transport
#: failure, an answer qswap cannot read): the brake counts them as spent.
UNKNOWN_RESULTS = frozenset({"error", "unavailable"})


class ResetBusy(ValidationError):
    """Another claim on the account is in flight; nothing was sent."""


class ResetChanged(ValidationError):
    """A claim was recorded for the account after the reading the choice
    rests on (the engine or another window used a reset meanwhile); nothing
    was sent."""


def outcome_kind(result) -> str:
    """What a claim's answer means for the person who asked: ``used`` (the
    reset was spent now), ``already`` (an earlier attempt went through: the
    server answers ``already_used`` to a retried request id), ``unknown`` (no
    decided answer: it may have been spent) or ``not-used`` (decided:
    nothing was spent)."""
    if result.ok:
        return "used"
    if result.result == "already_used":
        return "already"
    if result.result == "unavailable" and result.reason == "no_credit":
        return "not-used"  # Codex: decided, no credit left
    if result.result in UNKNOWN_RESULTS:
        return "unknown"
    return "not-used"


def noun_for(codex: bool) -> str:
    """What one reset is called: ``reset`` (Claude Code) or ``reset credit``."""
    return "reset credit" if codex else "reset"


def command_for(codex: bool) -> str:
    """The command that lists and spends them."""
    return "qswap codex reset" if codex else "qswap reset"


@dataclass(frozen=True)
class Choice:
    """The reset :func:`choose` picked, with the fresh reading it rests on."""

    number: str
    email: str
    name: str  # the account's short label, "#1 personal"
    grant: dict
    last_good: dict | None
    status: dict
    now: float
    threshold: float
    codex: bool = False
    #: When the reading the choice rests on was taken (None: unknown).
    fetched_at: float | None = None

    @property
    def reset_name(self) -> str:
        """``Fable reset``, ``weekly reset``; ``reset credit`` for Codex."""
        from ai_quota_swap import resets

        return "reset credit" if self.codex else resets.reset_name(self.grant)


@dataclass(frozen=True)
class Spent:
    """A claim made: what was chosen and the server's answer."""

    choice: Choice
    result: object  # resets.ClaimResult


def choose(switcher, account: str, grant_id: str | None = None, *,
           codex: bool = False, threshold: float = 95.0) -> Choice:
    """Re-read ``account``'s usage and pick the reset to spend: ``grant_id``,
    or the narrowest usable one that clears a window at its line. Raises
    :class:`ValidationError` with the command line's words when nothing can
    be spent (needs re-login, unreadable, none held, not usable now)."""
    from ai_quota_swap import display, resets

    noun, command = noun_for(codex), command_for(codex)
    num, email, _org = switcher.resolve_account(account)
    name = switcher.label(num, long=False)
    entry = switcher.refresh_account_usage(num)
    last_good = entry.last_good if entry is not None else None
    status = last_good.get("resets") if isinstance(last_good, dict) else None
    now = time.time()
    if status is None:
        why = entry.last_error if entry is not None else None
        if why == "invalid_grant" or (entry is not None and entry.sentinel == "re-login needed"):
            step = display.relogin_step("codex" if codex else "claude")
            raise ValidationError(
                f"{name} {display.NEEDS_RELOGIN}; nothing was spent.\n"
                f"{step[:1].upper()}{step[1:]}"
            )
        if why:
            raise ValidationError(
                f"{name}: its {noun}s could not be read ({display.error_label(why)}); "
                "try again later"
            )
        raise ValidationError(f"{name} reports no {noun}s; nothing to spend")
    if not status.get("eligible"):
        reason = status.get("ineligible_reason") or "unknown"
        raise ValidationError(f"{name} has no {noun}s (not eligible: {reason})")
    grants = status.get("grants") or []
    if grant_id:
        grant = next((g for g in grants if g["id"] == grant_id), None)
        if grant is None:
            raise ValidationError(f"{name} has no {noun} '{grant_id}' ({command} lists them)")
    else:
        # One that clears a window at the line first, the narrowest first:
        # with only Fable at its limit, a Fable reset before a weekly one.
        usable = resets.manual_order(status, last_good, threshold, now)
        if not usable:
            held = resets.unused_grants(status, now)
            if not held:
                raise ValidationError(f"{name} holds no {noun}s; nothing to spend")
            reasons = "; ".join(
                f"{resets.reset_name(g)}: "
                f"{display.grant_state(resets.grant_unusable_reason(g, now))}"
                for g in held
            )
            raise ValidationError(f"{name} has no {noun} it can use right now ({reasons})")
        grant = usable[0]
    unusable = resets.grant_unusable_reason(grant, now)
    if unusable:
        raise ValidationError(
            f"that {noun} cannot be used right now ({display.grant_state(unusable)})"
        )
    fetched_at = getattr(entry, "fetched_at", None)
    return Choice(num, email, name, grant, last_good, status, now, threshold, codex, fetched_at)


def claim(switcher, choice: Choice, *, started: Callable[[], None] | None = None):
    """Claim ``choice``'s reset (module docstring, step 3). ``started`` runs
    immediately before the request is sent (the switcher's ``before_send``):
    from then on the reset may have been used whatever happens. Returns the
    :class:`resets.ClaimResult`. Raises, with nothing sent, :class:`ResetBusy`
    while another claim on the account is in flight for a different grant,
    and :class:`ResetChanged` when a claim was recorded for the account after
    the reading the choice rests on."""
    from ai_quota_swap.autoswitch import (
        STATE_FILENAME,
        record_reset_claim,
        reserve_reset_claim,
        reset_claim_in_flight,
    )

    num, grant = choice.number, choice.grant
    now = time.time()
    refused: list[Exception] = []

    def admit(_state: dict, previous: object) -> bool:
        if not isinstance(previous, dict):
            return True
        if reset_claim_in_flight(previous, choice.fetched_at, now) and previous.get("grantId") != grant["id"]:
            refused.append(ResetBusy(
                f"A {noun_for(choice.codex)} on {choice.name} is already being used; "
                "try again in a moment."
            ))
            return False
        at = previous.get("at")
        if (
            isinstance(at, (int, float)) and not isinstance(at, bool)
            and isinstance(choice.fetched_at, (int, float)) and at >= choice.fetched_at
        ):
            refused.append(ResetChanged(
                f"Usage on {choice.name} changed while this was open; nothing was used."
            ))
            return False
        return True

    reserved = reserve_reset_claim(
        switcher.backup_dir / STATE_FILENAME, num, grant["id"],
        at=now, reason="manual", auto=False, host=switcher, admit=admit,
        keep_auto_on_retry=True,
    )
    if reserved is None:
        raise refused[0] if refused else ResetBusy(
            f"A {noun_for(choice.codex)} on {choice.name} is already being used; "
            "try again in a moment."
        )
    result = switcher.claim_usage_reset(
        num, grant["id"], reserved["requestId"], before_send=started,
    )
    try:
        record_reset_claim(
            switcher.backup_dir, num,
            {**reserved, "result": result.result, "settled": result.settled,
             "detail": result.reason or result.detail},
        )
    except Exception as e:  # the pending record still holds the request id
        _logger.warning("could not record usage reset claim on account %s: %r", num, e)
    return result


def spend(
    switcher,
    account: str,
    grant_id: str | None = None,
    *,
    codex: bool = False,
    settings=None,
    confirm: Callable[[Choice], bool] | None = None,
    started: Callable[[], None] | None = None,
) -> Spent | None:
    """The whole manual sequence (module docstring): ``qswap reset ACCOUNT
    --grant ID --yes`` is ``spend(switcher, ACCOUNT, ID)``. ``confirm`` is
    asked with the fresh choice before anything is claimed; False keeps the
    reset and returns None. ``settings`` is the ``resets`` section when the
    caller has it. Raises :class:`ValidationError` when nothing can be spent."""
    rs = settings if settings is not None else switcher.reset_settings()
    if not rs.enabled:
        raise ValidationError(
            "usage resets are off; turn them on with: "
            "qswap config set resets.enabled true"
        )
    choice = choose(switcher, account, grant_id, codex=codex, threshold=rs.threshold)
    if confirm is not None and not confirm(choice):
        return None
    return Spent(choice, claim(switcher, choice, started=started))


def question(choice: Choice) -> str:
    """What the command line asks before spending: ``Use this Fable reset?``."""
    return f"Use this {choice.reset_name}?"


def describe(choice: Choice, *, name: str | None = None) -> str:
    """What spending ``choice``'s reset would do (:func:`describe_reset`,
    :func:`describe_credit` for Codex)."""
    fn = describe_credit if choice.codex else describe_reset
    return fn(
        choice.number, choice.email, choice.grant, choice.last_good, choice.status,
        choice.now, threshold=choice.threshold, name=name, fetched_at=choice.fetched_at,
    )


def staleness(fetched_at: float | None, now: float) -> str | None:
    """``usage as of 12m ago; it could not be refreshed`` when the reading a
    reset is chosen on is older than the engine would spend on
    (``autoswitch.RESET_FRESH_READ_S``); None while it is fresh. A manual
    spend is not refused for it, only told."""
    from ai_quota_swap import display
    from ai_quota_swap.autoswitch import RESET_FRESH_READ_S

    if not isinstance(fetched_at, (int, float)) or now - fetched_at <= RESET_FRESH_READ_S:
        return None
    return f"usage {display.as_of(now - fetched_at)}; it could not be refreshed"


def outcome_text(spent: Spent, who: str) -> str:
    """The notification after a claim, in the auto log's words, by
    :func:`outcome_kind`: ``Used a Fable reset on #1 personal · Fable
    refilled · 2 left on this grant``; :func:`already_used_text`;
    :func:`may_have_been_used_text`; ``Did not use a Fable reset on #1
    personal: cooldown``."""
    from ai_quota_swap import resets

    result = spent.result
    what = f"a {spent.choice.reset_name}"
    kind = outcome_kind(result)
    if kind == "used":
        cleared = ", ".join(
            resets.WINDOW_LABELS.get(k) or k for k in result.cleared
        ) or ", ".join(
            resets.WINDOW_LABELS.get(k) or k for k in spent.choice.grant.get("clears") or ()
        ) or "usage"
        left = f" · {result.resets_left} left on this grant" if result.resets_left is not None else ""
        return f"Used {what} on {who} · {cleared} refilled{left}"
    if kind == "already":
        return already_used_text(what, who, codex=spent.choice.codex)
    if kind == "unknown":
        return may_have_been_used_text(
            what, who, _outcome_words(result.result, result.reason or result.detail),
            codex=spent.choice.codex,
        )
    if result.reason == "no_credit":
        return not_used_text(what, who, "no reset credit left; nothing was spent")
    return not_used_text(what, who, result.result, result.reason or result.detail)


def already_used_text(what: str, who: str, *, codex: bool = False) -> str:
    """``A Fable reset on #1 personal was already used: an earlier attempt
    went through. Run qswap reset to see where things stand.``: the server's
    ``already_used``, its answer to a retried request id."""
    return (
        f"{what[:1].upper()}{what[1:]} on {who} was already used: an earlier attempt "
        f"went through. Run {command_for(codex)} to see where things stand."
    )


def _outcome_words(outcome: str, detail: str | None = None) -> str:
    outcome = str(outcome).replace("_", " ")
    detail = str(detail or "").replace("_", " ")
    return outcome + (f" ({detail})" if detail and detail != outcome else "")


def not_used_text(what: str, who: str, outcome: str, detail: str | None = None) -> str:
    """``Did not use a Fable reset on #1 personal: refused (cooldown)``
    (:func:`autolog.reset_sentence`'s form). Only for what is known not to
    have been spent: an answer that says so, or a failure before the
    request."""
    return f"Did not use {what} on {who}: {_outcome_words(outcome, detail)}"


def may_have_been_used_text(what: str, who: str, reason: str, *, codex: bool = False) -> str:
    """``A Fable reset on #1 personal may have been used: error (network).
    Run qswap reset to see where things stand.``: for any failure once the
    request has started."""
    return (
        f"{what[:1].upper()}{what[1:]} on {who} may have been used: {reason}. "
        f"Run {command_for(codex)} to see where things stand."
    )


# -- the descriptions shown before asking ----------------------------------------


def describe_reset(
    num: str, email: str, grant: dict, usage: dict | None, status: dict, now: float,
    *, threshold: float = 95.0, name: str | None = None, fetched_at: float | None = None,
) -> str:
    """What spending ``grant`` would do, in the terms Claude Code uses, and
    what it trades when it is broader than the limit reached. ``name`` is
    the account's label (``#2 work (maya@example.com)``)."""
    from datetime import datetime, timezone

    from ai_quota_swap import resets
    from ai_quota_swap.labels import account_label
    from ai_quota_swap.oauth import reset_clock_string

    reset_name = resets.reset_name(grant)
    who = name or account_label(num, email=email)
    lines = [bolded(f"{who} · {reset_name}"), dimmed(f"  id {grant['id']}")]
    old = staleness(fetched_at, now)
    if old:
        lines.append(yellowed(f"  {old[:1].upper()}{old[1:]}"))
    if grant.get("label"):
        lines.append(dimmed(f"  {grant['label']}"))
    phrase = resets.clears_phrase(grant)
    lines.append(f"  {phrase[:1].upper()}{phrase[1:]}")
    trade = resets.trade_note(grant, usage, threshold)
    if trade:
        lines.append(yellowed(f"  {trade}"))
    refills = []
    for key in grant.get("clears") or ():
        label = resets.WINDOW_LABELS.get(key) or key
        pct = (grant.get("percent_used") or {}).get(key)
        refills.append(f"{label} (now {pct:.0f}%)" if isinstance(pct, (int, float)) else label)
    if refills:
        lines.append(f"  Refills {', '.join(refills)} to 0%")
    if grant.get("gives_back_weekly"):
        share = next(
            (
                s.get("share_of_weekly")
                for s in (usage or {}).get("scoped") or ()
                if isinstance(s, dict)
                and any(
                    (resets.WINDOW_LABELS.get(k) or "").lower() == str(s.get("name", "")).lower()
                    for k in grant.get("clears") or ()
                )
            ),
            None,
        )
        given = f" (about {share:.0f} pts)" if isinstance(share, (int, float)) else ""
        lines.append(f"  Also gives back what it used of the weekly window{given}")
    week = resets.parse_ts(status.get("weekly_resets_at"))
    if week is not None:
        try:
            clock = reset_clock_string(
                datetime.fromtimestamp(week, tz=timezone.utc),
                datetime.fromtimestamp(now, tz=timezone.utc),
            )
        except (ValueError, OverflowError, OSError):
            clock = None
        if clock:
            lines.append(f"  Your weekly reset stays {clock}")
    left = max(0, grant["resets_left"] - 1)
    end = resets.use_by(resets.parse_ts(grant.get("ends_at")), year=True)
    tail = f" · use by {end}" if end else ""
    lines.append(dimmed(f"  {left} left on this grant after{tail} · a used reset cannot be undone"))
    return "\n".join(lines)


def describe_credit(
    num: str, email: str, grant: dict, usage: dict | None, status: dict, now: float,
    *, threshold: float = 95.0, name: str | None = None, fetched_at: float | None = None,
) -> str:
    """What spending one Codex usage reset credit would do, before asking."""
    from ai_quota_swap import resets
    from ai_quota_swap.labels import account_label

    who = name or account_label(num, email=email)
    lines = [bolded(f"{who} · reset credit"), dimmed(f"  id {grant['id']}")]
    old = staleness(fetched_at, now)
    if old:
        lines.append(yellowed(f"  {old[:1].upper()}{old[1:]}"))
    if grant.get("label"):
        lines.append(dimmed(f"  {grant['label']}"))
    reset_name = resets.reset_name(grant)
    lines.append(f"  {reset_name[:1].upper()}{reset_name[1:]}: {resets.clears_phrase(grant)}")
    trade = resets.trade_note(grant, usage, threshold)
    if trade:
        lines.append(f"  {trade}")
    refills = []
    for key in grant.get("clears") or ():
        label = resets.WINDOW_LABELS.get(key) or key
        window = (usage or {}).get(key)
        pct = window.get("pct") if isinstance(window, dict) else None
        refills.append(f"{label} (now {pct:.0f}%)" if isinstance(pct, (int, float)) else label)
    if refills:
        lines.append(f"  Resets {', '.join(refills)} to 0%")
    end = resets.use_by(resets.parse_ts(grant.get("ends_at")), year=True)
    lines.append(f"  Expires {end}" if end else "  Does not expire")
    others = max(0, resets.total_left(status, now) - 1)
    lines.append(dimmed(
        f"  {others} other credit{'' if others == 1 else 's'} on this account · "
        "a spent credit cannot be undone"
    ))
    return "\n".join(lines)
