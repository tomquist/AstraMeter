"""Extended unit tests for marstek_api — covers helpers and network-facing functions."""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from astrameter.marstek_api import (
    MarstekApiError,
    MarstekConfig,
    _add_device,
    _desired_name,
    _fetch_token_and_devices,
    _generate_new_id,
    _http_get_json,
    _is_managed_prefix,
    _random_hex,
    ensure_managed_fake_device,
)

# -- _random_hex -----------------------------------------------------------


def test_random_hex_length():
    assert len(_random_hex(6)) == 6
    assert len(_random_hex(0)) == 0
    assert len(_random_hex(12)) == 12


def test_random_hex_chars():
    result = _random_hex(100)
    assert all(c in "0123456789abcdef" for c in result)


# -- _desired_name ---------------------------------------------------------


def test_desired_name_ct002():
    assert _desired_name("ct002") == "AstraMeter CT002"


def test_desired_name_ct003():
    assert _desired_name("ct003") == "AstraMeter CT003"


# -- _is_managed_prefix ----------------------------------------------------


def test_is_managed_prefix_valid():
    assert _is_managed_prefix("02b250aaaaaa") is True
    assert _is_managed_prefix("02B250AAAAAA") is True


def test_is_managed_prefix_invalid():
    assert _is_managed_prefix("ffffffffffff") is False
    assert _is_managed_prefix("") is False
    assert _is_managed_prefix("02b25") is False


def test_is_managed_prefix_non_string():
    assert _is_managed_prefix(12345) is False  # type: ignore[arg-type]
    assert _is_managed_prefix(None) is False  # type: ignore[arg-type]


# -- _http_get_json --------------------------------------------------------


def _mock_urlopen(body: str, status: int = 200):
    """Return a context-manager mock for urllib.request.urlopen."""
    resp = MagicMock()
    resp.read.return_value = body.encode("utf-8")
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_http_get_json_success():
    payload = {"code": "2", "token": "tok123"}
    with patch(
        "urllib.request.urlopen", return_value=_mock_urlopen(json.dumps(payload))
    ):
        result = _http_get_json("https://example.com/api", {"key": "val"})
    assert result == payload


def test_http_get_json_http_error():
    exc = urllib.error.HTTPError(
        "https://example.com/api",
        400,
        "Bad Request",
        {},  # type: ignore[arg-type]
        None,
    )
    exc.read = MagicMock(return_value=b'{"code":"0","msg":"bad"}')
    with (
        patch("urllib.request.urlopen", side_effect=exc),
        pytest.raises(MarstekApiError, match="HTTP 400"),
    ):
        _http_get_json("https://example.com/api", {})


def test_http_get_json_url_error():
    exc = urllib.error.URLError("DNS failure")
    with (
        patch("urllib.request.urlopen", side_effect=exc),
        pytest.raises(MarstekApiError, match="Network error"),
    ):
        _http_get_json("https://example.com/api", {})


def test_http_get_json_non_json_body():
    with (
        patch(
            "urllib.request.urlopen",
            return_value=_mock_urlopen("<html>Error</html>"),
        ),
        pytest.raises(MarstekApiError, match="Non-JSON response"),
    ):
        _http_get_json("https://example.com/api", {})


def test_http_get_json_unexpected_error():
    with (
        patch("urllib.request.urlopen", side_effect=RuntimeError("boom")),
        pytest.raises(MarstekApiError, match="Unexpected error"),
    ):
        _http_get_json("https://example.com/api", {})


def test_http_get_json_passes_headers():
    payload = {"ok": True}
    with patch(
        "urllib.request.urlopen", return_value=_mock_urlopen(json.dumps(payload))
    ) as m:
        _http_get_json("https://example.com/api", {}, headers={"X-Custom": "yes"})
    req = m.call_args[0][0]
    assert req.get_header("X-custom") == "yes"


# -- _fetch_token_and_devices ----------------------------------------------


