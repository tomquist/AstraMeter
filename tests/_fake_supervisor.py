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
import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

DEFAULT_TOKEN = "test-supervisor-token"
DEFAULT_SLUG = "a0ef98c5_b2500_meter"
GRID_SENSOR = "sensor.grid_power"
CONFIG_YAML = Path(__file__).parents[1] / "ha_addon" / "config.yaml"

#: How often a subscribed websocket client is told the readings changed. Fast
#: enough that a browser test sees the batteries steer without waiting.
PUSH_INTERVAL = 0.5


def _yaml_block(name: str, path: Path = CONFIG_YAML) -> dict[str, Any]:
    """The flat ``key: value`` pairs under a top-level block of config.yaml.

    Hand-parsed for the same reason the rest of the suite does it: both blocks
    this needs are flat, and a YAML dependency for that would be silly. Serving
    the add-on's *real* options and schema is the point — the guided form is
    then rendered from what Home Assistant would actually show, not a fixture
    that can drift away from it.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: dict[str, Any] = {}
    inside = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.rstrip() == f"{name}:"
            continue
        if not inside:
            continue
        match = re.match(r"\s{2}([a-z0-9_]+):\s*(.*?)\s*$", line)
        if match:
            out[match.group(1)] = _scalar(match.group(2))
    return out


#: The grammar Supervisor accepts for an add-on option, copied from
#: ``supervisor/validate.py`` so the rendering below matches it group for group.
_SCHEMA_ELEMENT = re.compile(
    r"^(?:"
    r"|bool"
    r"|email"
    r"|url"
    r"|port"
    r"|device(?:\((?P<filter>subsystem=[a-z]+)\))?"
    r"|str(?:\((?P<s_min>\d+)?,(?P<s_max>\d+)?\))?"
    r"|password(?:\((?P<p_min>\d+)?,(?P<p_max>\d+)?\))?"
    r"|int(?:\((?P<i_min>\d+)?,(?P<i_max>\d+)?\))?"
    r"|float(?:\((?P<f_min>[\d.]+)?,(?P<f_max>[\d.]+)?\))?"
    r"|match\((?P<match>.*)\)"
    r"|list\((?P<list>.+)\)"
    r")\??$"
)
_LENGTH_PARTS = ("i_min", "i_max", "f_min", "f_max", "s_min", "s_max", "p_min", "p_max")


def ui_schema(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Render config.yaml's validator strings the way Supervisor's API does.

    Supervisor never serves the ``key: "float(0,1)?"`` mapping an add-on
    declares. ``/addons/self/info`` returns a *list* of field descriptors
    rendered from it — ``{"name": "grid_predict_trust", "lengthMin": 0,
    "lengthMax": 1, "optional": true, "type": "float"}`` — and that list is
    the only shape a dashboard ever sees. Serving the mapping here is why a
    guided form that passed every test rendered fields named 0, 1, 2 against
    a real Home Assistant.

    Mirrors ``UiOptions`` in ``supervisor/addons/options.py``.
    """
    out: list[dict[str, Any]] = []
    for key, value in raw.items():
        if isinstance(value, list):
            # A repeated option: the single element describes every entry.
            if not value:
                continue
            if isinstance(value[0], dict):
                out.append(_nested_node(key, value[0], multiple=True))
            else:
                _single_node(out, value[0], key, multiple=True)
        elif isinstance(value, dict):
            out.append(_nested_node(key, value, multiple=False))
        else:
            _single_node(out, value, key)
    return out


def _nested_node(key: str, block: dict[str, Any], multiple: bool) -> dict[str, Any]:
    """A nested option block, described by the schema of its members."""
    nested: list[dict[str, Any]] = []
    for c_key, c_value in block.items():
        if isinstance(c_value, list):
            if c_value:
                _single_node(nested, c_value[0], c_key, multiple=True)
        else:
            _single_node(nested, c_value, c_key)
    return {
        "name": key,
        "type": "schema",
        "optional": True,
        "multiple": multiple,
        "schema": nested,
    }


