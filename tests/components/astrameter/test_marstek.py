"""Tests for Marstek cloud registration + the HA-MQTT responder."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.astrameter import marstek
from custom_components.astrameter.marstek import (
    MarstekResponder,
    async_register_managed_ct,
)
from homeassistant.core import HomeAssistant


async def test_register_returns_mac_and_ver(hass: HomeAssistant) -> None:
    created = {"mac": "AA:BB:CC:DD:EE:FF", "version": 148}
    with patch(
        "astrameter.marstek_api.ensure_managed_fake_device", return_value=created
    ):
        result = await async_register_managed_ct(
            hass, "ct002", "user@example.com", "pw", "https://eu.hamedata.com"
        )
    assert result == ("aabbccddeeff", 148)


async def test_register_none_on_no_device(hass: HomeAssistant) -> None:
    with patch("astrameter.marstek_api.ensure_managed_fake_device", return_value=None):
        result = await async_register_managed_ct(
            hass, "ct002", "user@example.com", "pw", "https://eu.hamedata.com"
        )
    assert result is None


async def test_responder_answers_cd1_poll(hass: HomeAssistant) -> None:
    """A cd=1 poll yields an aggregate frame published to both device topics."""

    async def _values() -> list[float]:
        return [100.0, 0.0, 0.0]

    responder = MarstekResponder(
        hass,
        ct_type="ct002",
        mac="aabbccddeeff",
        ver_v=148,
        wifi_rssi=-50,
        get_values=_values,
        get_connected_slave_count=lambda: 1,
        get_cd4_slave_csv=lambda: "",
    )

    published: list[tuple[str, bytes]] = []

    async def _fake_publish(_hass, topic, payload, **_kw):
        published.append((topic, payload))

    with patch.object(
        marstek.mqtt, "async_publish", AsyncMock(side_effect=_fake_publish)
    ):
        from astrameter.mqtt_insights.marstek_mqtt import parse_marstek_poll_payload

        poll = parse_marstek_poll_payload(b"cd=1")
        assert poll is not None
        await responder._serve(poll)

    assert len(published) == 2  # old + new device topic templates
    topics = {t for t, _ in published}
    assert "hame_energy/ct002/device/aabbccddeeff/ctrl" in topics
    body = published[0][1].decode()
    assert "pwr_a=100" in body and "slv_n=1" in body
