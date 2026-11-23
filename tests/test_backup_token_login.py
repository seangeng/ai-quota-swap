"""``qswap login N`` while a backup token stands in for N
(docs/design/backup-token.md): the renewal says the token goes back to
standing by. Uses ``tests.test_login``'s stand-in for ``claude auth login``;
every value is made up."""

from __future__ import annotations

import pytest

from ai_quota_swap import login
from ai_quota_swap.switcher import ClaudeAccountSwitcher
from ai_quota_swap.usage_store import FetchRecord
from tests.test_backup_token import _seed_token
from tests.test_login import FakeClaude, _creds, _go_live, _seed, _target, platform  # noqa: F401

WORK = "maya@northwind.example.com"
ORG = "00000000-0000-4000-8000-000000000002"
DAY = 86400.0


@pytest.fixture
def fleet(temp_home, platform):  # noqa: F811
    """#1 personal, the live login; #2 work in its organization."""
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    _seed(s, 1, "maya@example.com", _creds("one"), alias="personal",
          org="00000000-0000-4000-8000-000000000001")
    _seed(s, 2, WORK, _creds("two-old", login_left=2 * DAY), alias="work", org=ORG)
    _go_live(s, 1, "maya@example.com", _creds("one"), org="00000000-0000-4000-8000-000000000001")
    return s


def _dead(s):
    s._usage_store.record({"2": FetchRecord(error="invalid_grant")}, {"2": (WORK, ORG)})


def test_a_renewal_sends_the_backup_back_to_standing_by(fleet):
    s = fleet
    _seed_token(s, 7, parent=2)
    _dead(s)
    assert s._standing_in_backup("2") == "7"
    renewal = login.renew(s, _target(s), runner=FakeClaude(WORK, org=ORG), interactive=False)
    assert "#7 setup-token-7, its backup, goes back to standing by." in renewal.notes
    assert s._standing_in_backup("2") is None
    assert s.backup_view(active=s.current_account_number())["7"].state == "standing-by"


def test_no_note_while_the_backup_stands_by(fleet):
    s = fleet
    _seed_token(s, 7, parent=2)
    renewal = login.renew(s, _target(s), runner=FakeClaude(WORK, org=ORG), interactive=False)
    assert not any("backup" in note for note in renewal.notes)


def test_a_renewal_while_the_backup_is_the_active_account(fleet):
    s = fleet
    _seed_token(s, 7, parent=2)
    _dead(s)
    token = s.read_account_credentials("7", "setup-token-7@token.local")
    _go_live(s, 7, "setup-token-7@token.local", token)
    assert s.current_account_number() == "7"
    renewal = login.renew(s, _target(s), runner=FakeClaude(WORK, org=ORG), interactive=False)
    # Only the saved login was renewed; the backup stays live until a switch.
    assert renewal.live is False
    assert (
        "#7 setup-token-7, its backup, is still in use; switch back with: qswap switch 2"
        in renewal.notes
    )
    assert s.current_account_number() == "7"
    assert s.backup_view(active="7")["7"].state == "in-use"