def _single_node(
    out: list[dict[str, Any]], value: Any, key: str, multiple: bool = False
) -> None:
    """One field descriptor, in Supervisor's own key order."""
    spec = str(value)
    node: dict[str, Any] = {"name": key}
    if multiple:
        node["multiple"] = True
    match = _SCHEMA_ELEMENT.match(spec)
    if not match:
        # Supervisor drops an option it cannot parse rather than serving it.
        return
    for part in _LENGTH_PARTS:
        bound = match.group(part)
        if bound:
            node["lengthMin" if part.endswith("min") else "lengthMax"] = float(bound)
    node["optional" if spec.endswith("?") else "required"] = True
    if spec.startswith("str"):
        node["type"] = "string"
    elif spec.startswith("password"):
        node["type"] = "string"
        node["format"] = "password"
    elif spec.startswith("int") or spec.startswith("port"):
        node["type"] = "integer"
    elif spec.startswith("float"):
        node["type"] = "float"
    elif spec.startswith("bool"):
        node["type"] = "boolean"
    elif spec.startswith("email") or spec.startswith("url"):
        node["type"] = "string"
        node["format"] = "email" if spec.startswith("email") else "url"
    elif spec.startswith("match"):
        node["type"] = "string"
    elif spec.startswith("list"):
        node["type"] = "select"
        node["options"] = match.group("list").split("|")
    elif spec.startswith("device"):
        node["type"] = "select"
        node["options"] = []
    out.append(node)


def _friendly(entity: str, total: int) -> str:
    """What Home Assistant would show for one of the served power sensors."""
    if total == 1:
        return "Grid power"
    return f"Grid power {entity.rsplit('_', 1)[-1].upper()}"


def _scalar(raw: str) -> Any:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            continue
    return raw