def _make_fetch_mock(token_resp, list_resp):
    """Patch _http_get_json to return different results per URL."""
    call_count = {"n": 0}

    def side_effect(url, params, headers=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return token_resp
        return list_resp

    return side_effect


def test_fetch_token_and_devices_success():
    token_resp = {
        "code": "2",
        "token": "tok",
        "data": [
            {
                "devid": "abc",
                "name": "Dev1",
                "sn": "SN1",
                "mac": "aabbcc",
                "type": "HME-4",
                "access": "1",
                "bluetooth_name": "BT1",
            },
        ],
    }
    list_resp = {
        "data": [
            {
                "devid": "abc",
                "name": "Dev1EMS",
                "mac": "aabbcc-ems",
                "version": "121",
                "salt": "s",
            },
        ],
    }
    with patch(
        "astrameter.marstek_api._http_get_json",
        side_effect=_make_fetch_mock(token_resp, list_resp),
    ):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        token, devices = _fetch_token_and_devices(cfg)
    assert token == "tok"
    assert len(devices) == 1
    assert devices[0]["devid"] == "abc"
    assert devices[0]["version"] == "121"


def test_fetch_token_and_devices_no_token():
    token_resp = {"code": "0", "msg": "error"}
    with patch("astrameter.marstek_api._http_get_json", return_value=token_resp):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        with pytest.raises(MarstekApiError, match="Token fetch failed"):
            _fetch_token_and_devices(cfg)


def test_fetch_token_and_devices_password_error_translated():
    token_resp = {"code": "4", "msg": "密码错误"}
    with patch("astrameter.marstek_api._http_get_json", return_value=token_resp):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        with pytest.raises(MarstekApiError, match="password incorrect"):
            _fetch_token_and_devices(cfg)


def test_fetch_token_and_devices_non_list_data():
    token_resp = {"code": "2", "token": "tok", "data": "not-a-list"}
    list_resp = {"data": "also-not-a-list"}
    with patch(
        "astrameter.marstek_api._http_get_json",
        side_effect=_make_fetch_mock(token_resp, list_resp),
    ):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        token, devices = _fetch_token_and_devices(cfg)
    assert token == "tok"
    assert devices == []


# -- _add_device -----------------------------------------------------------


def test_add_device_success():
    resp = {"code": "2", "msg": "ok"}
    with patch("astrameter.marstek_api._http_get_json", return_value=resp):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        result = _add_device(cfg, "tok", "ct002", "02b250aaaaaa")
    assert result["code"] == "2"


def test_add_device_failure():
    resp = {"code": "0", "msg": "fail"}
    with patch("astrameter.marstek_api._http_get_json", return_value=resp):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        with pytest.raises(MarstekApiError, match="Add device failed"):
            _add_device(cfg, "tok", "ct002", "02b250aaaaaa")


# -- _generate_new_id edge case --------------------------------------------


def test_generate_new_id_exhaustion():
    """If every candidate collides (extremely unlikely in practice) we get an error."""
    devices = [{"devid": f"02b250{i:06x}", "mac": f"02b250{i:06x}"} for i in range(200)]
    # All candidates will collide since _random_hex is mocked to return 000000
    with patch("astrameter.marstek_api._random_hex", return_value="000000"):
        # Make sure 02b250000000 is in the existing set
        devices.append({"devid": "02b250000000", "mac": "02b250000000"})
        with pytest.raises(MarstekApiError, match="Could not generate unique"):
            _generate_new_id(devices)


# -- ensure_managed_fake_device --------------------------------------------


def test_ensure_managed_fake_device_non_ct_type():
    cfg = MarstekConfig(
        base_url="https://api.example.com", mailbox="a@b.com", password="pw"
    )
    assert ensure_managed_fake_device(cfg, "shellypro3em") is None


def test_ensure_managed_fake_device_existing():
    existing_dev = {"devid": "02b250aaaaaa", "mac": "02b250aaaaaa", "type": "HME-4"}
    token_resp = {"code": "2", "token": "tok", "data": [existing_dev]}
    list_resp = {"data": []}
    with patch(
        "astrameter.marstek_api._http_get_json",
        side_effect=_make_fetch_mock(token_resp, list_resp),
    ):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        result = ensure_managed_fake_device(cfg, "ct002")
    assert result is not None
    assert result["devid"] == "02b250aaaaaa"


def test_ensure_managed_fake_device_creates_new():
    call_count = {"n": 0}

    def side_effect(url, params, headers=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First _fetch_token_and_devices → token call
            return {"code": "2", "token": "tok", "data": []}
        if call_count["n"] == 2:
            # First _fetch_token_and_devices → list call
            return {"data": []}
        if call_count["n"] == 3:
            # _add_device call
            return {"code": "2", "msg": "ok"}
        if call_count["n"] == 4:
            # Second _fetch_token_and_devices → token call (re-fetch)
            new_dev = {"devid": "02b250cccccc", "mac": "02b250cccccc", "type": "HME-4"}
            return {"code": "2", "token": "tok2", "data": [new_dev]}
        # Second _fetch_token_and_devices → list call
        return {"data": []}

    with patch("astrameter.marstek_api._http_get_json", side_effect=side_effect):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        result = ensure_managed_fake_device(cfg, "ct002")
    assert result is not None
    assert result["devid"] == "02b250cccccc"


def test_ensure_managed_fake_device_creates_but_not_confirmed():
    call_count = {"n": 0}

    def side_effect(url, params, headers=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"code": "2", "token": "tok", "data": []}
        if call_count["n"] == 2:
            return {"data": []}
        if call_count["n"] == 3:
            return {"code": "2", "msg": "ok"}
        if call_count["n"] == 4:
            # Re-fetch still shows nothing
            return {"code": "2", "token": "tok2", "data": []}
        return {"data": []}

    with patch("astrameter.marstek_api._http_get_json", side_effect=side_effect):
        cfg = MarstekConfig(
            base_url="https://api.example.com", mailbox="a@b.com", password="pw"
        )
        result = ensure_managed_fake_device(cfg, "ct002")
    assert result is None
