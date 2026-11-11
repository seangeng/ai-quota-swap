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
- :func:`header_line`, :func:`window_rows` and :func:`state_lines` are its parts;
  :func:`header_parts`, :func:`window_suffix` and :func:`state_parts` give the
  same words unstyled, for a surface that draws them its own way (the dashboard).
- The phrases: :data:`NOTHING_MANAGED`, :data:`NEEDS_RELOGIN`,
  :data:`API_KEY_NO_USAGE`, :func:`usage_unavailable`, :func:`as_of`,
  :func:`held_resets` (and :func:`held_compact` for a compact row),
  :func:`api_key_summary`, :func:`reset_mode_line`, :func:`relogin_step`,
  :func:`nothing_managed_lines`.
- :func:`severity` and :func:`paint`: yellow from an account's switch line,
  red from the limit.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ai_quota_swap.labels import PROVIDER_SHORT, account_label, is_placeholder, shown_email

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
#: An API-key account with nothing spent in the figure qswap has: no Admin API
#: figure, and this machine's sessions (or the lack of any scan yet) show no
#: usage. The block's words, and a one-line row's (``api_key_summary``).
API_KEY_NO_USAGE = "no usage recorded on this machine"
API_KEY_NO_USAGE_SHORT = "no usage recorded"
#: An API-key account whose ledger exists but could not be read: a one-line
#: row says this much; the block adds why and the next step.
API_KEY_SPEND_UNKNOWN = "spend unknown"

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


def held_compact(counts: Sequence[tuple[str, int]], provider: str | None = "claude") -> str:
    """Held usage resets as one fact of a compact row (the menu bar's account
    rows): ``1 Fable reset``, ``2 weekly resets``, ``1 reset credit`` for one
    kind (:func:`held_resets`); several kinds in one phrase, ``resets: 3
    Fable, 1 weekly``, so the counts never read as separate facts."""
    if not counts:
        return ""
    if provider == "codex" or len(counts) == 1:
        return held_resets(counts, provider)
    return "resets: " + ", ".join(f"{n} {label}" for label, n in counts)


def api_key_summary(block: "AccountBlock") -> str:
    """An API-key account's state in a one-line row (the menu bar, the
    dashboard), after its ``API key`` fact: the block's first line, except
    that nothing recorded is said in two words (``no usage recorded``), a
    budget with nothing spent as ``$0 of $500 this month``, and a ledger that
    could not be read as ``spend unknown``."""
    note = block.notes[0] if block.notes else API_KEY_NO_USAGE
    if note == API_KEY_NO_USAGE:
        return API_KEY_NO_USAGE_SHORT
    if note.startswith(f"{API_KEY_SPEND_UNKNOWN}: "):
        return API_KEY_SPEND_UNKNOWN  # the state; the block says why
    return note.removesuffix(f" · {API_KEY_NO_USAGE}")


def reset_mode_line(mode: str, threshold: float, command: str = "qswap reset") -> str:
    """``resets.auto`` in words, as ``qswap reset``'s ``Auto:`` line says it:
    ``on-limit · when any account reaches 95%``. ``command`` is the one that
    spends a reset by hand (``qswap codex reset`` for Codex)."""
    return {
        "off": f"off · only {command} N spends one",
        "active": f"active · when the account in use reaches {threshold:g}% on a window "
                  "its reset clears",
        "last-resort": f"last-resort · when the active account reaches {threshold:g}% "
                       "and no switch helps",
        "on-limit": f"on-limit · when any account reaches {threshold:g}%",
    }.get(mode, mode)


#: What ``qswap remove`` / ``qswap codex remove`` ask; ``{name}`` is the
#: account's long label. Every surface that removes an account asks this.
_REMOVE_QUESTIONS = {
    "claude": "Remove {name} and delete its saved login?",
    "codex": "Stop managing {name} and delete qswap's saved copy of its login?",
}


def remove_question(provider: str | None, name: str) -> str:
    """The question before an account is removed: ``Remove #2 work
    (maya@example.com) and delete its saved login?``."""
    template = _REMOVE_QUESTIONS.get(provider or "claude", _REMOVE_QUESTIONS["claude"])
    return template.format(name=name)


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
        lines.append(f"{name} was not found on PATH. Install it and log in, then run: {add}")
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
    #: The subscription whose login is live under an active key (docs/design/
    #: api-key-sessions.md): shown ``underneath``, never ``active``.
    underneath: bool = False
    #: On an active key: the account underneath (``#1 personal``).
    over: str = ""
    #: On an active key: auto-switch entered it (``by auto-switch``).
    by_engine: bool = False


def label_for(block: AccountBlock, *, long: bool = True) -> str:
    """The block's account label (``#2 work (maya@example.com)``)."""
    return account_label(
        block.number,
        alias=block.alias or None,
        email=block.email,
        provider=block.provider if block.show_provider else None,
        long=long,
    )


