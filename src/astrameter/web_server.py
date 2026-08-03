"""
Embedded web server for AstraMeter.

Exposes a health-check endpoint (used by Docker HEALTHCHECK and the
Home Assistant addon watchdog) and, when enabled, the live status
dashboard plus a browser-based configuration editor.
"""

import errno
import json
import os
import threading

from aiohttp import web

from astrameter.config.logger import logger
from astrameter.status import HA_SIMPLE
from astrameter.status.secrets import redact_sections, restore_sections
from astrameter.version_info import get_git_commit_sha

# Supervisor proxies every ingress request from this fixed address on the
# hassio bridge.  It is the *peer* address, so unlike X-Ingress-Path or
# X-Hass-Source it cannot be forged by a LAN client hitting the
# host-networked port directly.
INGRESS_PEER = "172.30.32.2"

# Only reachable in the add-on, where ingress is the intended way in. Kept to
# plain inline markup: the bundle is exactly what this response is refusing to
# hand out.
_REFUSED_HTML = b"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AstraMeter \xe2\x80\x94 not reachable from here</title>
<style>
body{font:16px/1.6 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.5rem}
code{background:#8883;padding:.1em .35em;border-radius:.25em}
</style>
<h1>Not reachable from here</h1>
<p>The AstraMeter dashboard opens from the <strong>Home Assistant sidebar</strong>,
which is what authenticates you. This port has no login of its own, so it is
refused by default.</p>
<p>To use this address instead, turn on <code>dashboard_direct_access</code> in the
add-on's configuration &mdash; understanding that anyone on your network can then
open it.</p>
"""


def _health_json_bytes():
    """Return the JSON health-check response body as UTF-8 bytes."""
    payload = {"status": "healthy", "service": "astrameter"}
    sha = get_git_commit_sha()
    if sha:
        payload["git_commit"] = sha
    return json.dumps(payload).encode("utf-8")


def _json(payload, status=200, **headers):
    """JSON response with no-store caching unless overridden."""
    headers.setdefault("Cache-Control", "no-store")
    return web.Response(
        body=json.dumps(payload).encode("utf-8"),
        status=status,
        content_type="application/json",
        headers=headers,
    )


_CONSUMER_SETTERS = {
    "manual_target": "set_consumer_manual_target",
    "auto_target": "set_consumer_auto_target",
    "active": "set_consumer_active",
    "distribution_weight": "set_consumer_distribution_weight",
    "efficiency_window_weight": "set_consumer_efficiency_window_weight",
    "min_dc_output": "set_consumer_min_dc_output",
}

# The CT002 setters themselves do not bound their inputs — the ranges live in
# the MQTT command handlers.  The dashboard must enforce exactly the same
# ones, or a value MQTT would reject could be set here and then silently
# reverted on the next broker reconnect.
_CONTROL_RANGES = {
    "manual_target": (-10000.0, 10000.0),
    "distribution_weight": (0.0, 10.0),
    "efficiency_window_weight": (0.0, 100.0),
    "min_dc_output": (0.0, 1000.0),
}

_CONTROL_BOOLS = ("active", "auto_target")

# Fields the wire carries in different units from the setter, mirroring the
# MQTT handlers: the entity is a percentage, the setter takes a fraction.
_CONTROL_SCALE = {"efficiency_window_weight": 0.01}


def _coerce_control_value(field, value):
    """Validate and coerce a control value, mirroring the MQTT bounds."""
    import math

    if field in _CONTROL_BOOLS:
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be true or false")
        return value
    low, high = _CONTROL_RANGES[field]
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{field} must be between {low:g} and {high:g}")
    return number * _CONTROL_SCALE.get(field, 1.0)


def addon_option_names(schema) -> set[str]:
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


class WebServer:
    """Async HTTP server exposing health, dashboard, config and API routes."""

    def __init__(
        self,
        port=52500,
        bind_address="0.0.0.0",
        config_path: str | None = None,
        enable_web_config: bool = False,
        status=None,
    ):
        """Initialise the service; call ``start()`` to bind the port."""
        self.port = port
        self.bind_address = bind_address
        self.config_path = config_path
        self.enable_web_config = enable_web_config
        self.status = status
        self._runner = None
        #: Whether an unrenderable add-on schema has already been reported.
        self._logged_schema_shape = False

    # -- gating --------------------------------------------------------

    @property
    def enable_dashboard(self) -> bool:
        return bool(self.status is not None and self.status.dashboard_enabled)

    def _is_ingress(self, request) -> bool:
        """True when the request arrived through Home Assistant ingress.

        Ingress requests are already authenticated by Home Assistant, which
        is what makes the dashboard's write surface safe without any auth of
        our own.
        """
        return request.remote == INGRESS_PEER

    def _trusted(self, request) -> bool:
        """True when this request may see or change anything sensitive.

        Fail-closed under the add-on: with ``host_network: true`` the port is
        on the LAN unauthenticated, so serving it must be opted into. Outside
        the add-on there is no ingress and the plain port is the only way in
        (see ``StatusRegistry.serves_direct``).
        """
        if self.status is None:
            return False
        return self._is_ingress(request) or self.status.serves_direct()

    def _may_write(self, request) -> bool:
        return bool(
            self.status is not None
            and self.status.allow_write
            and self._trusted(request)
        )

    def _actor(self, request) -> str:
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

    def build_app(self):
        """Assemble the aiohttp application.

        Split out from ``start()`` so tests exercise the real route table
        rather than a copy of it that can drift.
        """
        app = web.Application()
        # aiohttp auto-handles HEAD for GET routes.
        for path in ("/health", "/health/", "/api", "/api/"):
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
        # standalone WEB_CONFIG_ENABLED flag.
        if self.enable_web_config or self.enable_dashboard:
            app.router.add_get("/config", self._handle_config_ui)
            app.router.add_get("/config/", self._handle_config_ui)
            self._add("GET", app, "/api/config", self._handle_api_config_get)
            self._add("GET", app, "/api/key-types", self._handle_api_key_types)
            self._add("POST", app, "/api/config", self._handle_api_config_post)
            self._add("POST", app, "/api/restart", self._handle_api_restart)

        # Catch-all for unknown paths
        app.router.add_route("*", "/{path:.*}", self._handle_not_found)
        return app

    async def start(self):
        """Bind the TCP port and start serving. Returns True on success, False on failure."""
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bind_address, self.port)
        try:
            await site.start()
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                logger.error(
                    f"Port {self.port} is already in use. Web server not started."
                )
            else:
                logger.error(f"Failed to bind to {self.bind_address}:{self.port}: {e}")
            await self._runner.cleanup()
            self._runner = None
            return False

        logger.info(f"Web server started on {self.bind_address}:{self.port}")
        self._log_access_posture()
        return True

    @staticmethod
    def _add(method, app, path, handler):
        """Register *path* both with and without a trailing slash.

        A redirect would send a `Location` header, which both ingress hops
        copy verbatim — navigating the user out of the ingress prefix.
        """
        app.router.add_route(method, path, handler)
        app.router.add_route(method, path + "/", handler)

    def _log_access_posture(self):
        if not self.enable_dashboard:
            if self.enable_web_config and self.config_path:
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

    async def stop(self):
        """Tear down the aiohttp runner and release the port."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Web server stopped")

    def is_running(self):
        """Return True if the HTTP server is currently running."""
        return self._runner is not None

    async def _handle_health(self, request):
        """Respond to GET /health and /api with a JSON healthy status."""
        logger.debug(
            "Health check request received from %s",
            request.remote,
        )
        return web.Response(
            body=_health_json_bytes(),
            content_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    # -- dashboard -----------------------------------------------------

    async def _handle_dashboard(self, request):
        """Serve the single-page dashboard."""
        if not self._trusted(request):
            # A person typed this into a browser, so answer in prose. The
            # frontend carries the same explanation for a refused poll, but it
            # never loads when the page itself is the thing being refused.
            return web.Response(
                body=_REFUSED_HTML,
                status=403,
                content_type="text/html",
                charset="utf-8",
            )
        from astrameter.status.assets import dashboard_html

        html = dashboard_html()
        if html is None:
            return _json(
                {"error": "Dashboard asset missing from this build"}, status=503
            )
        return web.Response(
            body=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    async def _handle_api_status(self, request):
        """Live status snapshot, with a weak ETag for cheap polling."""
        if not self._trusted(request) or self.status is None:
            return _json({"error": "Forbidden"}, status=403)
        etag = self.status.etag()
        if request.headers.get("If-None-Match") == etag:
            return web.Response(
                status=304, headers={"ETag": etag, "Cache-Control": "no-store"}
            )
        snapshot = self.status.snapshot(ingress=self._is_ingress(request))
        return _json(snapshot, ETag=etag)

    # -- live control --------------------------------------------------

    def _device(self, device_id):
        if self.status is None:
            return None
        entry = self.status.devices.get(device_id)
        return entry.device if entry else None

    async def _handle_control_consumer(self, request):
        """Apply a per-battery control change."""
        if not self._may_write(request):
            return _json({"error": "Forbidden"}, status=403)
        try:
            body = await request.json()
            device_id = body["device_id"]
            consumer_id = body["consumer_id"]
            field = body["field"]
            value = body["value"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            return _json({"error": f"Invalid request: {exc}"}, status=400)

        device = self._device(device_id)
        setter_name = _CONSUMER_SETTERS.get(field)
        setter = getattr(device, setter_name, None) if device and setter_name else None
        if setter is None:
            return _json({"error": "Unknown device or field"}, status=404)
        try:
            coerced = _coerce_control_value(field, value)
            setter(consumer_id, coerced)
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)

        logger.info(
            "Dashboard control: %s %s.%s = %r by %s",
            device_id,
            consumer_id,
            field,
            value,
            self._actor(request),
        )
        # Mirror to the retained MQTT command topic, otherwise the broker's
        # redelivery on the next reconnect reverts what the user just set.
        insights = getattr(self.status, "insights", None)
        if insights is not None and hasattr(insights, "publish_consumer_command"):
            try:
                # The command topic carries the *entity* unit, the same one
                # this request arrived in — mirror the wire value, not the
                # scaled setter argument, or the replay on the next reconnect
                # reapplies a percentage as a fraction.
                await insights.publish_consumer_command(
                    device_id, consumer_id, field, value
                )
            except Exception:
                logger.exception("Failed to mirror control write to MQTT")
        self.status.bump()
        return _json({"applied": True, "rev": self.status.revision()})

    async def _handle_control_device(self, request):
        """Apply a device-wide control change."""
        if not self._may_write(request):
            return _json({"error": "Forbidden"}, status=403)
        try:
            body = await request.json()
            device_id = body["device_id"]
            field = body["field"]
            value = body.get("value", True)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            return _json({"error": f"Invalid request: {exc}"}, status=400)

        device = self._device(device_id)
        if device is None:
            return _json({"error": "Unknown device"}, status=404)
        try:
            if field == "active_control":
                device.set_active_control(bool(value))
            elif field == "force_rotation":
                device.force_efficiency_rotation()
            else:
                return _json({"error": "Unknown field"}, status=404)
        except (AttributeError, ValueError) as exc:
            return _json({"error": str(exc)}, status=400)

        logger.info(
            "Dashboard control: %s.%s = %r by %s",
            device_id,
            field,
            value,
            self._actor(request),
        )
        insights = getattr(self.status, "insights", None)
        if insights is not None and hasattr(insights, "publish_device_command"):
            try:
                await insights.publish_device_command(device_id, {field: value})
            except Exception:
                logger.exception("Failed to mirror device write to MQTT")
        self.status.bump()
        return _json({"applied": True, "rev": self.status.revision()})

    # -- Home Assistant add-on options ---------------------------------

    def _supervisor(self, request):
        """A Supervisor client, or an error response explaining why not."""
        from astrameter.addon_client import SupervisorClient

        if not self._may_write(request):
            return None, _json({"error": "Forbidden"}, status=403)
        client = SupervisorClient()
        if not client.available():
            return None, _json({"error": "Not running as a Home Assistant add-on"}, 409)
        return client, None

    async def _handle_addon_options_get(self, request):
        """Current add-on options plus their schema, secrets redacted."""
        client, error = self._supervisor(request)
        if error:
            return error
        try:
            info = await client.get_info()
        except Exception as exc:
            return _json({"error": str(exc)}, status=502)
        options = dict(info.get("options") or {})
        # Not `or {}`: that flattens exactly the malformed shapes worth
        # reporting — `[]` would arrive here as an object and the diagnostic
        # would say nothing. `None` is Supervisor's documented "no schema".
        schema = info.get("schema")
        self._log_unrenderable_schema(schema)
        return _json(
            {
                "options": redact_sections({"o": options})["o"],
                "schema": {} if schema is None else schema,
                "slug": info.get("slug"),
                "ingress_panel": info.get("ingress_panel"),
            }
        )

    def _log_unrenderable_schema(self, schema) -> None:
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

    async def _handle_addon_options_post(self, request):
        """Write add-on options through Supervisor.

        Supervisor replaces the whole persisted overlay and silently drops
        keys absent from the schema, so a typo would lose a value with no
        error — every key is validated against the live schema first.
        """
        client, error = self._supervisor(request)
        if error:
            return error
        try:
            body = await request.json()
            options = body["options"]
            if not isinstance(options, dict):
                raise ValueError("'options' must be an object")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return _json({"error": f"Invalid request: {exc}"}, status=400)

        try:
            info = await client.get_info()
        except Exception as exc:
            return _json({"error": str(exc)}, status=502)
        unknown = sorted(set(options) - addon_option_names(info.get("schema")))
        if unknown:
            return _json(
                {"error": f"Unknown add-on option(s): {', '.join(unknown)}"}, status=400
            )
        merged = restore_sections(
            {"o": options}, {"o": dict(info.get("options") or {})}
        )["o"]

        try:
            await client.set_options(merged)
        except Exception as exc:
            return _json({"error": str(exc)}, status=400)
        logger.info("Add-on options updated by %s", self._actor(request))
        if body.get("restart"):
            await client.restart()
            return _json({"saved": True, "restart": "supervisor"})
        return _json({"saved": True, "restart": "none"})

    async def _handle_ha_entities(self, request):
        """Home Assistant sensors that could be a grid-power source.

        Powers the entity picker in the guided form: typing a raw entity id
        from memory is the single easiest way to misconfigure AstraMeter, and
        a wrong id fails at start-up rather than here.
        """
        from astrameter.addon_client import SupervisorClient

        if not self._trusted(request):
            return _json({"error": "Forbidden"}, status=403)
        client = SupervisorClient()
        if not client.available():
            return _json({"error": "Not running as a Home Assistant add-on"}, 409)
        try:
            entities = await client.list_power_entities()
        except Exception as exc:
            # A failed lookup must not block the form — the field stays a
            # plain text input and the user can still type an id.
            logger.warning("Could not list Home Assistant entities: %s", exc)
            return _json({"entities": [], "error": str(exc)})
        return _json({"entities": entities}, **{"Cache-Control": "max-age=30"})

    async def _handle_addon_restart(self, request):
        client, error = self._supervisor(request)
        if error:
            return error
        logger.info("Add-on restart requested by %s", self._actor(request))
        await client.restart()
        return _json({"restarting": True}, status=202)

    async def _handle_config_mode_post(self, request):
        """Switch between add-on options and a custom ``config.ini``.

        Simple → file materialises the config the add-on just generated into
        the shared config dir, so the user starts from what is actually
        running rather than a blank file.
        """
        client, error = self._supervisor(request)
        if error:
            return error
        try:
            body = await request.json()
            target = body["mode"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            return _json({"error": f"Invalid request: {exc}"}, status=400)

        from astrameter.status.config_mode import materialize_config

        if target == "file":
            filename = (body.get("filename") or "astrameter.ini").strip()
            try:
                materialize_config(self.status.app_config, filename)
            except ValueError as exc:
                # A filename the user chose that target_path refuses.
                return _json({"error": str(exc)}, status=400)
            except OSError as exc:
                return _json({"error": f"Cannot write config file: {exc}"}, status=500)
            options = {"custom_config": filename}
        elif target == "options":
            options = {"custom_config": ""}
        else:
            return _json({"error": "mode must be 'file' or 'options'"}, status=400)

        try:
            info = await client.get_info()
            merged = {**(info.get("options") or {}), **options}
            await client.set_options(merged)
        except Exception as exc:
            return _json({"error": str(exc)}, status=400)
        logger.info(
            "Configuration mode switched to %r by %s", target, self._actor(request)
        )
        await client.restart()
        return _json({"switched": True, "mode": target, "restart": "supervisor"})

    # -- config.ini editor ---------------------------------------------

    async def _handle_config_ui(self, request):
        """Serve the HTML configuration editor at GET /config."""
        if not self._trusted(request) and self.status is not None:
            return _json({"error": "Forbidden"}, status=403)
        from astrameter.web_config import CONFIG_EDITOR_HTML

        return web.Response(
            body=CONFIG_EDITOR_HTML.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    async def _handle_api_key_types(self, request):
        """Return the section key-type metadata as JSON at GET /api/key-types."""
        if not self._trusted(request) and self.status is not None:
            return _json({"error": "Forbidden"}, status=403)
        from astrameter.web_config import section_key_types_json

        return web.Response(
            body=section_key_types_json().encode("utf-8"),
            content_type="application/json",
            headers={"Cache-Control": "max-age=3600"},
        )

    def _config_write_blocked(self, request):
        """Why a config.ini write is refused, or None when it is allowed."""
        if self.status is None:
            return None if self.enable_web_config else "Forbidden"
        if not self._may_write(request):
            return "Forbidden"
        if self.status.config_mode == HA_SIMPLE:
            return (
                "This add-on regenerates config.ini on every start, so an edit "
                "here would be lost. Change the add-on options instead."
            )
        return None

    async def _handle_api_config_get(self, request):
        """Return the current config.ini contents as JSON at GET /api/config."""
        if not self._trusted(request) and self.status is not None:
            return _json({"error": "Forbidden"}, status=403)
        from astrameter.web_config import read_config_as_dict

        if not self.config_path:
            return _json({"error": "Config path not set"}, status=500)
        try:
            sections, order = read_config_as_dict(self.config_path)
            return _json(
                {"sections": redact_sections(sections), "order": order},
                **{"Cache-Control": "no-store"},
            )
        except Exception:
            logger.exception("Error reading config")
            return _json({"error": "Internal server error"}, status=500)

    async def _handle_api_config_post(self, request):
        """Write updated config sections from the JSON body at POST /api/config."""
        import shutil
        import tempfile

        from astrameter.web_config import (
            read_config_as_dict,
            validate_config,
            write_config_from_dict,
        )

        blocked = self._config_write_blocked(request)
        if blocked:
            return _json({"error": blocked}, status=403)
        if not self.config_path:
            return _json({"error": "Config path not set"}, status=500)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            sections = data.get("sections", {})
            if not isinstance(sections, dict):
                raise ValueError("'sections' must be an object")
            order = data.get("order", list(sections.keys()))
            if not isinstance(order, list):
                raise ValueError("'order' must be a list")
            # A value still equal to the sentinel means "keep what is
            # stored", so a redacted secret is never written back as bullets.
            current, _ = read_config_as_dict(self.config_path)
            sections = restore_sections(sections, current)
            # Write to a temp copy and validate before touching the live file.
            dir_name = os.path.dirname(self.config_path) or "."
            with tempfile.NamedTemporaryFile(
                "w", dir=dir_name, suffix=".tmp", delete=False
            ) as tmp:
                tmp_path = tmp.name
            try:
                if os.path.exists(self.config_path):
                    shutil.copyfile(self.config_path, tmp_path)
                write_config_from_dict(tmp_path, sections, order)
                validate_config(tmp_path)
            except Exception:
                os.unlink(tmp_path)
                raise
            os.unlink(tmp_path)
            write_config_from_dict(self.config_path, sections, order)
            logger.info("Configuration updated by %s", self._actor(request))
            return _json({"success": True})
        except (ValueError, json.JSONDecodeError) as e:
            logger.error("Invalid config request: %s", e)
            return _json({"error": str(e)}, status=400)
        except Exception:
            logger.exception("Error saving config")
            return _json({"error": "Internal server error"}, status=500)

    async def _handle_api_restart(self, request):
        """Acknowledge POST /api/restart and schedule an in-process restart via SIGUSR1."""
        import signal

        if self.status is not None and not self._may_write(request):
            return _json({"error": "Forbidden"}, status=403)
        logger.info("Restart requested by %s", self._actor(request))
        if self.status is not None:
            self.status.restart_pending = True
            self.status.bump()
        # SIGUSR1 so the handler in main.py restarts the device cycle instead
        # of exiting.  The web server itself survives it.
        threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGUSR1)).start()
        return _json({"restarting": True}, status=202)

    async def _handle_not_found(self, request):
        """Return a 404 JSON response for any unmatched route."""
        return _json({"error": "Not Found"}, status=404)
