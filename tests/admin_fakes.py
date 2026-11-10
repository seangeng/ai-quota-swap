"""Fakes for the Admin API tests: a recording transport and synthetic keys.

No real key appears anywhere: every key is a prefix plus filler built here.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlsplit

from ai_quota_swap.admin_api import Response

# An Admin key's shape, with filler (never a real key).
ADMIN_KEY = "sk-ant-admin01-" + "Zq7" * 12 + "-" + "Wx9" * 10
OTHER_ADMIN_KEY = "sk-ant-admin01-" + "Pm4" * 12 + "-" + "Kd2" * 10
# Managed API keys (filler). Their hints are what the listing would show.
KEY_A = "sk-ant-api03-" + "R2Dk" + "a1b2c3d4e5" * 8 + "igAA"
KEY_B = "sk-ant-api03-" + "M7Qp" + "z9y8x7w6v5" * 8 + "Lm3Q"
KEY_C = "sk-ant-api03-" + "T5Ve" + "q1w2e3r4t5" * 8 + "Hh8R"


def hint_for(key: str) -> str:
    """The listing's partial hint for ``key``: family prefix plus 3, ``...``, last 4."""
    return key[:16] + "..." + key[-4:]


def key_item(key_id: str, name: str, key: str | None, status: str = "active") -> dict:
    return {
        "id": key_id,
        "type": "api_key",
        "name": name,
        "partial_key_hint": hint_for(key) if key else None,
        "status": status,
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": None,
        "expires_at": None,
        "principal": None,
        "scope": {"type": "workspace", "workspace_id": "wrkspc_01Fake"},
    }


def usage_result(
    key_id: str | None, model: str, *, inp=0, out=0, read=0, w5=0, w1=0, speed=None,
    workspace=None,
) -> dict:
    return {
        "api_key_id": key_id,
        "model": model,
        "speed": speed,
        "workspace_id": workspace,
        "uncached_input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
        "server_tool_use": {"web_search_requests": 0},
    }


def bucket(day: str, results: list[dict]) -> dict:
    """A daily bucket starting at ``day`` (``YYYY-MM-DD``)."""
    from datetime import date, timedelta

    d = date.fromisoformat(day)
    return {
        "starting_at": f"{d.isoformat()}T00:00:00Z",
        "ending_at": f"{(d + timedelta(days=1)).isoformat()}T00:00:00Z",
        "results": results,
    }


def json_response(doc: object, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status, {k.lower(): v for k, v in (headers or {}).items()},
                    json.dumps(doc).encode())


def error_response(status: int, kind: str = "error", message: str = "nope",
                   headers: dict | None = None) -> Response:
    return json_response({"type": "error", "error": {"type": kind, "message": message}},
                         status, headers)


class FakeAnthropic:
    """Stands in for ``admin_api.send``: records each request and answers from
    ``routes[path]``, a list of answers consumed in order (the last repeats)
    or a callable ``(query) -> Response``. An answer may be an exception to
    raise."""

    def __init__(self, routes: dict | None = None):
        self.routes: dict = dict(routes or {})
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, timeout):
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        self.calls.append({
            "method": method, "url": url, "path": parts.path, "query": query,
            "headers": dict(headers), "timeout": timeout,
            "scheme": parts.scheme, "host": parts.netloc,
        })
        route = self.routes.get(parts.path)
        if route is None:
            return error_response(404, "not_found_error", f"no route {parts.path}")
        if callable(route):
            answer = route(query)
        else:
            answer = route[0] if len(route) == 1 else route.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


def org_info(org_id: str = "11111111-2222-3333-4444-555555555555", name: str = "Acme Corp") -> dict:
    return {"id": org_id, "type": "organization", "name": name}


def keys_page(items: list[dict], has_more: bool = False) -> dict:
    return {
        "data": items,
        "has_more": has_more,
        "first_id": items[0]["id"] if items else None,
        "last_id": items[-1]["id"] if items else None,
    }


def report(buckets: list[dict], next_page: str | None = None) -> dict:
    return {"data": buckets, "has_more": next_page is not None, "next_page": next_page}