@dataclass(frozen=True)
class HeaderParts:
    """A header line's pieces, already cut to fit, before any styling.

    ``number`` is ``#2`` (``Claude #2`` with the provider shown); ``name`` is
    the alias or the email; ``email`` is the address shown after an alias,
    ``""`` when there is none or no room. ``facts`` are ``(text, role)``
    in display order, roles ``active``, ``disabled``, ``api-key``, ``plan``,
    ``plan-lapsed`` and ``org``.
    """

    number: str
    name: str
    alias: bool
    email: str
    facts: tuple[tuple[str, str], ...]

    def plain(self, indent: str = "") -> str:
        head = f"{self.number} {self.name}" if self.number else self.name
        email = f" ({self.email})" if self.email else ""
        return indent + head + email + "".join(f" · {text}" for text, _ in self.facts)


def header_parts(block: AccountBlock, *, width: int = WIDTH, indent: str = "  ") -> HeaderParts:
    """The pieces of :func:`header_line`: the email is cut with an ellipsis
    when the line would not fit, and the organization gives way before it."""
    facts: list[tuple[str, str]] = []
    if block.active:
        facts.append(("active", "active"))
    elif block.underneath:
        facts.append((UNDERNEATH, "underneath"))
    if block.disabled:
        facts.append(("disabled", "disabled"))
    if block.kind == "api_key":
        facts.append(("API key", "api-key"))
    if block.over:
        facts.append((over_fact(block.over), "over"))
    if block.by_engine:
        facts.append((BY_ENGINE, "over"))
    if block.plan:
        facts.append((block.plan, "plan-lapsed" if block.plan_warning else "plan"))
    if block.org:
        facts.append((block.org, "org"))
    tail_plain = "".join(f" · {text}" for text, _ in facts)
    prefix = []
    number_len = len(f"#{block.number} ") if block.number not in (None, "") else 0
    if block.show_provider and block.provider:
        number_len += len(PROVIDER_SHORT.get(block.provider, block.provider)) + 1
    # A placeholder (api-key-5@token.local) is a name: shown as "api-key-5",
    # never as an address in parentheses (labels.shown_email).
    shown = shown_email(block.email)
    address = "" if is_placeholder(block.email) else block.email
    name_len = min(len(block.alias or shown or "account"), 24)
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
    name = block.alias or shown or "account"
    room = width - len(indent) - len(number) - 1 - len(name) - len(tail_plain)
    email = ""
    if block.alias and address:
        # " (email)" needs 3 columns besides the address itself; below a
        # useful length it is left out (the alias still names the account).
        if room >= 3 + min(len(address), 8):
            email = fit(address, room - 3)
    elif room < 0:
        name = fit(name, len(name) + room)
    return HeaderParts(number, name, bool(block.alias), email, tuple(facts))


def header_line(block: AccountBlock, *, width: int = WIDTH, indent: str = "  ") -> str:
    """``  #1 personal (maya@example.com) · active · Max 20x · Northwind Labs``.

    The email is cut with an ellipsis when the line would not fit.
    """
    from ai_quota_swap.printer import accent, bolded, muted, yellowed

    # ux.md: the active marker is bold; accent is for actions taken (and aliases).
    styles: dict[str, Callable[[str], str]] = {
        "active": bolded, "disabled": muted, "api-key": muted,
        "plan": muted, "plan-lapsed": yellowed, "org": muted,
        "underneath": muted, "over": muted,
    }
    parts = header_parts(block, width=width, indent=indent)
    styled_name = accent(parts.name) if parts.alias else parts.name
    head = f"{parts.number} {styled_name}" if parts.number else styled_name
    email_part = f" ({parts.email})" if parts.email else ""
    sep = muted(" · ")
    tail = "".join(f"{sep}{styles[role](text)}" for text, role in parts.facts)
    return f"{indent}{head}{muted(email_part)}{tail}"


def _pct_text(pct: float | None) -> str:
    return f"{pct:>3.0f}%" if isinstance(pct, (int, float)) else " n/a"


def window_suffix(w: WindowRow, *, countdown: bool = True) -> str:
    """What follows a window's percentage: ``resets 16:55 · in 2h 35m``, its
    note, and ``limit reached`` at the limit. ``countdown=False`` leaves the
    ``in …`` part out (a narrow row keeps the clock)."""
    facts = []
    if w.clock:
        when = f"resets {w.clock}"
        if w.countdown and countdown:
            when += f" · in {w.countdown}"
        facts.append(when)
    if w.note:
        facts.append(w.note)
    if severity(w.pct, w.line) == "limit":
        facts.append("limit reached")
    return " · ".join(facts)


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
        rest = window_suffix(w)
        pct = _pct_text(w.pct)
        if stale:
            line = dimmed(f"{label}  {pct}  {rest}".rstrip())
        else:
            line = f"{muted(label)}  {paint(pct, level)}  {muted(rest)}".rstrip()
        out.append(f"{indent}{branch} {line}")
    return out


def state_parts(block: AccountBlock) -> list[tuple[str, str]]:
    """The sentences :func:`state_lines` prints, unwrapped and unstyled, as
    ``(text, role)``: ``warn`` (needs re-login), ``dim`` (a state, a note,
    the age of a stale reading) or ``muted`` (the held resets)."""
    out: list[tuple[str, str]] = []
    if block.state == "needs-relogin":
        why = f" ({block.set_aside})" if block.set_aside else ""
        out.append((f"{NEEDS_RELOGIN}{why}: {relogin_step(block.provider)}", "warn"))
    elif block.state == "unavailable":
        retrying = block.error not in ("store-unmirrored", "invalid_client", "setup-token")
        out.append((
            usage_unavailable(block.error, retry_in_s=block.retry_in_s, retrying=retrying), "dim",
        ))
    elif block.state == "api-key" and not block.notes:
        out.append((API_KEY_NO_USAGE, "dim"))
    elif block.state == "not-read":
        out.append((NOT_READ_YET, "dim"))
    for note in block.notes:
        out.append((note, "dim"))
    if block.stale_s is not None and block.windows:
        text = as_of(block.stale_s)
        if block.error:
            text += f" · {usage_unavailable(block.error, retry_in_s=block.retry_in_s)}"
        out.append((text, "dim"))
    if block.held:
        out.append((block.held, "muted"))
    return out


