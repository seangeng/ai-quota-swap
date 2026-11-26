"""Explicit checks of a Console key. Only --inference sends a billable message."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from ai_quota_swap.fsutil import atomic_write_text
from ai_quota_swap.oauth import USAGE_PROBE_API_VERSION, USAGE_PROBE_MODEL, USAGE_PROBE_URL

# Closed vocabulary: provider response text and generated content never enter
# the cache, the log, the menu or JSON output.
LABELS = {
    "accepted": "message accepted",
    "counted": "token count accepted (billing untested)",
    "monthly-limit": "organization monthly API limit reached",
    "low-credit": "credit balance too low",
    "billing": "billing issue",
    "auth": "key rejected",
    "permission": "permission denied",
    "model": "probe model unavailable",
    "rate-limit": "rate limited",
    "server": "Anthropic service error",
    "network": "connection failed",
    "unreadable": "unrecognized API response",
    "refused": "request refused",
}
MAX_AGE = 24 * 3600


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _outcome(status: int, payload: object, inference: bool) -> str:
    data = payload if isinstance(payload, dict) else {}
    if status == 200:
        if inference and data.get("type") == "message" and data.get("role") == "assistant":
            return "accepted"
        tokens = data.get("input_tokens")
        if not inference and type(tokens) is int and tokens >= 0:
            return "counted"
        return "unreadable"
    err = data.get("error")
    err = err if isinstance(err, dict) else {}
    message = str(err.get("message", "")).lower()
    details = err.get("details")
    error_code = details.get("error_code") if isinstance(details, dict) else None
    if status in (400, 402, 403, 429):
        if "credit balance is too low" in message:
            return "low-credit"
        if error_code == "enforced_spend_limit_reached" or "monthly api usage threshold" in message:
            return "monthly-limit"
        if status == 402 or err.get("type") == "billing_error":
            return "billing"
    if status >= 500:
        return "server"
    return {401: "auth", 403: "permission", 404: "model", 429: "rate-limit"}.get(status, "refused")


def probe(key: str, *, inference: bool) -> dict:
    body = {"model": USAGE_PROBE_MODEL, "messages": [{"role": "user", "content": "hi"}]}
    url = USAGE_PROBE_URL
    if inference:
        body["max_tokens"] = 1
    else:
        url += "/count_tokens"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "anthropic-version": USAGE_PROBE_API_VERSION,
    })
    request.add_unredirected_header("x-api-key", key)
    status = None
    try:
        try:
            response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=12)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            status = response.code
            raw = response.read(32_769)
        try:
            payload = json.loads(raw) if len(raw) <= 32_768 else None
        except ValueError:
            payload = None
        outcome = _outcome(status, payload, inference)
    except (OSError, http.client.HTTPException):
        outcome = "network"
    return {"outcome": outcome, "httpStatus": status, "model": USAGE_PROBE_MODEL,
            "checkedAt": time.time()}


def _path(root: Path, key: str) -> Path:
    from ai_quota_swap.api_spend import key_fingerprint

    return root / "cache" / "key_checks" / (key_fingerprint(key) + ".json")


def save_inference(root: Path, key: str, result: dict) -> None:
    path = _path(root, key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(result), follow_symlinks=False)


def recent_inference(root: Path, key: str, now: float) -> dict | None:
    if not key:
        return None
    try:
        result = json.loads(_path(root, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict):
        return None
    at, outcome, status = (result.get(k) for k in ("checkedAt", "outcome", "httpStatus"))
    if (type(at) not in (int, float) or not 0 <= now - at < MAX_AGE
            or not isinstance(outcome, str) or outcome not in LABELS or outcome == "counted"
            or result.get("model") != USAGE_PROBE_MODEL
            or status is not None and type(status) is not int):
        return None
    return {"outcome": outcome, "checkedAt": at, "httpStatus": status, "model": USAGE_PROBE_MODEL}
