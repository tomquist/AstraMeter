"""Unit tests for Marstek MQTT helpers (pure, no broker)."""

from __future__ import annotations

import pytest

from .marstek_mqtt import (
    DEFAULT_VER_V,
    MarstekMqttBinding,
    app_topics_for,
    build_response,
    device_topics_for,
    is_poll_payload,
    normalize_mac,
    parse_app_topic,
)


def _binding(
    *, ct_type: str = "HME-4", mac: str = "02b250aabbcc", wifi_rssi: int = -50
) -> MarstekMqttBinding:
    async def _noop() -> list[float]:
        return [0.0, 0.0, 0.0]

    return MarstekMqttBinding(
        device_id="device-1",
        ct_type=ct_type,
        mac=mac,
        get_values=_noop,
        wifi_rssi=wifi_rssi,
    )


# ── normalize_mac ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AA:BB:CC:DD:EE:FF", "aabbccddeeff"),
        ("aa-bb-cc-dd-ee-ff", "aabbccddeeff"),
        ("AABBCCDDEEFF", "aabbccddeeff"),
        ("aabbccddeeff", "aabbccddeeff"),
        (" 02b250AABBCC ", "02b250aabbcc"),
    ],
)
def test_normalize_mac_accepts_common_formats(raw: str, expected: str) -> None:
    assert normalize_mac(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "zz", "aabbccddeef", "aabbccddeeffff", "ghijklmnopqr", "aa:bb:cc"],
)
def test_normalize_mac_rejects_invalid(raw: str) -> None:
    assert normalize_mac(raw) == ""


# ── is_poll_payload ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        b"cd=1",
        b"cd=1,foo=bar",
        b"foo=bar,cd=1",
        b" cd = 1 ",
        b"cd=1\n",
        b"CD=1",
    ],
)
def test_is_poll_payload_accepts_cd1(body: bytes) -> None:
    assert is_poll_payload(body) is True


@pytest.mark.parametrize(
    "body",
    [b"", b"cd=0", b"cd=", b"garbage", b"pwr_a=1", b"\xff\xfe"],
)
def test_is_poll_payload_rejects_non_cd1(body: bytes) -> None:
    assert is_poll_payload(body) is False


# ── parse_app_topic ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "topic,expected",
    [
        (
            "hame_energy/HME-4/App/02b250aabbcc/ctrl",
            ("HME-4", "02b250aabbcc"),
        ),
        (
            "marstek_energy/HME-3/App/02b250ccddee/ctrl",
            ("HME-3", "02b250ccddee"),
        ),
        (
            "hame_energy/HME-4/App/02B250AABBCC/ctrl",
            ("HME-4", "02b250aabbcc"),
        ),
    ],
)
def test_parse_app_topic_accepts_valid(topic: str, expected: tuple[str, str]) -> None:
    assert parse_app_topic(topic) == expected


@pytest.mark.parametrize(
    "topic",
    [
        "hame_energy/HME-4/device/02b250aabbcc/ctrl",
        "hame_energy/HME-4/App/02b250aabbcc",
        "marstek_energy/HME-4/App//ctrl",
        "other/HME-4/App/02b250aabbcc/ctrl",
        "hame_energy/HME-4/App/02b250aabbcc/extra/ctrl",
        "",
    ],
)
def test_parse_app_topic_rejects_invalid(topic: str) -> None:
    assert parse_app_topic(topic) is None


# ── topic helpers ─────────────────────────────────────────────────────────


def test_app_topics_for() -> None:
    b = _binding(ct_type="HME-4", mac="02b250aabbcc")
    assert app_topics_for(b) == (
        "hame_energy/HME-4/App/02b250aabbcc/ctrl",
        "marstek_energy/HME-4/App/02b250aabbcc/ctrl",
    )


def test_device_topics_for() -> None:
    b = _binding(ct_type="HME-3", mac="02b250ccddee")
    assert device_topics_for(b) == (
        "hame_energy/HME-3/device/02b250ccddee/ctrl",
        "marstek_energy/HME-3/device/02b250ccddee/ctrl",
    )


# ── build_response ────────────────────────────────────────────────────────


def test_build_response_includes_required_and_optional_keys() -> None:
    b = _binding(wifi_rssi=-50)
    body = build_response(b, [100.0, 200.0, 300.0])
    assert body == (
        b"pwr_a=100,pwr_b=200,pwr_c=300,pwr_t=600,wif_r=-50,ver_v=148,wif_s=2"
    )


def test_build_response_rounds_and_sums() -> None:
    b = _binding(wifi_rssi=-42)
    # 123.6 → 124, 45.4 → 45, -67.9 → -68; total = 124 + 45 - 68 = 101
    body = build_response(b, [123.6, 45.4, -67.9])
    text = body.decode()
    assert "pwr_a=124" in text
    assert "pwr_b=45" in text
    assert "pwr_c=-68" in text
    assert "pwr_t=101" in text
    assert "wif_r=-42" in text
    assert f"ver_v={DEFAULT_VER_V}" in text
    assert "wif_s=2" in text


def test_build_response_pads_short_list() -> None:
    b = _binding()
    body = build_response(b, [123.0])
    text = body.decode()
    assert "pwr_a=123" in text
    assert "pwr_b=0" in text
    assert "pwr_c=0" in text
    assert "pwr_t=123" in text


def test_build_response_custom_ver_v() -> None:
    async def _g() -> list[float]:
        return [0.0, 0.0, 0.0]

    b = MarstekMqttBinding(
        device_id="d",
        ct_type="HME-4",
        mac="02b250aabbcc",
        get_values=_g,
        wifi_rssi=-60,
        ver_v=200,
    )
    body = build_response(b, [0.0, 0.0, 0.0])
    assert b"ver_v=200" in body
