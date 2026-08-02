"""Tests for the dashboard's HTTP surface: the access gate, the routes and
the control-write validation."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrameter.ct002 import CT002
from astrameter.status import StatusRegistry
from astrameter.status.secrets import SENTINEL
from astrameter.web_server import WebServer


def _registry(tmp_path, **kwargs) -> StatusRegistry:
    kwargs.setdefault("config_path", str(tmp_path / "config.ini"))
    kwargs.setdefault("log_level", "info")
    kwargs.setdefault("version", "9.9.9")
    kwargs.setdefault("git_commit", "")
    kwargs.setdefault("dashboard_enabled", True)
    return StatusRegistry(**kwargs)


def _device() -> CT002:
    device = CT002(device_id="ct-1")
    device._update_consumer_report(
        "02b250000001", "A", -120, "HMK-2", source_ip="10.0.0.5"
    )
    device._last_grid_values = [12.0, -30.0, 5.0]
    device._last_grid_at = 1_770_000_000.0
    device._last_smooth_target = -13.0
    return device


async def _client(registry, **kwargs) -> TestClient:
    """A test client. Requests appear to come from 127.0.0.1, i.e. NOT ingress.

    Uses the production ``build_app()`` so the routes under test are exactly
    the routes that ship.
    """
    server = WebServer(config_path=registry.config_path, status=registry, **kwargs)
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    return client


# -- the access gate --------------------------------------------------


async def test_health_is_always_reachable(tmp_path):
    """The Docker HEALTHCHECK and the add-on watchdog both probe /health, so
    it must answer even when everything else is gated off."""
    client = await _client(_registry(tmp_path, direct_access=False))
    assert (await client.get("/health")).status == 200
    await client.close()


@pytest.mark.parametrize("path", ["/", "/api/status", "/api/config"])
async def test_direct_access_is_refused_by_default_under_the_addon(tmp_path, path):
    """Under host networking the port is on the LAN with no authentication,
    so anything sensitive must fail closed."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="ha_advanced")
    )
    assert (await client.get(path)).status == 403
    await client.close()


async def test_a_refused_page_explains_itself_in_prose(tmp_path):
    """Someone typed the address into a browser. A raw JSON error tells them
    nothing about the sidebar or the option that would let this port work."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="ha_advanced")
    )
    response = await client.get("/")
    assert response.status == 403
    assert response.content_type == "text/html"
    body = await response.text()
    assert "sidebar" in body
    assert "dashboard_direct_access" in body
    await client.close()


@pytest.mark.parametrize("path", ["/", "/api/status"])
async def test_standalone_serves_without_a_second_opt_in(tmp_path, path):
    """Outside the add-on there is no ingress peer, so the plain port is the
    only way in. Gating it behind a further flag would make DASHBOARD_ENABLED
    on its own do nothing at all — which is what a standalone user sets."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="standalone")
    )
    assert (await client.get(path)).status == 200
    await client.close()


async def test_forged_ingress_headers_do_not_grant_access(tmp_path):
    """The gate is the peer address precisely because a LAN client can set
    any header it likes."""
    client = await _client(
        _registry(tmp_path, direct_access=False, config_mode="ha_advanced")
    )
    response = await client.get(
        "/api/status",
        headers={
            "X-Ingress-Path": "/api/hassio_ingress/x",
            "X-Hass-Source": "core.ingress",
        },
    )
    assert response.status == 403
    await client.close()


async def test_direct_access_opt_in_allows_reads(tmp_path):
    client = await _client(_registry(tmp_path, direct_access=True))
    assert (await client.get("/api/status")).status == 200
    await client.close()


async def test_writes_need_both_trust_and_the_write_flag(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=False)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": "active",
            "value": False,
        },
    )
    assert response.status == 403
    await client.close()


async def test_dashboard_off_serves_no_routes(tmp_path):
    client = await _client(_registry(tmp_path, dashboard_enabled=False))
    assert (await client.get("/api/status")).status == 404
    assert (await client.get("/health")).status == 200
    await client.close()


# -- status -----------------------------------------------------------


async def test_status_returns_a_snapshot_and_revalidates(tmp_path):
    registry = _registry(tmp_path, direct_access=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)

    response = await client.get("/api/status")
    assert response.status == 200
    etag = response.headers["ETag"]
    body = await response.json()
    assert body["schema_version"] == 1
    assert body["devices"][0]["consumers"][0]["consumer_id"] == "02b250000001"

    again = await client.get("/api/status", headers={"If-None-Match": etag})
    assert again.status == 304
    assert await again.text() == ""
    await client.close()


async def test_no_handler_emits_a_location_header(tmp_path):
    """Both ingress hops copy Location verbatim, so a redirect would navigate
    the user straight out of the panel."""
    registry = _registry(tmp_path, direct_access=True)
    client = await _client(registry)
    for path in ("/health", "/api/status", "/api/status/", "/nope"):
        response = await client.get(path, allow_redirects=False)
        assert "Location" not in response.headers, path
        assert response.status != 301 and response.status != 302
    await client.close()


async def test_trailing_slash_variants_are_registered(tmp_path):
    client = await _client(_registry(tmp_path, direct_access=True))
    assert (await client.get("/api/status/")).status == 200
    await client.close()


# -- control writes ---------------------------------------------------


