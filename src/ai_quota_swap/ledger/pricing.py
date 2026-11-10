"""API prices, for an estimate of what the tokens would have cost.

Every figure is US dollars per million tokens, copied from the vendors'
published pricing pages on the date below. Nothing here was written from
memory; when a vendor changes a price, update the table from the page and
the date with it. Costs are computed when a report is printed, so a corrected
table applies to old usage too.

Sources (fetched 2026-11-10):

- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
  ("Model pricing", "Fast mode pricing"). Model ids follow
  https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions
  and the models overview page.
- OpenAI: https://developers.openai.com/api/docs/pricing (redirected from
  https://platform.openai.com/docs/pricing): "Flagship models", Standard
  tier, short context; "Specialized models" for gpt-5.3-codex.

What the table does not model (the report says these are estimates):

- Long-context and prompt-length tiers use the base tier: Claude Haiku 5.5
  prompts over 100,000 tokens, and OpenAI requests over 272K input tokens.
- Batch, Flex and data-residency (US-only inference, regional endpoints)
  pricing. Claude Code and Codex do not use the Batch API.
- OpenAI Fast (priority) processing: Codex rollouts do not record the tier.

Matching: an entry matches a model id that equals it, or that adds a dated
snapshot suffix (``claude-opus-4-20250514`` matches ``claude-opus-4``;
``gpt-5-2025-08-07`` matches ``gpt-5``). The longest matching entry wins. An
entry never matches a different version or variant (``claude-opus-4`` does
not price ``claude-opus-4-9``; ``gpt-5.3-codex`` does not price
``gpt-5.3-codex-spark``), so a model the table does not know shows ``n/a``
instead of a wrong figure. Entries in the user's ``pricing.json`` may end in
``*`` to match any id with that prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PRICES_FETCHED = "2026-11-10"
ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
OVERRIDE_FILENAME = "pricing.json"

# Suffix the Claude reader appends to a model id for a fast-mode request.
FAST_SUFFIX = "/fast"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float

    def cost(
        self,
        input: int,
        output: int,
        cache_read: int,
        cache_write_5m: int,
        cache_write_1h: int,
    ) -> float:
        return (
            input * self.input
            + output * self.output
            + cache_read * self.cache_read
            + cache_write_5m * self.cache_write_5m
            + cache_write_1h * self.cache_write_1h
        ) / 1_000_000


def _a(inp: float, w5: float, w1: float, read: float, out: float) -> Price:
    """An Anthropic row, in the page's column order: base input, 5m cache
    writes, 1h cache writes, cache hits and refreshes, output."""
    return Price(input=inp, output=out, cache_read=read, cache_write_5m=w5, cache_write_1h=w1)


def _o(
    inp: float,
    cached: float | None,
    out: float,
    w30m: float | None = None,
    w12h: float | None = None,
) -> Price:
    """An OpenAI row: input, cached input, output, and the 30-minute and
    12-hour cache-write prices where the page lists them. A price the page
    shows as "-" is charged as plain input here."""
    return Price(
        input=inp,
        output=out,
        cache_read=inp if cached is None else cached,
        cache_write_5m=inp if w30m is None else w30m,
        cache_write_1h=inp if w12h is None else w12h,
    )


# Anthropic "Model pricing" table, 2026-11-10.
ANTHROPIC: dict[str, Price] = {
    "claude-fable-5-2": _a(8, 10, 16, 0.20, 40),
    "claude-mythos-5-2": _a(8, 10, 16, 0.20, 40),
    "claude-fable-5-1": _a(10, 12.50, 20, 0.25, 50),
    "claude-mythos-5-1": _a(10, 12.50, 20, 0.25, 50),
    "claude-fable-5": _a(10, 12.50, 20, 1, 50),
    "claude-mythos-5": _a(10, 12.50, 20, 1, 50),
    "claude-opus-5-5": _a(4, 5, 8, 0.20, 20),
    "claude-opus-5": _a(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-8": _a(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-7": _a(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-6": _a(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-5": _a(5, 6.25, 10, 0.50, 25),
    "claude-opus-4-1": _a(15, 18.75, 30, 1.50, 75),
    "claude-opus-4": _a(15, 18.75, 30, 1.50, 75),
    "claude-sonnet-5-5": _a(2, 2.50, 4, 0.10, 10),
    "claude-sonnet-5": _a(2, 2.50, 4, 0.20, 10),
    "claude-sonnet-4-6": _a(3, 3.75, 6, 0.30, 15),
    "claude-sonnet-4-5": _a(3, 3.75, 6, 0.30, 15),
    "claude-sonnet-4": _a(3, 3.75, 6, 0.30, 15),
    # Base tier: prompts up to 100,000 tokens (see the module docstring).
    "claude-haiku-5-5": _a(0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5": _a(1, 1.25, 2, 0.10, 5),
}

# Anthropic "Fast mode pricing", 2026-11-10: the page lists input and output;
# cache prices apply the page's prompt-caching multipliers on top of the fast
# input price (5m write 1.25x, 1h write 2x, read 0.025x on Fable/Mythos 5.2,
# 0.05x on Opus 5.5, 0.1x otherwise).
ANTHROPIC_FAST: dict[str, Price] = {
    "claude-fable-5-2": _a(16, 20, 32, 0.40, 80),
    "claude-mythos-5-2": _a(16, 20, 32, 0.40, 80),
    "claude-opus-5-5": _a(8, 10, 16, 0.40, 40),
    "claude-opus-5": _a(10, 12.50, 20, 1, 50),
    "claude-opus-4-8": _a(10, 12.50, 20, 1, 50),
}

# OpenAI Standard tier, short context, 2026-11-10.
OPENAI: dict[str, Price] = {
    "gpt-6-astra": _o(10, 1, 50, 12.50, 25),
    "gpt-6.1-sol": _o(2, 0.10, 10, 2.50, 5),
    "gpt-6.1-terra": _o(1, 0.05, 5, 1.25, 2.50),
    "gpt-6-luna": _o(0.10, 0.01, 0.50, 0.125, 0.25),
    "gpt-6-sol": _o(2, 0.20, 10, 2.50, 5),
    "gpt-5.6-sol": _o(4, 0.40, 20, 5, 10),
    "gpt-5.6-terra": _o(2, 0.20, 12, 2.50, 5),
    "gpt-5.6-luna": _o(0.20, 0.02, 1.20, 0.25, 0.50),
    "gpt-5.5": _o(5, 0.50, 30),
    "gpt-5.5-pro": _o(30, None, 180),
    "gpt-5.4": _o(2.50, 0.25, 15),
    "gpt-5.4-mini": _o(0.75, 0.075, 4.50),
    "gpt-5.4-nano": _o(0.20, 0.02, 1.25),
    "gpt-5.4-pro": _o(30, None, 180),
    "gpt-5.3-codex": _o(1.75, 0.175, 14),
    "gpt-5.2": _o(1.75, 0.175, 14),
    "gpt-5.2-pro": _o(21, None, 168),
    "gpt-5.1": _o(1.25, 0.125, 10),
    "gpt-5": _o(1.25, 0.125, 10),
    "gpt-5-mini": _o(0.25, 0.025, 2),
    "gpt-5-nano": _o(0.05, 0.005, 0.40),
    "gpt-5-pro": _o(15, None, 120),
    "gpt-4.1": _o(2, 0.50, 8),
    "gpt-4.1-mini": _o(0.40, 0.10, 1.60),
    "gpt-4o": _o(2.50, 1.25, 10),
    "gpt-4o-mini": _o(0.15, 0.075, 0.60),
    "o3-pro": _o(20, None, 80),
    "o3": _o(2, 0.50, 8),
}

BUNDLED: dict[str, Price] = {**ANTHROPIC, **OPENAI}

_SNAPSHOT = re.compile(r"^[-@]\d{4}")
_BEDROCK_SUFFIX = re.compile(r"-v\d+(:\d+)?$")
_PRICE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def normalize_model(model: str) -> str:
    """Lower-case, and strip cloud prefixes and context-window tags.

    ``us.anthropic.claude-opus-4-5-20251101-v1:0`` -> ``claude-opus-4-5-20251101``;
    ``claude-opus-5-5[1m]`` -> ``claude-opus-5-5``.
    """
    m = model.strip().lower()
    if "anthropic." in m:
        m = m.split("anthropic.", 1)[1]
    m = re.sub(r"\[[^\]]*\]$", "", m)
    m = _BEDROCK_SUFFIX.sub("", m)
    return m


def _matches(model: str, key: str) -> bool:
    if key.endswith("*"):
        return model.startswith(key[:-1])
    if model == key:
        return True
    return model.startswith(key) and bool(_SNAPSHOT.match(model[len(key) :]))


class PriceTable:
    """The bundled table plus the user's overrides."""

    def __init__(
        self,
        prices: dict[str, Price | None] | None = None,
        fast: dict[str, Price] | None = None,
        warnings: list[str] | None = None,
        override_path: Path | None = None,
    ):
        self.prices: dict[str, Price | None] = dict(BUNDLED if prices is None else prices)
        self.fast: dict[str, Price] = dict(ANTHROPIC_FAST if fast is None else fast)
        self.warnings: list[str] = list(warnings or [])
        self.override_path = override_path
        self.overridden: set[str] = set()
        self._cache: dict[str, tuple[str, Price] | None] = {}

    @classmethod
    def load(cls, data_root: Path | None) -> PriceTable:
        """The bundled table with ``<data_root>/pricing.json`` applied, if present."""
        table = cls()
        if data_root is None:
            return table
        path = Path(data_root) / OVERRIDE_FILENAME
        table.override_path = path
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return table
        except OSError as e:
            table.warnings.append(f"{path}: not read ({e.strerror or e})")
            return table
        table.apply_overrides(text, str(path))
        return table

    def apply_overrides(self, text: str, label: str = OVERRIDE_FILENAME) -> None:
        try:
            doc = json.loads(text)
        except ValueError as e:
            self.warnings.append(f"{label}: not valid JSON ({e}); using bundled prices")
            return
        if isinstance(doc, dict) and isinstance(doc.get("models"), dict):
            doc = doc["models"]
        if not isinstance(doc, dict):
            self.warnings.append(f"{label}: expected an object of model -> prices")
            return
        for raw_key, entry in doc.items():
            if not isinstance(raw_key, str) or raw_key.startswith("$"):
                continue  # "$comment" and the like
            key = raw_key.strip().lower()
            if not key:
                continue
            if entry is None:
                self.prices[key] = None  # explicitly unpriced
                self.overridden.add(key)
                continue
            price = _price_from_json(entry)
            if price is None:
                self.warnings.append(
                    f"{label}: {raw_key!r} ignored (needs numeric 'input' and 'output', "
                    "in USD per million tokens)"
                )
                continue
            self.prices[key] = price
            self.overridden.add(key)
        self._cache.clear()

    def lookup(self, model: str) -> tuple[str, Price] | None:
        """``(matched entry, price)`` for a ledger model id, or None if unpriced."""
        if model in self._cache:
            return self._cache[model]
        fast = model.endswith(FAST_SUFFIX)
        base = normalize_model(model[: -len(FAST_SUFFIX)] if fast else model)
        result: tuple[str, Price] | None = None
        if fast:
            # A user entry for the exact "<model>/fast" id wins; otherwise the
            # fast table, never the standard price (that would undercount).
            user = self.prices.get(base + FAST_SUFFIX)
            if user is not None:
                result = (base + FAST_SUFFIX, user)
            else:
                result = self._best(base, self.fast)
        else:
            result = self._best(base, self.prices)
        self._cache[model] = result
        return result

    @staticmethod
    def _best(model: str, table: dict[str, Price | None]) -> tuple[str, Price] | None:
        best_key: str | None = None
        for key in table:
            if _matches(model, key) and (
                best_key is None or len(key.rstrip("*")) > len(best_key.rstrip("*"))
            ):
                best_key = key
        if best_key is None:
            return None
        price = table[best_key]
        return None if price is None else (best_key, price)


def _price_from_json(entry: object) -> Price | None:
    if not isinstance(entry, dict):
        return None
    vals: dict[str, float] = {}
    for f in _PRICE_FIELDS:
        v = entry.get(f)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return None
        vals[f] = float(v)
    if "input" not in vals or "output" not in vals:
        return None
    inp = vals["input"]
    return Price(
        input=inp,
        output=vals["output"],
        cache_read=vals.get("cache_read", inp),
        cache_write_5m=vals.get("cache_write_5m", inp),
        cache_write_1h=vals.get("cache_write_1h", vals.get("cache_write_5m", inp)),
    )