def state_lines(block: AccountBlock, *, width: int = WIDTH, indent: str = "     ") -> list[str]:
    """The sentences under the windows: the account's state, its notes and
    the held resets, wrapped to ``width``."""
    from ai_quota_swap.printer import dimmed, muted, yellowed

    styles: dict[str, Callable[[str], str]] = {"warn": yellowed, "dim": dimmed, "muted": muted}
    out: list[str] = []
    for text, role in state_parts(block):
        for line in wrap(text, width, indent):
            out.append(styles[role](line))
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
    underneath: bool = False,
    over: str = "",
    by_engine: bool = False,
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
        underneath=underneath and not active, over=over if active else "",
        by_engine=by_engine and active,
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


# -- API-key accounts and running sessions (docs/design/api-key-sessions.md) -------
#
# A running Claude Code session never picks up an API key: it looks the saved
# key up once, when it starts. Once the switch takes the login out of the
# login item, every session that started on a subscription answers "Not
# logged in" within about 30 seconds. These are the words for that, in every
# surface.

#: The flag that switches to an API-key account while sessions are running.
LOG_OUT_SESSIONS_FLAG = "--log-out-sessions"


#: A session count for "qswap could not tell": the sessions directory or
#: every live record in it could not be read. Never "nobody there".
SESSIONS_UNKNOWN = -1


def blocking_sessions(sessions: int, unreadable: int) -> int:
    """What bars a switch to an API-key account: the readable live sessions,
    else :data:`SESSIONS_UNKNOWN` when records could not be read, else 0."""
    if sessions:
        return sessions
    return SESSIONS_UNKNOWN if unreadable else 0


def _sessions(n: int) -> str:
    return f"{n} Claude Code session{'' if n == 1 else 's'}"


def unreadable_records_lines(records: Sequence[str], width: int = WIDTH) -> list[str]:
    """The session records qswap could not read, each path on a line of its
    own (never wrapped), and what to do about them, wrapped to ``width``;
    ``[]`` when there are none. A record is named after its process id, so
    one whose process is gone can be deleted. A path that is not a record is
    the sessions directory, which could not be listed."""
    files = [str(r) for r in records if str(r).lower().endswith(".json")]
    folders = [str(r) for r in records if not str(r).lower().endswith(".json")]
    lines: list[str] = []
    if files:
        lines.append(
            "This session record could not be read:" if len(files) == 1
            else "These session records could not be read:"
        )
        lines.extend(f"  {path}" for path in files)
        stem = files[0].replace("\\", "/").rsplit("/", 1)[-1][: -len(".json")]
        if len(files) == 1 and stem.isascii() and stem.isdigit():
            lines.append(f"If Claude Code process {stem} is gone, delete the file.")
        else:
            lines.extend(wrap(
                "Each is named after its Claude Code process. If that process is "
                "gone, delete the file.", width,
            ))
    if folders:
        lines.append("The session records folder could not be read:")
        lines.extend(f"  {path}" for path in folders)
        lines.append("Check that you can open it, then try again.")
    return lines


def api_key_refusal(
    target: str, number: str | int, sessions: int, *, from_key: str | None = None,
    prefix: str = "Error: ", records: Sequence[str] = (),
) -> str:
    """Why a switch to the API-key account ``target`` was refused while
    ``sessions`` Claude Code sessions run on the shared login, and the command
    that goes ahead. ``sessions`` is :data:`SESSIONS_UNKNOWN` (or 0) when the
    session records could not be read. ``from_key`` names the active account
    when it is an API key too. ``records`` are the session records qswap
    could not read (:func:`unreadable_records_lines`). Wrapped to 80 columns with ``prefix`` (how the command line
    starts the first line) counted in; the text itself excludes it."""
    are = "is" if sessions == 1 else "are"
    head = (
        f"not switched. {target} is an API-key account, and {_sessions(sessions)} "
        f"{are} running on the shared login. "
    )
    if sessions <= 0:
        head = (
            f"not switched. {target} is an API-key account, and qswap could not "
            "read Claude Code's session records, so it cannot tell whether "
            "sessions are running on the shared login. "
        )
    if from_key:
        body = (
            "A running session cannot move to another key: the ones started "
            f"while {from_key} was active keep using its key until you restart them."
        )
    else:
        each = "it stops" if sessions == 1 else "each one stops"
        body = (
            "A running session cannot move to an API key: about 30 seconds after "
            f'the switch {each} with "Not logged in" until you switch back or '
            "restart it."
        )
    # Two columns of margin: the refusal is also shown in dialogs.
    lines = wrap(prefix + head + body, WIDTH - 2)
    lines[0] = lines[0][len(prefix):]
    return "\n".join([
        *lines,
        _STARTED_AFTER,
        *unreadable_records_lines(records, WIDTH - 2),
        "To switch anyway:",
        f"  qswap switch {number} {LOG_OUT_SESSIONS_FLAG}",
    ])


