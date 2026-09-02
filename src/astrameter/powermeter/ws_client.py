"""The connect/read/reconnect loop the WebSocket-fed power sources share.

Home Assistant and the HomeWizard P1 dongle both push measurements over a
WebSocket that has to be reconnected for the life of the process. Everything
that is the same either way — owning the session and the reader task, closing
one message type on another, logging the drop and backing off — lives here, so
a fix to one source cannot leave the other behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AbstractAsyncContextManager

import aiohttp

from .base import PushPowermeter

logger = logging.getLogger("astrameter")

#: How long to wait before dialling again after the connection drops.
RECONNECT_DELAY_SECONDS = 5.0

#: WebSocket heartbeat (seconds).  With this set, aiohttp sends ping frames at
#: this interval and forcibly closes the connection if no pong is received
#: within 2x the heartbeat — catching half-open TCP sockets that would
#: otherwise freeze ``async for msg in ws`` forever.
WS_HEARTBEAT_SECONDS = 30.0

#: Frame types that mean this connection is over.
_CLOSING = (
    aiohttp.WSMsgType.ERROR,
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
)

WebSocket = aiohttp.ClientWebSocketResponse[bool]


async def cancel(task: asyncio.Task | None) -> None:
    """Cancel *task* and wait for it to finish unwinding."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class WebSocketPowermeter(PushPowermeter):
    """A push source fed by a WebSocket this class keeps re-dialling.

    A subclass says where to connect (:meth:`_connect`), what a text frame
    means (:meth:`_on_text`), and what to forget when the connection goes
    (:meth:`_on_disconnect`); the loop around all three is inherited.
    """

    #: Host this source talks to, named in the connection log lines.
    ip: str

    #: What the log lines call this source.
    _LOG_NAME = "WebSocket"

    def __init__(self) -> None:
        super().__init__()
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task[None] | None = None
        # Read-only health flag for stream_online(): set by the subclass once
        # the connection is up and subscribed, cleared whenever it drops.
        self._connected = False

    # -- what a subclass fills in --------------------------------------

    def _connect(
        self, session: aiohttp.ClientSession
    ) -> AbstractAsyncContextManager[WebSocket]:
        """Open the WebSocket — ``session.ws_connect(...)``, unawaited."""
        raise NotImplementedError

    async def _on_text(self, ws: WebSocket, raw: str) -> None:
        """Handle one text frame."""
        raise NotImplementedError

    def _on_disconnect(self) -> None:
        """Drop whatever the closed connection made true. Called after every drop."""

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        if self._session:
            return
        self._connected = False
        self._session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        # Cleared before cancelling: cancellation leaves the loop before its
        # own reset runs, so stream_online() would otherwise stay True.
        self._connected = False
        await cancel(self._ws_task)
        self._ws_task = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _ws_loop(self) -> None:
        while True:
            try:
                assert self._session is not None
                async with self._connect(self._session) as ws:
                    logger.info("%s WebSocket connected to %s", self._LOG_NAME, self.ip)
                    await self._read(ws)
                    logger.info("%s WebSocket closed", self._LOG_NAME)
            except Exception as exc:
                logger.error(
                    "%s WebSocket error: %s", self._LOG_NAME, exc, exc_info=True
                )
            self._connected = False
            self._on_disconnect()
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _read(self, ws: WebSocket) -> None:
        """Feed text frames to :meth:`_on_text` until the peer closes."""
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._on_text(ws, msg.data)
            elif msg.type in _CLOSING:
                break
