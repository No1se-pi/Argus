import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
from dotenv import dotenv_values

from app.vk_login import authorization_url, parse_callback, save_tokens


def test_authorization_uses_pkce_and_own_application():
    verifier = "test-verifier" * 5
    url = authorization_url(54884554, verifier, "random-state", "vkid.personal_info")
    query = parse_qs(urlparse(url).query)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert urlparse(url).netloc == "id.vk.ru"
    assert query["client_id"] == ["54884554"]
    assert query["redirect_uri"] == ["http://localhost"]
    assert query["code_challenge"] == [expected.decode()]
    assert query["response_type"] == ["code"]
    assert verifier not in url


@pytest.mark.parametrize("payload", [False, True])
def test_callback_supports_both_vk_formats(payload):
    value = {"state": "expected", "code": "one-use-code", "device_id": "device"}
    query = {"payload": json.dumps(value)} if payload else value
    assert parse_callback(query, "expected") == value


@pytest.mark.parametrize(
    "value",
    [
        {"state": "wrong", "code": "code", "device_id": "device"},
        {"code": "code", "device_id": "device"},
        {"state": "expected", "error": "access_denied"},
        {"state": "expected", "code": "code"},
        {"state": ["expected"], "code": "code", "device_id": "device"},
    ],
)
def test_callback_rejects_forged_or_incomplete_response(value):
    with pytest.raises(ValueError):
        parse_callback(value, "expected")


def test_save_updates_token_without_destroying_other_env_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep\nBOT_TOKEN='existing'\nVK_USER_ACCESS_TOKEN='old'\n", encoding="utf-8")
    bundle = tmp_path / "data" / "vk_id_tokens.json"
    save_tokens(env, bundle, {"access_token": "new", "refresh_token": "refresh"}, 123, "device")
    assert dotenv_values(env) == {"BOT_TOKEN": "existing", "VK_USER_ACCESS_TOKEN": "new"}
    assert "# keep" in env.read_text(encoding="utf-8")
    assert json.loads(bundle.read_text())["refresh_token"] == "refresh"
    assert not bundle.with_suffix(".tmp").exists()