_STARTED_AFTER = "Sessions started after the switch use the key."

#: The button (menu bar, dashboard) that goes ahead with such a switch.
API_KEY_SWITCH_BUTTON = "Switch and log out sessions"


def api_key_question(refusal: str) -> str:
    """:func:`api_key_refusal` as the body of a question whose button goes
    ahead: the explanation, without "not switched." and without the
    ``--log-out-sessions`` lines the button stands for; one paragraph, then
    the unreadable session records, if any, each path on its own line."""
    lines = refusal.splitlines()
    end = lines.index(_STARTED_AFTER) + 1 if _STARTED_AFTER in lines else len(lines)
    kept = [
        line.strip() for line in lines[:end]
        if line.strip() and not line.startswith("To switch anyway")
        and not line.strip().startswith("qswap switch")
    ]
    text = " ".join(kept).replace("not switched. ", "", 1)
    records = []
    for line in lines[end:]:
        if line.startswith("To switch anyway"):
            break
        records.append(line)
    if not text.startswith("qswap "):  # a command's name keeps its case
        text = text[:1].upper() + text[1:]
    return "\n".join([text, *records])


def api_key_question_title(sessions: int, *, from_key: bool = False) -> str:
    """``Log out 6 running sessions?``; a key-to-key switch logs no one out
    (``Switch keys with 6 sessions running?``), and a count qswap could not
    read asks without one."""
    if sessions <= 0:
        return "Could not count the running sessions"
    plural = "" if sessions == 1 else "s"
    if from_key:
        return f"Switch keys with {sessions} session{plural} running?"
    return f"Log out {sessions} running session{plural}?"


#: The button on a key-to-key question: that switch logs no one out.
API_KEY_TO_KEY_BUTTON = "Switch anyway"


def api_key_question_button(*, from_key: bool = False) -> str:
    """The button that goes ahead: :data:`API_KEY_SWITCH_BUTTON`, or
    :data:`API_KEY_TO_KEY_BUTTON` when the active account is a key too."""
    return API_KEY_TO_KEY_BUTTON if from_key else API_KEY_SWITCH_BUTTON


def api_key_skip_reason(sessions: int) -> str:
    """Why a rotation passed over an API-key account (``Skipped #5 ci: …``)."""
    if sessions <= 0:
        return "an API-key account, and running sessions could not be counted"
    are = "is" if sessions == 1 else "are"
    return f"an API-key account, and {_sessions(sessions)} {are} running"


def api_key_skip_detail(sessions: int) -> str:
    """The same in a JSON rotation warning: ``(API-key account, 6 sessions
    running)``."""
    if sessions <= 0:
        return "(API-key account, running sessions could not be counted)"
    return f"(API-key account, {sessions} sessions running)"


def api_key_engine_sentence(target: str, sessions: int, records: Sequence[str] = ()) -> str:
    """Why the auto-switch engine did not move onto the API-key account
    ``target``; then, on lines of their own, the session records that could
    not be read and what to do (:func:`unreadable_records_lines`)."""
    if sessions <= 0:
        text = (
            f"Not switching to {target}: it is an API-key account and running "
            "sessions could not be counted"
        )
    else:
        noun = "running session" if sessions == 1 else "running sessions"
        text = (
            f"Not switching to {target}: it is an API-key account and {sessions} "
            f"{noun} could not follow"
        )
    return "\n".join([text, *unreadable_records_lines(records)])


def session_place(pid: int, cwd: str) -> str:
    """One session in a list: ``pid 4242 · app`` (the directory's name)."""
    name = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    return f"pid {pid} · {name}" if name else f"pid {pid}"


def _kept_key_lines(key: str, its: str, sessions: Sequence[tuple[int, str]]) -> list[str]:
    """The sessions started while the API-key account ``key`` was active,
    which keep ``its`` key until they restart."""
    if not sessions:
        return []
    return [
        *wrap(
            f"These sessions were started while {key} was active and keep using "
            f"{its} until you restart them:"
        ),
        *(f"  {session_place(pid, cwd)}" for pid, cwd in sessions),
    ]


