"""How an account's state reads and looks, in every surface.

docs/design/ux.md asks that ``list``, ``status``, the auto log, the dashboard
and the menu bar use the same words, the same severity and the same age
format for one state. This module holds those words and the account block
that ``list`` and ``status`` print, as small pure functions over plain data:
no I/O, no switcher, no clock unless one is passed in. Colour comes from
:mod:`ai_quota_swap.printer`, whose stylers return the text unchanged when
colour is off, so every function here can be tested on its plain output.

The pieces, outermost first:

- :func:`render_block` turns an :class:`AccountBlock` into lines;
  :func:`block_from_entry` builds one from a stored usage reading.
- :func:`header_line`, :func:`window_rows` and :func:`state_lines` are its parts.
- The phrases: :data:`NOTHING_MANAGED`, :data:`NEEDS_RELOGIN`,
  :func:`usage_unavailable`, :func:`as_of`, :func:`held_resets`,
  :func:`relogin_step`, :func:`nothing_managed_lines`.
- :func:`severity` and :func:`paint`: yellow from an account's switch line,
  red from the limit.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ai_quota_swap.labels import PROVIDER_SHORT, account_label

#: Console width every message is written for (ux.md: "Fits 80 columns").
WIDTH = 80

#: The sentence for a provider (or the whole tool) with nothing managed.
NOTHING_MANAGED = "No accounts are managed yet."
#: An account the tool will not use until its owner logs in again.
NEEDS_RELOGIN = "needs re-login"
#: A reading that could not be refreshed and no earlier one to show.
USAGE_UNAVAILABLE = "usage unavailable"
#: A managed account whose usage has not been read yet.
NOT_READ_YET = "usage not read yet"
#: An API-key account: it has no subscription quota to show.
API_KEY_NO_QUOTA = "no quota; billed per token"

#: Provider ids to the names a sentence uses.
PROVIDER_NAMES = {"claude": "Claude Code", "codex": "Codex"}
#: The command that saves the live login, per provider.
ADD_COMMANDS = {"claude": "qswap add", "codex": "qswap codex add"}
#: How to add a further account without signing the current one out.
ANOTHER_ACCOUNT_STEPS = {
    "claude": "To add another account, log in to it in Claude Code (/login), "
              "then: qswap add",
    "codex": "To add another account without signing this one out, run: "
             "qswap codex login",
}

# Fetch-error kinds (``UsageEntry.last_error``) in words. Kinds not listed
# read as themselves, with "http-NNN" spelled "HTTP NNN".
_ERROR_WORDS = {
    "timeout": "timed out",
    "network": "network error",
    "offline": "offline",
    "http-429": "rate limited, HTTP 429",
    "invalid_grant": "refresh refused",
    "refresh-failed": "token refresh failed",
    "store-unmirrored": "CLAUDE_SECURESTORAGE_CONFIG_DIR is set; unset it or "
                        "run from a normal shell",
    "invalid_client": "qswap's sign-in client was refused; not this account",
    "consume-busy": "another qswap window holds this account; retries next pass",
    "stash-unreadable": "a saved login copy is unreadable; `qswap unclaimed` "
                        "shows it",
    "setup-token": "setup-tokens cannot read usage; `qswap config set "
                   "usage.headerProbe true` reads it from a 1-token request",
}

# Why the auto-switch engine set an account aside, in words.
_SET_ASIDE_WORDS = {
    "invalid_grant": "its saved login was refused",
    "identity-conflict": "its saved login belongs to another account",
}


# -- words -----------------------------------------------------------------------


def provider_name(provider: str | None) -> str:
    """``"Claude Code"`` / ``"Codex"`` for a provider id."""
    return PROVIDER_NAMES.get(provider or "claude", provider or "Claude Code")


def error_label(kind: str | None) -> str:
    """A fetch-error kind in words: ``"http-503"`` → ``"HTTP 503"``."""
    if not kind:
        return ""
    if kind in _ERROR_WORDS:
        return _ERROR_WORDS[kind]
    if kind.startswith("http-") and kind[5:].isdigit():
        return f"HTTP {kind[5:]}"
    return kind.replace("_", " ")


def age_text(seconds: float | None) -> str:
    """An age in the countdown grammar: ``"just now"``, ``"12m"``,
    ``"3h 5m"``, ``"2d 4h"``. One format for every surface."""
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    if total < 60:
        return "just now"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def ago(seconds: float | None) -> str:
    """``"12m ago"``, ``"3h 5m ago"``, or ``"just now"``."""
    text = age_text(seconds)
    return text if text in ("", "just now") else f"{text} ago"


def as_of(seconds: float | None) -> str:
    """A stale reading's age: ``"as of 3h 5m ago"``."""
    text = ago(seconds)
    return f"as of {text}" if text else ""


