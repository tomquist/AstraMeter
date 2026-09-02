from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import aiohttp

from .base import Powermeter

# Fail fast: the battery polls ~1/s, so a slow source should error quickly and
# let the next poll retry rather than pin a handler.
POLL_TIMEOUT = aiohttp.ClientTimeout(total=2, connect=1)

_NO_EXPIRED_STATUSES: frozenset[int] = frozenset()

T = TypeVar("T")


class SessionExpired(RuntimeError):
    """The device no longer accepts the login; see :func:`retry_after_relogin`."""


class HttpPowermeter(Powermeter):
    """Polls a device over HTTP; subclasses build the URLs and decode the body."""

    session: aiohttp.ClientSession | None = None
    # Total request timeout in seconds; ``None`` keeps POLL_TIMEOUT.
    timeout: float | None = None

    def __init__(self, *, timeout: float | None = None) -> None:
        if timeout is not None:
            self.timeout = timeout

    def _session_options(self) -> dict[str, Any]:
        """Keyword arguments for the ``aiohttp.ClientSession``.

        Override to add auth, headers or a connector. A device slower than
        :data:`POLL_TIMEOUT` passes ``timeout`` to the constructor instead;
        that drops the separate connect cap, because on such a device the
        accept alone can exceed 1 s (#551) while a bounded total still caps a
        stuck request.
        """
        if self.timeout is None:
            return {"timeout": POLL_TIMEOUT}
        return {"timeout": aiohttp.ClientTimeout(total=self.timeout)}

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

    @asynccontextmanager
    async def _get(
        self,
        url: str,
        *,
        expired_statuses: frozenset[int] = _NO_EXPIRED_STATUSES,
        **kwargs: Any,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """GET *url*, turning ``expired_statuses`` into :class:`SessionExpired`.

        Devices that answer a lapsed login with a status instead of an error
        body name it here so the caller only has to build the URL.
        """
        async with self._require_session().get(url, **kwargs) as resp:
            if resp.status in expired_statuses:
                raise SessionExpired
            resp.raise_for_status()
            yield resp

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        async with self._get(url, **kwargs) as resp:
            return await resp.json(content_type=None)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        async with self._get(url, **kwargs) as resp:
            return await resp.text()

    async def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        async with self._get(url, **kwargs) as resp:
            return await resp.read()


async def retry_after_relogin(
    fetch: Callable[[], Awaitable[T]], login: Callable[[], Awaitable[None]]
) -> T:
    """Run ``fetch``, logging in again and retrying once if the login lapsed."""
    try:
        return await fetch()
    except SessionExpired:
        await login()
        return await fetch()
