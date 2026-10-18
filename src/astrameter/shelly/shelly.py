from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from astrameter.config.logger import debug_traceback, logger
from astrameter.config.settings import ConfiguredPowermeter
from astrameter.meter_pool import powermeter_for, powermeter_name, read_fresh
from astrameter.request_dedupe import RequestDeduplicator
from astrameter.udp_server import DatagramSink, UdpServer

BATTERY_INACTIVE_TIMEOUT_SECONDS = 120
POLL_INTERVAL_EMA_ALPHA = 0.3


def _decode_request(data: bytes, addr: tuple[str, int]) -> dict[str, Any] | None:
    """The Shelly RPC call in *data*, or ``None`` when it is not one.

    A poll carries a channel index at ``params.id``; a datagram without it is
    something else on the port and gets no reply.
    """
    try:
        request = json.loads(data.decode())
    except UnicodeDecodeError:
        logger.debug("Ignoring non-UTF-8 datagram from %s:%s", addr[0], addr[1])
        return None
    except json.JSONDecodeError:
        logger.debug("Ignoring non-JSON datagram from %s:%s", addr[0], addr[1])
        return None
    if not isinstance(request, dict) or not isinstance(
        request.get("params", {}).get("id"), int
    ):
        return None
    logger.debug("Parsed request from %s: %s", addr[0], request)
    return request


def _three_phases(powers: list[float]) -> tuple[float, float, float]:
    """*powers* as three floats: a single reading is phase A, anything else zero."""
    if len(powers) == 1:
        return float(powers[0]), 0.0, 0.0
    if len(powers) >= 3:
        return float(powers[0]), float(powers[1]), float(powers[2])
    return 0.0, 0.0, 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class ShellyBatterySnapshot:
    """Immutable view of one battery polling this emulator.

    ``last_seen_at`` is a wall-clock epoch, ages and intervals seconds.
    ``poll_interval`` is the EMA-smoothed cadence and stays ``None`` until a
    battery has been seen twice.  ``udp_port`` is the port its last poll
    arrived on, which tells the firmware generations apart on a multi-port
    emulator.
    """

    ip: str
    udp_port: int
    last_seen_at: float
    last_seen_age: float
    poll_interval: float | None
    active: bool
    in_flight: bool


@dataclasses.dataclass(frozen=True, slots=True)
class ShellySnapshot:
    """Immutable view of the whole Shelly emulator for the status API."""

    device_id: str
    device_type: str
    udp_ports: tuple[int, ...]
    running: bool
    started_at: float | None
    inactive_timeout: int
    batteries: tuple[ShellyBatterySnapshot, ...]


