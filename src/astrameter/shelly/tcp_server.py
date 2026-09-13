"""The Shelly HTTP surface: routes, dispatch, and the websocket channel.

A consumer that has found the emulated device over mDNS talks to it here. Three
transports carry the same method set — a GET per method, a POST of a JSON-RPC
frame, and a websocket — and they answer identically, because a device that
gave different answers depending on how it was asked would be worse than one
that refused outright.

The reading itself comes from an injected callable rather than from a meter this
module knows about, which is what keeps the per-battery liveness and queueing
rules in one place next to the UDP path that already has them.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from astrameter.config.logger import logger
from astrameter.net_info import AnnouncedAddress
from astrameter.shelly import rpc, wire
from astrameter.shelly.identity import ShellyIdentity
from astrameter.shelly.rpc import ShellyProfile, ShellyRpcError

#: Sent on every response, as the firmware does.
SERVER_HEADER = "ShellyHTTP/1.0.0"

#: Envelope keys that are never method parameters.
_ENVELOPE_KEYS = ("id", "method", "src", "dst")

ReadReading = Callable[[str, str], Awaitable["list[float] | None"]]


def _json_response(payload: object, *, status: int = 200) -> web.Response:
    """*payload* as a response with a bare ``application/json`` type.

    Deliberately not ``web.json_response``: that appends ``; charset=utf-8``,
    which no real device sends, and a consumer comparing the header exactly
    would see every response as foreign.
    """
    return web.Response(
        body=wire.dumps(payload).encode(),
        status=status,
        content_type="application/json",
    )


def _error_response(error: ShellyRpcError) -> web.Response:
    response = _json_response(error.body(), status=error.http_status())
    if error.retry_after is not None:
        response.headers["Retry-After"] = str(error.retry_after)
    return response


class ShellyTcpServer:
    """One HTTP listener, serving one emulated device."""

    def __init__(
        self,
        *,
        port: int,
        identity: ShellyIdentity,
        profile: ShellyProfile,
        udp_port: int,
        read_reading: ReadReading,
        energy: rpc.EnergyCounters,
        serve_gen1: bool,
        announced_ip: AnnouncedAddress,
        settings_store: rpc.SettingsStore,
        started_at: float,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._configured_port = port
        self._identity = identity
        self._profile = profile
        self._udp_port = udp_port
        self._read_reading = read_reading
        self._energy = energy
        self._serve_gen1 = serve_gen1
        self._announced_ip = announced_ip
        self._settings_store = settings_store
        self._started_at = started_at
        if now is None:
            import time

            now = time.time
        self._now = now
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._bound_port: int | None = None

    # ------------------------------------------------------------------ setup

    def build_app(self) -> web.Application:
        """The routed application, split out so tests can drive it directly."""
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/shelly", self._handle_shelly)
        app.router.add_get("/settings", self._handle_settings)
        if self._serve_gen1:
            app.router.add_get("/status", self._handle_gen1_status)
            app.router.add_get("/emeter/{index}", self._handle_gen1_emeter)
        app.router.add_route("*", "/reset_data", self._handle_reset_data)
        app.router.add_get("/rpc", self._handle_websocket)
        app.router.add_post("/rpc", self._handle_post_rpc)
        app.router.add_get("/rpc/{method}", self._handle_get_rpc)
        app.router.add_post("/rpc/{method}", self._handle_post_rpc_method)
        # Registered last so it also catches verbs the routes above do not
        # declare: a real device answers "no such handler" rather than the
        # "method not allowed" aiohttp would otherwise synthesise.
        app.router.add_route("*", "/{tail:.*}", self._handle_not_found)
        return app

    async def start(self) -> bool:
        """Bind the listener. ``False`` when the port is unavailable.

        Never raises for a bind failure: the UDP emulation and the mDNS
        announcement have to come up regardless, and on the runtimes where a
        privileged port is not bindable this is the expected outcome rather
        than an error.
        """
        if self._configured_port < 0:
            return False
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._configured_port)
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            logger.error(
                "Could not bind the Shelly HTTP port %s (errno %s: %s). The "
                "battery-facing UDP emulation is unaffected. Ports below 1024 "
                "need extra privileges — see docs/faq.md — or set TCP_PORT in "
                "[EMULATOR_SHELLYPRO3EM] to a port above 1024.",
                self._configured_port,
                exc.errno,
                exc.strerror or exc,
            )
            return False
        self._runner = runner
        self._site = site
        self._bound_port = self._resolve_bound_port(site)
        logger.info("Shelly HTTP surface listening on TCP port %s", self.port)
        return True

    @staticmethod
    def _resolve_bound_port(site: web.TCPSite) -> int | None:
        """The port actually bound, so ``TCP_PORT = 0`` is discoverable."""
        server = getattr(site, "_server", None)
        sockets = getattr(server, "sockets", None) or ()
        for sock in sockets:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError, ValueError):  # pragma: no cover
                continue
        return None

    async def stop(self) -> None:
        """Release the listener, tolerating a partial start."""
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._bound_port = None

    @property
    def port(self) -> int:
        """The port in effect — the bound one once listening."""
        if self._bound_port is not None:
            return self._bound_port
        return self._configured_port

    @property
    def running(self) -> bool:
        return self._runner is not None

    # -------------------------------------------------------------- middleware

    @web.middleware
    async def _middleware(
        self, request: web.Request, handler: Callable[[web.Request], Any]
    ) -> web.StreamResponse:
        try:
            response = await handler(request)
        except ShellyRpcError as err:
            response = _error_response(err)
        except web.HTTPRequestEntityTooLarge:
            response = _error_response(
                ShellyRpcError(-103, "Request body too large", status=413)
            )
        response.headers.setdefault("Server", SERVER_HEADER)
        return response

    # ------------------------------------------------------------------ context

    def _context(self) -> rpc.RequestContext:
        return rpc.RequestContext(
            identity=self._identity,
            profile=self._profile,
            udp_port=self._udp_port,
            announced_ip=self._announced_ip.value,
            settings=self._settings_store,
            energy=self._energy,
            now=self._now(),
            started_at=self._started_at,
        )

    @staticmethod
    def _client_ip(request: web.Request) -> str:
        peer = request.remote
        return peer or ""

    async def _reading_for(self, request: web.Request, key: str) -> list[float] | None:
        """The reading *key* needs, or ``None`` when it may go without.

        The two composite methods degrade rather than fail: they are what a
        smart-home integration fetches while adding the device, so refusing
        them would stop it being added at all.
        """
        required = rpc.NEEDS_READING[key]
        if not required and key not in rpc.TRIES_READING:
            return None
        client_ip = self._client_ip(request)
        try:
            reading = await self._read_reading(client_ip, key)
        except ShellyRpcError:
            if required:
                raise
            return None
        if reading is not None:
            self._energy.update(rpc.three_phases(reading))
        return reading

    # -------------------------------------------------------------- RPC routes

    async def _handle_get_rpc(self, request: web.Request) -> web.Response:
        name = request.match_info["method"]
        builder = rpc.METHODS.get(name.lower())
        if builder is None:
            raise ShellyRpcError(404, f"No handler for {name}")
        params: dict[str, Any] = dict(request.query)
        reading = await self._reading_for(request, name.lower())
        return _json_response(builder(self._context(), params, reading))

    async def _handle_post_rpc(self, request: web.Request) -> web.Response:
        body = await self._read_body(request)
        if isinstance(body, list):
            raise ShellyRpcError(-103, "Batch requests are not supported")
        if not isinstance(body, dict):
            raise ShellyRpcError(-103, "Malformed JSON-RPC request")
        name = body.get("method")
        request_id = body.get("id")
        source = body.get("src")
        if not isinstance(name, str) or not name:
            raise ShellyRpcError(-103, "Missing required 'method'")
        builder = rpc.METHODS.get(name.lower())
        envelope = {
            "id": request_id,
            "src": self._identity.shelly_id,
            "dst": source,
        }
        if builder is None:
            error = ShellyRpcError(404, f"No handler for {name}")
            return _json_response({**envelope, **error.body()}, status=404)
        params = self._params_from(body)
        try:
            reading = await self._reading_for(request, name.lower())
            result = builder(self._context(), params, reading)
        except ShellyRpcError as err:
            return _json_response({**envelope, **err.body()}, status=err.http_status())
        return _json_response({**envelope, "result": result})

    async def _handle_post_rpc_method(self, request: web.Request) -> web.Response:
        """``POST /rpc/<Method>``, in both the framed and params-only forms.

        A body carrying ``method`` is a full frame and gets the envelope back; a
        body without one *is* the parameters, which is the form the vendor
        documents, and gets the bare result.
        """
        name = request.match_info["method"]
        body = await self._read_body(request)
        if isinstance(body, dict) and isinstance(body.get("method"), str):
            return await self._handle_post_rpc_framed(request, body)
        builder = rpc.METHODS.get(name.lower())
        if builder is None:
            raise ShellyRpcError(404, f"No handler for {name}")
        if body is None:
            params: dict[str, Any] = {}
        elif isinstance(body, dict):
            params = self._params_from(body)
        else:
            raise ShellyRpcError(-103, "Malformed JSON-RPC request")
        params.update(request.query)
        reading = await self._reading_for(request, name.lower())
        return _json_response(builder(self._context(), params, reading))

    async def _handle_post_rpc_framed(
        self, request: web.Request, body: dict[str, Any]
    ) -> web.Response:
        name = str(body["method"])
        builder = rpc.METHODS.get(name.lower())
        envelope = {
            "id": body.get("id"),
            "src": self._identity.shelly_id,
            "dst": body.get("src"),
        }
        if builder is None:
            error = ShellyRpcError(404, f"No handler for {name}")
            return _json_response({**envelope, **error.body()}, status=404)
        try:
            reading = await self._reading_for(request, name.lower())
            result = builder(self._context(), self._params_from(body), reading)
        except ShellyRpcError as err:
            return _json_response({**envelope, **err.body()}, status=err.http_status())
        return _json_response({**envelope, "result": result})

    async def _read_body(self, request: web.Request) -> Any:
        """The request body as JSON, regardless of the declared content type.

        The header is not inspected: a client that omits it, or sends
        ``text/plain``, is still served.
        """
        raw = await request.read()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ShellyRpcError(-103, "Malformed JSON-RPC request") from None

    @staticmethod
    def _params_from(body: dict[str, Any]) -> dict[str, Any]:
        """The parameters of a frame, whether nested or flat."""
        params = body.get("params")
        if isinstance(params, dict):
            return dict(params)
        return {key: value for key, value in body.items() if key not in _ENVELOPE_KEYS}

    # --------------------------------------------------------------- websocket

    async def _handle_websocket(self, request: web.Request) -> web.StreamResponse:
        """The websocket channel, which is how smart-home integrations talk.

        A plain GET on this path is answered with an error rather than left to
        fail inside the upgrade machinery, so a client that probes it gets a
        usable reply.
        """
        websocket = web.WebSocketResponse()
        if not websocket.can_prepare(request).ok:
            raise ShellyRpcError(-103, "Expected a WebSocket upgrade on GET /rpc")
        await websocket.prepare(request)
        async for message in websocket:
            if message.type is not web.WSMsgType.TEXT:
                continue
            reply = await self._websocket_reply(request, message.data)
            if reply is not None:
                await websocket.send_str(wire.dumps(reply))
        return websocket

    async def _websocket_reply(
        self, request: web.Request, data: str
    ) -> dict[str, Any] | None:
        """The frame to answer *data* with.

        Every frame gets an answer, including the ones we cannot serve: a
        client left waiting for a reply that never comes hangs, which is worse
        than a clean error.
        """
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            return {
                "src": self._identity.shelly_id,
                **ShellyRpcError(-103, "Malformed JSON-RPC request").body(),
            }
        if not isinstance(body, dict):
            return {
                "src": self._identity.shelly_id,
                **ShellyRpcError(-103, "Malformed JSON-RPC request").body(),
            }
        envelope = {
            "id": body.get("id"),
            "src": self._identity.shelly_id,
            "dst": body.get("src"),
        }
        name = body.get("method")
        if not isinstance(name, str) or not name:
            return {
                **envelope,
                **ShellyRpcError(-103, "Missing required 'method'").body(),
            }
        builder = rpc.METHODS.get(name.lower())
        if builder is None:
            return {**envelope, **ShellyRpcError(404, f"No handler for {name}").body()}
        try:
            reading = await self._reading_for(request, name.lower())
            result = builder(self._context(), self._params_from(body), reading)
        except ShellyRpcError as err:
            return {**envelope, **err.body()}
        return {**envelope, "result": result}

    # -------------------------------------------------------------- page routes

    async def _handle_shelly(self, request: web.Request) -> web.Response:
        reading = await self._reading_for(request, rpc.PATH_SHELLY)
        return _json_response(rpc.shelly_get_device_info(self._context(), {}, reading))

    async def _handle_settings(self, request: web.Request) -> web.Response:
        reading = await self._reading_for(request, rpc.PATH_SETTINGS)
        return _json_response(rpc.settings_page(self._context(), {}, reading))

    async def _handle_gen1_status(self, request: web.Request) -> web.Response:
        reading = await self._reading_for(request, rpc.PATH_STATUS)
        return _json_response(rpc.gen1_status(self._context(), {}, reading))

    async def _handle_gen1_emeter(self, request: web.Request) -> web.Response:
        raw = request.match_info["index"]
        try:
            index = int(raw)
        except ValueError:
            raise ShellyRpcError(404, f"No handler for /emeter/{raw}") from None
        if not 0 <= index < self._profile.num_meters:
            raise ShellyRpcError(404, f"No handler for /emeter/{raw}")
        reading = await self._reading_for(request, rpc.PATH_EMETER)
        return _json_response(rpc.gen1_emeters(self._context(), reading)[index])

    async def _handle_reset_data(self, request: web.Request) -> web.Response:
        """Acknowledged; the energy counters are not persisted anyway."""
        return _json_response({})

    async def _handle_not_found(self, request: web.Request) -> web.Response:
        raise ShellyRpcError(404, f"No handler for {request.path}")
