"""Every request shape a client can send, and the answer it gets.

A consumer that gets no answer hangs; one that gets an answer it cannot parse
gives up. Both look identical from the user's side — "discovered but offline" —
so the point of these tests is that *no* request shape is undefined.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrameter.net_info import AnnouncedAddress
from astrameter.shelly import rpc
from astrameter.shelly.identity import ShellyIdentity
from astrameter.shelly.rpc import ShellyRpcError
from astrameter.shelly.tcp_server import ShellyTcpServer

IDENTITY = ShellyIdentity(
    mac="B827EB364242",
    instance="ShellyPro3EM-B827EB364242",
    hostname="ShellyPro3EM-B827EB364242",
    shelly_id="shellypro3em-b827eb364242",
)
NOW = 1789999999.0
READING = [300.0, 0.0, -120.0]


async def _ok_read(client_ip: str, key: str) -> list[float]:
    return list(READING)


async def _failing_read(client_ip: str, key: str) -> list[float]:
    raise ShellyRpcError(1, rpc.NO_POWER_DATA)


def build_server(read: Any = _ok_read, *, serve_gen1: bool = True) -> ShellyTcpServer:
    return ShellyTcpServer(
        port=0,
        identity=IDENTITY,
        profile=rpc.PROFILES["shellypro3em"],
        udp_port=1010,
        read_reading=read,
        energy=rpc.EnergyCounters(
            seed={"a_total_act_energy": 12.40, "c_total_act_ret_energy": 4.90},
            now=lambda: NOW,
        ),
        serve_gen1=serve_gen1,
        announced_ip=AnnouncedAddress("192.168.1.50", resolve=lambda: "192.168.1.50"),
        settings_store=rpc.SettingsStore(),
        started_at=NOW - 3600,
        now=lambda: NOW,
    )


@pytest.fixture
async def client() -> Any:
    server = build_server()
    test_client = TestClient(TestServer(server.build_app()))
    await test_client.start_server()
    yield test_client
    await test_client.close()


@pytest.fixture
async def failing_client() -> Any:
    server = build_server(_failing_read)
    test_client = TestClient(TestServer(server.build_app()))
    await test_client.start_server()
    yield test_client
    await test_client.close()


#: The two setters need something to set, so a bare call is legitimately a
#: bad request. Everything else answers from static state or the reading.
_SETTER_QUERY = {
    "Cloud.SetConfig": "?config.enable=false",
    "Ws.SetConfig": "?config.enable=false",
}


async def test_every_method_answers_over_get(client: Any) -> None:
    """All 33, so a method that is listed but unroutable cannot slip through."""
    for name in rpc.METHOD_NAMES:
        response = await client.get(f"/rpc/{name}{_SETTER_QUERY.get(name, '')}")
        assert response.status == 200, name
        assert response.headers["Content-Type"] == "application/json", name


async def test_the_content_type_carries_no_charset(client: Any) -> None:
    """Real firmware sends the bare type.

    A consumer comparing the header exactly would otherwise see every response
    as foreign — and this is one constructor choice away from being wrong on
    every path at once.
    """
    for path in ("/shelly", "/settings", "/status", "/rpc/EM.GetStatus"):
        response = await client.get(path)
        assert response.headers["Content-Type"] == "application/json", path


async def test_the_server_header_is_present(client: Any) -> None:
    response = await client.get("/shelly")
    assert response.headers["Server"] == "ShellyHTTP/1.0.0"


async def test_dispatch_is_case_insensitive(client: Any) -> None:
    """One dispatch rule on every transport, rather than per-surface casing."""
    canonical = await (await client.get("/rpc/EM.GetStatus")).text()
    for spelling in ("em.getstatus", "EM.GETSTATUS", "Em.GetStatus"):
        assert await (await client.get(f"/rpc/{spelling}")).text() == canonical


async def test_unknown_query_parameters_are_ignored(client: Any) -> None:
    """A future vendor parameter degrades to today's answer, not an error."""
    response = await client.get("/rpc/EM.GetStatus?id=0&extra=1")
    assert response.status == 200


async def test_post_rpc_returns_the_full_envelope(client: Any) -> None:
    """``src`` is the lowercase device id, matching what ``/shelly`` reports."""
    response = await client.post(
        "/rpc",
        data=json.dumps(
            {"id": 5, "src": "batt", "method": "EM.GetStatus", "params": {"id": 0}}
        ),
    )
    assert response.status == 200
    body = await response.json()
    assert list(body) == ["id", "src", "dst", "result"]
    assert body["id"] == 5
    assert body["src"] == "shellypro3em-b827eb364242"
    assert body["dst"] == "batt"
    assert body["result"]["total_act_power"] == 180.0


async def test_post_to_a_method_path_accepts_a_params_only_body(
    client: Any,
) -> None:
    """The vendor's documented form: the body *is* the parameters.

    The reply is the bare result, with no envelope, because the request had no
    frame to echo.
    """
    response = await client.post("/rpc/EM.GetStatus", data=json.dumps({"id": 0}))
    assert response.status == 200
    body = await response.json()
    assert "result" not in body
    assert body["total_act_power"] == 180.0


