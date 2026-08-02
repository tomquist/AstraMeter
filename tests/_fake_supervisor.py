"""A stand-in Home Assistant Supervisor, for tests that need one to talk to.

Serves what an add-on actually uses: the Supervisor's own REST endpoints, and
Home Assistant behind the ``/core`` prefix — including the websocket the power
source subscribes to. Every call must carry the add-on's token, and refusals
are counted so a test can assert the add-on authenticated properly.

Used two ways:

* imported by ``test_addon_e2e.py``, which runs it in-process;
* run as a script (``python _fake_supervisor.py --port 80``) inside a container
  for the add-on image smoke test, where it must answer to the hostname
  ``supervisor``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from aiohttp import WSMsgType, web

DEFAULT_TOKEN = "test-supervisor-token"
DEFAULT_SLUG = "a0ef98c5_b2500_meter"
GRID_SENSOR = "sensor.grid_power"


class SupervisorState:
    """What the stand-in Supervisor knows and what it has been asked."""

    def __init__(
        self,
        watts: float = 321.0,
        token: str = DEFAULT_TOKEN,
        mqtt: dict[str, Any] | None = None,
        slug: str = DEFAULT_SLUG,
        sensor: str = GRID_SENSOR,
    ) -> None:
        self.watts = watts
        self.token = token
        self.mqtt = mqtt
        self.slug = slug
        self.sensor = sensor
        #: Calls that arrived without the add-on's token.
        self.unauthorized = 0

    def authorized(self, request: web.Request) -> bool:
        if request.headers.get("Authorization") == f"Bearer {self.token}":
            return True
        self.unauthorized += 1
        return False


def build_app(state: SupervisorState) -> web.Application:
    async def core_api(request: web.Request) -> web.Response:
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        return web.json_response({"message": "API running."})

    async def mqtt_service(request: web.Request) -> web.Response:
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        if state.mqtt is None:
            return web.json_response({"result": "error"}, status=400)
        return web.json_response({"result": "ok", "data": state.mqtt})

    async def addon_info(request: web.Request) -> web.Response:
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        return web.json_response({"result": "ok", "data": {"slug": state.slug}})

    async def entity_state(request: web.Request) -> web.Response:
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        return web.json_response(
            {"entity_id": request.match_info["entity"], "state": str(state.watts)}
        )

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2024.1.0"})
        authenticated = False
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                # An ERROR frame carries the exception, not JSON; anything
                # else is not something Home Assistant would send.
                await ws.close()
                return ws
            payload = json.loads(message.data)
            kind = payload.get("type")
            if kind == "auth":
                if payload.get("access_token") != state.token:
                    state.unauthorized += 1
                    await ws.send_json({"type": "auth_invalid"})
                    await ws.close()
                    return ws
                authenticated = True
                await ws.send_json({"type": "auth_ok", "ha_version": "2024.1.0"})
            elif not authenticated:
                # Home Assistant answers nothing before the auth handshake.
                state.unauthorized += 1
                await ws.close()
                return ws
            elif kind == "subscribe_entities":
                await ws.send_json(
                    {"id": payload["id"], "type": "result", "success": True}
                )
                await ws.send_json(
                    {
                        "id": payload["id"],
                        "type": "event",
                        "event": {"a": {state.sensor: {"s": str(state.watts)}}},
                    }
                )
        return ws

    app = web.Application()
    app.router.add_get("/core/api/", core_api)
    app.router.add_get("/services/mqtt", mqtt_service)
    app.router.add_get("/addons/self/info", addon_info)
    app.router.add_get("/core/api/websocket", websocket)
    app.router.add_get("/core/api/states/{entity}", entity_state)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--watts", type=float, default=321.0)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--sensor", default=GRID_SENSOR)
    args = parser.parse_args()
    state = SupervisorState(watts=args.watts, token=args.token, sensor=args.sensor)
    web.run_app(build_app(state), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
