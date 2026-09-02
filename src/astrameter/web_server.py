"""
Embedded web server for AstraMeter.

Exposes a health-check endpoint (used by Docker HEALTHCHECK and the
Home Assistant addon watchdog) and, when enabled, the live status
dashboard plus a browser-based configuration editor.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import signal
import tempfile
import threading
from collections.abc import Awaitable, Iterable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from astrameter.addon_client import SupervisorClient
from astrameter.config.logger import logger
from astrameter.ct002.controls import CONSUMER_CONTROLS_BY_FIELD, apply_device_control
from astrameter.status.assets import dashboard_html
from astrameter.status.config_mode import materialize_config
from astrameter.status.secrets import redact_sections, restore_sections
from astrameter.version_info import get_git_commit_sha
from astrameter.web_config import (
    CONFIG_EDITOR_HTML,
    SECTION_KEY_TYPES,
    read_config_as_dict,
    validate_config,
    write_config_from_dict,
)
from astrameter.web_guard import (
    REFUSED_HTML,
    ApiError,
    Handler,
    RefusedHostLog,
    answers_api_errors,
    error_response,
    forbidden,
    foreign_host_response,
    is_allowed_host,
    json_response,
    parse_allowed_hosts,
    requires_json_content_type,
)

if TYPE_CHECKING:
    from astrameter.status.registry import StatusRegistry

# Supervisor proxies every ingress request from this fixed address on the
# hassio bridge.  It is the *peer* address, so unlike X-Ingress-Path or
# X-Hass-Source it cannot be forged by a LAN client hitting the
# host-networked port directly.
INGRESS_PEER = "172.30.32.2"

#: How long a deferred restart waits before firing, so the response that asked
#: for it is on the wire before the container goes down.
RESTART_GRACE_S = 0.5

#: The health check, which is exempt from the host guard below: Docker's
#: HEALTHCHECK and the add-on watchdog reach it under whatever name the
#: operator's monitoring uses, and it neither reads state nor writes anything.
HEALTH_PATHS = ("/health", "/health/", "/api", "/api/")

#: What a malformed request body is answered with, whichever route read it.
_BAD_BODY = (KeyError, TypeError, ValueError, json.JSONDecodeError)


def addon_option_names(schema: Any) -> set[str]:
    """Every option name in an add-on schema, whichever shape it arrived in.

    Supervisor renders the add-on's declared schema before serving it: what
    ``/addons/self/info`` returns is a *list* of field descriptors
    (``{"name": "ct_mac", "optional": true, "type": "string"}``), not the
    ``name: validator`` mapping config.yaml declares. Both are read here so
    the same code works against either — and so a list cannot reach ``set()``,
    where its unhashable entries used to abort the save with a 500.
    """
    if isinstance(schema, list):
        return {
            entry["name"]
            for entry in schema
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
    if isinstance(schema, dict):
        return {key for key in schema if isinstance(key, str)}
    return set()


def _unrenderable_descriptors(schema: list) -> dict[str, str]:
    """Field descriptors the guided form has to show read-only, by name.

    A repeated option (``multiple``) or a nested block (``type: "schema"``)
    holds a list or an object; a text box over one would write a string back
    and flatten it.
    """
    odd: dict[str, str] = {}
    for index, entry in enumerate(schema):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            odd[f"#{index}"] = type(entry).__name__
            continue
        kind = entry.get("type")
        if kind == "schema":
            odd[entry["name"]] = (
                "repeated nested block" if entry.get("multiple") else "nested block"
            )
        elif entry.get("multiple"):
            odd[entry["name"]] = f"repeated {kind}"
    return odd


async def _body(request: web.Request) -> dict[str, Any]:
    """The request's JSON object, or an :class:`ApiError` naming what was wrong."""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
    except _BAD_BODY as exc:
        raise ApiError(f"Invalid request: {exc}", status=400) from exc
    return body


def _required(body: dict[str, Any], *keys: str) -> list[Any]:
    """The values of *keys*, refusing the request when one is missing."""
    try:
        return [body[key] for key in keys]
    except KeyError as exc:
        raise ApiError(f"Invalid request: {exc}", status=400) from exc


