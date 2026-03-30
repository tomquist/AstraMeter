"""Async HTTP powermeter simulator.

Exposes grid-power readings via a JSON HTTP endpoint compatible with
b2500-meter's ``[JSON_HTTP]`` powermeter config.  Also provides control
endpoints, a status endpoint, and an SSE stream for the TUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .battery import BatterySimulator
    from .load_model import LoadModel

logger = logging.getLogger("b2500_sim.powermeter")

PHASES = ("A", "B", "C")


class PowermeterSimulator:
    def __init__(
        self,
        batteries: list[BatterySimulator],
        load_model: LoadModel,
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        self.batteries = batteries
        self.load_model = load_model
        self.host = host
        self.port = port
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._sse_clients: list[web.StreamResponse] = []
        self._shutdown_event = asyncio.Event()
        self._setup_routes()

    # -- routes ------------------------------------------------------------

    def _setup_routes(self) -> None:
        self._app.router.add_get("/power", self._handle_power)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/events", self._handle_sse)
        self._app.router.add_post("/loads/{index}/toggle", self._handle_toggle_load)
        self._app.router.add_post("/solar", self._handle_set_solar)
        self._app.router.add_post("/batteries/{mac}/soc", self._handle_set_battery_soc)
        self._app.router.add_post(
            "/batteries/{mac}/max_power", self._handle_set_battery_max_power
        )
        self._app.router.add_post("/auto", self._handle_set_auto)
        self._app.router.add_post("/shutdown", self._handle_shutdown)

    # -- grid power --------------------------------------------------------

    def compute_grid(self) -> dict[str, float]:
        contribution = self.load_model.get_grid_contribution()
        grid: dict[str, float] = {}
        for i, phase in enumerate(PHASES):
            battery_sum = sum(
                b.current_power for b in self.batteries if b.phase == phase
            )
            grid[f"phase_{phase.lower()}"] = round(contribution[i] - battery_sum, 1)
        return grid

    # -- helpers -----------------------------------------------------------

    @staticmethod
    async def _parse_json(request: web.Request) -> dict | web.Response:
        """Parse JSON body, returning a dict or a 400 error response."""
        try:
            return await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "invalid or missing JSON"}, status=400)

    # -- handlers ----------------------------------------------------------

    async def _handle_power(self, _request: web.Request) -> web.Response:
        return web.json_response(self.compute_grid())

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self._build_status())

    async def _handle_toggle_load(self, request: web.Request) -> web.Response:
        try:
            index = int(request.match_info["index"])
        except (ValueError, KeyError):
            return web.json_response({"error": "invalid index"}, status=400)
        try:
            self.load_model.toggle_load(index)
        except IndexError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(self._build_status())

    async def _handle_set_solar(self, request: web.Request) -> web.Response:
        body = await self._parse_json(request)
        if isinstance(body, web.Response):
            return body
        watts = body.get("watts")
        if watts is None:
            return web.json_response({"error": "missing 'watts'"}, status=400)
        if isinstance(watts, str):
            if watts == "max":
                watts = self.load_model.solar_max
            elif watts == "off":
                watts = 0.0
            else:
                return web.json_response({"error": "invalid watts"}, status=400)
        self.load_model.set_solar(float(watts))
        return web.json_response(self._build_status())

    async def _handle_set_battery_soc(self, request: web.Request) -> web.Response:
        mac = request.match_info["mac"].upper()
        battery = self._find_battery(mac)
        if battery is None:
            return web.json_response({"error": "battery not found"}, status=404)
        body = await self._parse_json(request)
        if isinstance(body, web.Response):
            return body
        raw = body.get("soc")
        if raw is None:
            return web.json_response({"error": "missing 'soc'"}, status=400)
        try:
            soc = float(raw)
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid 'soc'"}, status=400)
        if not (0.0 <= soc <= 1.0):
            return web.json_response({"error": "invalid 'soc'"}, status=400)
        battery.soc = soc
        return web.json_response(self._build_status())

    async def _handle_set_battery_max_power(self, request: web.Request) -> web.Response:
        mac = request.match_info["mac"].upper()
        battery = self._find_battery(mac)
        if battery is None:
            return web.json_response({"error": "battery not found"}, status=404)
        body = await self._parse_json(request)
        if isinstance(body, web.Response):
            return body
        try:
            charge = int(body["charge"]) if "charge" in body else None
            discharge = int(body["discharge"]) if "discharge" in body else None
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid max power"}, status=400)
        if (charge is not None and charge < 0) or (
            discharge is not None and discharge < 0
        ):
            return web.json_response({"error": "max power must be >= 0"}, status=400)
        if charge is not None:
            battery.max_charge_power = charge
        if discharge is not None:
            battery.max_discharge_power = discharge
        return web.json_response(self._build_status())

    async def _handle_set_auto(self, request: web.Request) -> web.Response:
        body = await self._parse_json(request)
        if isinstance(body, web.Response):
            return body
        self.load_model.auto_mode = bool(body.get("enabled", False))
        return web.json_response(self._build_status())

    async def _handle_shutdown(self, _request: web.Request) -> web.Response:
        self._shutdown_event.set()
        return web.json_response({"status": "shutting_down"})

    # -- SSE ---------------------------------------------------------------

    async def _handle_sse(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await resp.prepare(request)
        self._sse_clients.append(resp)
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(1.0)
                data = json.dumps(self._build_status())
                try:
                    await resp.write(f"event: status\ndata: {data}\n\n".encode())
                except (ConnectionResetError, ConnectionAbortedError):
                    break
        finally:
            self._sse_clients.remove(resp)
        return resp

    # -- status ------------------------------------------------------------

    def _build_status(self) -> dict:
        grid = self.compute_grid()
        total = sum(grid.values())
        return {
            "grid": {**grid, "total": round(total, 1)},
            **self.load_model.to_dict(),
            "batteries": [b.to_dict() for b in self.batteries],
        }

    # -- helpers -----------------------------------------------------------

    def _find_battery(self, mac: str) -> BatterySimulator | None:
        for b in self.batteries:
            if b.mac == mac:
                return b
        return None

    # -- lifecycle ---------------------------------------------------------

    @property
    def shutdown_event(self) -> asyncio.Event:
        return self._shutdown_event

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("Powermeter HTTP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        for client in list(self._sse_clients):
            with contextlib.suppress(Exception):
                await client.write_eof()
        if self._runner:
            await self._runner.cleanup()
