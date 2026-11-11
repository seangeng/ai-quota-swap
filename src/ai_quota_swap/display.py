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
    if block.disabled:
        facts.append(("disabled", "disabled"))
    if block.kind == "api_key":
        facts.append(("API key", "api-key"))
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


def api_key_refusal(
    target: str, number: str | int, sessions: int, *, from_key: str | None = None,
    prefix: str = "Error: ",
) -> str:
    """Why a switch to the API-key account ``target`` was refused while
    ``sessions`` Claude Code sessions run on the shared login, and the command
    that goes ahead. ``sessions`` is :data:`SESSIONS_UNKNOWN` (or 0) when the
    session records could not be read. ``from_key`` names the active account
    when it is an API key too. Wrapped to 80 columns with ``prefix`` (how the command line
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
        "Sessions started after the switch use the key.",
        "To switch anyway:",
        f"  qswap switch {number} {LOG_OUT_SESSIONS_FLAG}",
    ])


#: The button (menu bar, dashboard) that goes ahead with such a switch.
API_KEY_SWITCH_BUTTON = "Switch and log out sessions"


def api_key_question(refusal: str) -> str:
    """:func:`api_key_refusal` as the body of a question whose button goes
    ahead: the explanation, without "not switched." and without the
    ``--log-out-sessions`` lines the button stands for; one paragraph."""
    kept = [
        line.strip() for line in refusal.splitlines()
        if line.strip() and not line.startswith("To switch anyway")
        and not line.strip().startswith("qswap switch")
    ]
    text = " ".join(kept).replace("not switched. ", "", 1)
    return text[:1].upper() + text[1:]


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


def api_key_engine_sentence(target: str, sessions: int) -> str:
    """Why the auto-switch engine did not move onto the API-key account
    ``target``."""
    if sessions <= 0:
        return (
            f"Not switching to {target}: it is an API-key account and running "
            "sessions could not be counted"
        )
    noun = "running session" if sessions == 1 else "running sessions"
    return (
        f"Not switching to {target}: it is an API-key account and {sessions} "
        f"{noun} could not follow"
    )


def session_place(pid: int, cwd: str) -> str:
    """One session in a list: ``pid 4242 · app`` (the directory's name)."""
    name = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    return f"pid {pid} · {name}" if name else f"pid {pid}"


def api_key_followup(
    *,
    target: str,
    target_is_key: bool,
    source: str | None,
    source_is_key: bool,
    running: int,
    started_on_key: Sequence[tuple[int, str]] = (),
) -> list[str]:
    """What follows "Switched …" when either side is an API-key account; ``[]``
    otherwise (the usual follow-up applies).

    ``running`` is how many Claude Code sessions ran on the shared login at the
    switch; ``started_on_key`` the ``(pid, cwd)`` of those started while the
    API-key account being left was active."""
    if target_is_key:
        lines: list[str] = []
        if source_is_key and source:
            if started_on_key:
                lines.extend(wrap(
                    f"These sessions were started while {source} was active and "
                    "keep using its key until you restart them:"
                ))
                lines.extend(f"  {session_place(pid, cwd)}" for pid, cwd in started_on_key)
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
