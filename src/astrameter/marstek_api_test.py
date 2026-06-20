from typing import Any

import astrameter.marstek_api as marstek_api
from astrameter.marstek_api import MarstekConfig, ensure_managed_fake_device

_CFG = MarstekConfig(
    base_url="https://eu.hamedata.com",
    mailbox="user@example.com",
    password="secret",
)

# A managed HME-4 the account already owns (devid == mac, `02b250` prefix).
_MANAGED_CT002 = {
    "devid": "02b250b26777",
    "mac": "02b250b26777",
    "type": "HME-4",
    "name": "AstraMeter CT002",
}


def _fake_http(uid: Any):
    """Return a `_http_get_json` stub: login carries `uid`, list echoes the CT."""

    def _impl(url: str, params: dict[str, Any], headers: dict[str, str] | None = None):
        if "v2_get_device.php" in url:
            return {"code": 2, "token": "tok", "uid": uid, "data": [_MANAGED_CT002]}
        if "getDeviceList" in url:
            return {"code": 1, "data": [_MANAGED_CT002]}
        raise AssertionError(f"unexpected url {url}")

    return _impl


def test_fetch_returns_account_uid(monkeypatch):
    monkeypatch.setattr(marstek_api, "_http_get_json", _fake_http(21495))
    token, devices, account_uid = marstek_api._fetch_token_and_devices(_CFG)
    assert token == "tok"
    assert devices[0]["mac"] == "02b250b26777"
    # The numeric login uid is normalized to a string for use as the cloud `aid`.
    assert account_uid == "21495"


def test_ensure_managed_device_carries_account_uid(monkeypatch):
    monkeypatch.setattr(marstek_api, "_http_get_json", _fake_http(21495))
    created = ensure_managed_fake_device(_CFG, "ct002")
    assert created is not None
    assert created["mac"] == "02b250b26777"
    assert created["account_uid"] == "21495"


def test_missing_uid_yields_empty_account_uid(monkeypatch):
    monkeypatch.setattr(marstek_api, "_http_get_json", _fake_http(None))
    created = ensure_managed_fake_device(_CFG, "ct002")
    assert created is not None
    assert created["account_uid"] == ""
