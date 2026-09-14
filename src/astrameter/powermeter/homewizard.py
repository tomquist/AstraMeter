import asyncio
import json
import logging
import os
import ssl
import time
from collections.abc import Callable

import aiohttp

from .base import stream_fresh
from .ws_client import (
    WS_HEARTBEAT_SECONDS,
    WebSocket,
    WebSocketConnect,
    WebSocketPowermeter,
    cancel,
)

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

# Certificate: https://api-documentation.homewizard.com/assets/files/homewizard-ca-cert-56d062ef8e71d1038f464ea905d42fc6.pem
# Docs: https://api-documentation.homewizard.com/docs/v2/authorization#https
CA_CERT_PATH = os.path.join(os.path.dirname(__file__), "homewizard_ca.pem")

# Maximum age of the last-received measurement before ``get_powermeter_watts``
# considers the value stale and raises.  HomeWizard P1 dongles push
# measurements roughly once per second, so 30 s of silence is a very
# large safety margin.
DEFAULT_MAX_MEASUREMENT_AGE_SECONDS = 30.0

# Independent software watchdog: if no measurement has arrived within
# this many seconds, the ws loop force-closes and reconnects even when
# aiohttp's heartbeat hasn't tripped (e.g. the dongle is ACKing ping
# frames but has stopped sending measurement events).
WATCHDOG_TIMEOUT_SECONDS = 45.0

# How far from zero the total has to be before three zero phases are read as
# "this meter publishes no per-phase power" rather than as a house sitting at
# zero.  Both registers are whole watts, and they are measured separately, so a
# healthy three-phase meter can round its phases to 0 W beside a total of ±1 W.
# It gates that one finding only: once made, it latches (see _note_total_only),
# so a meter without per-phase power keeps its small totals.
MIN_TOTAL_FALLBACK_W = 5.0

# Which register set a measurement was read from (see _note_source).
PER_PHASE = "per-phase"
TOTAL_ONLY = "total-only"

# How often a switch between the two may be reported at INFO.  A meter that
# alternates would otherwise emit a line per sample; the count carried by the
# next line says how many switches were folded into it, which is the part worth
# seeing.  Every sample is recorded at DEBUG regardless.
SOURCE_LOG_INTERVAL_S = 60.0

_PHASE_KEYS = ("power_l1_w", "power_l2_w", "power_l3_w")


