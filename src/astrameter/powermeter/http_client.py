from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import aiohttp

from .base import Powermeter

# Fail fast: the battery polls ~1/s, so a slow source should error quickly and
# let the next poll retry rather than pin a handler.
POLL_TIMEOUT = aiohttp.ClientTimeout(total=2, connect=1)

T = TypeVar("T")


class HttpPowermeter(Powermeter):
    """Polls a device over HTTP; subclasses build the URLs and decode the body."""

    session: aiohttp.ClientSession | None = None

    def _session_options(self) -> dict[str, Any]:
        """Keyword arguments for the ``aiohttp.ClientSession``.

        Override to add auth or headers, or to give a slow device more time
        than :data:`POLL_TIMEOUT`.
        """
        return {"timeout": POLL_TIMEOUT}

    async def start(self) -> None:
        if self.session:
            return
        self.session = aiohttp.ClientSession(**self._session_options())

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    def _require_session(self) -> aiohttp.ClientSession:
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        return self.session

    async def get_json(self, url: str) -> Any:
        async with self._require_session().get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


class SessionExpired(RuntimeError):
    """The device no longer accepts the login; see :func:`retry_after_relogin`."""


async def retry_after_relogin(
    fetch: Callable[[], Awaitable[T]], login: Callable[[], Awaitable[None]]
) -> T:
    """Run ``fetch``, logging in again and retrying once if the login lapsed."""
    try:
        return await fetch()
    except SessionExpired:
        await login()
        return await fetch()
