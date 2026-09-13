from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from astrameter.config.logger import debug_traceback, logger
from astrameter.config.settings import ConfiguredPowermeter, ShellySettings
from astrameter.mdns import MdnsAdvertiser, parse_txt_overrides, shelly_services
from astrameter.meter_pool import powermeter_for, powermeter_name, read_fresh
from astrameter.net_info import AnnouncedAddress, announced_ipv4
from astrameter.request_dedupe import RequestDeduplicator
from astrameter.shelly import rpc
from astrameter.shelly.identity import ShellyIdentity
from astrameter.shelly.rpc import NO_POWER_DATA, ShellyRpcError
from astrameter.shelly.tcp_server import ShellyTcpServer
from astrameter.udp_server import DatagramSink, UdpServer

BATTERY_INACTIVE_TIMEOUT_SECONDS = 120
POLL_INTERVAL_EMA_ALPHA = 0.3

#: How many HTTP requests from one client may queue for the next meter reading
#: before the rest are shed. A client fanning out the older per-phase pages
#: issues four at once, and a smart-home integration pipelines its calls on one
#: socket, so four is above every legitimate burst while still bounding the
#: memory a single client can pin.
MAX_TCP_WAITERS_PER_BATTERY = 4


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
    battery has been seen twice.
    """

    ip: str
    last_seen_at: float
    last_seen_age: float
    poll_interval: float | None
    active: bool
    in_flight: bool
    #: Which transports this battery has polled on — ``"udp"``, ``"tcp"`` or
    #: ``"udp+tcp"``. A battery that found us over mDNS polls HTTP; one
    #: configured by address polls UDP; some do both.
    transport: str


@dataclasses.dataclass(frozen=True, slots=True)
class ShellySnapshot:
    """Immutable view of the whole Shelly emulator for the status API."""

    device_id: str
    device_type: str
    udp_port: int
    running: bool
    started_at: float | None
    inactive_timeout: int
    batteries: tuple[ShellyBatterySnapshot, ...]
    #: The HTTP surface. ``tcp_port`` is the bound port while listening, the
    #: configured one before a successful bind, and ``None`` when this device
    #: serves no HTTP at all — either because it is not the owner, or because
    #: the port is set to ``-1``.
    tcp_port: int | None = None
    tcp_running: bool = False
    #: mDNS presence, and the identity it announces. All five are ``None`` on a
    #: device that is not the HTTP/mDNS owner, which every default install has
    #: one of: the ``shellypro3em`` pair is two devices sharing one identity.
    mdns_registered: bool = False
    mac: str | None = None
    shelly_id: str | None = None
    mdns_hostname: str | None = None
    mdns_instance: str | None = None
    mdns_ip: str | None = None


class Shelly:
    def __init__(
        self,
        powermeters: list[ConfiguredPowermeter],
        udp_port: int,
        device_id: str,
        dedupe_time_window: float = 0.0,
        device_type: str = "",
        identity: ShellyIdentity | None = None,
        settings: ShellySettings | None = None,
        owns_tcp: bool = False,
    ) -> None:
        self._udp_port = udp_port
        self._device_id = device_id
        self._device_type = device_type
        self._powermeters = powermeters
        # The HTTP/mDNS identity, and whether this device is the one that
        # serves them. The `shellypro3em` pair is two devices sharing one
        # identity, so exactly one of them owns the single listener.
        self._identity = identity
        self._settings = settings or ShellySettings()
        self._owns_tcp = owns_tcp
        self._tcp_server: ShellyTcpServer | None = None
        self._advertiser: MdnsAdvertiser | None = None
        self._announced: AnnouncedAddress | None = None
        self._energy: rpc.EnergyCounters | None = None
        self._settings_store = rpc.SettingsStore()
        self._announce_task: asyncio.Task[None] | None = None
        self._server: UdpServer | None = None
        self._battery_last_seen: dict[str, float] = {}
        # Which transports each battery has been seen on. Written only for the
        # meter surfaces, so it is bounded by the battery list itself.
        self._battery_transports: dict[str, set[str]] = {}
        # Per-client HTTP state. The lock serialises one client's requests so a
        # burst cannot feed the same stale reading to several of them; the
        # counter bounds how many may queue. Both auto-vivify, so a client's
        # first request cannot raise, and both are bounded by the touch map
        # below — which is stamped for *every* IP served, including the ones
        # that never count as a battery, because otherwise a smart-home
        # integration polling us forever would grow them without limit.
        self._battery_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._battery_waiters: Counter[str] = Counter()
        self._http_client_last_touch: dict[str, float] = {}
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
        self._inflight_batteries: set[str] = set()
        self._stopped = asyncio.Event()
        self._inactive_check_task: asyncio.Task[None] | None = None
        self._dedupe_time_window = max(0.0, dedupe_time_window)
        self._dedup: RequestDeduplicator[str] = RequestDeduplicator(
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

    def _transport_label(self, transport: str) -> str:
        """How a transport reads in a log line, with its port."""
        if transport == "tcp":
            return f"HTTP port {self.tcp_port}"
        return f"UDP port {self._udp_port}"

    def _track_battery_seen(self, battery_ip: str, transport: str) -> float | None:
        """Refresh liveness for *battery_ip*, seen on *transport*.

        *transport* is a key — ``"udp"`` or ``"tcp"`` — not a rendered label;
        the label is derived from it where it is logged, so the snapshot and
        the log lines cannot describe the same request differently.
        """
        now = time.time()
        self._battery_transports.setdefault(battery_ip, set()).add(transport)

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
        if was_inactive:
            self._inactive_batteries.remove(battery_ip)

        if first_seen:
            logger.info(
                "Battery detected on Shelly %s: %s",
                self._transport_label(transport),
                battery_ip,
            )
        elif was_inactive:
            logger.info(
                "Battery reconnected on Shelly %s after inactivity: %s",
                self._transport_label(transport),
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
                "Battery inactive on Shelly %s for >= %ss: %s",
                "+".join(sorted(self._battery_transports.get(battery_ip, {"udp"}))),
                BATTERY_INACTIVE_TIMEOUT_SECONDS,
                battery_ip,
            )
            self._call_event_listener(battery_ip, {"_removed": True})

    def _prune_http_client_state(self, now: float) -> None:
        """Drop the per-client HTTP state of clients that have gone away.

        Swept from the touch map rather than from the battery list, because a
        client that only ever calls the reading-free methods — a smart-home
        integration, a scanner — never appears in the battery list but does
        create state here. Left alone while a request is in flight or the lock
        is held: dropping a lock someone is parked on would let a second waiter
        straight through.
        """
        for ip, touched in list(self._http_client_last_touch.items()):
            if now - touched < BATTERY_INACTIVE_TIMEOUT_SECONDS:
                continue
            lock = self._battery_locks.get(ip)
            if self._battery_waiters.get(ip) or (lock is not None and lock.locked()):
                continue
            del self._http_client_last_touch[ip]
            # `pop` rather than `del`: a client that raised before the cap
            # check has a touch entry and neither of the other two, and a
            # `defaultdict` raises on a missing key where a `Counter` does not.
            self._battery_locks.pop(ip, None)
            self._battery_waiters.pop(ip, None)

    def _emit_grid_power_event(
        self, battery_ip: str, powers: list[float], poll_interval: float | None
    ) -> None:
        """Publish the reading a battery was just served.

        Both transports call this, so the payload cannot drift between them.
        """
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

    async def _read_for_http(self, client_ip: str, key: str) -> list[float] | None:
        """The reading an HTTP request needs, or ``None`` when it may go without.

        *key* is what dispatched the request — a lower-cased method name or one
        of the non-RPC paths — and everything else is derived from it, so a
        caller cannot ask for a reading under rules that disagree with the
        tables. Whether a failure here is an error or a degraded answer is the
        caller's decision, not this method's: it raises either way.
        """
        required = rpc.NEEDS_READING[key]
        counts = key in rpc.COUNTS_AS_BATTERY_POLL
        # Stamped first, before anything else can create per-client state, so
        # the sweep above always has a key to find the rest by.
        self._http_client_last_touch[client_ip] = time.time()

        configured = powermeter_for(self._powermeters, client_ip)
        if configured is None:
            logger.warning("No powermeter found for client %s", client_ip)
            raise ShellyRpcError(1, NO_POWER_DATA)

        if self._battery_waiters[client_ip] >= MAX_TCP_WAITERS_PER_BATTERY:
            if not required:
                # No slot taken and no waiting: the composite methods answer
                # from static state with the measurements nulled rather than
                # queueing behind a backlog.
                return None
            logger.debug(
                "Shedding a Shelly HTTP request from %s: %s already queued",
                client_ip,
                MAX_TCP_WAITERS_PER_BATTERY,
            )
            raise ShellyRpcError(1, NO_POWER_DATA, retry_after=1)

        self._battery_waiters[client_ip] += 1
        try:
            async with self._battery_locks[client_ip]:
                poll_interval = (
                    self._track_battery_seen(client_ip, "tcp") if counts else None
                )
                try:
                    powers = await read_fresh(configured)
                except Exception as exc:
                    # Any read failure is code 1, exactly as on the UDP path: a
                    # powermeter backend is effectively third-party code, and
                    # letting an arbitrary exception escape would turn the
                    # composite methods into 500s and break device setup.
                    logger.warning(
                        "Could not read meter values from %s (%s): %s",
                        powermeter_name(configured.powermeter),
                        client_ip,
                        exc,
                        exc_info=debug_traceback(),
                    )
                    raise ShellyRpcError(1, NO_POWER_DATA) from exc
                if not powers:
                    # A meter that has not produced its first reading yet. The
                    # UDP path drops the datagram, which a battery retries;
                    # an HTTP client needs an answer, and zeros would read as
                    # a real measurement of no load.
                    raise ShellyRpcError(1, NO_POWER_DATA)
                if counts:
                    self._emit_grid_power_event(client_ip, powers, poll_interval)
                return powers
        finally:
            self._battery_waiters[client_ip] -= 1

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
        self, data: bytes, addr: tuple[str, int], transport: DatagramSink
    ) -> None:
        try:
            await self._handle_request(data, addr, transport)
        except Exception:
            logger.exception("Error handling Shelly request from %s", addr)

    async def _handle_request(
        self, data: bytes, addr: tuple[str, int], transport: DatagramSink
    ) -> None:
        battery_ip = addr[0]
        poll_interval = self._track_battery_seen(battery_ip, "udp")

        if not self._dedup.should_process(battery_ip):
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
        if battery_ip in self._inflight_batteries:
            logger.debug(
                "Coalescing Shelly poll from %s: a handler is already awaiting "
                "the next meter reading; dropping this duplicate to avoid a "
                "burst of readings",
                addr,
            )
            return

        self._inflight_batteries.add(battery_ip)
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

            self._emit_grid_power_event(battery_ip, powers, poll_interval)
        finally:
            self._inflight_batteries.discard(battery_ip)

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
                self._prune_http_client_state(time.time())
        except asyncio.CancelledError:
            pass

    async def _announce_watchdog_loop(self) -> None:
        """Keep the advertised address, and the mDNS registration, current.

        Runs whenever this device owns the HTTP surface, regardless of whether
        mDNS is enabled or whether either listener came up. That is deliberate:
        the advertised address is also what the HTTP surface reports as its own
        address, so gating this on the advertiser would freeze that value after
        a DHCP change in exactly the configurations that have no advertiser.
        """
        try:
            while True:
                await asyncio.sleep(rpc.ANNOUNCE_WATCHDOG_S)
                if self._advertiser is not None:
                    with contextlib.suppress(OSError, RuntimeError, ValueError):
                        await self._advertiser.refresh_interfaces()
                elif self._settings.mdns_enabled:
                    # A failed start is retried, so a responder that could not
                    # come up at boot — no network yet — recovers by itself.
                    await self._start_mdns()
                if self._announced is None:
                    continue
                updated = self._announced.refresh()
                if updated is not None and self._advertiser is not None:
                    with contextlib.suppress(OSError, RuntimeError, ValueError):
                        await self._advertiser.refresh(updated)
        except asyncio.CancelledError:
            pass

    async def _start_udp(self) -> None:
        """Stage one: the battery-facing UDP responder."""
        try:
            self._server = await UdpServer.serve(
                self._udp_port, self._safe_handle_request
            )
        except OSError as exc:
            logger.error(
                "Could not bind the Shelly UDP port %s (errno %s: %s). Batteries "
                "that poll this port will not be answered; the HTTP surface and "
                "mDNS are unaffected. Ports below 1024 need extra privileges — "
                "see docs/faq.md.",
                self._udp_port,
                exc.errno,
                exc.strerror or exc,
            )
            self._server = None
            return
        self._udp_port = self._server.port or self._udp_port
        self._running = True
        logger.info("Shelly emulator listening on UDP port %s", self._udp_port)

    async def _start_tcp(self) -> None:
        """Stage two: the HTTP surface, on the owning device only."""
        if not self._owns_tcp or self._identity is None:
            return
        if self._settings.tcp_port < 0:
            logger.info(
                "Shelly HTTP surface disabled (TCP_PORT = -1); mDNS still "
                "announces the configured port"
            )
            return
        profile = rpc.PROFILES.get(self._device_type)
        if profile is None:
            return
        self._energy = rpc.EnergyCounters(now=time.time)
        server = ShellyTcpServer(
            port=self._settings.tcp_port,
            identity=self._identity,
            profile=profile,
            udp_port=self._udp_port,
            read_reading=self._read_for_http,
            energy=self._energy,
            serve_gen1=self._settings.serve_gen1_endpoints,
            announced_ip=self._require_announced(),
            settings_store=self._settings_store,
            started_at=self._started_at or time.time(),
        )
        try:
            started = await server.start()
        except OSError:
            started = False
        self._tcp_server = server if started else None

    def _require_announced(self) -> AnnouncedAddress:
        """The one live copy of the advertised address, created on demand."""
        if self._announced is None:
            host = self._settings.mdns_host
            self._announced = AnnouncedAddress(
                announced_ipv4(host), resolve=lambda: announced_ipv4(host)
            )
        return self._announced

    async def _start_mdns(self) -> None:
        """Stage three: the mDNS announcement.

        Runs whether or not the two listeners came up. A device whose UDP bind
        failed and whose HTTP port is disabled still announces itself, because
        some consumers discover over mDNS and then poll the UDP port — and the
        port they are told about is the configured one, not a bound one.
        """
        if not self._owns_tcp or self._identity is None:
            return
        if not self._settings.mdns_enabled:
            return
        profile = rpc.PROFILES.get(self._device_type)
        if profile is None:
            return
        port = (
            self._tcp_server.port
            if self._tcp_server is not None
            else self._settings.tcp_port
        )
        if port < 0:
            port = self._udp_port
        services = shelly_services(
            self._identity,
            profile,
            port,
            self._require_announced().value,
            parse_txt_overrides(self._settings.mdns_txt),
        )
        advertiser = MdnsAdvertiser(services)
        try:
            started = await advertiser.start()
        except (OSError, RuntimeError, ValueError) as exc:
            # Every one of these is reachable: no IPv4 interface up yet, an
            # address that belongs to no adapter, or an interface name where an
            # address was expected. None of them is fatal.
            logger.warning(
                "Could not announce the emulated Shelly over mDNS: %s. The HTTP "
                "surface is unaffected, and this is retried every %ss.",
                exc,
                rpc.ANNOUNCE_WATCHDOG_S,
            )
            return
        self._advertiser = advertiser if started else None

    async def start(self) -> None:
        """Bring the device up in three independent stages.

        No stage can abort a later one, and none raises for a bind failure: a
        device that cannot bind its UDP port still serves HTTP, and one that
        cannot bind HTTP still answers batteries and still announces itself.
        The alternative — the first failure taking the whole device down — is
        what made a privileged-port problem look like a missing device.
        """
        self._stopped.clear()
        self._started_at = time.time()
        # Both tasks start before any bind, and neither is conditional on one.
        # The liveness sweep drives eviction for *both* transports, so a device
        # serving only HTTP still needs it; the watchdog keeps the advertised
        # address current even with no advertiser to announce it.
        self._inactive_check_task = asyncio.create_task(self._inactive_check_loop())
        if self._owns_tcp and self._identity is not None:
            self._announce_task = asyncio.create_task(self._announce_watchdog_loop())
        await self._start_udp()
        await self._start_tcp()
        await self._start_mdns()

    @property
    def udp_port(self) -> int:
        return self._udp_port

    @property
    def tcp_port(self) -> int | None:
        """The HTTP port in effect, or ``None`` when this device serves none.

        ``None`` covers both "not the owner" and "the port is set to ``-1``";
        before a successful bind it reports the configured port, so a failed
        bind is visible as a port that is set but not running.
        """
        if not self._owns_tcp or self._identity is None:
            return None
        if self._tcp_server is not None:
            return self._tcp_server.port
        if self._settings.tcp_port < 0:
            return None
        return self._settings.tcp_port

    def _battery_transport(self, ip: str) -> str:
        seen = self._battery_transports.get(ip) or {"udp"}
        return "udp+tcp" if seen == {"udp", "tcp"} else next(iter(seen))

    def status_snapshot(self) -> ShellySnapshot:
        """Immutable view of the emulator for the status API.

        MUST stay a plain ``def``: the UDP handlers and the HTTP handlers
        share one asyncio loop, so an await-free builder is atomic against
        every in-flight datagram.  Adding an ``await`` here silently yields
        torn snapshots that mix two polls.
        """
        now = time.time()
        identity = self._identity if self._owns_tcp else None
        return ShellySnapshot(
            device_id=self._device_id,
            device_type=self._device_type,
            udp_port=self._udp_port,
            running=self._running,
            started_at=self._started_at or None,
            inactive_timeout=BATTERY_INACTIVE_TIMEOUT_SECONDS,
            batteries=tuple(
                ShellyBatterySnapshot(
                    ip=ip,
                    last_seen_at=last_seen,
                    last_seen_age=max(0.0, now - last_seen),
                    poll_interval=self._battery_poll_interval.get(ip),
                    active=ip not in self._inactive_batteries,
                    in_flight=ip in self._inflight_batteries,
                    transport=self._battery_transport(ip),
                )
                # Sorted so a battery keeps its list position across polls.
                for ip, last_seen in sorted(self._battery_last_seen.items())
            ),
            tcp_port=self.tcp_port,
            tcp_running=self._tcp_server is not None,
            mdns_registered=(
                self._advertiser is not None and self._advertiser.registered
            ),
            # Gated on ownership, not merely on having been handed an
            # identity: these five describe an HTTP and mDNS presence, and a
            # non-owner has none. Reporting them anyway would put a second
            # device on the dashboard claiming to announce the same names.
            mac=identity.mac if identity else None,
            shelly_id=identity.shelly_id if identity else None,
            mdns_hostname=identity.hostname if identity else None,
            mdns_instance=identity.instance if identity else None,
            mdns_ip=self._announced.value if identity and self._announced else None,
        )

    async def wait(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        """Tear down, tolerating every partial state ``start()`` can leave.

        The advertiser goes first so its goodbyes reach the network before the
        socket closes; a consumer that sees them drops the device immediately
        rather than waiting out its cache.
        """
        for task in (self._inactive_check_task, self._announce_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._inactive_check_task = None
        self._announce_task = None
        if self._advertiser is not None:
            with contextlib.suppress(OSError, RuntimeError):
                await self._advertiser.stop()
            self._advertiser = None
        if self._tcp_server is not None:
            await self._tcp_server.stop()
            self._tcp_server = None
        if self._server:
            await self._server.close()
            self._server = None
        self._running = False
        self._stopped.set()
