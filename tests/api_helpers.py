"""Shared setup for the API-account tests: a seeded store and CLI drivers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from ai_quota_swap import cli
from ai_quota_swap.models import Platform
from ai_quota_swap.switcher import ClaudeAccountSwitcher


def pin_platform(monkeypatch, platform: Platform = Platform.LINUX) -> None:
    """Every switcher built from here on (the CLI builds its own) sees ``platform``."""
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))


def make_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    return s


def seed_oauth(s: ClaudeAccountSwitcher, num: int, email: str) -> None:
    s._write_account_credentials(
        str(num), email,
        json.dumps({"claudeAiOauth": {"accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}),
    )
    s._write_account_config(
        str(num), email,
        json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"}}),
    )
    data = s._get_sequence_data()
    data["accounts"][str(num)] = {
        "email": email, "uuid": f"uuid-{num}", "organizationUuid": "",
        "organizationName": "", "added": "2026-01-01T00:00:00Z",
    }
    if num not in data["sequence"]:
        data["sequence"].append(num)
        data["sequence"].sort()
    s._write_json(s.sequence_file, data)


def seed_api_key(s: ClaudeAccountSwitcher, num: int, key: str, email: str | None = None) -> str:
    s.add_account_from_token(key, email=email, slot=num, assume_yes=True)
    return (s._get_sequence_data() or {})["accounts"][str(num)]["email"]


def record(s: ClaudeAccountSwitcher, num: int) -> dict:
    return (s._get_sequence_data() or {})["accounts"][str(num)]


def set_record(s: ClaudeAccountSwitcher, num: int, **fields) -> None:
    data = s._get_sequence_data()
    data["accounts"][str(num)].update(fields)
    s._write_json(s.sequence_file, data)


def run_main(argv: list[str]) -> None:
    """A full ``qswap ...`` argv through ``main()``."""
    with patch.object(sys, "argv", ["qswap", *argv]), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("ai_quota_swap.update_check.check_for_update", return_value=None):
        cli.main()


def data_root() -> Path:
    from ai_quota_swap.paths import get_backup_root

    return get_backup_root()
