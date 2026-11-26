import io
import json
import time
import urllib.error

import pytest

from ai_quota_swap import api_check, api_spend
from ai_quota_swap.json_output import api_spend_to_json
from tests.admin_fakes import KEY_A, KEY_B
from tests.api_helpers import make_switcher, run_main, seed_api_key, seed_oauth


class Reply(io.BytesIO):
    code = 200


def test_cli_probe_and_cached_menu_reading(temp_home, monkeypatch, capsys):
    switcher = make_switcher()
    seed_oauth(switcher, 1, "home@example.com")
    seed_api_key(switcher, 5, KEY_A)
    capsys.readouterr()
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert KEY_A == dict(request.header_items())["X-api-key"]
            if request.full_url.endswith("/count_tokens"):
                return Reply(b'{"input_tokens":8}')
            body = {"error": {"type": "invalid_request_error",
                              "message": f"Your credit balance is too low. {KEY_A}"}}
            raise urllib.error.HTTPError(request.full_url, 400, "bad request", {}, Reply(json.dumps(body).encode()))

    monkeypatch.setattr(api_check.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(SystemExit) as rejected:
        run_main(["api", "check", "5", "--inference", "--json"])
    assert rejected.value.code == 1
    out, err = capsys.readouterr()
    result = json.loads(out)
    assert result["outcome"] == "low-credit" and result["httpStatus"] == 400
    assert KEY_A not in out + err
    assert len(requests) == 1 and requests[0].full_url == "https://api.anthropic.com/v1/messages"
    assert json.loads(requests[0].data)["max_tokens"] == 1
    run_main(["api", "check", "5"])
    assert "billing untested" in capsys.readouterr().out
    assert len(requests) == 2 and "max_tokens" not in json.loads(requests[1].data)
    info = {str(row[0]): row for row in switcher._build_accounts_info() if str(row[0]) == "5"}
    reading = api_spend.readings(switcher, info, fetch=set())["5"]
    assert api_spend_to_json(reading)["inferenceCheck"]["outcome"] == "low-credit"
    assert "credit balance too low" in api_spend.detail_text(reading)
    assert api_spend.limit_reason(reading) is None
    root = switcher.backup_dir
    saved, = (root / "cache" / "key_checks").glob("*.json")
    assert KEY_A not in saved.name + saved.read_text()
    assert api_check.recent_inference(root, KEY_B, time.time()) is None
    assert result["checkedAt"].endswith("Z")
    assert api_check.recent_inference(root, KEY_A, time.time() + api_check.MAX_AGE) is None
    with pytest.raises(SystemExit):
        run_main(["api", "check", "1", "--inference"])
    assert len(requests) == 2


@pytest.mark.parametrize(("status", "payload", "outcome"), [
    (429, {"error": {"message": "Your organization has crossed its monthly API usage threshold."}}, "monthly-limit"),
    (429, {"error": {"message": "Too many requests"}}, "rate-limit"),
    (402, {"error": {"type": "billing_error"}}, "billing"),
    (401, {}, "auth"), (403, {}, "permission"), (404, {}, "model"),
    (529, None, "server"), (307, None, "refused"), (200, {}, "unreadable"),
    (200, {"type": "message", "role": "assistant"}, "accepted"),
])
def test_check_classification(status, payload, outcome):
    assert api_check._outcome(status, payload, inference=True) == outcome


def test_key_material_as_identifier_is_never_echoed(temp_home, capsys):
    with pytest.raises(SystemExit) as exc:
        run_main(["api", "check", KEY_A, "--json"])
    assert exc.value.code == 64
    out, err = capsys.readouterr()
    assert KEY_A not in out + err


def test_admin_key_never_sent(temp_home, capsys):
    from tests.admin_fakes import ADMIN_KEY

    switcher = make_switcher()
    seed_api_key(switcher, 5, ADMIN_KEY)
    with pytest.raises(SystemExit) as exc:
        run_main(["api", "check", "5", "--inference"])
    assert exc.value.code == 1
    assert "Admin keys belong" in "".join(capsys.readouterr())