def api_key_followup(
    *,
    target: str,
    target_is_key: bool,
    source: str | None,
    source_is_key: bool,
    running: int,
    started_on_key: Sequence[tuple[int, str]] = (),
    kept: Sequence[tuple[str, Sequence[tuple[int, str]]]] = (),
    reactivated: bool = False,
) -> list[str]:
    """What follows "Switched …" when either side is an API-key account; ``[]``
    otherwise (the usual follow-up applies).

    ``running`` is how many Claude Code sessions ran on the shared login at the
    switch; ``started_on_key`` the ``(pid, cwd)`` of those started while the
    API-key account being left was active. ``kept`` lists, per API-key
    account (its label), the sessions started while it was active, which keep
    its key; a key-to-key switch lists them all (``started_on_key`` under
    ``source`` when ``kept`` is empty). ``reactivated``: ``target`` was
    already active and now has a different saved key, which running sessions
    do not pick up."""
    if target_is_key:
        lines: list[str] = []
        if reactivated:
            for key, sessions in kept:
                its = "its previous key" if key == target else "its key"
                lines.extend(_kept_key_lines(key, its, sessions))
            if running and not kept:
                lines.extend(wrap(
                    "Running sessions keep the previous key until you restart them."
                ))
            lines.append("Claude Code sessions started from now on use the key.")
            return lines
        if source_is_key and source:
            groups = list(kept) or ([(source, started_on_key)] if started_on_key else [])
            for key, sessions in groups:
                lines.extend(_kept_key_lines(key, "its key", sessions))
            lines.append("Claude Code sessions started from now on use the key.")
            return lines
        if running:
            noun = "running session" if running == 1 else "running sessions"
            lines.extend(wrap(
                f'{running} {noun} will stop with "Not logged in" within about 30 seconds.'
            ))
            lines.extend(wrap(
                "Restart a session to put it on the key. Sessions started from "
                "now on use it."
            ))
            return lines
        return ["Claude Code sessions started from now on use the key."]
    if source_is_key and source and started_on_key:
        return wrap(
            f"Sessions started while {source} was active move to {target} "
            "within about 30 seconds."
        )
    return []


# -- part 2: the key helper (docs/design/api-key-sessions.md) -----------------------
#
# An API-key account is active while qswap's apiKeyHelper line is in Claude
# Code's user settings. Every session that reads that file follows it within a
# few seconds, running or new; the subscription whose login is live stays
# logged in "underneath", and sessions return to it when the line goes.

#: How a switch made an account active (the activation log's ``how``).
HOW_HELPER = "helper"
HOW_SAVED_KEY = "saved-key"
HOW_LOGIN = "login"

#: The question's title (menu bar, dashboard) when the key helper cannot be
#: added and the saved-key way is offered instead.
KEY_HELPER_QUESTION_TITLE = "Use the saved key instead?"

#: The sentence every move onto a key carries. Remote Control needs the
#: claude.ai login too: Anthropic's documentation says API keys are not
#: supported there (decision B9, F57).
KEY_CONNECTORS = (
    "claude.ai connectors and Remote Control are unavailable while a key is in use."
)

#: The header fact of the subscription under an active key.
UNDERNEATH = "underneath"
#: The header fact of a key auto-switch entered (decision B8).
BY_ENGINE = "by auto-switch"


#: The way off a key when there is no subscription to go back to.
KEY_NO_WAY_BACK = "log in to a subscription (claude, then /login), then run: qswap add"


def key_unwatched(key: str, back: str | None, watch: object = None) -> list[str]:
    """Auto-switch entered the active key: when no engine watches it, nothing
    leaves the key at its budget or credit, or when a subscription has room.
    ``back`` is the way back (``key_way_back``); None: no subscription.
    ``watch`` is ``engine_watching()`` (``autoswitch.KeyWatch``): no line
    while a loop watches, when it last checked while a recent ``--once``
    tick does, and otherwise that only a dry run runs or when an engine last
    checked (decisions DS3, DS4). Without it: nothing is running."""
    from ai_quota_swap.autoswitch import key_watch_words

    words = key_watch_words(key, watch)
    if words is None:
        return []
    words = words[0].upper() + words[1:]
    if watch:
        return wrap(f"{words}.")
    step = f"To go back: {back}" if back else f"To leave it, {KEY_NO_WAY_BACK}"
    return wrap(f"{words}. {step}")


def over_fact(underneath: str) -> str:
    """The header fact of an active key: ``over #1 personal``."""
    return f"over {underneath}"


def key_switch_followup(
    *, target: str, underneath: str | None, from_key: str | None = None,
    back: str | None = None, no_way_back: bool = False,
) -> list[str]:
    """What follows "Switched … (API key)" on the helper path: where running
    sessions go, the login that stays underneath (an account, or the address
    of a login qswap does not manage), what a key turns off, and the way back
    (``back``, the one ``key_way_back`` gives every surface: the switch to the
    account underneath or to a subscription, or ``qswap add`` over a login
    qswap does not manage). ``no_way_back``: there is no subscription to go
    back to, so it says how to leave (decision DS11)."""
    if from_key:
        first = (
            f"Running Claude Code sessions move from {from_key} to {target} within "
            "a few seconds."
        )
    else:
        first = "Running Claude Code sessions move to the key within a few seconds."
    parts = [first]
    if underneath:
        parts.append(f"{underneath} stays logged in underneath.")
    parts.append(KEY_CONNECTORS)
    if back == "qswap add":
        parts.append(f"To go back, add that login first: {back}")
    elif back:
        parts.append(f"Go back with: {back}")
    elif no_way_back:
        parts.append(f"To leave it, {KEY_NO_WAY_BACK}")
    return wrap(" ".join(parts))


def key_leave_followup(target: str, *, login_switch: bool = False) -> list[str]:
    """What follows "Switched #5 ci → #1 personal" when a key is left. With
    ``login_switch`` the login changed too (the target was not underneath),
    which Claude Code picks up within about 30 seconds."""
    if login_switch:
        return wrap(
            f"Running Claude Code sessions move to {target} within about 30 seconds."
        )
    return wrap(f"Running Claude Code sessions are back on {target} within a few seconds.")


