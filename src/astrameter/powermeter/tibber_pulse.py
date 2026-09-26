import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import aiohttp
from aiohttp import BasicAuth, ClientResponseError

from .base import PushPowermeter, stream_fresh
from .http_client import HttpPowermeter
from .sml import (
    _OBIS_POWER_CURRENT,
    _OBIS_POWER_L1,
    _OBIS_POWER_L2,
    _OBIS_POWER_L3,
    parse_sml_powers,
)
from .ws_client import cancel

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

# The Pulse Bridge mirrors a push source (the meter emits ~1/s, with jitter):
# polling it occasionally returns an incomplete or CRC-bad telegram that can't
# be decoded. Such misses are transient and self-healing, so reuse the last
# good reading for up to this long rather than erroring on every miss (#518).
# Beyond the window a genuinely broken bridge/meter still surfaces as an error.
_STALE_AFTER_S = 15.0

# The bridge's webserver is slow — responses regularly take >1 s (#551) — so
# the default must leave comfortable headroom. Overridable via TIMEOUT.
DEFAULT_TIMEOUT_S = 5.0

# Bridge firmware ~1794 (September 2026) renamed the telegram endpoint from
# ``/data.json`` to ``/node_data.json`` and 404s the old path (#685); older
# firmware only serves ``/data.json``. No path works on both, so try the new
# one first (bridges update over the air) and fall back to the other on 404.
_DATA_ENDPOINTS = ("node_data.json", "data.json")

# Push: firmware since 1428-6debbaf6 / 795-379a5e21 streams every telegram the
# meter emits over ``ws://<bridge>/ws``, so readings arrive as they happen
# instead of one HTTP round trip to a slow webserver per poll. Older firmware
# answers the handshake with 404; some newer firmware accepts it but sends no
# telegrams. Either way the source polls instead — and between pushes, too, so
# a reconnect or a stalled stream never leaves it without a reading.

# A pushed reading older than this is no longer live; reads poll instead. The
# meter emits every 1-4 s, so this is a few missed telegrams.
_PUSH_MAX_AGE_S = 10.0

# Reconnect when the socket has carried nothing for this long; the bridge does
# not reliably close it when the meter goes quiet.
_PUSH_IDLE_S = 30.0

# The bridge drops the socket every 30-60 s as a matter of course; reconnect
# almost at once so no telegram is missed. Failures back off up to the cap.
_PUSH_RECONNECT_S = 0.1
_PUSH_BACKOFF_MAX_S = 30.0

# Leaving a socket waits this long at most for the bridge's close reply; it
# often drops the connection without one.
_PUSH_WS_TIMEOUT = aiohttp.ClientWSTimeout(ws_close=2.0)

# Give up on push when no telegram for this node has arrived this long after
# start while HTTP polling works: the bridge accepts the socket but won't
# stream this meter over it.
_PUSH_GRACE_S = 60.0

# Frame header attributes: ``key:value`` or ``key:"value with spaces"``.
_HEADER_ATTR = re.compile(r'(\w+):(?:"([^"]*)"|(\S*))')

_CLOSING = (
    aiohttp.WSMsgType.ERROR,
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
)


def split_push_frame(data: bytes) -> tuple[dict[str, str], bytes] | None:
    """Split a push frame ``<key:value ...>BODY`` into its header and body.

    The body is the raw telegram and may itself contain ``>``, so only the
    first one ends the header. ``None`` for anything not shaped like a frame.
    """
    if not data.startswith(b"<"):
        return None
    end = data.find(b">")
    if end < 0:
        return None
    head = data[1:end].decode("ascii", errors="ignore")
    header = {k: quoted or bare for k, quoted, bare in _HEADER_ATTR.findall(head)}
    return header, data[end + 1 :]