class SupervisorState:
    """What the stand-in Supervisor knows and what it has been asked."""

    def __init__(
        self,
        watts: float = 321.0,
        token: str = DEFAULT_TOKEN,
        mqtt: dict[str, Any] | None = None,
        slug: str = DEFAULT_SLUG,
        sensor: str = GRID_SENSOR,
        power_url: str | None = None,
        phase_sensors: list[str] | None = None,
        options_path: Path | None = None,
    ) -> None:
        self.watts = watts
        self.token = token
        self.mqtt = mqtt
        self.slug = slug
        self.sensor = sensor
        #: Poll this for live per-phase readings instead of serving a constant.
        #: The browser E2E points it at the battery simulator, so the readings
        #: the add-on sees are the ones the simulated house is producing.
        self.power_url = power_url
        self.phase_sensors = phase_sensors or []
        #: Where the options this Supervisor stores are mirrored, so the app
        #: under test reads back what the dashboard wrote — the Supervisor
        #: rewrites /data/options.json on every change.
        self.options_path = options_path
        #: The add-on's stored options, as the Configuration tab would show.
        self.options: dict[str, Any] = {}
        #: The add-on's declared schema, in config.yaml's validator-string
        #: form. Served through :func:`ui_schema`, which is what Supervisor
        #: puts on the wire; kept raw here because validation reads it.
        self.schema: dict[str, Any] = {}
        self.ingress_panel = True
        self.restarts = 0
        #: Calls that arrived without the add-on's token.
        self.unauthorized = 0

    async def phase_watts(self) -> list[float]:
        """Current per-phase readings."""
        if not self.power_url:
            return [self.watts, self.watts, self.watts]
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    self.power_url, timeout=aiohttp.ClientTimeout(total=2)
                ) as r,
            ):
                body = await r.json()
        except Exception:
            return [self.watts, self.watts, self.watts]
        return [float(body.get(f"phase_{p}", 0.0)) for p in ("a", "b", "c")]

    async def entity_states(self) -> dict[str, float]:
        """Entity id -> reading, for whichever sensors this run advertises.

        One sensor gets the whole-house total, which is what a single signed
        grid-power sensor reads; several get a phase each.
        """
        if not self.phase_sensors:
            return {self.sensor: self.watts}
        phases = await self.phase_watts()
        if len(self.phase_sensors) == 1:
            return {self.phase_sensors[0]: round(sum(phases), 1)}
        return dict(zip(self.phase_sensors, phases, strict=False))

    def store_options(self, options: dict[str, Any]) -> None:
        self.options = options
        if self.options_path is not None:
            self.options_path.write_text(json.dumps(options), encoding="utf-8")

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
        return web.json_response(
            {
                "result": "ok",
                "data": {
                    "slug": state.slug,
                    "version": "next",
                    "state": "started",
                    "ingress_panel": state.ingress_panel,
                    "ingress_url": f"/api/hassio_ingress/{state.slug}/",
                    "options": state.options,
                    # Rendered, not raw: Supervisor serves field descriptors.
                    "schema": ui_schema(state.schema),
                },
            }
        )

    async def addon_options(request: web.Request) -> web.Response:
        """Store the options, the way the Supervisor does: a full replace.

        It validates too, and a rejection is a message the dashboard shows
        verbatim — so a range the schema declares is enforced here rather than
        quietly accepted.
        """
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        payload = await request.json()
        if "ingress_panel" in payload:
            state.ingress_panel = bool(payload["ingress_panel"])
            return web.json_response({"result": "ok"})
        options = payload.get("options") or {}
        for key, value in options.items():
            spec = str(state.schema.get(key, ""))
            if not spec.startswith("float(0,1)"):
                continue
            try:
                # The real Supervisor answers 400 for a value it cannot
                # coerce, not a 500 — a raw ValueError here would hide the
                # message the dashboard is supposed to show.
                in_range = 0 <= float(value) <= 1
            except (TypeError, ValueError):
                in_range = False
            if not in_range:
                return web.json_response(
                    {
                        "result": "error",
                        "message": (
                            "expected float in range [0, 1] for dictionary value "
                            f"@ data['{key}']. Got {json.dumps(value)}"
                        ),
                    },
                    status=400,
                )
        state.store_options(options)
        return web.json_response({"result": "ok"})

    async def addon_restart(request: web.Request) -> web.Response:
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        state.restarts += 1
        return web.json_response({"result": "ok"})

    async def entity_state(request: web.Request) -> web.Response:
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        entity = request.match_info["entity"]
        states = await state.entity_states()
        return web.json_response(
            {"entity_id": entity, "state": str(states.get(entity, state.watts))}
        )

    async def entity_states(request: web.Request) -> web.Response:
        """Every entity, which is what the dashboard's sensor picker lists.

        A realistic mix, so the picker is seen filtering rather than just
        echoing: power sensors with and without a device class, plus entities
        that must not be offered.
        """
        if not state.authorized(request):
            return web.json_response({"message": "unauthorized"}, status=401)
        live = await state.entity_states()
        body = [
            {
                "entity_id": entity,
                "state": str(watts),
                "attributes": {
                    "friendly_name": _friendly(entity, len(live)),
                    "device_class": "power",
                    "unit_of_measurement": "W",
                },
            }
            for entity, watts in live.items()
        ]
        body += [
            {
                "entity_id": "sensor.p1_meter_active_power",
                "state": "1.24",
                "attributes": {
                    "friendly_name": "P1 meter active power",
                    "unit_of_measurement": "kW",
                },
            },
            {
                "entity_id": "sensor.house_energy_today",
                "state": "12.4",
                "attributes": {
                    "friendly_name": "Energy today",
                    "device_class": "energy",
                    "unit_of_measurement": "kWh",
                },
            },
            {
                "entity_id": "sensor.outside_temperature",
                "state": "18.1",
                "attributes": {
                    "friendly_name": "Outside temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°C",
                },
            },
            {
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"friendly_name": "Kitchen light"},
            },
            # Megawatts is a real unit the meter converts, so a sensor
            # reading in it belongs in the picker like any other.
            {
                "entity_id": "sensor.substation_load",
                "state": "0.0021",
                "attributes": {
                    "friendly_name": "Substation load",
                    "unit_of_measurement": "MW",
                },
            },
            # Says it is power, reads in energy — a template sensor with the
            # wrong device class. Offered (someone is looking for it) but
            # marked, because the meter refuses to read it.
            {
                "entity_id": "sensor.pv_yield_total",
                "state": "1284.5",
                "attributes": {
                    "friendly_name": "PV yield total",
                    "device_class": "power",
                    "unit_of_measurement": "kWh",
                },
            },
            # Not a `sensor.`, but readings come from /api/states/<id>, which
            # does not care — so this is a usable grid source and the picker
            # has to offer it.
            {
                "entity_id": "number.verbrauch_15",
                "state": "812.0",
                "attributes": {
                    "friendly_name": "Verbrauch",
                    "device_class": "power",
                    "unit_of_measurement": "W",
                },
            },
        ]
        return web.json_response(body)

    async def push_updates(ws: web.WebSocketResponse, sub_id: int) -> None:
        """Keep a subscriber up to date with a source that keeps moving."""
        while not ws.closed:
            await asyncio.sleep(PUSH_INTERVAL)
            states = await state.entity_states()
            with contextlib.suppress(Exception):
                await ws.send_json(
                    {
                        "id": sub_id,
                        "type": "event",
                        "event": {
                            "c": {
                                entity: {"+": {"s": str(watts)}}
                                for entity, watts in states.items()
                            }
                        },
                    }
                )

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2024.1.0"})
        authenticated = False
        pusher: asyncio.Task | None = None
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
                states = await state.entity_states()
                await ws.send_json(
                    {
                        "id": payload["id"],
                        "type": "event",
                        "event": {
                            "a": {
                                entity: {"s": str(watts)}
                                for entity, watts in states.items()
                            }
                        },
                    }
                )
                if state.power_url and pusher is None:
                    # A live source keeps changing, and the subscriber only
                    # hears about it through this socket.
                    pusher = asyncio.create_task(push_updates(ws, payload["id"]))
        if pusher is not None:
            pusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pusher
        return ws

    app = web.Application()
    app.router.add_get("/core/api/", core_api)
    app.router.add_get("/services/mqtt", mqtt_service)
    app.router.add_get("/addons/self/info", addon_info)
    app.router.add_post("/addons/self/options", addon_options)
    app.router.add_post("/addons/self/restart", addon_restart)
    app.router.add_get("/core/api/websocket", websocket)
    app.router.add_get("/core/api/states", entity_states)
    app.router.add_get("/core/api/states/{entity}", entity_state)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--watts", type=float, default=321.0)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--sensor", default=GRID_SENSOR)
    parser.add_argument(
        "--power-url",
        help="Poll this for live per-phase readings (the simulator's /power) "
        "instead of serving a constant",
    )
    parser.add_argument(
        "--phase-sensors",
        help="Comma-separated entity ids to serve the phases as",
    )
    parser.add_argument(
        "--options",
        help="Mirror the stored add-on options to this file, so the app under "
        "test reads back what was written through the Supervisor",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Address to listen on",
    )
    args = parser.parse_args()
    state = SupervisorState(
        watts=args.watts,
        token=args.token,
        sensor=args.sensor,
        power_url=args.power_url,
        phase_sensors=[s.strip() for s in (args.phase_sensors or "").split(",") if s],
        options_path=Path(args.options) if args.options else None,
    )
    # The real Supervisor serves the add-on's own manifest; so does this, so a
    # guided form is rendered from the options Home Assistant would show.
    state.schema = _yaml_block("schema")
    state.options = _yaml_block("options")
    # Supervisor describes a repeated option as a list, not a validator
    # string. The add-on has none today, so the only way the guided form ever
    # meets that shape is here — and it used to throw inside render and freeze
    # the whole page on "Loading add-on options...".
    state.schema["extra_hosts"] = ["str"]
    state.options["extra_hosts"] = ["alpha", "beta"]
    if state.options_path is not None and state.options_path.exists():
        state.options.update(json.loads(state.options_path.read_text()))
    state.store_options(state.options)
    web.run_app(build_app(state), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