class WebServer:
    """Async HTTP server exposing health, dashboard, config and API routes."""

    def __init__(
        self,
        port: int = 52500,
        bind_address: str = "0.0.0.0",
        config_path: str | None = None,
        enable_web_config: bool | None = None,
        status: StatusRegistry | None = None,
        allowed_hosts: str | Iterable[str] | None = None,
    ):
        """Initialise the service; call ``start()`` to bind the port."""
        self.port = port
        self.bind_address = bind_address
        self.config_path = config_path
        self.enable_web_config = enable_web_config
        self.status = status
        self.allowed_hosts = parse_allowed_hosts(allowed_hosts)
        # The dashboard hides its Configuration tab when the backend names no
        # config_mode, which is how an ESPHome device says it has nothing to
        # edit. Say the same here when the editor is off, so the tab is never
        # a link to routes this server does not serve.
        if status is not None and not self.serve_config_editor:
            status.config_editor = False
        self._runner: web.AppRunner | None = None
        self._refused_hosts = RefusedHostLog()
        #: Whether an unrenderable add-on schema has already been reported.
        self._logged_schema_shape = False
        #: Deferred restarts, held so the loop cannot collect them mid-flight.
        self._pending_restarts: set[asyncio.Task[None]] = set()

    # -- gating --------------------------------------------------------

    @property
    def enable_dashboard(self) -> bool:
        return bool(self.status is not None and self.status.dashboard_enabled)

    @property
    def serve_config_editor(self) -> bool:
        """Whether the ``config.ini`` editor is part of this server.

        ``WEB_CONFIG_ENABLED`` is tri-state deliberately. Left unset it
        follows the dashboard, whose Configuration tab *is* the editor — the
        flag is not something a new user should have to find to get the
        feature the page advertises. Set, it is an explicit answer either way:
        ``True`` serves the editor with no dashboard, which is what the flag
        meant before there was one, and ``False`` keeps it off even when the
        dashboard is on. That last case is the point. The dashboard defaults
        on and writable, so without it, upgrading would hand back an
        unauthenticated config surface to the one user who went looking for
        the switch that turns it off — and a config write is a ``[SCRIPT]``
        section away from being a shell command.
        """
        if self.enable_web_config is None:
            return self.enable_dashboard
        return self.enable_web_config

    def _is_ingress(self, request: web.Request) -> bool:
        """True when the request arrived through Home Assistant ingress.

        Ingress requests are already authenticated by Home Assistant, which
        is what makes the dashboard's write surface safe without any auth of
        our own.
        """
        return request.remote == INGRESS_PEER

    def _refuse_foreign_host(self, request: web.Request) -> web.StreamResponse | None:
        """Refuse a request whose ``Host`` is a name that could be rebound.

        Returns the refusal, or ``None`` to let the request through. See
        :func:`is_allowed_host` for what the guard is defending against.

        Ingress is exempt: the Supervisor sets ``Host`` to whatever name the
        user reaches Home Assistant under, which is a name we cannot know and
        do not need to — the peer address already proves the hop, and Home
        Assistant has authenticated the user before this point.
        """
        if request.path in HEALTH_PATHS or self._is_ingress(request):
            return None
        host = request.headers.get("Host", "")
        if is_allowed_host(host, self.allowed_hosts):
            return None
        shown = host or "(no Host header)"
        self._refused_hosts.log(shown, request.path)
        return foreign_host_response(request, shown)

    def _trusted(self, request: web.Request) -> bool:
        """True when this request may see or change anything sensitive.

        Fail-closed under the add-on: with ``host_network: true`` the port is
        on the LAN unauthenticated, so serving it must be opted into. Outside
        the add-on there is no ingress and the plain port is the only way in
        (see ``StatusRegistry.serves_direct``).
        """
        if self.status is None:
            return False
        return self._is_ingress(request) or self.status.serves_direct()

    def _may_read(self, request: web.Request) -> bool:
        """Like :meth:`_trusted`, but a server with no registry serves anyway.

        The config editor can run standalone, with nothing to be trusted
        *about*; its own ``serve_config_editor`` gate decided that already.
        """
        return self.status is None or self._trusted(request)

    def _may_write(self, request: web.Request) -> bool:
        return self.status is not None and self.status.may_write(
            ingress=self._is_ingress(request)
        )

    def _actor(self, request: web.Request) -> str:
        """Who made a mutating request, for the audit log."""
        # Home Assistant sets these on an ingress request. Reached directly the
        # port is on the LAN, where any client can send the same headers, so
        # believing them there would let a caller sign the audit trail with
        # someone else's name.
        if not self._is_ingress(request):
            return f"direct {request.remote}"
        name = request.headers.get("X-Remote-User-Display-Name")
        uid = request.headers.get("X-Remote-User-Id")
        if name or uid:
            return f"{name or 'unknown'} ({uid or 'no id'})"
        return f"direct {request.remote}"

    # -- wiring --------------------------------------------------------

    def build_app(self) -> web.Application:
        """Assemble the aiohttp application.

        Split out from ``start()`` so tests exercise the real route table
        rather than a copy of it that can drift.
        """

        # The host guard runs as middleware rather than per route: unlike the
        # content-type check it also has to cover the two pages registered
        # directly below (``/`` and ``/config``), and a document served under a
        # rebound name is the request that goes on to drive the API.
        @web.middleware
        async def host_guard(
            request: web.Request, handler: Handler
        ) -> web.StreamResponse:
            refusal = self._refuse_foreign_host(request)
            if refusal is not None:
                return refusal
            return await handler(request)

        app = web.Application(middlewares=[host_guard])
        # aiohttp auto-handles HEAD for GET routes.
        for path in HEALTH_PATHS:
            app.router.add_get(path, self._handle_health)

        if self.enable_dashboard:
            # Registered once: aiohttp raises on a duplicate resource, and
            # "/" and "" are the same route.
            app.router.add_get("/", self._handle_dashboard)
            self._add("GET", app, "/api/status", self._handle_api_status)
            self._add(
                "POST", app, "/api/control/consumer", self._handle_control_consumer
            )
            self._add("POST", app, "/api/control/device", self._handle_control_device)
            self._add("GET", app, "/api/addon/options", self._handle_addon_options_get)
            self._add(
                "POST", app, "/api/addon/options", self._handle_addon_options_post
            )
            self._add("POST", app, "/api/addon/restart", self._handle_addon_restart)
            self._add("GET", app, "/api/ha/entities", self._handle_ha_entities)
            self._add("POST", app, "/api/config-mode", self._handle_config_mode_post)

        # The INI editor is reachable either from the dashboard or from the
        # standalone WEB_CONFIG_ENABLED flag — see `serve_config_editor` for
        # how the two combine, and why a flag set to False wins over both.
        if self.serve_config_editor:
            app.router.add_get("/config", self._handle_config_ui)
            app.router.add_get("/config/", self._handle_config_ui)
            self._add("GET", app, "/api/config", self._handle_api_config_get)
            self._add("GET", app, "/api/key-types", self._handle_api_key_types)
            self._add("POST", app, "/api/config", self._handle_api_config_post)
            self._add("POST", app, "/api/restart", self._handle_api_restart)

        # Catch-all for unknown paths
        app.router.add_route("*", "/{path:.*}", self._handle_not_found)
        return app

    @staticmethod
    def _add(method: str, app: web.Application, path: str, handler: Handler) -> None:
        """Register *path* both with and without a trailing slash.

        A redirect would send a `Location` header, which both ingress hops
        copy verbatim — navigating the user out of the ingress prefix.

        Every API route is wrapped here rather than at each handler, so a route
        added later cannot forget either wrapper: an :class:`ApiError` becomes
        the response it names, and every ``POST`` passes the JSON content-type
        guard — see :data:`astrameter.web_guard.JSON_CONTENT_TYPE` for what
        that one defends against.
        """
        handler = answers_api_errors(handler)
        if method == "POST":
            handler = requires_json_content_type(handler)
        app.router.add_route(method, path, handler)
        app.router.add_route(method, path + "/", handler)

    async def start(self) -> bool:
        """Bind the TCP port and start serving. Returns True on success, False on failure."""
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bind_address, self.port)
        try:
            await site.start()
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                logger.error(
                    "Port %s is already in use. Web server not started.", self.port
                )
            else:
                logger.error(
                    "Failed to bind to %s:%s: %s", self.bind_address, self.port, exc
                )
            await self._runner.cleanup()
            self._runner = None
            return False

        logger.info("Web server started on %s:%s", self.bind_address, self.port)
        self._log_access_posture()
        return True

    def _log_access_posture(self) -> None:
        if not self.enable_dashboard:
            if self.serve_config_editor and self.config_path:
                logger.warning(
                    "Config editor is ENABLED — unauthenticated read/write access "
                    "is active. Disable WEB_CONFIG_ENABLED when not in use."
                )
            return
        mode = self.status.config_mode if self.status else "unknown"
        logger.info("Dashboard enabled (config mode: %s)", mode)
        if self.status is None or not self.status.serves_direct():
            return
        if self.status.under_supervisor():
            logger.warning(
                "Dashboard direct access is ENABLED: %s:%s is reachable without "
                "Home Assistant authentication. Only enable this on a trusted "
                "network.",
                self.bind_address,
                self.port,
            )
        else:
            # Not an opt-in here — it is the only way in — but the page is
            # still unauthenticated, which the operator should know.
            logger.info(
                "Dashboard is served on %s:%s with no authentication.",
                self.bind_address,
                self.port,
            )

    async def stop(self) -> None:
        """Tear down the aiohttp runner and release the port."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Web server stopped")

    def is_running(self) -> bool:
        """Return True if the HTTP server is currently running."""
        return self._runner is not None

    async def _handle_health(self, request: web.Request) -> web.StreamResponse:
        """Respond to GET /health and /api with a JSON healthy status."""
        logger.debug("Health check request received from %s", request.remote)
        payload = {"status": "healthy", "service": "astrameter"}
        sha = get_git_commit_sha()
        if sha:
            payload["git_commit"] = sha
        return json_response(payload, cache="no-cache")

    # -- dashboard -----------------------------------------------------

    async def _handle_dashboard(self, request: web.Request) -> web.StreamResponse:
        """Serve the single-page dashboard."""
        if not self._trusted(request):
            # A person typed this into a browser, so answer in prose. The
            # frontend carries the same explanation for a refused poll, but it
            # never loads when the page itself is the thing being refused.
            return web.Response(status=403, text=REFUSED_HTML, content_type="text/html")
        html = dashboard_html()
        if html is None:
            return error_response("Dashboard asset missing from this build", status=503)
        return web.Response(
            body=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    async def _handle_api_status(self, request: web.Request) -> web.StreamResponse:
        """Live status snapshot, with a weak ETag for cheap polling."""
        if not self._trusted(request) or self.status is None:
            return forbidden()
        etag = self.status.etag()
        if request.headers.get("If-None-Match") == etag:
            return web.Response(
                status=304, headers={"ETag": etag, "Cache-Control": "no-store"}
            )
        snapshot = self.status.snapshot(ingress=self._is_ingress(request))
        return json_response(snapshot, ETag=etag)

    # -- live control --------------------------------------------------

    def _device(self, device_id: str) -> Any:
        if self.status is None:
            return None
        entry = self.status.devices.get(device_id)
        return entry.device if entry else None

    async def _mirror_to_mqtt(self, kind: str, publish: Awaitable[None]) -> None:
        """Mirror a control write to the retained MQTT command topic.

        Without it the broker's redelivery on the next reconnect reverts what
        the user just set.
        """
        try:
            await publish
        except Exception:
            logger.exception("Failed to mirror %s write to MQTT", kind)

    def _insights(self) -> Any:
        """The MQTT Insights service, when one is running."""
        return self.status.insights if self.status is not None else None

    def _applied(self) -> web.StreamResponse:
        """Acknowledge a control write with the revision it produced."""
        assert self.status is not None  # _may_write() already refused otherwise
        self.status.bump()
        return json_response({"applied": True, "rev": self.status.revision()})

    async def _handle_control_consumer(
        self, request: web.Request
    ) -> web.StreamResponse:
        """Apply a per-battery control change."""
        if not self._may_write(request):
            return forbidden()
        body = await _body(request)
        device_id, consumer_id, field, value = _required(
            body, "device_id", "consumer_id", "field", "value"
        )

        device = self._device(device_id)
        control = CONSUMER_CONTROLS_BY_FIELD.get(field)
        if device is None or control is None:
            return error_response("Unknown device or field", status=404)
        try:
            control.apply(device, consumer_id, control.coerce(value))
        except AttributeError:
            # A device without this setter — a Shelly emulation, say — is
            # registered too, and has no per-battery controls.
            return error_response("Unknown device or field", status=404)
        except ValueError as exc:
            return error_response(str(exc), status=400)

        logger.info(
            "Dashboard control: %s %s.%s = %r by %s",
            device_id,
            consumer_id,
            field,
            value,
            self._actor(request),
        )
        insights = self._insights()
        if insights is not None:
            # The command topic carries the *entity* unit, the same one this
            # request arrived in — mirror the wire value, not the scaled setter
            # argument, or the replay on the next reconnect reapplies a
            # percentage as a fraction.
            await self._mirror_to_mqtt(
                "control",
                insights.publish_consumer_command(device_id, consumer_id, field, value),
            )
        return self._applied()

    async def _handle_control_device(self, request: web.Request) -> web.StreamResponse:
        """Apply a device-wide control change."""
        if not self._may_write(request):
            return forbidden()
        body = await _body(request)
        (device_id, field) = _required(body, "device_id", "field")
        value = body.get("value", True)

        device = self._device(device_id)
        if device is None:
            return error_response("Unknown device", status=404)
        try:
            apply_device_control(device, field, bool(value))
        except KeyError:
            return error_response("Unknown field", status=404)
        except (AttributeError, ValueError) as exc:
            return error_response(str(exc), status=400)

        logger.info(
            "Dashboard control: %s.%s = %r by %s",
            device_id,
            field,
            value,
            self._actor(request),
        )
        insights = self._insights()
        if insights is not None:
            await self._mirror_to_mqtt(
                "device", insights.publish_device_command(device_id, {field: value})
            )
        return self._applied()

    # -- Home Assistant add-on options ---------------------------------

    def _supervisor(self, request: web.Request) -> SupervisorClient:
        """A Supervisor client for a request allowed to write through it."""
        if not self._may_write(request):
            raise ApiError("Forbidden", status=403)
        client = SupervisorClient()
        if not client.available():
            raise ApiError("Not running as a Home Assistant add-on", status=409)
        return client

    @staticmethod
    async def _addon_info(client: SupervisorClient) -> dict[str, Any]:
        """The add-on's current options and schema, as Supervisor reports them."""
        try:
            return await client.get_info()
        except Exception as exc:
            raise ApiError(str(exc), status=502) from exc

    async def _handle_addon_options_get(
        self, request: web.Request
    ) -> web.StreamResponse:
        """Current add-on options plus their schema, secrets redacted."""
        info = await self._addon_info(self._supervisor(request))
        options = dict(info.get("options") or {})
        # Not `or {}`: that flattens exactly the malformed shapes worth
        # reporting — `[]` would arrive here as an object and the diagnostic
        # would say nothing. `None` is Supervisor's documented "no schema".
        schema = info.get("schema")
        self._log_unrenderable_schema(schema)
        return json_response(
            {
                "options": redact_sections({"o": options})["o"],
                "schema": {} if schema is None else schema,
                "slug": info.get("slug"),
                "ingress_panel": info.get("ingress_panel"),
            }
        )

    def _log_unrenderable_schema(self, schema: Any) -> None:
        """Name any schema entry the guided form cannot turn into a control.

        A repeated or nested option renders read-only, and without this the
        only clue is a greyed-out field. Says it once: the log is where an
        operator looks, and the route is hit on every visit to the tab.
        Types and option names only, never a value.
        """
        # `null` is documented and means the add-on declares no schema; the
        # form says so on its own and there is nothing wrong to report.
        if self._logged_schema_shape or schema is None:
            return
        if isinstance(schema, list):
            odd = _unrenderable_descriptors(schema)
        elif isinstance(schema, dict):
            odd = {
                k: type(v).__name__ for k, v in schema.items() if not isinstance(v, str)
            }
        else:
            self._logged_schema_shape = True
            logger.warning(
                "Supervisor returned the add-on schema as %s, which is neither "
                "a list of fields nor an object; the guided form cannot build "
                "controls from it. Please report this with your Home Assistant "
                "version.",
                type(schema).__name__,
            )
            return
        if odd:
            self._logged_schema_shape = True
            logger.warning(
                "Add-on options the guided form cannot edit: %s. They are shown "
                "read-only; change them on the add-on's own Configuration page.",
                ", ".join(f"{k} ({t})" for k, t in sorted(odd.items())),
            )

    async def _handle_addon_options_post(
        self, request: web.Request
    ) -> web.StreamResponse:
        """Write add-on options through Supervisor.

        Supervisor replaces the whole persisted overlay and silently drops
        keys absent from the schema, so a typo would lose a value with no
        error — every key is validated against the live schema first.
        """
        client = self._supervisor(request)
        body = await _body(request)
        (options,) = _required(body, "options")
        if not isinstance(options, dict):
            raise ApiError("Invalid request: 'options' must be an object", status=400)

        info = await self._addon_info(client)
        unknown = sorted(set(options) - addon_option_names(info.get("schema")))
        if unknown:
            return error_response(
                f"Unknown add-on option(s): {', '.join(unknown)}", status=400
            )
        merged = restore_sections(
            {"o": options}, {"o": dict(info.get("options") or {})}
        )["o"]
        await self._set_options(client, merged)

        logger.info("Add-on options updated by %s", self._actor(request))
        if body.get("restart"):
            self._restart_after_response(client)
            return json_response({"saved": True, "restart": "supervisor"})
        return json_response({"saved": True, "restart": "none"})

    @staticmethod
    async def _set_options(client: SupervisorClient, options: dict[str, Any]) -> None:
        try:
            await client.set_options(options)
        except Exception as exc:
            raise ApiError(str(exc), status=400) from exc

    async def _handle_ha_entities(self, request: web.Request) -> web.StreamResponse:
        """Home Assistant sensors that could be a grid-power source.

        Powers the entity picker in the guided form: typing a raw entity id
        from memory is the single easiest way to misconfigure AstraMeter, and
        a wrong id fails at start-up rather than here.
        """
        if not self._trusted(request):
            return forbidden()
        client = SupervisorClient()
        if not client.available():
            return error_response("Not running as a Home Assistant add-on", status=409)
        try:
            entities = await client.list_power_entities()
        except Exception as exc:
            # A failed lookup must not block the form — the field stays a
            # plain text input and the user can still type an id.
            logger.warning("Could not list Home Assistant entities: %s", exc)
            return json_response({"entities": [], "error": str(exc)})
        return json_response({"entities": entities}, cache="max-age=30")

    def _restart_after_response(self, client: SupervisorClient) -> None:
        """Ask Supervisor to restart us, once this response is on the wire.

        The restart tears down the container serving the request, so awaiting
        it inside the handler kills the process before the reply is flushed:
        the browser gets a 502 from the ingress proxy for a call that in fact
        succeeded, and the page shows an error for a switch that worked.
        Deferring it by a beat lets the reply land first — the dashboard then
        shows its own "restarting" state and reconnects when we come back.
        """

        async def restart() -> None:
            await asyncio.sleep(RESTART_GRACE_S)
            try:
                await client.restart()
            except Exception:
                # Nothing is left to answer to: the reply went out long ago.
                logger.exception("Add-on restart failed")

        task = asyncio.get_running_loop().create_task(restart())
        # A task nothing holds can be collected before it runs.
        self._pending_restarts.add(task)
        task.add_done_callback(self._pending_restarts.discard)

    async def _handle_addon_restart(self, request: web.Request) -> web.StreamResponse:
        client = self._supervisor(request)
        logger.info("Add-on restart requested by %s", self._actor(request))
        self._restart_after_response(client)
        return json_response({"restarting": True}, status=202)

    async def _handle_config_mode_post(
        self, request: web.Request
    ) -> web.StreamResponse:
        """Switch between add-on options and a custom ``config.ini``.

        Simple → file materialises the config the add-on just generated into
        the shared config dir, so the user starts from what is actually
        running rather than a blank file.
        """
        client = self._supervisor(request)
        body = await _body(request)
        (target,) = _required(body, "mode")

        if target == "file":
            filename = (body.get("filename") or "astrameter.ini").strip()
            options = {"custom_config": self._materialize(filename)}
        elif target == "options":
            options = {"custom_config": ""}
        else:
            return error_response("mode must be 'file' or 'options'", status=400)

        info = await self._addon_info(client)
        await self._set_options(client, {**(info.get("options") or {}), **options})
        logger.info(
            "Configuration mode switched to %r by %s", target, self._actor(request)
        )
        self._restart_after_response(client)
        return json_response(
            {"switched": True, "mode": target, "restart": "supervisor"}
        )

    def _materialize(self, filename: str) -> str:
        """Write the running config to *filename*, and return the name stored."""
        assert self.status is not None  # _supervisor() already refused otherwise
        try:
            materialize_config(self.status.app_config, filename)
        except ValueError as exc:
            # A filename the user chose that target_path refuses.
            raise ApiError(str(exc), status=400) from exc
        except OSError as exc:
            raise ApiError(f"Cannot write config file: {exc}", status=500) from exc
        return filename

    # -- config.ini editor ---------------------------------------------

    async def _handle_config_ui(self, request: web.Request) -> web.StreamResponse:
        """Serve the HTML configuration editor at GET /config."""
        if not self._may_read(request):
            return forbidden()
        return web.Response(
            body=CONFIG_EDITOR_HTML.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    async def _handle_api_key_types(self, request: web.Request) -> web.StreamResponse:
        """Return the section key-type metadata as JSON at GET /api/key-types."""
        if not self._may_read(request):
            return forbidden()
        return json_response(SECTION_KEY_TYPES, cache="max-age=3600")

    def _config_write_blocked(self, request: web.Request) -> str | None:
        """Why a config.ini write is refused, or None when it is allowed.

        The rule itself is ``StatusRegistry.config_writable``, so what the
        dashboard advertises and what this enforces cannot drift apart; only
        the wording of the refusal is decided here.
        """
        if self.status is None:
            return None if self.serve_config_editor else "Forbidden"
        if not self._may_write(request):
            return "Forbidden"
        if not self.status.config_writable(ingress=self._is_ingress(request)):
            return (
                "This add-on regenerates config.ini on every start, so an edit "
                "here would be lost. Change the add-on options instead."
            )
        return None

    def _require_config_path(self) -> str:
        if not self.config_path:
            raise ApiError("Config path not set", status=500)
        return self.config_path

    async def _handle_api_config_get(self, request: web.Request) -> web.StreamResponse:
        """Return the current config.ini contents as JSON at GET /api/config."""
        if not self._may_read(request):
            return forbidden()
        path = self._require_config_path()
        try:
            sections, order = read_config_as_dict(path)
            return json_response(
                {"sections": redact_sections(sections), "order": order}
            )
        except Exception:
            logger.exception("Error reading config")
            return error_response("Internal server error", status=500)

    async def _handle_api_config_post(self, request: web.Request) -> web.StreamResponse:
        """Write updated config sections from the JSON body at POST /api/config."""
        blocked = self._config_write_blocked(request)
        if blocked:
            return error_response(blocked, status=403)
        path = self._require_config_path()
        body = await _body(request)

        try:
            sections = body.get("sections", {})
            if not isinstance(sections, dict):
                raise ValueError("'sections' must be an object")
            order = body.get("order", list(sections.keys()))
            if not isinstance(order, list):
                raise ValueError("'order' must be a list")
            # A value still equal to the sentinel means "keep what is
            # stored", so a redacted secret is never written back as bullets.
            current, _ = read_config_as_dict(path)
            sections = restore_sections(sections, current)
            _validate_config_write(path, sections, order)
            write_config_from_dict(path, sections, order)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error("Invalid config request: %s", exc)
            return error_response(str(exc), status=400)
        except Exception:
            logger.exception("Error saving config")
            return error_response("Internal server error", status=500)

        logger.info("Configuration updated by %s", self._actor(request))
        return json_response({"success": True})

    async def _handle_api_restart(self, request: web.Request) -> web.StreamResponse:
        """Acknowledge POST /api/restart and schedule an in-process restart via SIGUSR1."""
        if self.status is not None and not self._may_write(request):
            return forbidden()
        logger.info("Restart requested by %s", self._actor(request))
        if self.status is not None:
            self.status.restart_pending = True
            self.status.bump()
        # SIGUSR1 so the handler in main.py restarts the device cycle instead
        # of exiting.  The web server itself survives it.
        threading.Timer(
            RESTART_GRACE_S, lambda: os.kill(os.getpid(), signal.SIGUSR1)
        ).start()
        return json_response({"restarting": True}, status=202)

    async def _handle_not_found(self, request: web.Request) -> web.StreamResponse:
        """Return a 404 JSON response for any unmatched route."""
        return error_response("Not Found", status=404)


def _validate_config_write(path: str, sections: dict, order: list) -> None:
    """Trial-write *sections* beside *path* and load it, before touching the live file.

    A config that fails to parse would otherwise take the service down on its
    next start, with the editor reporting success.
    """
    with tempfile.NamedTemporaryFile(
        "w", dir=os.path.dirname(path) or ".", suffix=".tmp", delete=False
    ) as tmp:
        trial = tmp.name
    try:
        if os.path.exists(path):
            shutil.copyfile(path, trial)
        write_config_from_dict(trial, sections, order)
        validate_config(trial)
    finally:
        os.unlink(trial)