class Shelly:
    """One emulated Shelly, answering on every port in *udp_ports*.

    The Pro 3EM is served on two ports at once because battery firmware
    generations look for it on different ones; it is still one meter with one
    device id, so it is one emulator rather than a pair that would collide on
    that id.  Liveness is tracked per battery IP, while dedupe and in-flight
    coalescing are per IP *and* port, as they were when each port had an
    emulator of its own.
    """

    def __init__(
        self,
        powermeters: list[ConfiguredPowermeter],
        udp_ports: Sequence[int],
        device_id: str,
        dedupe_time_window: float = 0.0,
        device_type: str = "",
    ) -> None:
        if not udp_ports:
            raise ValueError("A Shelly emulator needs at least one UDP port")
        self._udp_ports = tuple(udp_ports)
        self._device_id = device_id
        self._device_type = device_type
        self._powermeters = powermeters
        self._servers: list[UdpServer] = []
        self._battery_last_seen: dict[str, float] = {}
        self._battery_port: dict[str, int] = {}
        self._battery_poll_interval: dict[str, float] = {}
        self._inactive_batteries: set[str] = set()
        # Batteries with a request handler currently parked between the meter
        # read and its response.  The battery runs a closed-loop zero-export
        # controller, so the grid reading we report is the error signal it
        # integrates against its own output.  While WAIT_FOR_NEXT_MESSAGE — or a
        # slow/throttled meter — holds that read, the battery keeps polling
        # (~1/s) and each datagram spawns its own handler task; without
        # coalescing every parked handler would wake on the same stale reading
        # and answer, feeding the loop the same pre-adjustment error several
        # times before the plant can reflect its response, which winds the
        # battery past target exactly like a burst of deltas.  A single
        # in-flight handler per battery answers per reading; duplicates drop.
        self._inflight_batteries: set[tuple[str, int]] = set()
        self._stopped = asyncio.Event()
        self._inactive_check_task: asyncio.Task[None] | None = None
        self._dedupe_time_window = max(0.0, dedupe_time_window)
        self._dedup: RequestDeduplicator[tuple[str, int]] = RequestDeduplicator(
            self._dedupe_time_window
        )
        self.event_listener: Callable[[str, str, dict[str, Any]], None] | None = None
        # Read-only status surface (see status_snapshot).
        self._started_at: float = 0.0
        self._running: bool = False

    def _calculate_derived_values(self, power: float) -> float:
        decimal_point_enforcer = 0.001
        if abs(power) < 0.1:
            return decimal_point_enforcer

        return round(
            power
            + (decimal_point_enforcer if power == round(power) or power == 0 else 0),
            1,
        )

    def _create_em_response(
        self, request_id: Any, powers: list[float]
    ) -> dict[str, Any]:
        if len(powers) == 1:
            powers = [powers[0], 0, 0]
        elif len(powers) != 3:
            powers = [0, 0, 0]

        a = self._calculate_derived_values(powers[0])
        b = self._calculate_derived_values(powers[1])
        c = self._calculate_derived_values(powers[2])

        total_act_power = round(sum(powers), 3)
        total_act_power = total_act_power + (
            0.001
            if total_act_power == round(total_act_power) or total_act_power == 0
            else 0
        )

        return {
            "id": request_id,
            "src": self._device_id,
            "dst": "unknown",
            "result": {
                "a_act_power": a,
                "b_act_power": b,
                "c_act_power": c,
                "total_act_power": total_act_power,
            },
        }

    def _create_em1_response(
        self, request_id: Any, powers: list[float]
    ) -> dict[str, Any]:
        total_power = round(sum(powers), 3)
        total_power = total_power + (
            0.001 if total_power == round(total_power) or total_power == 0 else 0
        )

        return {
            "id": request_id,
            "src": self._device_id,
            "dst": "unknown",
            "result": {
                "act_power": total_power,
            },
        }

    def _track_battery_seen(
        self, addr: tuple[str, int], local_port: int | None = None
    ) -> float | None:
        battery_ip = addr[0]
        port = self._udp_ports[0] if local_port is None else local_port
        now = time.time()

        first_seen = battery_ip not in self._battery_last_seen
        was_inactive = battery_ip in self._inactive_batteries

        # Compute EMA-smoothed poll interval
        poll_interval: float | None = None
        if not first_seen:
            raw_interval = now - self._battery_last_seen[battery_ip]
            prev = self._battery_poll_interval.get(battery_ip)
            if prev is None:
                self._battery_poll_interval[battery_ip] = round(raw_interval, 1)
            else:
                self._battery_poll_interval[battery_ip] = round(
                    POLL_INTERVAL_EMA_ALPHA * raw_interval
                    + (1 - POLL_INTERVAL_EMA_ALPHA) * prev,
                    1,
                )
            poll_interval = self._battery_poll_interval[battery_ip]

        self._battery_last_seen[battery_ip] = now
        self._battery_port[battery_ip] = port
        if was_inactive:
            self._inactive_batteries.remove(battery_ip)

        if first_seen:
            logger.info(
                "Battery detected on Shelly UDP port %s: %s",
                port,
                battery_ip,
            )
        elif was_inactive:
            logger.info(
                "Battery reconnected on Shelly UDP port %s after inactivity: %s",
                port,
                battery_ip,
            )

        return poll_interval

    def _log_inactive_batteries(self) -> None:
        now = time.time()
        newly_inactive_batteries = []

        for battery_ip, last_seen in self._battery_last_seen.items():
            if (
                now - last_seen >= BATTERY_INACTIVE_TIMEOUT_SECONDS
                and battery_ip not in self._inactive_batteries
            ):
                self._inactive_batteries.add(battery_ip)
                newly_inactive_batteries.append(battery_ip)

        for battery_ip in newly_inactive_batteries:
            logger.info(
                "Battery inactive on Shelly UDP port %s for >= %ss: %s",
                self._battery_port.get(battery_ip, self._udp_ports[0]),
                BATTERY_INACTIVE_TIMEOUT_SECONDS,
                battery_ip,
            )
            self._call_event_listener(battery_ip, {"_removed": True})

    def _call_event_listener(self, battery_ip: str, data: dict[str, Any]) -> None:
        if not self.event_listener:
            return
        try:
            self.event_listener(self._device_id, battery_ip, data)
        except Exception as exc:
            logger.warning(
                "event_listener failed for %s: %s", battery_ip, exc, exc_info=True
            )

    async def _safe_handle_request(
        self,
        data: bytes,
        addr: tuple[str, int],
        transport: DatagramSink,
        local_port: int,
    ) -> None:
        try:
            await self._handle_request(data, addr, transport, local_port)
        except Exception:
            logger.exception("Error handling Shelly request from %s", addr)

    async def _handle_request(
        self,
        data: bytes,
        addr: tuple[str, int],
        transport: DatagramSink,
        local_port: int | None = None,
    ) -> None:
        """Answer one poll that arrived on *local_port* (default: the first)."""
        battery_ip = addr[0]
        port = self._udp_ports[0] if local_port is None else local_port
        key = (battery_ip, port)
        poll_interval = self._track_battery_seen(addr, port)

        if not self._dedup.should_process(key):
            logger.debug("Ignoring request from %s due to dedupe window", addr)
            return

        request = _decode_request(data, addr)
        if request is None:
            return

        configured = powermeter_for(self._powermeters, battery_ip)
        if configured is None:
            logger.warning("No powermeter found for client %s", battery_ip)
            return

        # Coalesce concurrent polls from the same battery.  If a handler for
        # this battery is already parked awaiting the next meter reading, the
        # reading has not been answered yet — letting this duplicate poll wait
        # and respond too would feed the battery's zero-export loop the same
        # stale error several times the moment the meter updates and wakes
        # every parked handler, overshooting target.  Drop it;
        # _track_battery_seen above already refreshed the liveness and
        # poll-interval state, and the in-flight handler sends the one response
        # for the next reading.
        if key in self._inflight_batteries:
            logger.debug(
                "Coalescing Shelly poll from %s: a handler is already awaiting "
                "the next meter reading; dropping this duplicate to avoid a "
                "burst of readings",
                addr,
            )
            return

        self._inflight_batteries.add(key)
        try:
            try:
                powers = await read_fresh(configured)
            except Exception as exc:
                # Reading the meter can fail transiently (e.g. an HTTP source
                # timing out). Log a one-liner at the normal level and reserve
                # the full traceback for DEBUG so an outage doesn't flood the
                # log with stack traces on every poll.
                logger.warning(
                    "Could not read meter values from %s (%s): %s",
                    powermeter_name(configured.powermeter),
                    battery_ip,
                    exc,
                    exc_info=debug_traceback(),
                )
                return

            response = self._response_for(request, powers)
            if response is None:
                return
            response_json = json.dumps(response, separators=(",", ":"))
            logger.debug("Sending response: %s", response_json)
            transport.sendto(response_json.encode(), addr)

            l1, l2, l3 = _three_phases(powers)
            self._call_event_listener(
                battery_ip,
                {
                    "grid_power": {
                        "l1": l1,
                        "l2": l2,
                        "l3": l3,
                        "total": l1 + l2 + l3,
                    },
                    "active": battery_ip not in self._inactive_batteries,
                    "poll_interval": poll_interval,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "battery_count": len(self._battery_last_seen),
                },
            )
        finally:
            self._inflight_batteries.discard(key)

    def _response_for(
        self, request: dict[str, Any], powers: list[float]
    ) -> dict[str, Any] | None:
        """The reply *request* asks for, or ``None`` for a method we do not serve."""
        method = request.get("method")
        if method == "EM.GetStatus":
            return self._create_em_response(request["id"], powers)
        if method == "EM1.GetStatus":
            return self._create_em1_response(request["id"], powers)
        return None

    async def _inactive_check_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                self._log_inactive_batteries()
                # Keep dedup entries until they've aged past both the
                # inactive-battery horizon and the configured window, so
                # windows greater than 120s are still honored.
                self._dedup.purge_older_than(
                    max(BATTERY_INACTIVE_TIMEOUT_SECONDS, self._dedupe_time_window)
                )
        except asyncio.CancelledError:
            pass

    async def _serve(self, port: int) -> UdpServer:
        """Bind *port*, tagging each datagram with the port it arrived on."""
        local_port = port

        async def handle(
            data: bytes, addr: tuple[str, int], transport: DatagramSink
        ) -> None:
            await self._safe_handle_request(data, addr, transport, local_port)

        server = await UdpServer.serve(port, handle)
        # Rebinding the closure's cell: port 0 resolves only once bound.
        local_port = server.port or port
        return server

    async def start(self) -> None:
        """Listen on every configured port that can be bound.

        A port that fails (1010 needs privileges an unprivileged container
        lacks, see the FAQ) is logged and skipped so the others keep serving;
        only when none binds does the emulator fail to start.
        """
        bound: list[int] = []
        error: OSError | None = None
        for port in self._udp_ports:
            try:
                server = await self._serve(port)
            except OSError as exc:
                logger.error(
                    "Shelly emulator could not listen on UDP port %s: %s", port, exc
                )
                error = exc
                continue
            self._servers.append(server)
            bound.append(server.port or port)
        if not bound:
            assert error is not None
            raise error
        self._udp_ports = tuple(bound)
        self._stopped.clear()
        self._inactive_check_task = asyncio.create_task(self._inactive_check_loop())
        self._started_at = time.time()
        self._running = True
        for port in bound:
            logger.info("Shelly emulator listening on UDP port %s", port)

    @property
    def udp_ports(self) -> tuple[int, ...]:
        """The ports served — after :meth:`start`, only those actually bound."""
        return self._udp_ports

    def status_snapshot(self) -> ShellySnapshot:
        """Immutable view of the emulator for the status API.

        MUST stay a plain ``def``: the UDP handlers and the HTTP handlers
        share one asyncio loop, so an await-free builder is atomic against
        every in-flight datagram.  Adding an ``await`` here silently yields
        torn snapshots that mix two polls.
        """
        now = time.time()
        return ShellySnapshot(
            device_id=self._device_id,
            device_type=self._device_type,
            udp_ports=self._udp_ports,
            running=self._running,
            started_at=self._started_at or None,
            inactive_timeout=BATTERY_INACTIVE_TIMEOUT_SECONDS,
            batteries=tuple(
                ShellyBatterySnapshot(
                    ip=ip,
                    udp_port=self._battery_port.get(ip, self._udp_ports[0]),
                    last_seen_at=last_seen,
                    last_seen_age=max(0.0, now - last_seen),
                    poll_interval=self._battery_poll_interval.get(ip),
                    active=ip not in self._inactive_batteries,
                    in_flight=any(key[0] == ip for key in self._inflight_batteries),
                )
                # Sorted so a battery keeps its list position across polls.
                for ip, last_seen in sorted(self._battery_last_seen.items())
            ),
        )

    async def wait(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        if self._inactive_check_task:
            self._inactive_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._inactive_check_task
            self._inactive_check_task = None
        for server in self._servers:
            await server.close()
        self._servers = []
        self._running = False
        self._stopped.set()
