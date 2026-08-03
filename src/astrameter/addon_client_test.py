"""Tests for the Supervisor add-on client, against a local stub Supervisor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import test_utils, web

from astrameter.addon_client import SupervisorClient, SupervisorError

_TOKEN = "sekrit-supervisor-token"


class _StubSupervisor:
    """Records what the client sent and replays canned responses."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.options_response: tuple[int, dict[str, Any]] = (200, {"result": "ok"})
        self.reset_on_restart = False

    async def _record(self, request: web.Request) -> None:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.path, body))

    async def info(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response(
            {
                "result": "ok",
                "data": {
                    "options": {"log_level": "info"},
                    "schema": {"log_level": "str"},
                },
            }
        )

    async def options(self, request: web.Request) -> web.Response:
        await self._record(request)
        status, payload = self.options_response
        return web.json_response(payload, status=status)

    async def restart(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.reset_on_restart:
            # What a real restart looks like from here: the container serving
            # the request goes away mid-response.
            transport = request.transport
            assert transport is not None
            transport.close()
        return web.json_response({"result": "ok"})


@pytest.fixture
async def stub() -> AsyncIterator[_StubSupervisor]:
    supervisor = _StubSupervisor()
    app = web.Application()
    app.router.add_get("/addons/self/info", supervisor.info)
    app.router.add_post("/addons/self/options", supervisor.options)
    app.router.add_post("/addons/self/restart", supervisor.restart)
    server = test_utils.TestServer(app)
    await server.start_server()
    supervisor.base_url = str(server.make_url("")).rstrip("/")  # type: ignore[attr-defined]
    yield supervisor
    await server.close()


def _client(stub: _StubSupervisor, *, token: str | None = _TOKEN) -> SupervisorClient:
    return SupervisorClient(base_url=stub.base_url, token=token)  # type: ignore[attr-defined]


def test_unavailable_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert SupervisorClient().available() is False
    monkeypatch.setenv("SUPERVISOR_TOKEN", _TOKEN)
    assert SupervisorClient().available() is True


async def test_calls_without_token_raise_before_any_request(
    stub: _StubSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with pytest.raises(SupervisorError):
        await _client(stub, token=None).get_info()
    assert stub.requests == []


async def test_get_info_returns_data_and_sends_bearer(stub: _StubSupervisor) -> None:
    data = await _client(stub).get_info()
    assert data["schema"] == {"log_level": "str"}
    assert stub.requests == [("/addons/self/info", None)]


async def test_set_options_wraps_the_full_dict(stub: _StubSupervisor) -> None:
    await _client(stub).set_options({"log_level": "debug", "device_types": "ct002"})
    assert stub.requests == [
        (
            "/addons/self/options",
            {"options": {"log_level": "debug", "device_types": "ct002"}},
        )
    ]


async def test_set_ingress_panel_is_not_an_options_write(stub: _StubSupervisor) -> None:
    await _client(stub).set_ingress_panel(True)
    assert stub.requests == [("/addons/self/options", {"ingress_panel": True})]


async def test_options_400_message_passthrough(stub: _StubSupervisor) -> None:
    message = "expected int for dictionary value @ data['throttle_interval']"
    stub.options_response = (400, {"result": "error", "message": message})
    with pytest.raises(SupervisorError) as excinfo:
        await _client(stub).set_options({"throttle_interval": "nope"})
    assert excinfo.value.message == message
    assert str(excinfo.value) == message
    assert excinfo.value.status == 400


async def test_error_never_leaks_the_token(stub: _StubSupervisor) -> None:
    stub.options_response = (403, {"result": "error", "message": "Not allowed"})
    with pytest.raises(SupervisorError) as excinfo:
        await _client(stub).set_options({"log_level": "debug"})
    assert _TOKEN not in str(excinfo.value)
    assert _TOKEN not in repr(excinfo.value)


async def test_restart_treats_reset_as_success(stub: _StubSupervisor) -> None:
    stub.reset_on_restart = True
    await _client(stub).restart()
    assert stub.requests == [("/addons/self/restart", None)]


async def test_restart_accepts_a_reply_that_does_arrive(stub: _StubSupervisor) -> None:
    await _client(stub).restart()
    assert stub.requests == [("/addons/self/restart", None)]


class _StatesClient(SupervisorClient):
    """A client whose Core-proxy call returns a canned /states payload."""

    def __init__(self, states):
        super().__init__(base_url="http://supervisor", token="t")
        self._states = states

    async def _request_raw(self, method, path):
        assert path == "/core/api/states"
        return self._states


async def test_power_entities_keep_unclassed_watt_sensors():
    """Plenty of real installs expose grid power as a plain sensor in W or kW
    with no device_class; excluding those would make the picker useless for
    exactly the people who need it."""
    client = _StatesClient(
        [
            {
                "entity_id": "sensor.grid",
                "state": "412.8",
                "attributes": {
                    "friendly_name": "Grid power",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                },
            },
            {
                "entity_id": "sensor.p1",
                "state": "1.24",
                "attributes": {"unit_of_measurement": "kW"},
            },
            {
                "entity_id": "sensor.energy_today",
                "state": "12.4",
                "attributes": {
                    "device_class": "energy",
                    "unit_of_measurement": "kWh",
                },
            },
            {
                "entity_id": "sensor.outside_temp",
                "state": "18.1",
                "attributes": {
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                },
            },
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
            # Readings are fetched per entity from /api/states/<id>, which is
            # domain-agnostic, so this is a working power source — excluding
            # it told the user their entity did not exist.
            {
                "entity_id": "number.verbrauch",
                "state": "812.0",
                "attributes": {
                    "friendly_name": "Verbrauch",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                },
            },
            # The other arm of the same test: outside `sensor.` *and* with no
            # device class, so only the unit says this is power.
            {
                "entity_id": "input_number.grid_watts",
                "state": "0.81",
                "attributes": {"unit_of_measurement": "kW"},
            },
        ]
    )
    entities = await client.list_power_entities()
    assert [e["entity_id"] for e in entities] == [
        "input_number.grid_watts",
        "number.verbrauch",
        "sensor.grid",
        "sensor.p1",
    ]
    assert entities[0]["name"] == "input_number.grid_watts"
    assert entities[0]["device_class"] is None
    assert entities[2]["name"] == "Grid power"
    assert entities[2]["state"] == "412.8"
    # An id with no friendly name still needs something to render.
    assert entities[3]["name"] == "sensor.p1"


async def test_power_entities_offer_every_unit_the_meter_converts():
    """The picker's list has to be the powermeter's list.

    When MW and mW were added to the converter the picker still said "W or
    kW", so a sensor that read perfectly was hidden — and a configured one was
    flagged "not found in Home Assistant".
    """
    client = _StatesClient(
        [
            {
                "entity_id": "sensor.megawatts",
                "state": "0.0004",
                "attributes": {"unit_of_measurement": "MW"},
            },
            {
                "entity_id": "sensor.milliwatts",
                "state": "412800",
                "attributes": {"unit_of_measurement": "mW"},
            },
        ]
    )
    entities = await client.list_power_entities()
    assert [e["entity_id"] for e in entities] == [
        "sensor.megawatts",
        "sensor.milliwatts",
    ]
    assert all(e["readable"] for e in entities)


async def test_a_mislabelled_power_sensor_is_offered_but_marked():
    """`device_class: power` with a unit the meter refuses is a common
    mistake in a template sensor. Hiding it makes the entity the user is
    hunting for vanish; offering it silently makes every read fail."""
    client = _StatesClient(
        [
            {
                "entity_id": "sensor.grid_energy",
                "state": "12.4",
                "attributes": {
                    "friendly_name": "Grid energy",
                    "device_class": "power",
                    "unit_of_measurement": "kWh",
                },
            },
            {
                "entity_id": "sensor.grid_power",
                "state": "412.8",
                "attributes": {"device_class": "power", "unit_of_measurement": "W"},
            },
        ]
    )
    by_id = {e["entity_id"]: e for e in await client.list_power_entities()}
    assert by_id["sensor.grid_energy"]["readable"] is False
    assert by_id["sensor.grid_power"]["readable"] is True


async def test_a_unit_free_entity_is_readable_when_it_claims_to_be_power():
    """No unit attribute means "assume watts" to the powermeter, so a
    device_class that says power must not be marked unreadable."""
    client = _StatesClient(
        [
            {
                "entity_id": "sensor.bare",
                "state": "412.8",
                "attributes": {"device_class": "power"},
            }
        ]
    )
    assert (await client.list_power_entities())[0]["readable"] is True


async def test_power_entities_tolerate_a_junk_payload():
    client = _StatesClient(["not a dict", {"no_entity_id": True}, None])
    assert await client.list_power_entities() == []


async def test_power_entities_empty_when_core_returns_an_object():
    client = _StatesClient({"message": "unauthorized"})
    assert await client.list_power_entities() == []