async def test_post_to_a_method_path_accepts_a_full_frame(client: Any) -> None:
    response = await client.post(
        "/rpc/EM.GetStatus",
        data=json.dumps({"id": 7, "src": "c", "method": "EM.GetStatus"}),
    )
    body = await response.json()
    assert body["id"] == 7
    assert body["dst"] == "c"
    assert "result" in body


async def test_a_body_is_parsed_whatever_the_content_type(client: Any) -> None:
    """A client that omits the header, or declares text, is still served."""
    for headers in ({"Content-Type": "text/plain"}, {}):
        response = await client.post(
            "/rpc",
            data=json.dumps({"id": 1, "method": "Shelly.GetDeviceInfo"}),
            headers=headers,
        )
        assert response.status == 200


@pytest.mark.parametrize(
    ("path", "code", "message"),
    [
        ("/rpc/Nope.Nope", 404, "No handler for Nope.Nope"),
        ("/foo", 404, "No handler for /foo"),
        ("/rpc/", 404, "No handler for /rpc/"),
        ("/emeter/3", 404, "No handler for /emeter/3"),
        ("/rpc/EM.GetStatus?id=3", -105, "Bad id=3"),
        ("/rpc/EM.GetStatus?id=abc", -103, "Invalid 'id'"),
        ("/rpc/Cloud.SetConfig", -103, "Missing required 'config'"),
        ("/rpc/Cloud.SetConfig?config=", -103, "Invalid 'config'"),
        ("/rpc", -103, "Expected a WebSocket upgrade on GET /rpc"),
    ],
)
async def test_error_bodies(client: Any, path: str, code: int, message: str) -> None:
    response = await client.get(path)
    body = await response.json()
    assert body["error"] == {"code": code, "message": message}
    assert response.status == rpc.DEFAULT_STATUS[code]


async def test_an_unknown_method_over_post_carries_the_envelope(
    client: Any,
) -> None:
    response = await client.post(
        "/rpc", data=json.dumps({"id": 5, "src": "batt", "method": "Nope.Nope"})
    )
    assert response.status == 404
    body = await response.json()
    assert body["id"] == 5
    assert body["src"] == "shellypro3em-b827eb364242"
    assert body["dst"] == "batt"
    assert body["error"] == {"code": 404, "message": "No handler for Nope.Nope"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{nope", "Malformed JSON-RPC request"),
        ('[{"method":"EM.GetStatus"}]', "Batch requests are not supported"),
        ('{"id":1}', "Missing required 'method'"),
        ('"a string"', "Malformed JSON-RPC request"),
    ],
)
async def test_malformed_post_bodies(client: Any, payload: str, message: str) -> None:
    response = await client.post("/rpc", data=payload)
    assert response.status == 400
    assert (await response.json())["error"]["message"] == message


async def test_an_oversize_body_is_refused_with_413(client: Any) -> None:
    """Carries the invalid-argument code, but the status has to say too large.

    The code alone maps to 400, so this is the one case that needs an explicit
    status or it answers the wrong one.
    """
    response = await client.post("/rpc", data="x" * (1024 * 1024 + 1))
    assert response.status == 413
    body = await response.json()
    assert body["error"] == {"code": -103, "message": "Request body too large"}


@pytest.mark.parametrize("verb", ["put", "delete", "patch"])
async def test_other_verbs_are_not_found_rather_than_not_allowed(
    client: Any, verb: str
) -> None:
    """A real device has no such handler; it does not negotiate methods."""
    response = await getattr(client, verb)("/rpc")
    assert response.status == 404
    assert (await response.json())["error"]["code"] == 404


async def test_reset_data_is_acknowledged(client: Any) -> None:
    for verb in ("get", "post"):
        response = await getattr(client, verb)("/reset_data")
        assert response.status == 200
        assert await response.json() == {}


async def test_the_older_pages_can_be_switched_off() -> None:
    """One config line restores the narrower surface."""
    server = build_server(serve_gen1=False)
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        assert (await client.get("/status")).status == 404
        assert (await client.get("/emeter/0")).status == 404
        # The Gen2 surface is unaffected.
        assert (await client.get("/rpc/EM.GetStatus")).status == 200
        assert (await client.get("/shelly")).status == 200
    finally:
        await client.close()


async def test_the_websocket_answers_every_frame(client: Any) -> None:
    """Including the ones it cannot serve.

    A client left waiting for a reply that never arrives hangs, which is worse
    than a clean error — and this is the transport smart-home integrations use.
    """
    websocket = await client.ws_connect("/rpc")
    try:
        await websocket.send_str(
            json.dumps({"id": 1, "src": "c", "method": "Shelly.GetDeviceInfo"})
        )
        reply = json.loads((await websocket.receive()).data)
        assert reply["id"] == 1
        assert reply["src"] == "shellypro3em-b827eb364242"
        assert reply["dst"] == "c"
        assert reply["result"]["gen"] == 2

        for frame, expected in (
            ({"id": 2, "src": "c", "method": "Nope.Nope"}, 404),
            ({"id": 3, "src": "c"}, -103),
            (
                {"id": 4, "src": "c", "method": "EM.GetStatus", "params": {"id": 9}},
                -105,
            ),
        ):
            await websocket.send_str(json.dumps(frame))
            reply = json.loads((await websocket.receive()).data)
            assert reply["error"]["code"] == expected, frame

        await websocket.send_str("{nope")
        reply = json.loads((await websocket.receive()).data)
        assert reply["error"]["code"] == -103
    finally:
        await websocket.close()


