"""Usage resets: Anthropic's banked refills of a usage window.

Claude Code ships these as ``/limit-reset`` (internal program name
``cedar_ember``). An eligible account holds *grants* — "one Fable reset for
Max", "one more at 12 months' tenure" — each of which, when claimed, sets the
windows it ``clears`` back to zero while the weekly reset day stays where it
was. Grants expire (``ends_at``) and are lost if unused.

Wire protocol, as Claude Code 2.1.321 speaks it:

* **Status** rides on the usage endpoint: ``GET /api/oauth/usage?cedar_ember=1``
  adds a ``cedar_ember`` block (eligibility, grants, the server's
  ``next_grant_id``, the weekly reset instant, any cooldown). The server gates
  it by client surface: a non-Claude-Code ``User-Agent`` reads back
  ``eligible: false, ineligible_reason: "surface"``, so the read sends the
  installed Claude Code CLI's own agent string (:func:`user_agent`).
* **Claim** is ``POST /api/organizations/{org}/reset_rate_limits`` with
  ``{"program": "cedar_ember", "grant_id", "request_id"}``. ``request_id`` is
  an idempotency key: re-sending the same one for an unconfirmed claim cannot
  spend a second reset. The answer's ``result`` is one of ``reset``,
  ``already_used``, ``not_limited``, ``cooldown``, ``ineligible`` or
  ``unavailable`` (an unknown grant id answers ``ineligible``/
  ``unknown_grant`` and spends nothing).

Resets are not interchangeable. Every grant has a *scope* (:func:`grant_scope`),
derived from the windows it ``clears`` and cross-checked against what the
server declares (``gives_back_weekly``, ``type``):

* ``model``: per-model weekly windows only, not the all-models weekly window
  (a Fable reset). Claude Code calls a grant with ``gives_back_weekly`` a
  "Fable reset" whatever else it says.
* ``session``: the 5-hour window only.
* ``weekly``: the all-models weekly window (and whatever else it lists).
* ``full``: both the 5-hour and the all-models weekly windows.
* ``unknown``: anything else; shown as "reset" with its listed windows and
  never spent automatically.

The planner spends the narrowest reset that does the job, and spends a
``weekly`` or ``full`` reset by itself only for an all-usage window (5h or 7d)
unless ``resets.weeklyForModelLimit`` allows it.

What Claude Code (2.1.322) does with kinds: it knows four ("session",
"weekly", "fable", "full"); a grant with ``gives_back_weekly`` is "fable",
otherwise the server's ``type``, otherwise read from ``clears``. The user
picks a kind (``/limit-reset fable|full|5-hour|weekly``, or a chooser), and
the client claims that kind's first usable grant, which need not be
``next_grant_id`` (used only for telemetry). After a successful claim it
marks exactly the windows the answer's ``cleared`` lists as reset.

Everything here is pure or a single network call; when to claim is decided by
:func:`plan_reset` (pure, unit-tested) and executed by the auto engine or
``qswap reset``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_logger = logging.getLogger("ai-quota-swap")

PROGRAM = "cedar_ember"
USAGE_QUERY = "cedar_ember=1"
CLAIM_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
CLAIM_TIMEOUT_S = 25.0
# Claude Code's own validation of the ids it sends (a malformed one is refused
# client-side, never POSTed).
GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ORG_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Used only when no installed Claude Code can be found: the build whose
# protocol this module was written against.
FALLBACK_CLI_VERSION = "2.1.321"

CLAIM_RESULTS = (
    "reset", "already_used", "not_limited", "cooldown", "ineligible", "unavailable",
)
# Results the server decided (as opposed to our own transport failures, which
# leave the claim unconfirmed and retryable with the same request id).
SETTLED_RESULTS = frozenset(CLAIM_RESULTS) - {"unavailable"}

# Window keys a grant can clear, as the server names them, mapped to the label
# qswap gives that window (``oauth.relevant_windows``). ``None``: a window
# qswap does not track (Cowork, OAuth apps, ...).
WINDOW_LABELS: dict[str, str | None] = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_overage_included": "Fable",
    "seven_day_opus": "Opus",
    "seven_day_sonnet": "Sonnet",
    "seven_day_cowork": None,
    "seven_day_omelette": None,
    "seven_day_oauth_apps": None,
}
# The all-usage windows: every model's use counts toward them.
ALL_USAGE_KEYS = ("five_hour", "seven_day")
# Per-model weekly windows (Claude Code's own class for these is "model").
MODEL_KEYS = frozenset({"seven_day_overage_included", "seven_day_opus", "seven_day_sonnet"})

# What a grant is for (:func:`grant_scope`), narrowest first. Breadth orders
# the planner's choice: a model or session reset before a weekly one, a
# weekly one before a full one; an unknown shape is never auto-spent.
SCOPES = ("model", "session", "weekly", "full", "unknown")
SCOPE_BREADTH = {"model": 0, "session": 0, "weekly": 1, "full": 2, "unknown": 3}
BROAD_SCOPES = frozenset({"weekly", "full"})
# A window key the server sends that qswap does not know is kept, for display,
# when it is a plain identifier; anything else is shown as "?".
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

AUTO_MODES = ("off", "active", "last-resort", "on-limit")


# -- client identity ---------------------------------------------------------

_cli_version_cache: str | None = None


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(part) for part in v.split("."))


def _npm_package_version(start: Path) -> str | None:
    """Version of an npm-installed ``@anthropic-ai/claude-code`` above ``start``."""
    for parent in (start, *start.parents):
        pkg = parent / "package.json"
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == "@anthropic-ai/claude-code":
            version = data.get("version")
            return version if isinstance(version, str) and _VERSION_RE.match(version) else None
    return None


def detect_cli_version() -> str:
    """The installed Claude Code CLI's version, for the reset-status read.

    ``QSWAP_CLAUDE_CLI_VERSION`` overrides. Otherwise: the native installer's
    version-named binary behind ``claude`` on PATH, then the newest entry in
    its versions directory, then an npm install's package.json, then
    :data:`FALLBACK_CLI_VERSION`. Never spawns ``claude`` (a 300 MB binary is
    not worth starting per poll); cached for the process.
    """
    global _cli_version_cache
    if _cli_version_cache is not None:
        return _cli_version_cache
    version: str | None = None
    env = os.environ.get("QSWAP_CLAUDE_CLI_VERSION", "").strip()
    if _VERSION_RE.match(env):
        version = env
    if version is None:
        from ai_quota_swap import clis

        found = clis.find_cli("claude")
        if found:
            real = Path(os.path.realpath(found))
            if _VERSION_RE.match(real.name):
                version = real.name
            else:
                version = _npm_package_version(real.parent)
    if version is None:
        versions_dir = Path.home() / ".local" / "share" / "claude" / "versions"
        try:
            names = [p.name for p in versions_dir.iterdir() if _VERSION_RE.match(p.name)]
        except OSError:
            names = []
        if names:
            version = max(names, key=_version_key)
    _cli_version_cache = version or FALLBACK_CLI_VERSION
    return _cli_version_cache


def user_agent() -> str:
    """The agent string Claude Code's CLI sends (``claude-cli/<v> (external, cli)``)."""
    return f"claude-cli/{detect_cli_version()} (external, cli)"


