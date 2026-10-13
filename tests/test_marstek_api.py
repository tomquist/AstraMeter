import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Any

import pytest

from astrameter import marstek_api
from astrameter.marstek_api import (
    MarstekConfig,
    _add_device,
    _desired_bluetooth_name,
    _desired_type,
    _find_existing_managed_device,
    _generate_new_id,
    _translate_marstek_message,
)


def test_desired_type_mapping() -> None:
    assert _desired_type("ct002") == "HME-4"
    assert _desired_type("ct003") == "HME-3"


def test_desired_bluetooth_name_uses_model_prefix() -> None:
    assert _desired_bluetooth_name("ct002", "02b250abcdef") == "MST-TPM_cdef"
    assert _desired_bluetooth_name("ct003", "02b250abcdef") == "MST-SMR_cdef"


@pytest.mark.parametrize(
    ("device_type", "expected_type", "expected_bt_name"),
    [("ct002", "HME-4", "MST-TPM_cdef"), ("ct003", "HME-3", "MST-SMR_cdef")],
)
def test_add_device_payload_bluetooth_name(
    monkeypatch: pytest.MonkeyPatch,
    device_type: str,
    expected_type: str,
    expected_bt_name: str,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_http_get_json(
        url: str, params: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        calls.append((url, params))
        return {"code": "1"}

    monkeypatch.setattr(marstek_api, "_http_get_json", fake_http_get_json)
    cfg = MarstekConfig(
        base_url="https://example.invalid", mailbox="user@example.com", password="pw"
    )

    _add_device(cfg, "tok", device_type, "02b250abcdef")

    assert len(calls) == 1
    url, payload = calls[0]
    assert url.endswith("/app/Solar/v2_add_device.php")
    assert payload["type"] == expected_type
    assert payload["bluetooth_name"] == expected_bt_name


def test_find_existing_managed_device_ignores_bluetooth_name() -> None:
    # A CT002 registered before the MST-TPM_ prefix carries MST-SMR_ and must
    # still be reused rather than duplicated.
    devices = [
        {
            "devid": "02b250aaaaaa",
            "mac": "02b250aaaaaa",
            "type": "HME-4",
            "bluetooth_name": "MST-SMR_aaaa",
        },
    ]

    found = _find_existing_managed_device(devices, expected_type="HME-4")
    assert found is not None
    assert found["devid"] == "02b250aaaaaa"


def test_find_existing_managed_device_matches_prefix_and_type() -> None:
    devices = [
        {"devid": "02b250aaaaaa", "mac": "02b250aaaaaa", "type": "HME-4"},
        {"devid": "ffffffffffff", "mac": "ffffffffffff", "type": "HME-3"},
    ]

    found = _find_existing_managed_device(devices, expected_type="HME-4")
    assert found is not None
    assert found["devid"] == "02b250aaaaaa"


def test_find_existing_managed_device_ignores_wrong_type() -> None:
    devices = [
        {"devid": "02b250aaaaaa", "mac": "02b250aaaaaa", "type": "HME-3"},
    ]

    found = _find_existing_managed_device(devices, expected_type="HME-4")
    assert found is None


def test_generate_new_id_uses_prefix_and_avoids_collisions() -> None:
    devices = [
        {"devid": "02b250aaaaaa", "mac": "02b250aaaaaa"},
        {"devid": "02b250bbbbbb", "mac": "02b250bbbbbb"},
    ]

    new_id = _generate_new_id(devices)
    assert new_id.startswith("02b250")
    assert len(new_id) == 12
    assert new_id not in {"02b250aaaaaa", "02b250bbbbbb"}


def test_translate_marstek_message_password_error_chinese() -> None:
    assert _translate_marstek_message("4", "密码错误") == "password incorrect"


def test_translate_marstek_message_passthrough_unknown() -> None:
    assert _translate_marstek_message("999", "some backend text") == "some backend text"