#: After the saved key of the active key's account was replaced. The line is
#: the same, so Claude Code keeps its cached key until the cache expires (5
#: minutes in Claude Code 2.1.322) or a request is rejected (a 401 clears it).
KEY_REFRESH = (
    "Running Claude Code sessions pick up the new key within 5 minutes, or at once "
    "after a request is rejected."
)


def key_still_active(key: str, number: str | int) -> list[str]:
    """After ``qswap add`` saved the login while a key is active: sessions
    keep the key, and the switch that puts them on that login."""
    return wrap(
        f"{key} is still the active API key; Claude Code sessions use it, not this "
        f"login. To use this login: qswap switch {number}"
    )


def key_helper_refusal(
    target: str, number: str | int, reason: str, sessions: int = 0, *,
    from_key: str | None = None, prefix: str = "Error: ", records: Sequence[str] = (),
    fix: str | None = None, paths: Sequence[str] = (),
) -> str:
    """Why a switch to the API-key account ``target`` was refused when qswap
    cannot add its key line (``reason``: the settings file's path and what
    is wrong with it), and the saved-key path that goes ahead anyway, which
    only sessions started after the switch use. ``sessions`` is how many
    Claude Code sessions run on the shared login (:data:`SESSIONS_UNKNOWN`
    when the records could not be read); ``from_key`` names the active account
    when it is a saved key too; ``records`` are the session records qswap
    could not read (:func:`unreadable_records_lines`); ``paths``, what the
    ``fix`` is about (run profiles), each whole on a line of its own."""
    head = (
        f"not switched. qswap cannot add the key line for {target} to the "
        f"Claude Code settings: {reason}. Without it, the key can only be saved "
        "for Claude Code, which a running session does not pick up."
    )
    if from_key:
        body = (
            "Sessions started while "
            f"{from_key} was active keep using its key until you restart them."
        )
    elif sessions > 0:
        are = "is" if sessions == 1 else "are"
        each = "it stops" if sessions == 1 else "each one stops"
        body = (
            f"{_sessions(sessions)} {are} running on the shared login: about 30 "
            f'seconds after the switch {each} with "Not logged in" until you '
            "switch back or restart it."
        )
    elif sessions < 0:
        body = (
            "qswap could not read the Claude Code session records, so it cannot "
            "tell whether sessions are running on the shared login; any that are "
            'stop with "Not logged in" about 30 seconds after the switch.'
        )
    else:
        body = "No Claude Code session is running on the shared login."
    lines = wrap(prefix + head + " " + body, WIDTH - 2)
    lines[0] = lines[0][len(prefix):]
    return "\n".join([
        *lines,
        _STARTED_AFTER,
        *unreadable_records_lines(records, WIDTH - 2),
        *(f"  {path}" for path in paths),
        # The fix that keeps everyone logged in comes before the override
        # (F47): "Fix the JSON in ~/.claude/settings.json, then: ...".
        *(wrap(f"{fix}, then: qswap switch {number}") if fix else []),
        "To switch anyway:",
        f"  qswap switch {number} {LOG_OUT_SESSIONS_FLAG}",
    ])


