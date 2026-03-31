"""
Health Check Service for B2500 Meter

Provides HTTP health check endpoints for monitoring service health.
Compatible with both Home Assistant addon watchdog and Docker health checks.
"""

import errno
import json

from aiohttp import web

from b2500_meter.config.logger import logger
from b2500_meter.version_info import get_git_commit_sha


def _health_json_bytes():
    payload = {"status": "healthy", "service": "b2500-meter"}
    sha = get_git_commit_sha()
    if sha:
        payload["git_commit"] = sha
    return json.dumps(payload).encode("utf-8")


class HealthCheckService:
    """Async health check service using aiohttp."""

    def __init__(self, port=52500, bind_address="0.0.0.0"):
        self.port = port
        self.bind_address = bind_address
        self._runner = None

    async def start(self):
        app = web.Application()
        # aiohttp auto-handles HEAD for GET routes.
        for path in ("/health", "/health/", "/api", "/api/"):
            app.router.add_get(path, self._handle_get)
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
                    f"Port {self.port} is already in use. Health check service not started."
                )
            else:
                logger.error(f"Failed to bind to {self.bind_address}:{self.port}: {e}")
            await self._runner.cleanup()
            self._runner = None
            return False

        logger.info(f"Health check service started on {self.bind_address}:{self.port}")
        return True

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Health check service stopped")

    def is_running(self):
        return self._runner is not None

    async def _handle_get(self, request):
        logger.debug(
            "Health check request received from %s",
            request.remote,
        )
        return web.Response(
            body=_health_json_bytes(),
            content_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    async def _handle_not_found(self, request):
        return web.Response(
            body=b'{"error": "Not Found"}',
            status=404,
            content_type="application/json",
        )