def usage_unavailable(
    kind: str | None = None, *, retry_in_s: float | None = None, retrying: bool = True
) -> str:
    """``"usage unavailable (HTTP 503), retrying"``; with a known backoff
    ``"…, retrying in 4m"``. ``retrying=False`` for a state that will not
    clear by itself."""
    text = USAGE_UNAVAILABLE
    words = error_label(kind)
    if words:
        text += f" ({words})"
    if retrying:
        if retry_in_s is not None and retry_in_s >= 60:
            text += f", retrying in {age_text(retry_in_s)}"
        else:
            text += ", retrying"
    return text


def relogin_step(provider: str | None = "claude") -> str:
    """The exact way out of ``needs re-login``."""
    if provider == "codex":
        return "log in to it again: qswap codex login"
    return "log in to it in Claude Code (/login), then run: qswap add"


def set_aside_reason(reason: str | None) -> str:
    """Why the engine set an account aside, in words."""
    if not reason:
        return "auto-switch set it aside"
    return _SET_ASIDE_WORDS.get(reason, f"auto-switch set it aside ({error_label(reason)})")


def held_resets(counts: Sequence[tuple[str, int]], provider: str | None = "claude") -> str:
    """Usage resets an account holds: ``"1 Fable reset"``, ``"2 weekly
    resets"``, ``"1 Fable reset and 1 weekly reset"``; Codex counts reset
    credits (``"1 reset credit"``). ``counts`` is ``resets.counts_by_kind``."""
    if provider == "codex":
        n = sum(count for _, count in counts)
        return f"{n} reset credit{'' if n == 1 else 's'}"
    parts = [f"{n} {label} reset{'' if n == 1 else 's'}" for label, n in counts]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def grant_state(reason: str | None) -> str:
    """One wording for why a held reset cannot be used yet (``list`` and
    ``reset`` both say this); ``None`` reads "usable now"."""
    if reason is None:
        return "usable now"
    if reason == "needs a limit hit first":
        return "usable once a limit is reached"
    return reason


def nothing_managed_lines(
    provider: str = "claude",
    *,
    live_email: str | None = None,
    cli_found: bool | None = None,
    sentence: bool = True,
) -> list[str]:
    """The empty state and its next step, for one provider.

    ``live_email`` is the provider's live, unmanaged login; ``cli_found``
    whether its command is on PATH (None: not checked). ``sentence=False``
    leaves out :data:`NOTHING_MANAGED`, for a second provider under the first.
    """
    name = provider_name(provider)
    add = ADD_COMMANDS.get(provider, "qswap add")
    lines = [NOTHING_MANAGED] if sentence else []
    if live_email:
        lines.append(f"{name} is logged in as {live_email}. Add it with: {add}")
    elif cli_found is False:
        command = "codex" if provider == "codex" else "claude"
        lines.append(
            f"{name} was not found ({command} is not on PATH). Install it, "
            f"log in, then run: {add}"
        )
    elif provider == "codex":
        lines.append(f"{name} is not logged in. Run codex login, then: {add}")
    else:
        lines.append(f"{name} is not logged in. Log in with claude (/login), then run: {add}")
    return lines


# -- severity ----------------------------------------------------------------------


def severity(pct: float | None, line: float | None = None) -> str:
    """``"limit"`` at 100%, ``"warn"`` from the account's switch ``line``,
    else ``"ok"``; ``"ok"`` for an unknown reading."""
    if not isinstance(pct, (int, float)):
        return "ok"
    if pct >= 100:
        return "limit"
    if line is not None and pct >= line:
        return "warn"
    return "ok"


def paint(text: str, level: str) -> str:
    """Colour ``text`` for a :func:`severity` level (the words carry the
    meaning too; colour never does alone)."""
    from ai_quota_swap.printer import muted, reddened, yellowed

    if level == "limit":
        return reddened(text)
    if level == "warn":
        return yellowed(text)
    return muted(text)


# -- layout helpers --------------------------------------------------------------


def fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` columns, ending in an ellipsis when cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def wrap(text: str, width: int = WIDTH, indent: str = "", subsequent: str | None = None) -> list[str]:
    """``text`` wrapped to ``width`` columns; never splits a word, a quoted
    argument, or the command after "run: " (printer.wrap_message's rule)."""
    from ai_quota_swap.printer import _NBSP, _protect

    lines = textwrap.wrap(
        _protect(text),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent if subsequent is None else subsequent,
        break_on_hyphens=False,
        break_long_words=False,
    ) or [indent.rstrip()]
    return [line.replace(_NBSP, " ") for line in lines]


# -- the account block -----------------------------------------------------------


@dataclass(frozen=True)
class WindowRow:
    """One quota window (or the extra-usage, login or limit row).

    ``pct`` None is a window whose utilization was not reported. ``clock``
    and ``countdown`` say when it refills; ``note`` follows them
    (``"ahead of pace"``, ``"$18.40 of $100.00"``). ``line`` is the account's
    switch line for this window, for :func:`severity`.
    """

    label: str
    pct: float | None = None
    clock: str | None = None
    countdown: str | None = None
    note: str = ""
    line: float | None = None
    text: str | None = None  # a row with no percentage ("expires Nov 14 03:00")


@dataclass(frozen=True)
class AccountBlock:
    """Everything one account's block shows, already in words.

    ``state`` is one of ``"ok"``, ``"needs-relogin"``, ``"unavailable"``,
    ``"api-key"``, ``"not-read"`` or ``"note"`` (a provider-specific note in
    ``notes``). ``windows`` render under the header; ``notes`` are sentences
    after them; ``held`` is the usage-reset line. ``stale_s`` is the age of a
    reading old enough to say so.
    """

    number: int | str | None
    email: str = ""
    alias: str = ""
    provider: str | None = None
    show_provider: bool = False
    active: bool = False
    disabled: bool = False
    plan: str = ""
    plan_warning: bool = False
    org: str = ""
    kind: str = "oauth"
    state: str = "ok"
    windows: tuple[WindowRow, ...] = ()
    notes: tuple[str, ...] = ()
    held: str = ""
    stale_s: float | None = None
    error: str | None = None
    retry_in_s: float | None = None
    set_aside: str | None = None


def label_for(block: AccountBlock, *, long: bool = True) -> str:
    """The block's account label (``#2 work (maya@example.com)``)."""
    return account_label(
        block.number,
        alias=block.alias or None,
        email=block.email,
        provider=block.provider if block.show_provider else None,
        long=long,
    )


def header_line(block: AccountBlock, *, width: int = WIDTH, indent: str = "  ") -> str:
    """``  #1 personal (maya@example.com) · active · Max 20x · Northwind Labs``.

    The email is cut with an ellipsis when the line would not fit.
    """
    from ai_quota_swap.printer import accent, bolded, muted, yellowed

    # ux.md: the active marker is bold; accent is for actions taken (and aliases).
    facts: list[tuple[str, Callable[[str], str]]] = []
    if block.active:
        facts.append(("active", bolded))
    if block.disabled:
        facts.append(("disabled", muted))
    if block.kind == "api_key":
        facts.append(("API key", muted))
    if block.plan:
        facts.append((block.plan, yellowed if block.plan_warning else muted))
    if block.org:
        facts.append((block.org, muted))
    tail_plain = "".join(f" · {text}" for text, _ in facts)
    prefix = []
    number_len = len(f"#{block.number} ") if block.number not in (None, "") else 0
    if block.show_provider and block.provider:
        number_len += len(PROVIDER_SHORT.get(block.provider, block.provider)) + 1
    name_len = min(len(block.alias or block.email or "account"), 24)
    over = len(indent) + number_len + name_len + len(tail_plain) - width
    if over > 0 and block.org and facts and facts[-1][0] == block.org:
        # The organization gives way first, then goes altogether.
        room_for_org = len(block.org) - over
        if room_for_org >= 8:
            facts[-1] = (fit(block.org, room_for_org), facts[-1][1])
        else:
            facts.pop()
        tail_plain = "".join(f" · {text}" for text, _ in facts)
    if block.show_provider and block.provider:
        prefix.append(PROVIDER_SHORT.get(block.provider, block.provider))
    if block.number is not None and str(block.number) != "":
        prefix.append(f"#{block.number}")
    number = " ".join(prefix)
    name = block.alias or block.email or "account"
    room = width - len(indent) - len(number) - 1 - len(name) - len(tail_plain)
    email_part = ""
    if block.alias and block.email:
        # " (email)" needs 3 columns besides the address itself; below a
        # useful length it is left out (the alias still names the account).
        if room >= 3 + min(len(block.email), 8):
            email_part = f" ({fit(block.email, room - 3)})"
    elif room < 0:
        name = fit(name, len(name) + room)
    styled_name = accent(name) if block.alias else name
    head = f"{number} {styled_name}" if number else styled_name
    sep = muted(" · ")
    tail = "".join(f"{sep}{style(text)}" for text, style in facts)
    return f"{indent}{head}{muted(email_part)}{tail}"


def _pct_text(pct: float | None) -> str:
    return f"{pct:>3.0f}%" if isinstance(pct, (int, float)) else " n/a"


def window_rows(
    windows: Sequence[WindowRow], *, indent: str = "     ", stale: bool = False
) -> list[str]:
    """``├ 5h     46%  resets 16:55 · in 2h 35m`` for each window.

    Labels share one column. A window at its limit says so in words; one at
    or past its switch line is yellow, at the limit red. ``stale`` dims
    every row (the reading is old).
    """
    from ai_quota_swap.printer import dimmed, muted

    if not windows:
        return []
    # At least "extra"/"Fable" wide, so neighbouring blocks line up.
    width = max(5, *(len(w.label) for w in windows))
    out = []
    for j, w in enumerate(windows):
        branch = dimmed("└" if j == len(windows) - 1 else "├")
        label = f"{w.label:<{width}}"
        if w.text is not None:
            # "expires CLOCK" puts its clock under the windows' "resets CLOCK".
            body = f"{label}  {'':3}  {w.text}"
            out.append(f"{indent}{branch} {dimmed(body) if stale else muted(body)}")
            continue
        level = severity(w.pct, w.line)
        facts = []
        if w.clock:
            when = f"resets {w.clock}"
            if w.countdown:
                when += f" · in {w.countdown}"
            facts.append(when)
        if w.note:
            facts.append(w.note)
        if level == "limit":
            facts.append("limit reached")
        rest = " · ".join(facts)
        pct = _pct_text(w.pct)
        if stale:
            line = dimmed(f"{label}  {pct}  {rest}".rstrip())
        else:
            line = f"{muted(label)}  {paint(pct, level)}  {muted(rest)}".rstrip()
        out.append(f"{indent}{branch} {line}")
    return out


def state_lines(block: AccountBlock, *, width: int = WIDTH, indent: str = "     ") -> list[str]:
    """The sentences under the windows: the account's state, its notes and
    the held resets, wrapped to ``width``."""
    from ai_quota_swap.printer import dimmed, muted, yellowed

    out: list[str] = []
    room = width

    def add(text: str, style: Callable[[str], str]) -> None:
        for line in wrap(text, room, indent):
            out.append(style(line))

    if block.state == "needs-relogin":
        why = f" ({block.set_aside})" if block.set_aside else ""
        add(f"{NEEDS_RELOGIN}{why}: {relogin_step(block.provider)}", yellowed)
    elif block.state == "unavailable":
        retrying = block.error not in ("store-unmirrored", "invalid_client", "setup-token")
        add(usage_unavailable(block.error, retry_in_s=block.retry_in_s, retrying=retrying), dimmed)
    elif block.state == "api-key" and not block.notes:
        add(API_KEY_NO_QUOTA, dimmed)
    elif block.state == "not-read":
        add(NOT_READ_YET, dimmed)
    for note in block.notes:
        add(note, dimmed)
    if block.stale_s is not None and block.windows:
        text = as_of(block.stale_s)
        if block.error:
            text += f" · {usage_unavailable(block.error, retry_in_s=block.retry_in_s)}"
        add(text, dimmed)
    if block.held:
        add(block.held, muted)
    return out


def render_block(block: AccountBlock, *, width: int = WIDTH, indent: str = "  ") -> list[str]:
    """The account block ``list`` and ``status`` print::

          #1 personal (maya@example.com) · active · Max 20x
             ├ 5h     46%  resets 16:55 · in 2h 35m
             ├ 7d     38%  resets Nov 13 20:20 · in 3d 6h
             └ Fable  52%  resets Nov 13 20:20 · in 3d 6h
             Holds 1 Fable reset · usable once a limit is reached · use by Dec 28
    """
    body_indent = indent + "   "
    lines = [header_line(block, width=width, indent=indent)]
    if block.state == "needs-relogin":
        # The way out comes first; the numbers, if any, are what it last read.
        lines.extend(state_lines(block, width=width, indent=body_indent))
        lines.extend(window_rows(block.windows, indent=body_indent, stale=True))
        return lines
    lines.extend(window_rows(block.windows, indent=body_indent, stale=block.stale_s is not None))
    lines.extend(state_lines(block, width=width, indent=body_indent))
    return lines


# -- building a block from a reading ----------------------------------------------


@dataclass(frozen=True)
class Lines:
    """An account's switch lines: the 5h window's, and the weekly windows'
    (7d and per-model) when they have their own."""

    five_hour: float | None = None
    weekly: float | None = None

    def for_label(self, label: str) -> float | None:
        if label == "5h":
            return self.five_hour
        return self.weekly if self.weekly is not None else self.five_hour


@dataclass(frozen=True)
class Reading:
    """The parts of a stored usage entry the block needs (``UsageEntry``'s
    field names), so a caller without the store can build one too."""

    sentinel: str | None = None
    last_good: dict | None = None
    fetched_at: float | None = None
    age_s: float | None = None
    last_error: str | None = None
    backoff_until: float | None = None
    api_spend: dict | None = None
    extra: dict = field(default_factory=dict)


def _windows_from_usage(
    usage: dict,
    lines: Lines,
    *,
    fetched_at: float | None,
    reset_strings: Callable[[dict], tuple[str, str] | None],
    ahead_of_pace: Callable[[dict, float | None], bool],
    limit_label: str = "limit",
) -> list[WindowRow]:
    rows: list[WindowRow] = []
    spend = usage.get("spend")
    if isinstance(spend, dict) and spend.get("limit") is not None:
        cell = reset_strings(spend)
        rows.append(WindowRow(
            "extra", spend.get("pct"),
            clock=cell[1] if cell else None, countdown=cell[0] if cell else None,
            note=f"${spend['used']:,.2f} of ${spend['limit']:,.2f}",
        ))
    for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
        w = usage.get(key)
        if not w:
            continue
        cell = reset_strings(w)
        pct = w.get("pct") if isinstance(w.get("pct"), (int, float)) else None
        note = ""
        if label == "7d" and (pct is None or pct < 100) and ahead_of_pace(w, fetched_at):
            note = "ahead of pace"
        rows.append(WindowRow(
            label, pct, clock=cell[1] if cell else None,
            countdown=cell[0] if cell else None, note=note, line=lines.for_label(label),
        ))
    for w in usage.get("scoped") or ():
        if not isinstance(w, dict) or "name" not in w:
            continue
        cell = reset_strings(w)
        pct = w.get("pct")
        note = "ahead of pace" if (pct or 0) < 100 and ahead_of_pace(w, fetched_at) else ""
        rows.append(WindowRow(
            str(w["name"]), pct, clock=cell[1] if cell else None,
            countdown=cell[0] if cell else None, note=note, line=lines.for_label(str(w["name"])),
        ))
    rejected = usage.get("rejected")
    if isinstance(rejected, dict):
        cell = reset_strings(rejected)
        if cell:
            rows.append(WindowRow(limit_label, text=f"reached · resets {cell[1]} · in {cell[0]}"))
        else:
            rows.append(WindowRow(limit_label, text="reached"))
    return rows


def held_line(status: dict | None, now: float, provider: str | None = "claude") -> str:
    """``"Holds 1 Fable reset · usable once a limit is reached · use by Dec 28"``,
    or ``""`` when the account holds none."""
    from ai_quota_swap import resets

    grants = resets.unused_grants(status, now)
    if not grants:
        return ""
    counts = resets.counts_by_kind(status, now)
    parts = [f"Holds {held_resets(counts, provider)}"]
    reasons = [resets.grant_unusable_reason(g, now) for g in grants]
    if not any(r is None for r in reasons):
        distinct = {grant_state(r) for r in reasons}
        parts.append(distinct.pop() if len(distinct) == 1 else "not usable now")
    ends = [t for t in (resets.parse_ts(g.get("ends_at")) for g in grants) if t is not None]
    first = resets.use_by(min(ends)) if ends else None
    if first:
        parts.append(f"use by {first}")
    return " · ".join(parts)


def block_from_entry(
    number: int | str | None,
    entry: Reading | object | None,
    *,
    email: str = "",
    alias: str = "",
    provider: str = "claude",
    show_provider: bool = False,
    active: bool = False,
    disabled: bool = False,
    plan: str = "",
    plan_warning: bool = False,
    org: str = "",
    kind: str = "oauth",
    lines: Lines = Lines(),
    set_aside: str | None = None,
    sentinel_notes: dict[str, str] | None = None,
    login_row: WindowRow | None = None,
    now: float,
    stale_after_s: float,
    reset_strings: Callable[[dict], tuple[str, str] | None] | None = None,
    ahead_of_pace: Callable[[dict, float | None], bool] | None = None,
    api_lines: Callable[[dict], Iterable[str]] | None = None,
) -> AccountBlock:
    """An :class:`AccountBlock` from one account's stored reading.

    ``entry`` is a ``UsageEntry`` (or a :class:`Reading`); None reads as
    "usage not read yet". ``set_aside`` is the engine's reason for holding
    the account out (``needs re-login`` wins over any reading).
    ``sentinel_notes`` words the provider's own sentinel states;
    ``stale_after_s`` is the age past which a reading says "as of …".
    ``reset_strings`` and ``ahead_of_pace`` default to the ones ``oauth`` and
    ``pace`` provide.
    """
    from ai_quota_swap.json_output import (
        USAGE_API_KEY,
        USAGE_RELOGIN_REQUIRED,
    )

    if reset_strings is None:
        from ai_quota_swap.oauth import fresh_reset_strings as reset_strings
    if ahead_of_pace is None:
        from ai_quota_swap import pace

        def ahead_of_pace(window: dict, fetched_at: float | None) -> bool:
            result = pace.compute_pace(window, fetched_at=fetched_at)
            return bool(result and result.ahead)

    base = dict(
        number=number, email=email, alias=alias, provider=provider,
        show_provider=show_provider, active=active, disabled=disabled,
        plan=plan, plan_warning=plan_warning, org=org, kind=kind,
    )
    sentinel = getattr(entry, "sentinel", None)
    last_good = getattr(entry, "last_good", None)
    fetched_at = getattr(entry, "fetched_at", None)
    age_s = getattr(entry, "age_s", None)
    last_error = getattr(entry, "last_error", None)
    backoff_until = getattr(entry, "backoff_until", None)
    retry_in = backoff_until - now if isinstance(backoff_until, (int, float)) and backoff_until > now else None

    def windows_of(usage: dict | None) -> list[WindowRow]:
        if not isinstance(usage, dict):
            return []
        return _windows_from_usage(
            usage, lines, fetched_at=fetched_at,
            reset_strings=reset_strings, ahead_of_pace=ahead_of_pace,
        )

    held = held_line(last_good.get("resets") if isinstance(last_good, dict) else None, now, provider)
    extra_rows = [login_row] if login_row is not None else []

    if set_aside is not None or sentinel == USAGE_RELOGIN_REQUIRED:
        reason = set_aside_reason(set_aside) if set_aside is not None else None
        return AccountBlock(
            **base, state="needs-relogin", windows=tuple(windows_of(last_good)),
            set_aside=reason,
        )
    if sentinel == USAGE_API_KEY or kind == "api_key":
        spend = getattr(entry, "api_spend", None)
        notes = tuple(api_lines(spend)) if api_lines is not None and isinstance(spend, dict) else ()
        return AccountBlock(**{**base, "kind": "api_key"}, state="api-key", notes=notes)
    if sentinel is not None:
        note = (sentinel_notes or {}).get(sentinel, sentinel)
        notes = [note]
        if isinstance(last_good, dict) and fetched_at is not None:
            from ai_quota_swap import oauth

            headroom = oauth.account_headroom(last_good)
            if headroom is not None:
                notes.append(f"last seen {100 - headroom:.0f}% used · {ago(now - fetched_at)}")
        return AccountBlock(**base, state="note", notes=tuple(notes), windows=tuple(extra_rows))
    if isinstance(last_good, dict):
        rows = windows_of(last_good) + extra_rows
        stale = age_s if isinstance(age_s, (int, float)) and age_s > stale_after_s else None
        return AccountBlock(
            **base, state="ok", windows=tuple(rows), held=held, stale_s=stale,
            error=last_error if stale is not None else None,
            retry_in_s=retry_in if stale is not None else None,
        )
    if entry is None or (last_error is None and fetched_at is None):
        return AccountBlock(**base, state="not-read", windows=tuple(extra_rows))
    return AccountBlock(
        **base, state="unavailable", error=last_error, retry_in_s=retry_in,
        windows=tuple(extra_rows),
    )


def provider_heading(provider: str, *, both: bool) -> str:
    """The heading over one provider's blocks in ``list``."""
    return f"{provider_name(provider)} accounts"