# -- status ------------------------------------------------------------------


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _window_keys(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [k for k in value if isinstance(k, str) and k in WINDOW_LABELS]


def _unknown_window_keys(value: object) -> list[str]:
    """Entries of a ``clears`` list that are not windows qswap knows.

    Claude Code drops them silently; qswap keeps them so a grant that claims
    to clear something unknown is shown as such and never spent automatically
    on the strength of only the part qswap understands."""
    if not isinstance(value, list):
        return []
    out = []
    for k in value:
        if isinstance(k, str) and k in WINDOW_LABELS:
            continue
        name = k if isinstance(k, str) and _SAFE_KEY_RE.match(k) else "?"
        if name not in out:
            out.append(name)
    return out[:8]


def _int_at_least_zero(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if value >= 0 else None


def _parse_grant(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    gid = raw.get("id")
    left = _int_at_least_zero(raw.get("resets_left"))
    if not isinstance(gid, str) or not GRANT_ID_RE.match(gid) or left is None:
        return None
    percent_used: dict[str, float] = {}
    pu = raw.get("percent_used")
    if isinstance(pu, dict):
        for key, value in pu.items():
            if (
                key in WINDOW_LABELS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                percent_used[key] = float(value)
    grant_type = raw.get("type")
    return {
        "id": gid,
        "label": raw.get("label") if isinstance(raw.get("label"), str) else "",
        "resets_left": left,
        "resets_total": _int_at_least_zero(raw.get("resets_total")) or left,
        "starts_at": _str_or_none(raw.get("starts_at")),
        "ends_at": _str_or_none(raw.get("ends_at")),
        "clears": _window_keys(raw.get("clears")),
        "clears_unknown": _unknown_window_keys(raw.get("clears")),
        "blocking": _window_keys(raw.get("blocking")),
        "paused": raw.get("paused") is True,
        "usable_now": raw.get("usable_now") is True,
        # Claude Code defaults this to True when absent: a grant that may only
        # be used at a limit is the conservative reading.
        "use_requires_limit": raw.get("use_requires_limit") is not False,
        "gives_back_weekly": raw.get("gives_back_weekly") is True,
        "type": grant_type if grant_type in ("session", "weekly", "full") else None,
        "unavailable_reason": _str_or_none(raw.get("unavailable_reason")),
        "percent_used": percent_used,
    }


def parse_status(block: object) -> dict | None:
    """Normalize the usage response's ``cedar_ember`` block, or None.

    Keeps the server's snake_case names (they sit inside qswap's usage dict
    next to ``five_hour``/``seven_day``) and drops anything malformed, grant
    by grant, the way Claude Code does. An ineligible account is still a
    status (``{"eligible": False, "ineligible_reason": ...}``) so ``qswap
    reset`` can say why nothing is offered.
    """
    if not isinstance(block, dict) or not isinstance(block.get("eligible"), bool):
        return None
    raw_grants = block.get("grants")
    grants = [
        g for g in map(_parse_grant, raw_grants if isinstance(raw_grants, list) else [])
        if g is not None
    ]
    next_id = block.get("next_grant_id")
    if not (isinstance(next_id, str) and any(g["id"] == next_id for g in grants)):
        next_id = None
    status: dict = {
        "eligible": block["eligible"],
        "ineligible_reason": _str_or_none(block.get("ineligible_reason")),
        "at_limit": block.get("at_limit") is True,
        "exhausted": _window_keys(block.get("exhausted")),
        "next_grant_id": next_id,
        "weekly_resets_at": _str_or_none(block.get("weekly_resets_at")),
        "cooldown_until": _str_or_none(block.get("cooldown_until")),
        "grants": grants,
    }
    return status


def parse_ts(value: object) -> float | None:
    """Epoch seconds of an ISO-8601 instant, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def local_datetime(ts: float | None) -> datetime | None:
    """Local-time datetime for an epoch, or None when the platform cannot
    represent it (a far-future ``ends_at`` such as year 9999 overflows the
    local conversion east of UTC). Display only."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def use_by(ts: float | None, *, year: bool = False) -> str | None:
    """``Mar 9`` (``Mar 9, 2027`` with ``year``) for an expiry, or None."""
    dt = local_datetime(ts)
    if dt is None:
        return None
    return f"{dt.strftime('%b')} {dt.day}" + (f", {dt.year}" if year else "")


def grant_unusable_reason(grant: dict, now: float) -> str | None:
    """Why a grant cannot be claimed right now, or None when it can."""
    if grant.get("resets_left", 0) <= 0:
        return "used"
    ends = parse_ts(grant.get("ends_at"))
    if ends is not None and ends <= now:
        return "expired"
    starts = parse_ts(grant.get("starts_at"))
    if starts is not None and starts > now:
        return "not started"
    if grant.get("paused"):
        return "paused"
    if grant.get("unavailable_reason"):
        return str(grant["unavailable_reason"]).replace("_", " ")
    if grant.get("blocking"):
        labels = [WINDOW_LABELS.get(k) or k for k in grant["blocking"]]
        return "blocked by " + ", ".join(labels)
    if not grant.get("usable_now"):
        return "needs a limit hit first" if grant.get("use_requires_limit") else "not usable now"
    return None


def _scope_from_clears(clears: Iterable[str]) -> str:
    """The scope a ``clears`` list (known window keys) describes on its own."""
    keys = set(clears)
    has_5h = "five_hour" in keys
    has_7d = "seven_day" in keys
    models = keys & MODEL_KEYS
    others = keys - set(ALL_USAGE_KEYS) - MODEL_KEYS  # Cowork, OAuth apps, ...
    if has_5h and has_7d:
        return "full"
    if has_7d:
        return "weekly"
    if has_5h:
        return "unknown" if models or others else "session"
    return "model" if models else "unknown"


def grant_scope(grant: dict) -> str:
    """What spending ``grant`` is for: ``model``, ``session``, ``weekly``,
    ``full`` or ``unknown`` (see the module docstring).

    The windows a grant ``clears`` decide it, because they are what the claim
    sets to zero. What the server declares is a cross-check:

    * ``gives_back_weekly`` marks a Fable reset (Claude Code calls such a
      grant a "Fable reset" before looking at anything else); it agrees with
      a grant that clears per-model windows only.
    * ``type: "full"`` is no evidence of breadth: the live server sends it on
      a Fable reset that clears only the Fable window, and a Codex credit
      ("Full reset (Weekly + 5 hr)") on a plan with only a weekly window can
      clear only that window. The windows decide.
    * ``type: "weekly"`` on a grant that lists only per-model windows reads
      as weekly (the broader reading keeps it from being spent on a narrow
      limit); one that lists both 5h and 7d is full. A declared type the
      windows contradict any other way makes the grant ``unknown``.
    """
    derived = _scope_from_clears(grant.get("clears") or ())
    if derived == "unknown":
        return "unknown"
    if derived == "model" and grant.get("gives_back_weekly"):
        return "model"
    declared = grant.get("type")
    if declared not in ("session", "weekly") or declared == derived:
        return derived
    if declared == "weekly" and derived == "model":
        return "weekly"
    if derived == "full":
        return "full"
    return "unknown"


def _model_names(grant: dict) -> list[str]:
    """Display names of the per-model windows ``grant`` clears, in order."""
    names = [WINDOW_LABELS.get(k) for k in grant.get("clears") or () if k in MODEL_KEYS]
    return list(dict.fromkeys(n for n in names if n))


def grant_kind(grant: dict) -> str:
    """Short name of what a grant refills: "Fable" (the model it is for),
    "session", "weekly", "full", or "reset" for a shape qswap cannot name.
    The ``kind`` of ``--json`` output and :class:`ResetPlan`."""
    scope = grant_scope(grant)
    if scope == "model":
        return "+".join(_model_names(grant)) or "model"
    if scope == "unknown":
        return "reset"
    return scope


def scope_noun(scope: str | None, kind: str | None = None) -> str:
    """"Fable reset", "5-hour reset", "weekly reset", "full reset" or "reset",
    the way Claude Code's /limit-reset names them. ``kind`` names a model
    reset; without a ``scope`` (a plan written before scopes existed) the
    ``kind`` alone is used."""
    if scope == "model":
        return f"{kind} reset" if kind and kind != "model" else "model reset"
    if scope == "session":
        return "5-hour reset"
    if scope in ("weekly", "full"):
        return f"{scope} reset"
    if scope is None and kind and kind != "reset":
        return f"{kind} reset" if kind != "session" else "5-hour reset"
    return "reset"


def reset_name(grant: dict) -> str:
    """What one of ``grant``'s resets is called: "Fable reset", "weekly reset", ..."""
    return scope_noun(grant_scope(grant), grant_kind(grant))


def scope_phrase(scope: str | None, models: Sequence[str] = ()) -> str:
    """What a reset of ``scope`` clears, in plain words."""
    if scope == "model":
        return f"clears {_and(models) or 'one model'} usage only"
    if scope == "session":
        return "clears 5-hour usage only"
    if scope == "weekly":
        return "clears all weekly usage"
    if scope == "full":
        return "clears all usage"
    return "clears windows qswap cannot name"


def clears_phrase(grant: dict) -> str:
    """What ``grant`` clears, for a newcomer: "clears Fable usage only",
    "clears all weekly usage", ... An unknown shape lists its windows; a
    window key qswap does not know is named as the server sent it."""
    scope = grant_scope(grant)
    unknown = list(grant.get("clears_unknown") or ())
    if scope == "unknown":
        listed = [WINDOW_LABELS.get(k) or k for k in grant.get("clears") or ()] + unknown
        return f"clears {', '.join(listed)}" if listed else "clears nothing qswap can name"
    phrase = scope_phrase(scope, _model_names(grant))
    if unknown:
        phrase += f" (also lists {', '.join(unknown)})"
    return phrase


def _and(names: Sequence[str]) -> str:
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def primary_window_keys(grant: dict, scope: str | None = None) -> list[str]:
    """The windows ``grant`` is *for*, in its ``clears`` order: the one a
    session or weekly reset is named after, both all-usage windows for a full
    reset, the model windows for a model reset; none for an unknown shape.
    The planner spends a reset only for one of these (unless
    ``resets.weeklyForModelLimit`` widens a weekly or full reset to its model
    windows too)."""
    scope = scope or grant_scope(grant)
    wanted = {
        "session": {"five_hour"},
        "weekly": {"seven_day"},
        "full": {"five_hour", "seven_day"},
        "model": MODEL_KEYS,
    }.get(scope, set())
    return [k for k in grant.get("clears") or () if k in wanted]


def unused_grants(status: dict | None, now: float) -> list[dict]:
    """Grants with resets left that have not expired (usable now or later)."""
    if not isinstance(status, dict):
        return []
    out = []
    for g in status.get("grants") or ():
        if g.get("resets_left", 0) <= 0:
            continue
        ends = parse_ts(g.get("ends_at"))
        if ends is not None and ends <= now:
            continue
        out.append(g)
    return out


def _count_label(grant: dict) -> str:
    """A grant's kind as a count reads: "2 Fable", "1 weekly", "1 five-hour"."""
    scope = grant_scope(grant)
    if scope == "model":
        return grant_kind(grant)
    return {"session": "five-hour", "weekly": "weekly", "full": "full"}.get(scope, "other")


def counts_by_kind(status: dict | None, now: float) -> list[tuple[str, int]]:
    """Unused resets held, counted per kind, narrowest kind first:
    ``[("Fable", 2), ("weekly", 1)]``."""
    counts: dict[str, int] = {}
    order: dict[str, tuple[int, int]] = {}
    for i, g in enumerate(unused_grants(status, now)):
        label = _count_label(g)
        counts[label] = counts.get(label, 0) + g["resets_left"]
        order.setdefault(label, (SCOPE_BREADTH[grant_scope(g)], i))
    return [(label, counts[label]) for label in sorted(counts, key=order.__getitem__)]


def held_by_kind(status: dict | None, now: float) -> str | None:
    """"2 Fable · 1 weekly", or None when none are held."""
    counts = counts_by_kind(status, now)
    if not counts:
        return None
    return " · ".join(f"{n} {label}" for label, n in counts)


def badge(status: dict | None, now: float) -> str:
    """What a compact view (the dashboard card, the menu bar row) says about
    the resets an account holds: "3 resets" while they are all of one kind,
    counts by kind ("resets: 2 Fable · 1 weekly") once kinds differ, since
    one kind cannot stand in for another; "" when none are held."""
    counts = counts_by_kind(status, now)
    if not counts:
        return ""
    if len(counts) == 1:
        n = counts[0][1]
        return f"{n} reset{'s' if n != 1 else ''}"
    return f"resets: {held_by_kind(status, now)}"


def summary(status: dict | None, now: float, *, counted: bool = True) -> str | None:
    """One-line holding: "3 Fable · use by Mar 9", or None when none are held.

    Counts resets (not grants) per kind, narrowest kind first ("2 Fable ·
    1 weekly · use by Mar 9"), with the earliest expiry. ``counted=False``
    drops the count when every reset is of one kind ("Fable · use by Mar 9"),
    for a row that prints the total in its own column.
    """
    grants = unused_grants(status, now)
    if not grants:
        return None
    counts = counts_by_kind(status, now)
    if not counted and len(counts) == 1:
        held = counts[0][0]
    else:
        held = " · ".join(f"{n} {label}" for label, n in counts)
    ends = [t for t in (parse_ts(g.get("ends_at")) for g in grants) if t is not None]
    first = use_by(min(ends)) if ends else None
    if first:
        held += f" · use by {first}"
    if not any(grant_unusable_reason(g, now) is None for g in grants):
        held += " · not usable now"
    return held


def total_left(status: dict | None, now: float) -> int:
    return sum(g["resets_left"] for g in unused_grants(status, now))


def status_to_json(status: dict, now: float) -> dict:
    """camelCase projection for ``--json`` output, with derived fields
    (``kind``, ``scope``, ``unusableReason``, the total ``resetsLeft``)
    spelled out. ``clearsUnknown`` lists window keys qswap does not know."""
    return {
        "eligible": status.get("eligible", False),
        "ineligibleReason": status.get("ineligible_reason"),
        "atLimit": status.get("at_limit", False),
        "nextGrantId": status.get("next_grant_id"),
        "weeklyResetsAt": status.get("weekly_resets_at"),
        "cooldownUntil": status.get("cooldown_until"),
        "resetsLeft": total_left(status, now),
        "grants": [
            {
                "id": g["id"],
                "label": g.get("label", ""),
                "kind": grant_kind(g),
                "scope": grant_scope(g),
                "resetsLeft": g["resets_left"],
                "resetsTotal": g.get("resets_total"),
                "startsAt": g.get("starts_at"),
                "endsAt": g.get("ends_at"),
                "clears": list(g.get("clears") or ()),
                "clearsUnknown": list(g.get("clears_unknown") or ()),
                "blocking": list(g.get("blocking") or ()),
                "usableNow": g.get("usable_now", False),
                "useRequiresLimit": g.get("use_requires_limit", True),
                "givesBackWeekly": g.get("gives_back_weekly", False),
                "paused": g.get("paused", False),
                "unavailableReason": g.get("unavailable_reason"),
                "unusableReason": grant_unusable_reason(g, now),
                "percentUsed": dict(g.get("percent_used") or {}),
            }
            for g in status.get("grants") or ()
        ],
    }


# -- claim -------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimResult:
    """The server's answer to one claim, or our transport failure.

    ``result`` is a server value (:data:`CLAIM_RESULTS`) or one of
    ``rate_limited``/``auth_error``/``error`` when no answer arrived; those
    leave the claim unconfirmed, to be retried with the same request id.
    """

    result: str
    reason: str | None = None
    resets_left: int | None = None
    cleared: tuple[str, ...] = ()
    weekly_resets_at: str | None = None
    cooldown_until: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.result == "reset"

    @property
    def settled(self) -> bool:
        """The server decided; a retry needs a fresh request id."""
        return self.result in SETTLED_RESULTS

    def to_json(self) -> dict:
        return {
            "result": self.result,
            "reason": self.reason,
            "resetsLeft": self.resets_left,
            "cleared": list(self.cleared),
            "weeklyResetsAt": self.weekly_resets_at,
            "cooldownUntil": self.cooldown_until,
            "detail": self.detail,
        }


def claim(
    access_token: str,
    org_uuid: str,
    grant_id: str,
    request_id: str,
    *,
    timeout: float = CLAIM_TIMEOUT_S,
) -> ClaimResult:
    """Spend one reset of ``grant_id`` on the organization's account. Never raises."""
    if not GRANT_ID_RE.match(grant_id or "") or not REQUEST_ID_RE.match(request_id or ""):
        return ClaimResult("error", detail="malformed grant or request id")
    if not _ORG_RE.match(org_uuid or ""):
        return ClaimResult("error", detail="account has no organization id")
    body = json.dumps(
        {"program": PROGRAM, "grant_id": grant_id, "request_id": request_id}
    ).encode()
    from ai_quota_swap.oauth import OAUTH_BETA_HEADER  # late: oauth imports this module

    req = urllib.request.Request(
        CLAIM_URL.format(org=org_uuid),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "Content-Type": "application/json",
            "User-Agent": user_agent(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return ClaimResult("rate_limited", detail="http-429")
        if e.code in (401, 403):
            return ClaimResult("auth_error", detail=f"http-{e.code}")
        return ClaimResult("error", detail=f"http-{e.code}")
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        _logger.debug("reset claim transport failure: %r", e)
        return ClaimResult("error", detail="network")
    except ValueError:
        return ClaimResult("error", detail="bad-response")
    except Exception as e:  # http.client.IncompleteRead, BadStatusLine, ...
        _logger.debug("reset claim failed: %r", e)
        return ClaimResult("error", detail=type(e).__name__)
    if not isinstance(data, dict):
        return ClaimResult("error", detail="bad-response")
    result = data.get("result")
    if result not in CLAIM_RESULTS:
        result = "unavailable"
    cleared = tuple(_window_keys(data.get("cleared")))
    return ClaimResult(
        result=result,
        reason=_str_or_none(data.get("reason")),
        resets_left=_int_at_least_zero(data.get("resets_left")),
        cleared=cleared,
        weekly_resets_at=_str_or_none(data.get("weekly_resets_at")),
        cooldown_until=_str_or_none(data.get("cooldown_until")),
    )


# -- planning ----------------------------------------------------------------


@dataclass(frozen=True)
class AccountView:
    """What the planner knows about one account this tick.

    ``usage`` is the decision value (a usage dict, or None/sentinel when
    unknown). ``threshold``/``weekly_threshold`` are the account's own switch
    lines, the ones the engine judges it by. ``can_claim``: the planner may
    spend this account's resets now (rotation-eligible, OAuth, no claim still
    settling); an account that cannot claim still counts when asking whether
    switching could help instead. ``settling``: a claim on it has not yet been
    followed by a fresh reading, so ``usage`` may show pre-claim numbers.
    """

    num: str
    usage: object
    threshold: float
    weekly_threshold: float | None = None
    can_claim: bool = True
    settling: bool = False


@dataclass(frozen=True)
class ResetPlan:
    """One claim the planner wants, with the numbers that justified it."""

    account: str
    grant_id: str
    grant_kind: str
    reason: str  # "on-limit" | "last-resort" | "expiring"
    window: str  # the cleared window that motivated it, e.g. "Fable"
    pct: float
    window_resets_at: str | None
    hours_left: float | None
    grant_ends_at: str | None
    projected: dict[str, float] = field(default_factory=dict)
    scope: str | None = None  # grant_scope of the grant

    @property
    def noun(self) -> str:
        """"Fable reset", "weekly reset", ..."""
        return scope_noun(self.scope, self.grant_kind)

    @property
    def for_model_window(self) -> bool:
        """Whether the motivating window is a per-model one (not 5h/7d)."""
        return self.window not in ("5h", "7d")

    def to_json(self) -> dict:
        return {
            "account": self.account,
            "grantId": self.grant_id,
            "grantKind": self.grant_kind,
            "scope": self.scope,
            "reason": self.reason,
            "window": self.window,
            "pct": self.pct,
            "windowResetsAt": self.window_resets_at,
            "hoursLeft": None if self.hours_left is None else round(self.hours_left, 1),
            "grantEndsAt": self.grant_ends_at,
            "projected": self.projected,
        }


@dataclass(frozen=True)
class _Candidate:
    grant: dict
    window: str
    pct: float
    resets_at: str | None
    hours_left: float | None
    expiring: bool
    trigger: float
    projected: dict[str, float]
    rank: tuple
    scope: str = "unknown"

    @property
    def breadth(self) -> int:
        return SCOPE_BREADTH[self.scope]

    @property
    def ends(self) -> float:
        return self.rank[2]


def _windows(usage: dict) -> list[tuple[str, float, str | None]]:
    from ai_quota_swap.oauth import relevant_windows

    # A header-probe (setup-token) reading answers no "all models" request,
    # but the engine still reads its 5h/7d windows: fall back to those so
    # such an account still counts as somewhere to switch.
    return relevant_windows(usage, ("all",)) or relevant_windows(usage, ())


def _expiry_runway_s(grants: Sequence[dict], grant: dict, days: float) -> float:
    """Runway kept before ``grant`` expires: ``days`` per unused reset that
    expires no later than it. Three resets expiring together open the guard
    three runways early, so they can be spent one window apart instead of
    racing a single last week."""
    ends = parse_ts(grant.get("ends_at"))
    if ends is None:
        return 0.0
    due = sum(
        g["resets_left"]
        for g in grants
        if (t := parse_ts(g.get("ends_at"))) is not None and t <= ends
    )
    return max(1, due) * days * 86400.0


def _evaluate(view: AccountView, settings, now: float) -> list[_Candidate]:
    """Every grant this account could usefully spend now, best first.

    Useful means: a window the grant is *for* (:func:`primary_window_keys`:
    the model window of a model reset, 7d for a weekly reset, 5h or 7d for a
    full one; ``settings.weekly_for_model_limit`` adds a weekly or full
    reset's model windows) is at/over the trigger line (``settings.threshold``,
    or ``expiry_threshold`` for a grant inside its expiry runway), that window
    is not about to refill on its own (``min_hours_left``), and after the
    claim no window of the account would still sit at/over
    ``settings.threshold``: a Fable reset on an account whose all-models
    weekly window stays at 98% buys nothing, and neither does a weekly reset
    that leaves the Fable window it does not list at 100%. The projection
    zeroes exactly the windows the grant clears; a grant that
    ``gives_back_weekly`` is credited with the cleared model's share of the
    weekly window (``share_of_weekly``) when the response reported one, and
    with nothing otherwise. A grant of unknown scope, or one that lists a
    window qswap does not know, is never a candidate.

    Best first: narrowest scope, then the server's next pick, then the
    soonest to expire, then the fewest windows cleared.
    """
    usage = view.usage
    if not isinstance(usage, dict):
        return []
    status = usage.get("resets")
    if not isinstance(status, dict) or not status.get("eligible"):
        return []
    cooldown = parse_ts(status.get("cooldown_until"))
    if cooldown is not None and cooldown > now:
        return []
    windows = _windows(usage)
    by_label = {label.lower(): (label, pct, resets_at) for label, pct, resets_at in windows}
    scoped_share = {
        str(s.get("name", "")).lower(): s.get("share_of_weekly")
        for s in usage.get("scoped") or ()
        if isinstance(s, dict)
    }
    grants = unused_grants(status, now)
    next_id = status.get("next_grant_id")
    widen = bool(getattr(settings, "weekly_for_model_limit", False))
    out: list[_Candidate] = []
    for g in grants:
        if grant_unusable_reason(g, now) is not None:
            continue
        scope = grant_scope(g)
        if scope == "unknown" or g.get("clears_unknown"):
            continue  # what it would clear is not fully known: never automatic
        cleared_keys = [k for k in g["clears"] if WINDOW_LABELS.get(k)]
        cleared = {WINDOW_LABELS[k].lower() for k in cleared_keys}
        if not cleared:
            continue
        # The windows that may justify spending it: what it is for. A weekly
        # or full reset clears all weekly usage, so a per-model limit alone
        # is no reason to spend one unless the user said so.
        reason_keys = set(primary_window_keys(g, scope))
        if widen and scope in BROAD_SCOPES:
            reason_keys |= set(cleared_keys)
        expiring = False
        ends = parse_ts(g.get("ends_at"))
        if ends is not None and settings.expiry_days > 0:
            expiring = ends - now <= _expiry_runway_s(grants, g, settings.expiry_days)
        trigger = min(settings.threshold, settings.expiry_threshold) if expiring else settings.threshold
        # The motivating window: the fullest window it is for that is at the
        # trigger AND not about to refill on its own (min_hours_left) — judged
        # per window, so a full grant whose 5h window refills in 3h can still
        # be justified by its 7d window. Under the expiry guard the trigger is
        # expiry_threshold, still measured on those windows: the all-models
        # weekly for a weekly reset, the model window for a model reset.
        best: tuple[str, float, str | None, float | None] | None = None
        for key in cleared_keys:
            if key not in reason_keys:
                continue
            label = WINDOW_LABELS[key]
            window = by_label.get(label.lower())
            server_pct = g["percent_used"].get(key)
            pct = server_pct if server_pct is not None else (window[1] if window else None)
            if pct is None or pct < trigger:
                continue
            resets_at = window[2] if window else None
            if resets_at is None and key != "five_hour":
                resets_at = status.get("weekly_resets_at")
            reset_ts = parse_ts(resets_at)
            hours = None if reset_ts is None else (reset_ts - now) / 3600.0
            if hours is not None and hours < settings.min_hours_left:
                continue  # refills on its own soon: the reset would buy little
            if best is None or pct > best[1]:
                best = (window[0] if window else label, float(pct), resets_at, hours)
        if best is None:
            continue
        hours_left = best[3]
        give_back = 0.0
        if g.get("gives_back_weekly") and "7d" not in cleared:
            for label in cleared:
                share = scoped_share.get(label)
                if isinstance(share, (int, float)) and not isinstance(share, bool):
                    give_back += float(share)
        projected: dict[str, float] = {}
        useful = True
        for label, pct, _ in windows:
            if label.lower() in cleared:
                after = 0.0
            elif label == "7d" and give_back:
                after = max(0.0, pct - give_back)
            else:
                after = pct
            projected[label] = after
            if after >= settings.threshold:
                useful = False
        if not useful:
            continue
        # Narrowest scope first (a Fable reset before a weekly one that would
        # also clear the window). Claude Code claims whichever grant of the
        # chosen kind is first, not only next_grant_id, so the server's pick
        # orders grants of one breadth rather than overriding scope.
        rank = (
            SCOPE_BREADTH[scope],
            g["id"] != next_id,  # the server's own next pick first
            ends if ends is not None else math.inf,  # then the soonest to expire
            len(g["clears"]),  # then the fewest windows that still helps
        )
        out.append(
            _Candidate(
                g, best[0], best[1], best[2], hours_left, expiring, trigger, projected,
                rank, scope,
            )
        )
    out.sort(key=lambda c: c.rank)
    return out


def _lands(view: AccountView, projected: Mapping[str, float], watched: tuple[str, ...]) -> bool:
    """Whether the engine would switch onto an account with these windows:
    every window it watches below the account's own switch line."""
    from ai_quota_swap.lines import window_line

    watched_lower = {m.lower() for m in watched}
    seen = False
    for label, pct in projected.items():
        if label not in ("5h", "7d") and "all" not in watched_lower and label.lower() not in watched_lower:
            continue
        seen = True
        if pct >= window_line(label, view.threshold, view.weekly_threshold):
            return False
    return seen


def _engine_watches(label: str, watched: tuple[str, ...]) -> bool:
    lowered = {m.lower() for m in watched}
    return label in ("5h", "7d") or "all" in lowered or label.lower() in lowered


def _someone_else_lands(
    views: Sequence[AccountView], current: str, watched: tuple[str, ...]
) -> bool:
    """Whether the engine has another account to land on right now (a
    settling peer counts: its refill may well have landed)."""
    for view in views:
        if view.num == current:
            continue
        if view.settling:
            return True
        if isinstance(view.usage, dict):
            now_windows = {label: pct for label, pct, _ in _windows(view.usage)}
            if now_windows and _lands(view, now_windows, watched):
                return True
    return False


def _engine_would_leave(
    view: AccountView,
    cand: "_Candidate",
    views: Sequence[AccountView],
    current: str,
    watched: tuple[str, ...],
) -> bool:
    """After this refill, would the engine still switch off the active
    account? Then the reset buys nothing now: keep it banked."""
    return not _lands(view, cand.projected, watched) and _someone_else_lands(
        views, current, watched
    )


def _switch_could_help(
    views: Sequence[AccountView], current: str, window: str, watched: tuple[str, ...]
) -> bool:
    """Could the engine relieve the active account's ``window`` by switching?

    Only for a window the engine watches (5h/7d, or a model named in
    ``autoswitch.model``) — a Fable limit the engine ignores is never
    switched away from, so a reset is the only remedy. And only when some
    other account is below every watched line, which is the engine's own
    landing rule. Unknown usage is no room: the engine never lands on it. A
    peer still settling a claim counts as room: its reading predates a
    refill that may well have landed, and spending a second reset on that
    stale picture is the costly mistake.
    """
    if not _engine_watches(window, watched):
        return False
    return _someone_else_lands(views, current, watched)


def _choose(
    cands: list[tuple[AccountView, _Candidate]],
    current: str | None,
    *,
    expiring: bool = False,
):
    """Active account first (no switch needed), then the narrowest scope (a
    Fable reset before a weekly one, whatever their windows' clocks say),
    then the most time left in the refilled window (the most of the refill
    that can actually be used). For the expiry guard (``expiring``) the
    soonest deadline comes before scope: a reset about to be lost is spent
    before a narrower one that will keep."""
    def key(item):
        view, cand = item
        hours = cand.hours_left if cand.hours_left is not None else -1.0
        if expiring:
            return (view.num != current, cand.ends, cand.breadth, -hours, cand.rank, view.num)
        return (view.num != current, cand.breadth, -hours, cand.rank, view.num)

    return min(cands, key=key) if cands else None


def plan_reset(
    views: Sequence[AccountView],
    *,
    current: str | None,
    settings,
    watched: tuple[str, ...] = (),
    now: float,
) -> ResetPlan | None:
    """The one reset worth claiming now, or None. Pure.

    ``settings`` is a :class:`ai_quota_swap.settings.ResetSettings`; ``watched``
    the engine's ``autoswitch.model`` names. Policy (``settings.auto``):

    * ``off``: never.
    * ``active``: the account in use reached ``threshold`` on a window one
      of its own grants clears: spend that grant rather than move, whether
      or not another account has room.
    * ``on-limit``: any rotation account whose cleared window reached
      ``threshold`` with ``min_hours_left`` to go, if the claim clears it.
    * ``last-resort``: only when the active account is at its reset line and
      switching cannot relieve it — no other account has room on that window
      (or the engine does not watch that window at all). The active account's
      own grant is preferred; failing that, an account that would become a
      switch target once refilled.

    Every auto mode also spends grants inside their expiry runway (see
    :func:`_expiry_runway_s`) from ``expiry_threshold``, so banked resets are
    not lost to their deadline.

    Kinds (:func:`grant_scope`): a reset is only spent for a window it is
    for, the narrowest that does the job first, and a weekly or full reset
    only for an all-usage window (5h or 7d) unless
    ``settings.weekly_for_model_limit``; see :func:`_evaluate`.
    """
    if settings.auto not in ("active", "on-limit", "last-resort"):
        return None
    evaluated: list[tuple[AccountView, _Candidate]] = []
    for view in views:
        if not view.can_claim:
            continue
        for cand in _evaluate(view, settings, now):
            evaluated.append((view, cand))
    if not evaluated:
        return None

    def plan(view: AccountView, cand: _Candidate, reason: str) -> ResetPlan:
        return ResetPlan(
            account=view.num,
            grant_id=cand.grant["id"],
            grant_kind=grant_kind(cand.grant),
            reason=reason,
            window=cand.window,
            pct=cand.pct,
            window_resets_at=cand.resets_at,
            hours_left=cand.hours_left,
            grant_ends_at=cand.grant.get("ends_at"),
            projected=cand.projected,
            scope=cand.scope,
        )

    # The normal trigger first: a grant inside its expiry runway that is also
    # at the full line is simply on-limit.
    at_line = [(v, c) for v, c in evaluated if c.pct >= settings.threshold]
    if settings.auto == "active":
        own = [
            (v, c) for v, c in at_line
            if v.num == current and not _engine_would_leave(v, c, views, current, watched)
        ]
        chosen = _choose(own, current)
        if chosen is not None:
            return plan(*chosen, "active-limit")
    elif settings.auto == "on-limit":
        chosen = _choose(at_line, current)
        if chosen is not None:
            return plan(*chosen, "on-limit")
    elif current is not None:
        active = [(v, c) for v, c in at_line if v.num == current]
        stuck = [
            (v, c) for v, c in active
            if not _switch_could_help(views, current, c.window, watched)
            and not _engine_would_leave(v, c, views, current, watched)
        ]
        chosen = _choose(stuck, current)
        if chosen is not None:
            return plan(*chosen, "last-resort")
        # With a useful reset of its own the active account was only not
        # stuck (the engine can switch away): keep the reset banked. Without
        # one, a stuck active account is rescued by refilling another account
        # the engine can then land on.
        active_view = next((v for v in views if v.num == current), None)
        if (
            not active
            and active_view is not None
            and not active_view.settling  # its 100% may be a refill not yet re-read
            and _active_stuck_window(active_view, views, current, settings, watched)
        ):
            landing = [
                (v, c) for v, c in at_line
                if v.num != current and _lands(v, c.projected, watched)
            ]
            chosen = _choose(landing, current)
            if chosen is not None:
                return plan(*chosen, "last-resort")

    expiring = [(v, c) for v, c in evaluated if c.expiring]
    chosen = _choose(expiring, current, expiring=True)
    if chosen is not None:
        return plan(*chosen, "expiring")
    return None


def kept_by_scope(real: ResetPlan | None, widened: ResetPlan | None) -> ResetPlan | None:
    """The reset auto mode keeps only because of its kind, or None.

    ``real`` is the plan under the user's settings, ``widened`` the plan with
    ``weekly_for_model_limit`` on. When the real policy spends nothing and
    the widened one would spend a weekly or full reset on a per-model window,
    that is a reset kept because it is broader than the limit reached: worth
    one line of explanation (``qswap reset``)."""
    if real is not None or widened is None:
        return None
    if widened.scope not in BROAD_SCOPES or not widened.for_model_window:
        return None
    return widened


def kept_note(plan: ResetPlan) -> str:
    """One line on why auto mode keeps ``plan``'s broad reset (see
    :func:`kept_by_scope`)."""
    noun = plan.noun
    return (
        f"Auto keeps the {noun} on #{plan.account}: only {plan.window} is at its "
        f"limit ({plan.pct:.0f}%), and a {noun} {scope_phrase(plan.scope)} "
        "(resets.weeklyForModelLimit true would spend it)."
    )


def _over_the_line(usage: object, threshold: float) -> list[tuple[str, float]]:
    if not isinstance(usage, dict):
        return []
    return [(label, pct) for label, pct, _ in _windows(usage) if pct >= threshold]


def trade_note(grant: dict, usage: object, threshold: float) -> str | None:
    """What spending a broad ``grant`` by hand trades, when that is more than
    the limit reached: "This weekly reset clears all weekly usage; only Fable
    is at its limit (7d is at 31%)." None for a narrow reset, or when an
    all-usage window (or nothing) is at/over ``threshold``."""
    scope = grant_scope(grant)
    if scope not in BROAD_SCOPES:
        return None
    over = _over_the_line(usage, threshold)
    if not over or any(label in ("5h", "7d") or label.lower() == "rejected" for label, _ in over):
        return None
    models = [label for label, _ in over]
    one = len(models) == 1
    all_usage = [
        f"{label} is at {pct:.0f}%"
        for label, pct, _ in _windows(usage)  # type: ignore[arg-type]
        if label in ("5h", "7d")
    ]
    tail = f" ({', '.join(all_usage)})" if all_usage else ""
    return (
        f"This {reset_name(grant)} {clears_phrase(grant)}; only {_and(models)} "
        f"{'is' if one else 'are'} at {'its' if one else 'their'} limit{tail}."
    )


def manual_order(status: dict, usage: object, threshold: float, now: float) -> list[dict]:
    """Usable grants in the order ``qswap reset N`` picks from without
    ``--grant``: one that clears a window at/over ``threshold`` first, then
    the narrowest scope, the server's next pick, the soonest expiry. So with
    only Fable at its limit a Fable reset is offered before a weekly one."""
    windows = {
        label.lower(): pct for label, pct, _ in _windows(usage)
    } if isinstance(usage, dict) else {}
    next_id = status.get("next_grant_id")

    def helps(g: dict) -> bool:
        for key in g.get("clears") or ():
            label = WINDOW_LABELS.get(key)
            if not label:
                continue
            pct = (g.get("percent_used") or {}).get(key)
            if pct is None:
                pct = windows.get(label.lower())
            if pct is not None and pct >= threshold:
                return True
        return False

    usable = [g for g in status.get("grants") or () if grant_unusable_reason(g, now) is None]
    return sorted(usable, key=lambda g: (
        not helps(g),
        SCOPE_BREADTH[grant_scope(g)],
        g["id"] != next_id,
        parse_ts(g.get("ends_at")) or math.inf,
    ))


def _active_stuck_window(
    active: AccountView | None,
    views: Sequence[AccountView],
    current: str,
    settings,
    watched: tuple[str, ...],
) -> str | None:
    """The active account's WATCHED window at/over the reset line that no
    switch can relieve, or None. An unwatched window (a Fable limit without
    ``autoswitch.model``) never counts: the engine would not leave the active
    account for it, so refilling another account there would buy nothing."""
    if active is None or not isinstance(active.usage, dict):
        return None
    for label, pct, _ in _windows(active.usage):
        if (
            pct >= settings.threshold
            and _engine_watches(label, watched)
            and not _switch_could_help(views, current, label, watched)
        ):
            return label
    return None


def views_from(
    usage: Mapping[str, object],
    *,
    claimable: Iterable[str],
    thresholds: Mapping[str, float],
    weekly_threshold: float | None,
    accounts: Iterable[str],
    settling: Iterable[str] = (),
) -> list[AccountView]:
    """AccountViews for ``accounts`` (rotation order); only ``claimable`` ones
    may spend, and ``settling`` ones carry a claim not yet re-read."""
    allowed = set(claimable)
    unsettled = set(settling)
    return [
        AccountView(
            num=num,
            usage=usage.get(num),
            threshold=thresholds[num],
            weekly_threshold=weekly_threshold,
            can_claim=num in allowed and num not in unsettled,
            settling=num in unsettled,
        )
        for num in accounts
    ]