async def test_control_write_applies_through_the_validated_setter(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)
    client = await _client(registry)

    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": "active",
            "value": False,
        },
    )
    assert response.status == 200
    assert (await response.json())["applied"] is True
    assert device.is_consumer_active("02b250000001") is False
    await client.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manual_target", 99999),
        ("manual_target", -99999),
        ("manual_target", "abc"),
        ("distribution_weight", 50),
        ("efficiency_window_weight", 101),
        ("min_dc_output", 5000),
        ("active", "yes"),
    ],
)
async def test_out_of_range_control_writes_are_rejected(tmp_path, field, value):
    """The CT002 setters do not bound their inputs — the ranges live in the
    MQTT handlers. A value MQTT would reject must be rejected here too, or the
    broker's retained state would silently revert it on the next reconnect."""
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": field,
            "value": value,
        },
    )
    assert response.status == 400
    await client.close()


async def test_unknown_device_or_field_is_a_404(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    for body in (
        {"device_id": "nope", "consumer_id": "x", "field": "active", "value": True},
        {"device_id": "ct-1", "consumer_id": "x", "field": "bogus", "value": True},
    ):
        assert (await client.post("/api/control/consumer", json=body)).status == 404
    await client.close()


# -- config -----------------------------------------------------------


async def test_config_get_redacts_and_post_restores_secrets(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text(
        "[GENERAL]\nDEVICE_TYPE = ct002\n\n[MQTT_INSIGHTS]\nBROKER = 10.0.0.2\nPASSWORD = hunter2\n"
    )
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    client = await _client(registry)

    body = await (await client.get("/api/config")).json()
    assert body["sections"]["MQTT_INSIGHTS"]["PASSWORD"] == SENTINEL
    assert "hunter2" not in json.dumps(body)

    # Echoing the sentinel back must keep the stored password, not blank it.
    body["sections"]["MQTT_INSIGHTS"]["BROKER"] = "10.0.0.9"
    saved = await client.post("/api/config", json=body)
    assert saved.status == 200
    text = config.read_text()
    assert "PASSWORD = hunter2" in text
    assert "BROKER = 10.0.0.9" in text
    await client.close()


async def test_simple_mode_refuses_config_writes(tmp_path):
    """The add-on regenerates config.ini on every start, so a write here
    would vanish at the next restart."""
    config = tmp_path / "config.ini"
    config.write_text("[GENERAL]\nDEVICE_TYPE = ct002\n")
    registry = _registry(
        tmp_path, direct_access=True, allow_write=True, config_mode="ha_simple"
    )
    client = await _client(registry)
    response = await client.post(
        "/api/config",
        json={"sections": {"GENERAL": {"DEVICE_TYPE": "ct003"}}, "order": ["GENERAL"]},
    )
    assert response.status == 403
    assert "add-on options" in (await response.json())["error"]
    await client.close()


# -- device-wide controls (the MQTT switch + button) ------------------


async def test_active_control_can_be_toggled(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)
    client = await _client(registry)
    response = await client.post(
        "/api/control/device",
        json={"device_id": "ct-1", "field": "active_control", "value": False},
    )
    assert response.status == 200
    assert device.active_control is False
    await client.close()


async def test_force_rotation_is_accepted(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    response = await client.post(
        "/api/control/device",
        json={"device_id": "ct-1", "field": "force_rotation", "value": True},
    )
    assert response.status == 200
    assert (await response.json())["applied"] is True
    await client.close()


async def test_unknown_device_field_is_404(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    assert (
        await client.post(
            "/api/control/device",
            json={"device_id": "ct-1", "field": "nope", "value": True},
        )
    ).status == 404
    await client.close()


async def test_device_control_needs_the_write_flag(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=False)
    registry.register_device("ct-1", "ct002", _device())
    client = await _client(registry)
    assert (
        await client.post(
            "/api/control/device",
            json={"device_id": "ct-1", "field": "force_rotation", "value": True},
        )
    ).status == 403
    await client.close()


# -- units match the MQTT entity, not the setter ----------------------


async def test_efficiency_window_weight_is_a_percentage_on_the_wire(tmp_path):
    """The MQTT entity is a 0-100 percentage while the setter takes a 0-1
    fraction, and the two must not disagree: 50 over HTTP has to mean the
    same thing as 50 over MQTT."""
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)

    mirrored: list[tuple] = []

    class _Insights:
        async def publish_consumer_command(self, device_id, consumer_id, field, value):
            mirrored.append((device_id, consumer_id, field, value))

    registry.insights = _Insights()
    client = await _client(registry)

    response = await client.post(
        "/api/control/consumer",
        json={
            "device_id": "ct-1",
            "consumer_id": "02b250000001",
            "field": "efficiency_window_weight",
            "value": 50,
        },
    )
    assert response.status == 200
    consumer = device.snapshot_consumer("02b250000001")
    assert consumer is not None
    assert consumer.efficiency_window_weight == pytest.approx(0.5)

    # ...and it reads back in the same unit it was written in.
    body = await (await client.get("/api/status")).json()
    row = body["devices"][0]["consumers"][0]
    assert row["efficiency_window_weight_pct"] == pytest.approx(50.0)

    # The retained MQTT command is replayed into the handler that reads the
    # *entity* unit, so mirroring the scaled 0.5 would reapply it as 0.5 %
    # on the next reconnect — the exact revert this mirror exists to stop.
    assert mirrored == [("ct-1", "02b250000001", "efficiency_window_weight", 50)]
    await client.close()


async def test_full_efficiency_window_is_accepted(tmp_path):
    registry = _registry(tmp_path, direct_access=True, allow_write=True)
    device = _device()
    registry.register_device("ct-1", "ct002", device)
    client = await _client(registry)
    assert (
        await client.post(
            "/api/control/consumer",
            json={
                "device_id": "ct-1",
                "consumer_id": "02b250000001",
                "field": "efficiency_window_weight",
                "value": 100,
            },
        )
    ).status == 200
    consumer = device.snapshot_consumer("02b250000001")
    assert consumer is not None
    assert consumer.efficiency_window_weight == pytest.approx(1.0)
    await client.close()