def _number(value: object) -> float | None:
    """The reading as a float, or ``None`` when the field is absent or not numeric."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _raw(data: dict, key: str) -> object:
    """The field as published, or a marker naming why it was unusable.

    The selection collapses "absent" and "not a number" into one outcome, so
    the DEBUG line has to say which it was to be worth capturing.
    """
    if key not in data:
        return "-"
    value = data[key]
    return value if _number(value) is not None else f"!{value!r}"


def _phase_values(data: dict) -> list[float] | None:
    """The per-phase readings, or ``None`` when the meter publishes none.

    L2/L3 are absent on a single-phase meter and read as 0 W; a phase that is
    present but not numeric makes the whole set unusable, because there is no
    way to tell which leg the missing watts belong to.
    """
    if "power_l1_w" not in data:
        return None
    values = [
        _number(data.get(key, 0)) for key in ("power_l1_w", "power_l2_w", "power_l3_w")
    ]
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


class HomeWizardPowermeter(WebSocketPowermeter):
    _TIMEOUT_MESSAGE = "Timeout waiting for HomeWizard measurement"
    _LOG_NAME = "HomeWizard"

    def __init__(
        self,
        ip: str,
        token: str,
        serial: str,
        verify_ssl: bool = True,
        *,
        max_measurement_age_seconds: float = DEFAULT_MAX_MEASUREMENT_AGE_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.ip = ip
        self.token = token
        self.serial = serial
        self._verify_ssl = verify_ssl
        self._ssl_context: ssl.SSLContext | None = None
        self._max_measurement_age_seconds = max(0.0, max_measurement_age_seconds)
        self._clock = clock or time.monotonic
        self.values: list[float] | None = None
        self._last_measurement_time: float | None = None
        # True only while measurements arrive as a *continuous* stream (each one
        # before the previous goes stale).  A broken P1 dongle still accepts the
        # WebSocket and replays a single cached value every time the watchdog
        # force-reconnects; that lone sample would otherwise reset the freshness
        # window and flap the "Online" sensor on/off.  See stream_online().
        self._stream_healthy = False
        # Set whenever we receive a new measurement; the read watchdog clears it
        # after checking staleness to re-arm the timer.
        self._fresh_measurement_event = asyncio.Event()
        # Set once the meter has been found to publish a total but no per-phase
        # power, so later samples keep reading the total however small it gets.
        # See _note_total_only().
        self._phases_unusable = False
        # Which register set the last measurement was read from, and how many
        # times that has changed / been reported.  Starts at PER_PHASE, the
        # ordinary case, so a meter that opens on a total-only sample still
        # announces itself.  See _note_source().
        self._reading_source = PER_PHASE
        self._source_switches = 0
        self._logged_switches = 0
        self._last_source_log = 0.0

        if not verify_ssl:
            logger.warning(
                "HomeWizard: TLS certificate verification is disabled "
                "(VERIFY_SSL=False); use only on a trusted LAN"
            )

    def _build_ssl_context(self) -> ssl.SSLContext:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self._verify_ssl:
            ssl_context.load_verify_locations(CA_CERT_PATH)
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
        else:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    async def start(self) -> None:
        if self._session:
            return
        self.values = None
        self._last_measurement_time = None
        self._stream_healthy = False
        self._message_event.clear()
        self._fresh_measurement_event.clear()
        self._phases_unusable = False
        self._reading_source = PER_PHASE
        self._source_switches = 0
        self._logged_switches = 0
        self._last_source_log = 0.0
        await super().start()

    def _connect(self, session: aiohttp.ClientSession) -> WebSocketConnect:
        if self._ssl_context is None:
            # Built once and kept: _connect runs on every reconnect, and
            # building the context reads the bundled CA off disk.
            self._ssl_context = self._build_ssl_context()
        return session.ws_connect(
            f"wss://{self.ip}/api/ws",
            ssl=self._ssl_context,
            server_hostname=f"appliance/p1dongle/{self.serial}",
            heartbeat=WS_HEARTBEAT_SECONDS,
        )

    async def _read(self, ws: WebSocket) -> None:
        # A watchdog alongside the reader force-closes the socket when no
        # measurement arrives, which the aiohttp heartbeat cannot catch: the
        # dongle's TCP keepalives keep answering while the measurement stream
        # has stalled a layer above them.
        watchdog = asyncio.create_task(self._measurement_watchdog(ws))
        try:
            await super()._read(ws)
        finally:
            await cancel(watchdog)

    async def _measurement_watchdog(self, ws: WebSocket) -> None:
        """Force-close *ws* when no measurement has arrived within
        :data:`WATCHDOG_TIMEOUT_SECONDS`.

        HomeWizard P1 dongles normally push a measurement every ~1 s. A dongle
        that stops streaming without closing the TCP connection would otherwise
        sit in the read loop forever.
        """
        while True:
            self._fresh_measurement_event.clear()
            try:
                await asyncio.wait_for(
                    self._fresh_measurement_event.wait(),
                    timeout=WATCHDOG_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "HomeWizard watchdog: no measurement for %.0fs, "
                    "force-closing WebSocket to trigger a reconnect",
                    WATCHDOG_TIMEOUT_SECONDS,
                )
                await ws.close()
                return

    async def _on_text(self, ws: WebSocket, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("HomeWizard: failed to decode message: %s", raw)
            return

        if not isinstance(msg, dict):
            logger.error("HomeWizard: unexpected message format: %s", raw)
            return

        msg_type = msg.get("type")
        if msg_type == "authorization_requested":
            await ws.send_json({"type": "authorization", "data": self.token})
        elif msg_type == "authorized":
            logger.info("HomeWizard: authorized, subscribing to measurements")
            await ws.send_json({"type": "subscribe", "data": "measurement"})
            self._connected = True
        elif msg_type == "measurement":
            data = msg.get("data")
            if isinstance(data, dict):
                self._handle_measurement(data)
        elif msg_type == "error":
            error_data = msg.get("data", {})
            logger.error("HomeWizard error: %s", error_data.get("message", msg))
        else:
            logger.debug("HomeWizard: unknown message type: %s", msg_type)

    def _handle_measurement(self, data: dict) -> None:
        total = _number(data.get("power_w"))
        phases = _phase_values(data)
        if phases is not None and any(phases):
            # Whatever was concluded before, this meter does publish per-phase.
            self._phases_unusable = False
            values, source = phases, PER_PHASE
        elif total is not None and (
            phases is None
            or self._phases_unusable
            or abs(total) >= MIN_TOTAL_FALLBACK_W
        ):
            if phases is not None:
                self._note_total_only()
            values, source = [total], TOTAL_ONLY
        elif phases is not None:
            values, source = phases, PER_PHASE
        else:
            return
        self._note_source(source, data, values)

        now = self._clock()
        max_age = self._max_measurement_age_seconds
        prev = self._last_measurement_time
        if max_age <= 0:
            # Staleness check disabled: treat every sample as a live stream.
            self._stream_healthy = True
        else:
            # Healthy only if this sample arrived before the previous went stale;
            # a lone sample after a gap (a broken dongle's replayed cache) is not a
            # live stream and must not flip stream_online() back on.
            self._stream_healthy = prev is not None and (now - prev) <= max_age

        self.values = values
        self._last_measurement_time = now
        self._message_event.set()
        self._fresh_measurement_event.set()

    def _note_total_only(self) -> None:
        """Record that this meter publishes no per-phase power.

        The finding latches: from here on the total is read however small it
        gets, instead of the reading dropping back to three zeroes every time
        the house passes near zero.  It is cleared again by any non-zero phase.
        Saying so is :meth:`_note_source`'s job, which sees both directions.
        """
        self._phases_unusable = True

    def _note_source(self, source: str, data: dict, values: list[float]) -> None:
        """Record which register set this measurement was read from.

        Every sample goes to DEBUG with the registers as published beside the
        decision taken from them.  A meter that intermittently zeroes its
        per-phase registers reads as a house at 0 W on one sample and as the
        whole house on the next, and downstream there is nothing left to tell
        those apart — the wrappers, the emulator and the balancer all see one
        number (issue #655).  This is the only place the two are still
        distinguishable.

        A switch between the two is worth an INFO line of its own, so a report
        that arrives without a DEBUG log still shows it.  Rate-limited, because
        a meter alternating every sample would otherwise bury the log it is
        trying to explain; the switches folded into the wait are counted into
        the next line, and that count is the symptom.

        Not a fault to fix: a three-phase connection without neutral (3x230 V,
        common in Belgium) is a perfectly ordinary supply whose meter publishes
        the total only, with the per-phase registers left at 0 W.  It explains
        why the dashboard then shows the whole house on phase A, the way a
        single-phase meter's reading does.
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "HomeWizard measurement: l1=%s l2=%s l3=%s power_w=%s -> %s %s",
                *(_raw(data, key) for key in _PHASE_KEYS),
                _raw(data, "power_w"),
                source,
                values,
            )
        if source == self._reading_source:
            return
        self._reading_source = source
        self._source_switches += 1
        now = self._clock()
        if (
            self._source_switches > 1
            and now - self._last_source_log < SOURCE_LOG_INTERVAL_S
        ):
            return
        # Everything switched since the last line except the switch it reports.
        folded = self._source_switches - self._logged_switches - 1
        self._logged_switches = self._source_switches
        self._last_source_log = now
        logger.info(
            "HomeWizard: %s (l1=%s l2=%s l3=%s power_w=%s)%s",
            (
                "meter publishes no per-phase power (all phases 0 W); "
                "reading the total instead"
                if source == TOTAL_ONLY
                else "per-phase power is back; reading the phases again"
            ),
            *(_raw(data, key) for key in _PHASE_KEYS),
            _raw(data, "power_w"),
            f"; {folded} further switches since the last line" if folded > 0 else "",
        )

    def stream_online(self) -> bool | None:
        return (
            self._connected
            and self._stream_healthy
            and stream_fresh(
                self._last_measurement_time,
                self._max_measurement_age_seconds,
                self._clock,
            )
        )

    async def get_powermeter_watts(self) -> list[float]:
        last = self._last_measurement_time
        if self.values is None or last is None:
            raise ValueError("No value received from HomeWizard")
        max_age = self._max_measurement_age_seconds
        if not stream_fresh(last, max_age, self._clock):
            age = self._clock() - last
            raise ValueError(
                f"HomeWizard measurement is stale ({age:.1f}s old, max {max_age:.1f}s)"
            )
        return list(self.values)