async def test_every_method_answers_over_the_websocket(client: Any) -> None:
    """The same 33 over the transport smart-home integrations use."""
    websocket = await client.ws_connect("/rpc")
    try:
        for index, name in enumerate(rpc.METHOD_NAMES):
            params = {"config": {"enable": False}} if name in _SETTER_QUERY else {}
            await websocket.send_str(
                json.dumps({"id": index, "src": "c", "method": name, "params": params})
            )
            reply = json.loads((await websocket.receive()).data)
            assert "result" in reply, name
    finally:
        await websocket.close()


async def test_the_websocket_answers_a_ping(client: Any) -> None:
    """A smart-home integration drops a device that stops answering pings.

    ``autoping=False`` on the client so the frame is observable rather than
    absorbed by the client library before the test can see it.
    """
    websocket = await client.ws_connect("/rpc", autoping=False)
    try:
        await websocket.ping()
        message = await websocket.receive()
        assert message.type.name == "PONG"
    finally:
        await websocket.close()


async def test_a_failing_meter_still_answers_the_composites(
    failing_client: Any,
) -> None:
    """The guarantee a smart-home integration depends on to *add* the device.

    It fetches these while setting the device up, so a 503 here would stop the
    device being added at all rather than leaving one value stale.
    """
    for path in ("/rpc/Shelly.GetStatus", "/rpc/Shelly.GetComponents"):
        response = await failing_client.get(path)
        assert response.status == 200, path
    status = await (await failing_client.get("/rpc/Shelly.GetStatus")).json()
    assert status["em:0"]["errors"] == ["power_meter_failure"]
    assert len(status["em:0"]) == 25


async def test_a_failing_meter_still_answers_the_identity_pages(
    failing_client: Any,
) -> None:
    """These never touch the meter, so they cannot fail with it."""
    for path in ("/shelly", "/settings", "/rpc/Shelly.GetDeviceInfo"):
        assert (await failing_client.get(path)).status == 200, path


async def test_a_failing_meter_reports_the_measurement_surfaces_unavailable(
    failing_client: Any,
) -> None:
    for path in (
        "/rpc/EM.GetStatus",
        "/rpc/EM1.GetStatus",
        "/rpc/EMData.GetStatus",
        "/status",
        "/emeter/0",
    ):
        response = await failing_client.get(path)
        assert response.status == 503, path
        body = await response.json()
        assert body["error"]["code"] == 1, path
        assert body["error"]["message"] == rpc.NO_POWER_DATA, path


async def test_only_the_documented_error_is_rendered_as_an_error_body() -> None:
    """This module renders one exception type; the reader normalises the rest.

    Normalising *here* as well would hide a real bug behind a "meter
    unavailable" that no operator could distinguish from an actual outage. The
    emulator's reader is what turns every meter failure into code 1, and its
    own tests cover that.
    """

    async def boom(client_ip: str, key: str) -> list[float]:
        raise RuntimeError("a bug, not a meter outage")

    server = build_server(boom)
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    try:
        response = await client.get("/rpc/EM.GetStatus")
        assert response.status == 500
        assert "error" not in await response.text()
    finally:
        await client.close()


async def test_a_setter_round_trips_on_every_surface(client: Any) -> None:
    """All three transports write to one store, so they cannot disagree."""
    response = await client.get("/rpc/Cloud.SetConfig?config.enable=true")
    assert await response.json() == {"restart_required": False}
    assert (await (await client.get("/rpc/Cloud.GetConfig")).json())["enable"] is True

    await client.post(
        "/rpc",
        data=json.dumps(
            {
                "id": 1,
                "method": "Cloud.SetConfig",
                "params": {"config": {"enable": False}},
            }
        ),
    )
    assert (await (await client.get("/rpc/Cloud.GetConfig")).json())["enable"] is False

    websocket = await client.ws_connect("/rpc")
    try:
        await websocket.send_str(
            json.dumps(
                {
                    "id": 2,
                    "method": "Ws.SetConfig",
                    "params": {"config": {"enable": True}},
                }
            )
        )
        reply = json.loads((await websocket.receive()).data)
        assert reply["result"] == {"restart_required": False}
    finally:
        await websocket.close()
    config = await (await client.get("/rpc/Ws.GetConfig")).json()
    assert config["enable"] is True
    # The keys the request did not mention are still there.
    assert config["ssl_ca"] == "*"


async def test_a_disabled_port_serves_nothing() -> None:
    """``TCP_PORT = -1`` means no listener, without failing the start."""
    server = build_server()
    server._configured_port = -1
    assert await server.start() is False
    assert server.running is False
