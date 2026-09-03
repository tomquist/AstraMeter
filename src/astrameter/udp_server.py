"""A bound UDP socket that answers each datagram in its own task.

Both emulators — the CT002 responder and the Shelly one — serve a request by
awaiting a meter read, so a handler cannot run inline on the event loop's
datagram callback without stalling every other battery behind the slowest one.
Each datagram therefore gets a task, and shutdown has to cancel whatever is
still parked mid-poll.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, cast


class DatagramSink(Protocol):
    """The one thing a handler does with the transport it is handed."""

    def sendto(self, data: bytes, addr: tuple) -> None: ...


Handler = Callable[[bytes, tuple, asyncio.DatagramTransport], Coroutine[Any, Any, None]]


class _HandlerProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._transport: asyncio.DatagramTransport | None = None
        # asyncio keeps only a weak reference to a running task, so a handler
        # nobody holds can be collected mid-poll; the set is that reference.
        self._tasks: set[asyncio.Task] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # Widened by the base class, and narrowed rather than checked: the
        # concrete datagram transports do not inherit `DatagramTransport`, so
        # an isinstance test here refuses the very object asyncio hands over.
        self._transport = cast("asyncio.DatagramTransport", transport)

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        assert self._transport is not None
        task: asyncio.Task = asyncio.create_task(
            self._handler(data, addr, self._transport)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class UdpServer:
    """A listening UDP socket, bound by :meth:`serve` and closed by :meth:`close`."""

    def __init__(
        self, transport: asyncio.DatagramTransport, protocol: _HandlerProtocol
    ) -> None:
        self._transport = transport
        self._protocol = protocol

    @classmethod
    async def serve(cls, port: int, handler: Handler) -> UdpServer:
        """Bind *port* on every interface and route datagrams to *handler*."""
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _HandlerProtocol(handler),
            local_addr=("0.0.0.0", port),
        )
        return cls(transport, protocol)

    @property
    def port(self) -> int:
        """The port actually bound — the real one when 0 asked for any free port."""
        sockname = self._transport.get_extra_info("sockname")
        return int(sockname[1]) if sockname else 0

    async def close(self) -> None:
        """Stop listening, then cancel and await every in-flight handler.

        Closing the transport first stops new datagrams from spawning tasks, so
        the drain below cannot race a fresh arrival.
        """
        self._transport.close()
        await self._protocol.drain()
