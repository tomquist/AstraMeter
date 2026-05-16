"""
Embedded web server for AstraMeter.

Exposes a health-check endpoint (used by Docker HEALTHCHECK and the
Home Assistant addon watchdog) and, when enabled, a browser-based
configuration editor.
"""

import errno
import json
import os
import threading

from aiohttp import web

from astrameter.config.logger import logger
from astrameter.version_info import get_git_commit_sha


def _health_json_bytes():
    """Return the JSON health-check response body as UTF-8 bytes."""
    payload = {"status": "healthy", "service": "astrameter"}
    sha = get_git_commit_sha()
    if sha:
        payload["git_commit"] = sha
    return json.dumps(payload).encode("utf-8")


class WebServer:
    """Async HTTP server exposing health, config-editor and API routes."""

    def __init__(
        self,
        port=52500,
        bind_address="0.0.0.0",
        config_path: str | None = None,
        enable_web_config: bool = False,
    ):
        """Initialise the service; call ``start()`` to bind the port."""
        self.port = port
        self.bind_address = bind_address
        self.config_path = config_path
        self.enable_web_config = enable_web_config
        self._runner = None

    async def start(self):
        """Bind the TCP port and start serving. Returns True on success, False on failure."""
        app = web.Application()
        # aiohttp auto-handles HEAD for GET routes.
        for path in ("/health", "/health/", "/api", "/api/"):
            app.router.add_get(path, self._handle_health)
        if self.enable_web_config:
            app.router.add_get("/config", self._handle_config_ui)
            app.router.add_get("/config/", self._handle_config_ui)
            app.router.add_get("/api/config", self._handle_api_config_get)
            app.router.add_get("/api/config/", self._handle_api_config_get)
            app.router.add_get("/api/key-types", self._handle_api_key_types)
            app.router.add_get("/api/key-types/", self._handle_api_key_types)
            app.router.add_post("/api/config", self._handle_api_config_post)
            app.router.add_post("/api/config/", self._handle_api_config_post)
            app.router.add_post("/api/restart", self._handle_api_restart)
            app.router.add_post("/api/restart/", self._handle_api_restart)
        # Catch-all for unknown paths
        app.router.add_route("*", "/{path:.*}", self._handle_not_found)

        self._runner = web.AppRunner(app, access_log=None)
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
        if self.enable_web_config and self.config_path:
            logger.warning(
                "Config editor is ENABLED — unauthenticated read/write access is active. "
                "Disable WEB_CONFIG_ENABLED when not in use."
            )
            logger.info(
                f"Config editor accessible at http://{self.bind_address}:{self.port}/config"
            )
        return True

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

    async def _handle_config_ui(self, request):
        """Serve the HTML configuration editor at GET /config."""
        from astrameter.web_config import CONFIG_EDITOR_HTML

        return web.Response(
            body=CONFIG_EDITOR_HTML.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    async def _handle_api_key_types(self, request):
        """Return the section key-type metadata as JSON at GET /api/key-types."""
        from astrameter.web_config import section_key_types_json

        return web.Response(
            body=section_key_types_json().encode("utf-8"),
            content_type="application/json",
            headers={"Cache-Control": "max-age=3600"},
        )

    async def _handle_api_config_get(self, request):
        """Return the current config.ini contents as JSON at GET /api/config."""
        from astrameter.web_config import config_to_json

        if not self.config_path:
            return web.Response(
                body=json.dumps({"error": "Config path not set"}).encode(),
                status=500,
                content_type="application/json",
            )
        try:
            payload = config_to_json(self.config_path)
            return web.Response(
                body=payload.encode("utf-8"),
                content_type="application/json",
                headers={"Cache-Control": "no-cache"},
            )
        except Exception:
            logger.exception("Error reading config")
            return web.Response(
                body=json.dumps({"error": "Internal server error"}).encode(),
                status=500,
                content_type="application/json",
            )

    async def _handle_api_config_post(self, request):
        """Write updated config sections from the JSON body at POST /api/config."""
        import shutil
        import tempfile

        from astrameter.web_config import validate_config, write_config_from_dict

        if not self.config_path:
            return web.Response(
                body=json.dumps({"error": "Config path not set"}).encode(),
                status=500,
                content_type="application/json",
            )
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
            logger.info("Configuration updated via web UI")
            return web.Response(
                body=json.dumps({"success": True}).encode(),
                content_type="application/json",
            )
        except (ValueError, json.JSONDecodeError) as e:
            logger.error("Invalid config request: %s", e)
            return web.Response(
                body=json.dumps({"error": str(e)}).encode(),
                status=400,
                content_type="application/json",
            )
        except Exception:
            logger.exception("Error saving config")
            return web.Response(
                body=json.dumps({"error": "Internal server error"}).encode(),
                status=500,
                content_type="application/json",
            )

    async def _handle_api_restart(self, request):
        """Acknowledge POST /api/restart and schedule an in-process restart via SIGUSR1."""
        import signal

        response = web.Response(
            body=json.dumps(
                {"success": True, "message": "Service is restarting..."}
            ).encode(),
            content_type="application/json",
        )
        logger.info("Restart requested via web UI")
        # Send SIGUSR1 so the handler in main.py sets restart_requested=True
        # before raising KeyboardInterrupt, causing the outer loop to reload
        # the config and re-run instead of exiting.
        threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGUSR1)).start()
        return response

    async def _handle_not_found(self, request):
        """Return a 404 JSON response for any unmatched route."""
        return web.Response(
            body=b'{"error": "Not Found"}',
            status=404,
            content_type="application/json",
        )
