"""Generate the settings tables in docs/settings.md from the code.

    uv run python scripts/settings_doc.py           # rewrite the generated part
    uv run python scripts/settings_doc.py --check   # exit 1 if it is out of date

Every setting, its default and its allowed values come from
``ai_quota_swap.settings.SETTING_SPECS``. The plain-words meaning and the
example value for each come from this file; a setting without them makes
``tests/test_docs.py`` fail, so a new setting cannot ship undocumented.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "settings.md"
BEGIN = "<!-- BEGIN GENERATED SETTINGS: scripts/settings_doc.py writes this part -->"
END = "<!-- END GENERATED SETTINGS -->"

SECTIONS = {
    "autoswitch": "Auto-switch (both providers)",
    "resets": "Usage resets (both providers)",
    "codex": "Codex",
    "run": "Sessions",
    "usage": "Usage readings",
    "ui": "Appearance",
    "updates": "Updates",
}

# What each setting does, in plain words. The allowed values are appended
# from the spec, so do not repeat them here.
MEANINGS: dict[str, str] = {
    "autoswitch.threshold": (
        "The switch line, in percent used. Auto-switch looks for another account "
        "once the active account's 5-hour window reaches it, or any window while "
        "`autoswitch.weeklyThreshold` is not set. Codex uses `codex.threshold` "
        "instead. `qswap threshold` sets it for one account."
    ),
    "autoswitch.weeklyThreshold": (
        "A separate switch line for the 7-day and per-model windows. Not set: "
        "`autoswitch.threshold` covers every window."
    ),
    "autoswitch.intervalSeconds": "Seconds between auto-switch checks.",
    "autoswitch.cooldownSeconds": (
        "The least time between two switches made before a limit. A switch away "
        "from an account at its limit does not wait for it."
    ),
    "autoswitch.hysteresisPct": (
        "How many points of headroom a target needs over the active account "
        "before a switch below the limit, so two accounts near the line do not "
        "trade places on every check."
    ),
    "autoswitch.strategy": (
        "How auto-switch picks the account to move to. "
        "[auto-switch.md](auto-switch.md) explains each."
    ),
    "autoswitch.includeApiKeyAccounts": (
        "Let auto-switch move to an API-key account when no subscription account "
        "has room. API keys are billed per token."
    ),
    "autoswitch.unhealthyTicks": (
        "Checks in a row on which the active account's usage cannot be read "
        "before auto-switch moves to another account."
    ),
    "autoswitch.model": (
        "Also switch when one of these models' weekly limits is reached: one "
        "name (`Fable`), several (`Fable,Opus`) or `all`, as the per-model rows "
        "of `qswap list` name them. Not set: only the 5-hour and 7-day windows count."
    ),
    "ui.theme": (
        "Colours of the dashboard and of command output. `auto` follows the "
        "terminal's background."
    ),
    "run.shareHistory": (
        "`qswap run` sessions read and write the one chat history in `~/.claude`. "
        "`false` gives each account its own; `--no-share-history` does that for "
        "one launch."
    ),
    "updates.check": (
        "Look up the newest release once a day and print a hint when there is "
        "one. `qswap upgrade` works either way."
    ),
    "usage.headerProbe": (
        "Read a setup-token account's 5-hour and 7-day usage from the rate-limit "
        "headers of a one-token Haiku request, since the usage endpoint does not "
        "answer setup-tokens. Each probe is a real request on that account."
    ),
    "resets.enabled": (
        "Read which usage resets (Claude) and reset credits (Codex) each account "
        "holds, along with its usage. `false` stops reading them."
    ),
    "resets.auto": (
        "When auto-switch spends a reset by itself. [resets.md](resets.md) "
        "explains each mode; `qswap reset N` works in every mode."
    ),
    "resets.threshold": "Spend a reset once a window it clears reaches this percent.",
    "resets.minHoursLeft": (
        "Keep a reset when the window it would clear refills on its own within "
        "this many hours."
    ),
    "resets.expiryDays": (
        "Days of runway per unused reset before it expires. Inside that runway, "
        "auto mode spends it early rather than lose it. `0` turns this off."
    ),
    "resets.expiryThreshold": (
        "Inside that runway, spend the reset once the window it is for reaches "
        "this percent."
    ),
    "resets.weeklyForModelLimit": (
        "Let auto mode spend a weekly or full reset when only a per-model limit, "
        "such as Fable's, is reached. `qswap reset N` is never held back by it."
    ),
    "codex.restartDaemon": (
        "After `qswap codex switch`, restart Codex's background server so new "
        "sessions use the new account. The restart interrupts work in progress on it."
    ),
    "codex.threshold": (
        "`qswap codex auto`'s switch line, in place of `autoswitch.threshold`. "
        "High by default because a Codex switch interrupts work in progress."
    ),
}

# The value each row's example command sets: valid, and not the default.
EXAMPLES: dict[str, str] = {
    "autoswitch.threshold": "85",
    "autoswitch.weeklyThreshold": "97",
    "autoswitch.intervalSeconds": "120",
    "autoswitch.cooldownSeconds": "600",
    "autoswitch.hysteresisPct": "5",
    "autoswitch.strategy": "weekly-first",
    "autoswitch.includeApiKeyAccounts": "true",
    "autoswitch.unhealthyTicks": "5",
    "autoswitch.model": "Fable",
    "ui.theme": "light",
    "run.shareHistory": "false",
    "updates.check": "false",
    "usage.headerProbe": "true",
    "resets.enabled": "false",
    "resets.auto": "off",
    "resets.threshold": "100",
    "resets.minHoursLeft": "12",
    "resets.expiryDays": "0",
    "resets.expiryThreshold": "70",
    "resets.weeklyForModelLimit": "true",
    "codex.restartDaemon": "false",
    "codex.threshold": "90",
}


def _values(spec) -> str:
    from ai_quota_swap.settings import format_setting_value

    if spec.kind in ("float", "int"):
        noun = "whole number" if spec.kind == "int" else "number"
        return f"A {noun} from {format_setting_value(spec.lo)} to {format_setting_value(spec.hi)}."
    if spec.kind == "choice":
        return "One of " + ", ".join(f"`{c}`" for c in spec.choices) + "."
    if spec.kind == "bool":
        return "`true` or `false`."
    return ""


def render() -> str:
    """The generated part of the page, markers included."""
    from ai_quota_swap.settings import SETTING_SPECS, format_setting_value

    missing = [k for k in SETTING_SPECS if k not in MEANINGS or k not in EXAMPLES]
    if missing:
        raise SystemExit(f"settings without a meaning or example in {__file__}: {', '.join(missing)}")
    sections = list(SECTIONS)
    sections += [s for s in dict.fromkeys(sp.section for sp in SETTING_SPECS.values()) if s not in SECTIONS]
    out = [BEGIN, ""]
    for section in sections:
        specs = [sp for sp in SETTING_SPECS.values() if sp.section == section]
        if not specs:
            continue
        out += [f"### {SECTIONS.get(section, section)}", ""]
        out += ["| Setting | Default | Meaning | Change it |", "|---|---|---|---|"]
        for sp in specs:
            meaning = MEANINGS[sp.dotted]
            values = _values(sp)
            if values:
                meaning = f"{meaning} {values}"
            command = f"`qswap config set {sp.dotted} {EXAMPLES[sp.dotted]}`"
            out.append(
                f"| `{sp.dotted}` | `{format_setting_value(sp.default)}` | {meaning} | {command} |"
            )
        out.append("")
    out.append(END)
    return "\n".join(out)


def current(text: str) -> str | None:
    """The generated part as it stands in ``text``, or None without markers."""
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        return None
    return text[start:end + len(END)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="settings_doc.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="exit 1 if docs/settings.md is out of date")
    args = p.parse_args(argv)
    sys.path.insert(0, str(REPO / "src"))
    text = PAGE.read_text(encoding="utf-8")
    old = current(text)
    if old is None:
        print(f"{PAGE}: the markers are missing", file=sys.stderr)
        return 1
    new = render()
    if old == new:
        return 0
    if args.check:
        print(f"{PAGE} is out of date; run: uv run python scripts/settings_doc.py", file=sys.stderr)
        return 1
    PAGE.write_text(text.replace(old, new), encoding="utf-8")
    print(f"wrote {PAGE.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
