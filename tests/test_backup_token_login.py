"""``qswap login N`` while a backup token stands in for N
(docs/design/backup-token.md): the renewal says the token goes back to
standing by. Uses ``tests.test_login``'s fleet and its stand-in for
``claude auth login``; every value is made up."""

from __future__ import annotations

from ai_quota_swap import login
from ai_quota_swap.usage_store import FetchRecord
from tests.test_backup_token import _seed_token
from tests.test_login import FakeClaude, _target, fleet, platform  # noqa: F401  (fixtures)

WORK = "maya@northwind.example.com"


def test_a_renewal_sends_the_backup_back_to_standing_by(fleet):  # noqa: F811
    s = fleet
    _seed_token(s, 7, parent=2)
    s._usage_store.record({"2": FetchRecord(error="invalid_grant")}, {"2": (WORK, "")})
    assert s._standing_in_backup("2") == "7"
    renewal = login.renew(s, _target(s), runner=FakeClaude(WORK), interactive=False)
    assert "#7 setup-token-7, its backup, goes back to standing by." in renewal.notes
    assert s._standing_in_backup("2") is None
    assert s.backup_view(active=s.current_account_number())["7"].state == "standing-by"


def test_no_note_while_the_backup_stands_by(fleet):  # noqa: F811
    s = fleet
    _seed_token(s, 7, parent=2)
    renewal = login.renew(s, _target(s), runner=FakeClaude(WORK), interactive=False)
    assert not any("backup" in note for note in renewal.notes)
