import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrameter.marstek_api import (
    _desired_type,
    _find_existing_managed_device,
    _generate_new_id,
    _translate_marstek_message,
)


def test_desired_type_mapping():
    assert _desired_type("ct002") == "HME-4"
    assert _desired_type("ct003") == "HME-3"


def test_find_existing_managed_device_matches_prefix_and_type():
    devices = [
        {"devid": "02b250aaaaaa", "mac": "02b250aaaaaa", "type": "HME-4"},
        {"devid": "ffffffffffff", "mac": "ffffffffffff", "type": "HME-3"},
    ]

    found = _find_existing_managed_device(devices, expected_type="HME-4")
    assert found is not None
    assert found["devid"] == "02b250aaaaaa"


def test_find_existing_managed_device_ignores_wrong_type():
    devices = [
        {"devid": "02b250aaaaaa", "mac": "02b250aaaaaa", "type": "HME-3"},
    ]

    found = _find_existing_managed_device(devices, expected_type="HME-4")
    assert found is None


def test_generate_new_id_uses_prefix_and_avoids_collisions():
    devices = [
        {"devid": "02b250aaaaaa", "mac": "02b250aaaaaa"},
        {"devid": "02b250bbbbbb", "mac": "02b250bbbbbb"},
    ]

    new_id = _generate_new_id(devices)
    assert new_id.startswith("02b250")
    assert len(new_id) == 12
    assert new_id not in {"02b250aaaaaa", "02b250bbbbbb"}


def test_translate_marstek_message_password_error_chinese():
    assert _translate_marstek_message("4", "密码错误") == "password incorrect"


def test_translate_marstek_message_passthrough_unknown():
    assert _translate_marstek_message("999", "some backend text") == "some backend text"