def join_names(names: Sequence[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``: the one way names are joined in a
    sentence."""
    names = list(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


_and = join_names


def shown_dir(path: str) -> str:
    """A data directory in a sentence: in double quotes when it holds a
    space, so it is never broken across lines."""
    return f'"{path}"' if any(c.isspace() for c in path) else path


def other_qswap_sentence(other_dir: str) -> str:
    """Whose ``apiKeyHelper`` it is, when it is another qswap's (SS4)."""
    return (
        "That apiKeyHelper is the key line of another qswap, whose data directory "
        f"is {shown_dir(other_dir)}."
    )


def key_helper_foreign_refusal(
    target: str, number: str | int, path: str, *, log_out_sessions: bool = False,
    prefix: str = "Error: ", other_dir: str | None = None,
) -> str:
    """Another ``apiKeyHelper`` is in the settings (decision B6, F47): every
    session uses it now, and it outranks a saved key too, so the one way
    forward is to remove it. ``log_out_sessions``: the refusal of
    ``--log-out-sessions`` itself. ``other_dir``: the helper is another
    qswap's key line, for that data directory (decision SS4)."""
    if log_out_sessions:
        head = (
            f"not switched. {path} sets another apiKeyHelper, which outranks a saved "
            "key: with --log-out-sessions every session would be logged out and "
            "still use that helper."
        )
    else:
        head = (
            f"not switched. {foreign_helper_clause(path)}, so qswap cannot add the "
            f"key line for {target} beside it."
        )
    if other_dir:
        head += " " + other_qswap_sentence(other_dir)
    lines = wrap(prefix + head, WIDTH - 2)
    lines[0] = lines[0][len(prefix):]
    return "\n".join([
        *lines, *wrap(f"{foreign_helper_fix(path)}, then: qswap switch {number}"),
    ])


def foreign_helper_clause(path: str) -> str:
    """What another ``apiKeyHelper`` in the settings does, in the words the
    refusal and the note share (decision DS16)."""
    return (
        f"{path} already sets another apiKeyHelper, and every Claude Code session "
        "uses it now, before any login or saved key"
    )


def foreign_helper_fix(path: str) -> str:
    """The one way forward from another ``apiKeyHelper`` in the settings."""
    return f"Remove apiKeyHelper from {path}"


def foreign_helper_note(path: str) -> list[str]:
    """Another program's ``apiKeyHelper`` (decision DS16): the note list,
    status, the dashboard and the menu bar show, in the refusal's words."""
    return wrap(
        f"{foreign_helper_clause(path)}, so qswap cannot add a key line beside it. "
        f"{foreign_helper_fix(path)}"
    )


def key_line_defeated_clause(path: str, names: Sequence[str]) -> str:
    """What a variable in the settings' ``env`` block does to the key line,
    in the words the refusal and the note share (decisions B6, DS16)."""
    # The file sets them: the verb follows the file, however many there are.
    return (
        f"{path} sets {_and(names)} in its env block, which Claude Code obeys before "
        "any key line"
    )


def key_line_defeated_fix(path: str, names: Sequence[str], number: str | int | None = None) -> str:
    """Remove the variables; then the switch to ``number`` when one is named."""
    then = f", then: qswap switch {number}" if number is not None else ""
    return f"Remove {_and(names)} from the env block of {path}{then}"


def key_line_defeated_refusal(
    target: str, number: str | int, path: str, names: Sequence[str], *,
    prefix: str = "Error: ",
) -> str:
    """The settings' ``env`` block sets a variable Claude Code obeys before
    any key line (decision B6, F59): named, never its value."""
    head = (
        f"not switched. {key_line_defeated_clause(path, names)}, so sessions would "
        f"not use {target}."
    )
    lines = wrap(prefix + head, WIDTH - 2)
    lines[0] = lines[0][len(prefix):]
    return "\n".join([*lines, *wrap(key_line_defeated_fix(path, names, number))])


#: The env variables that switch Claude Code to another provider: with one
#: set, no key of Anthropic's is sent.
_PROVIDER_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def key_line_outranked_consequence(
    names: Sequence[str], key: str | None, host: str | None = None,
) -> str:
    """What a variable in the env block does while the key line of ``key``
    is in. A credential (``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_API_KEY``)
    takes the place of one header, and sessions still send the key from the
    line in the other: measured on Claude Code 2.1.322 (x-api-key the key,
    the bearer the token), and to ``host`` when ``ANTHROPIC_BASE_URL`` sends
    it there. Which one Anthropic bills was not tested. Another provider
    sends no key of Anthropic's. With no key active, no switch to one."""
    if not key:
        return "so qswap does not switch to an API key"
    if any(name in _PROVIDER_VARS for name in names):
        return f"so sessions do not use {key}"
    what = "the credential that variable sets" if len(names) == 1 else (
        "the credentials those variables set"
    )
    to = f" to {host}" if host else ""
    return (
        f"yet sessions still send the key of {key} with every request{to}, beside "
        f"{what}, so either may be billed"
    )


def key_line_outranked_note(
    path: str, names: Sequence[str], key: str | None, number: str | int | None,
    *, host: str | None = None,
) -> list[str]:
    """A variable in the env block outranks the key line (decision DS16):
    over the active key ``key`` (its number ``number``), or, with none
    active, over every key qswap would switch to. The refusal's words and
    fix; what it does to the active key (:func:`key_line_outranked_consequence`)."""
    what = key_line_outranked_consequence(names, key, host)
    return wrap(
        f"{key_line_defeated_clause(path, names)}, {what}. "
        f"{key_line_defeated_fix(path, names, number if key else None)}"
    )


def other_qswap_note(path: str, other_dir: str) -> list[str]:
    """The settings hold another qswap's key line (decision SS4): its key is
    what every session uses, and this qswap does not touch it."""
    return wrap(
        f"{path} holds the key line of another qswap, whose data directory is "
        f"{shown_dir(other_dir)}, so Claude Code sessions use that qswap's key, "
        f"not an account here, until the line is removed. {foreign_helper_fix(path)}"
    )


def other_qswap_followup(path: str, other_dir: str) -> list[str]:
    """After a switch to a subscription while another qswap's key line is in
    the settings (decision SS4): the line stays, and so do the sessions."""
    return wrap(
        "Claude Code sessions stay on the key of another qswap, whose data "
        f"directory is {shown_dir(other_dir)}, until its key line is removed from "
        f"{path}."
    )


def key_sent_elsewhere(path: str, host: str, target: str) -> str:
    """The one warning when the settings send the key to another host."""
    return (
        f"Warning: {path} sets ANTHROPIC_BASE_URL, so Claude Code sends the key of "
        f"{target} to {host}."
    )


#: What a line that cannot give a key leaves running sessions with (E2, read
#: on Claude Code 2.1.322): the key they hold, until the settings change or a
#: request is rejected; only new sessions get none.
BROKEN_LINE_RUNNING = "running ones keep the key they have until the settings change"


def key_helper_problem(
    number: str, why: str, fix_command: str, *, key: str | None = None,
) -> list[str]:
    """The lines every surface shows for a helper line naming an account that
    does not exist or is not an API-key account (``why``: ``missing`` or
    ``not-api-key``). ``key``: the account's name when it exists (``#7
    work``, decision DS7); else its number."""
    key = key or f"#{number}"
    what = "does not exist" if why == "missing" else "is not an API-key account"
    return wrap(
        f"The Claude Code settings make {key} the active API key, but {key} "
        f"{what}: new Claude Code sessions fail to get a key, and {BROKEN_LINE_RUNNING}. "
        f"Remove the line: {fix_command}"
    )


def key_helper_moved(number: str, fix_command: str, *, key: str | None = None) -> list[str]:
    """qswap's key line runs a qswap that is no longer there (a reinstall
    moved it), so sessions on the key cannot get it. ``key``: the account's
    name (``#5 ci``, decision DS7); else its number."""
    key = key or f"#{number}"
    return wrap(
        f"The key line for {key} in the Claude Code settings runs a qswap that "
        f"is no longer there: new Claude Code sessions fail to get the key, and "
        f"{BROKEN_LINE_RUNNING}. Point it at this qswap: {fix_command}"
    )


#: Said when the user names the key auto-switch entered.
KEY_KEPT = (
    "Auto-switch keeps it now, and leaves it at its budget, when its credit is "
    "spent or expires, or when its spend cannot be measured for 20 minutes."
)


def settings_fix(path: str, error: str | None) -> str:
    """The step for a settings file qswap cannot use: ``Make PATH readable``
    when reading it failed (its permissions, say), else ``Fix the JSON in
    PATH`` (it was read and does not parse)."""
    if (error or "").startswith("could not be read"):
        return f"Make {path} readable"
    return f"Fix the JSON in {path}"


def key_settings_unreadable(path: str, error: str | None, key: str) -> list[str]:
    """The settings cannot be read or parsed while qswap's record names a
    key: the one note list, status, the dashboard, the menu bar, doctor, the
    guards and a refused switch all show (decision B5)."""
    why = f" ({error})" if error else ""
    return wrap(
        f"{path} cannot be read{why}; qswap last made {key} the active API key "
        f"there, and its key line may still be in it. {settings_fix(path, error)}, "
        "then retry."
    )


def key_line_came_back(key: str, fix_command: str) -> list[str]:
    """qswap took the key line out and an older copy of the settings, written
    back by another program, put it in again."""
    return wrap(
        f"The key line for {key} came back after qswap took it out: another program "
        f"wrote an older copy of the Claude Code settings, so sessions use {key} "
        f"again. To leave it: {fix_command}"
    )


def key_line_lost(key: str, path: str, number: str | int, back: str | None) -> list[str]:
    """qswap's record names a key whose line is no longer in the settings
    (removed by hand, say). The way off the key comes first (decision E1):
    ``back``, the switch to the account underneath (``key_way_back``), which
    clears the record; None: no subscription to go to. Using the key again
    comes second, and says so."""
    step = f"To leave it: {back}." if back else f"To leave it, {KEY_NO_WAY_BACK}."
    return wrap(
        f"qswap made {key} the active API key, but its key line is no longer in "
        f"{path}, so Claude Code sessions do not use it. {step} To use the key "
        f"again: qswap switch {number}"
    )


def profiles_still_linked_line(count: int, key: str, fix_command: str) -> list[str]:
    """Run profiles still sharing the user settings while the key line is in."""
    noun = "profile still shares" if count == 1 else "profiles still share"
    its = "its" if count == 1 else "their"
    return wrap(
        f"{count} qswap run {noun} the Claude Code settings, so {its} sessions use "
        f"{key} too. Give {'it its' if count == 1 else 'them their'} own copy: {fix_command}"
    )


def stale_copies_line(count: int, fix_command: str) -> list[str]:
    """The run profiles left with a copy of the settings after the key line
    was removed (a crash between the two steps)."""
    noun = "profile keeps" if count == 1 else "profiles keep"
    return wrap(
        f"{count} qswap run {noun} a copy of the Claude Code settings from while a "
        f"key was active. Restore the shared settings: {fix_command}"
    )


#: The variables that make Claude Code ignore the active account.
OVERRIDE_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


def exported_overrides(environ) -> list[str]:
    """Which of :data:`OVERRIDE_ENV_VARS` are set (names only, never values)."""
    return [name for name in OVERRIDE_ENV_VARS if (environ.get(name) or "").strip()]


def override_warning(names: Sequence[str]) -> str:
    """The one line ``qswap switch`` prints when such a variable is set."""
    if len(names) == 1:
        subject = f"{names[0]} is set"
    else:
        subject = f"{', '.join(names[:-1])} and {names[-1]} are set"
    return f"Warning: {subject}; claude started here ignores the switch."


#: Claude Code ranks the key line above this variable (its documented
#: precedence; decision B6, F62): a session started with it follows the key.
OAUTH_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def override_warnings(names: Sequence[str], *, key_line: bool = False) -> list[str]:
    """What ``qswap switch`` warns about the exported variables: with
    ``key_line`` (a switch onto a key through the line) the OAuth token
    variable does not override the switch, its sessions use the key too."""
    names = list(names)
    out: list[str] = []
    ignoring = [n for n in names if not (key_line and n == OAUTH_TOKEN_VAR)]
    if ignoring:
        out.append(override_warning(ignoring))
    if key_line and OAUTH_TOKEN_VAR in names:
        out.append(
            f"Warning: {OAUTH_TOKEN_VAR} is set, but the key line comes before it, so "
            "claude started here uses the key too."
        )
    return out