class TibberPulse(HttpPowermeter, PushPowermeter):
    """Reads a Tibber Pulse via the local Pulse Bridge.

    By default it takes each SML telegram as the bridge pushes it over
    ``ws://<bridge>/ws``, and polls the bridge's ``/node_data.json`` endpoint
    (``/data.json`` on firmware before ~1794) whenever no live push reading is
    at hand: before the first one, across a reconnect, or for good once the
    bridge proves it can't push. ``force_polling`` skips push altogether.

    Either way it talks HTTP Basic auth to the bridge and decodes the
    instantaneous active power locally — no Tibber cloud involved. The
    bridge's local webserver must be enabled (``webserver-force-enable``) and
    the password is the nine-character code printed on the bridge (e.g.
    ``AD56-54BA``); the user is ``admin``.

    Returns signed power (positive = grid import, negative = feed-in) as either
    three per-phase values or a single aggregate, matching the OBIS registers
    the meter exposes. Flip the sign with ``POWER_MULTIPLIER = -1`` if reversed.
    """

    _TIMEOUT_MESSAGE = "Timeout waiting for Tibber Pulse telegram"

    def __init__(
        self,
        ip: str,
        password: str,
        node_id: str = "1",
        user: str = "admin",
        *,
        obis_power_current: str = _OBIS_POWER_CURRENT,
        obis_power_l1: str = _OBIS_POWER_L1,
        obis_power_l2: str = _OBIS_POWER_L2,
        obis_power_l3: str = _OBIS_POWER_L3,
        timeout: float = DEFAULT_TIMEOUT_S,
        force_polling: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.ip = ip
        self.password = password
        self.node_id = node_id
        self.user = user
        self.force_polling = force_polling
        self._obis_current = obis_power_current
        self._obis_l1 = obis_power_l1
        self._obis_l2 = obis_power_l2
        self._obis_l3 = obis_power_l3
        self._clock = clock or time.monotonic
        # Last successfully decoded reading and when it was decoded, so a
        # transient undecodable telegram can reuse it instead of erroring.
        self._last_powers: list[float] | None = None
        self._last_good: float | None = None
        # The endpoint that last answered, so the fallback costs one extra
        # request per switch rather than one per poll.
        self._endpoint = _DATA_ENDPOINTS[0]
        # Push state. ``_push_enabled`` is cleared for good once the bridge
        # proves it can't push; ``_polled_ok`` records that HTTP works, which
        # is what makes a silent socket a reason to stop trying.
        self._push_enabled = False
        self._push_task: asyncio.Task[None] | None = None
        self._push_powers: list[float] | None = None
        self._push_time: float | None = None
        self._polled_ok = False
        # This node's EUI, which push frames name their meter by; one bridge
        # can serve several Pulses.
        self._device: str | None = None

    def _session_options(self) -> dict[str, Any]:
        return {
            **super()._session_options(),
            "auth": BasicAuth(self.user, self.password),
        }

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        if self.session:
            return
        await super().start()
        self._push_enabled = not self.force_polling
        self._push_powers = None
        self._push_time = None
        self._polled_ok = False
        if self._push_enabled:
            self._push_task = asyncio.create_task(self._push_loop())

    async def stop(self) -> None:
        self._push_enabled = False
        await cancel(self._push_task)
        self._push_task = None
        await super().stop()

    # -- reading -------------------------------------------------------

    def _push_live(self) -> bool:
        return self._push_enabled and stream_fresh(
            self._push_time, _PUSH_MAX_AGE_S, self._clock
        )

    async def get_powermeter_watts(self) -> list[float]:
        if self._push_live() and self._push_powers is not None:
            return list(self._push_powers)
        return await self._poll()

    async def wait_for_message(self, timeout: float = 5) -> None:
        # Never blocks: without a live push reading, a read polls for one.
        return

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        # Only a live stream has a next message to wait for; otherwise the
        # read that follows polls a fresh telegram itself.
        if self._push_live():
            await super().wait_for_next_message(timeout)

    def stream_online(self) -> bool | None:
        # Unknown rather than offline without a live stream: the health loop
        # then judges the source by a poll, which is how it is being read.
        return True if self._push_live() else None

    # -- polling -------------------------------------------------------

    async def _fetch_telegram(self) -> bytes:
        # Pick the fallback from the path this poll tried, not from
        # ``self._endpoint``, which an overlapping poll may already have moved.
        tried = self._endpoint
        try:
            return await self.get_bytes(self._url(tried))
        except ClientResponseError as e:
            if e.status != 404:
                raise
        # The bridge doesn't serve this path, so it runs the other firmware
        # generation: at startup, or after an OTA update while running.
        other = next(ep for ep in _DATA_ENDPOINTS if ep != tried)
        data = await self.get_bytes(self._url(other))
        logger.info("Tibber Pulse: bridge serves /%s, using it from now on", other)
        self._endpoint = other
        return data

    def _url(self, endpoint: str) -> str:
        return f"http://{self.ip}/{endpoint}?node_id={self.node_id}"

    def _decode(self, telegram: bytes) -> list[float] | None:
        powers = parse_sml_powers(
            telegram,
            self._obis_current,
            self._obis_l1,
            self._obis_l2,
            self._obis_l3,
        )
        if not powers:
            return None
        result = [float(x) for x in powers]
        self._last_powers = result
        self._last_good = self._clock()
        return result

    async def _poll(self) -> list[float]:
        result = self._decode(await self._fetch_telegram())
        if result is not None:
            self._polled_ok = True
            return result
        # Transient decode miss: reuse the last good reading for a bounded
        # window so an occasional bad telegram doesn't spam warnings or starve
        # the control loop (#518). Past the window, surface the failure.
        if self._last_powers is not None and self._last_good is not None:
            age = self._clock() - self._last_good
            if age <= _STALE_AFTER_S:
                logger.debug(
                    "Tibber Pulse: undecodable telegram, reusing last good "
                    "values %s (age %.1fs)",
                    self._last_powers,
                    age,
                )
                return list(self._last_powers)
        raise ValueError("Could not decode SML telegram from Tibber Pulse")

    # -- push ----------------------------------------------------------

    def _push_hopeless(self, started: float) -> bool:
        """Whether the bridge works over HTTP but has never pushed this meter."""
        return (
            self._push_time is None
            and self._polled_ok
            and self._clock() - started >= _PUSH_GRACE_S
        )

    async def _push_loop(self) -> None:
        started = self._clock()
        delay = _PUSH_RECONNECT_S
        while True:
            try:
                if self._device is None:
                    self._device = await self._resolve_device()
                await self._stream(started)
                delay = _PUSH_RECONNECT_S
            except aiohttp.WSServerHandshakeError as e:
                if e.status == 404:
                    logger.info(
                        "Tibber Pulse: bridge firmware has no push endpoint "
                        "(/ws answered 404); polling instead"
                    )
                    self._push_enabled = False
                    return
                logger.warning("Tibber Pulse: push connection refused: %s", e)
                delay = min(max(delay, 1.0) * 2, _PUSH_BACKOFF_MAX_S)
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as e:
                logger.debug("Tibber Pulse: push connection lost: %r", e)
                delay = min(max(delay, 1.0) * 2, _PUSH_BACKOFF_MAX_S)
            except Exception as e:
                logger.warning(
                    "Tibber Pulse: push connection failed: %s", e, exc_info=True
                )
                delay = min(max(delay, 1.0) * 2, _PUSH_BACKOFF_MAX_S)
            if self._push_hopeless(started):
                logger.info(
                    "Tibber Pulse: bridge sends no meter telegrams over push; "
                    "polling instead"
                )
                self._push_enabled = False
                return
            await asyncio.sleep(delay)

    async def _resolve_device(self) -> str | None:
        """This node's EUI from ``/nodes.json``, or ``None`` if unavailable."""
        try:
            nodes = await self.get_json(f"http://{self.ip}/nodes.json")
        except Exception as e:
            logger.debug("Tibber Pulse: could not read /nodes.json: %r", e)
            return None
        for node in nodes if isinstance(nodes, list) else []:
            if isinstance(node, dict) and str(node.get("node_id")) == self.node_id:
                eui = node.get("eui")
                return eui.lower() if isinstance(eui, str) and eui else None
        return None

    async def _stream(self, started: float) -> None:
        """Read one push connection until the bridge drops it or it idles."""
        async with self._require_session().ws_connect(
            f"ws://{self.ip}/ws", compress=0, timeout=_PUSH_WS_TIMEOUT
        ) as ws:
            logger.debug("Tibber Pulse: push connected")
            while not self._push_hopeless(started):
                try:
                    msg = await ws.receive(timeout=_PUSH_IDLE_S)
                except (asyncio.TimeoutError, TimeoutError):
                    # Quiet socket: ordinary for this bridge, so a prompt
                    # reconnect rather than a backoff.
                    return
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._on_frame(msg.data)
                elif msg.type in _CLOSING:
                    return

    def _on_frame(self, data: bytes) -> None:
        frame = split_push_frame(data)
        if frame is None:
            return
        header, body = frame
        if "sml" not in header.get("topic", "").lower():
            return
        device = header.get("device")
        if device is not None and device.lower() != self._device:
            # Another Pulse on this bridge — or ours, before its EUI is known,
            # which leaves nothing to tell the two apart.
            return
        result = self._decode(body)
        if result is None:
            return
        if self._push_time is None:
            logger.info("Tibber Pulse: receiving live readings over push")
        self._push_powers = result
        self._push_time = self._clock()
        self._message_event.set()
