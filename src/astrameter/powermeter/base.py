# Powermeter classes
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import aiohttp
from aiohttp import ClientTimeout


def stream_fresh(
    last_monotonic: float | None,
    max_age: float,
    clock: Callable[[], float],
) -> bool:
    """Freshness check shared by cadence-based push powermeters.

    Returns ``False`` if nothing has been received yet; ``True`` when
    ``max_age <= 0`` (freshness disabled); otherwise ``True`` only while the
    last message is no older than ``max_age`` seconds.
    """
    if last_monotonic is None:
        return False
    if max_age <= 0:
        return True
    return (clock() - last_monotonic) <= max_age


class Powermeter:
    # Labels the powermeter's diagnostic device in MQTT Insights. Set by the
    # outermost HealthTrackingPowermeter wrapper to the config section name.
    name: str = ""

    async def get_powermeter_watts(self) -> list[float]:
        raise NotImplementedError()

    async def get_powermeter_watts_raw(self) -> list[float]:
        """Per-phase watts before section/global processing wrappers.

        Used when a consumer (e.g. Marstek MQTT display) should match the physical
        meter while control still uses :meth:`get_powermeter_watts`. Defaults to
        the same values as :meth:`get_powermeter_watts` for sources with no inner
        pipeline.
        """
        return await self.get_powermeter_watts()

    def stream_online(self) -> bool | None:
        """Health hook for the MQTT Insights "Online" diagnostic sensor.

        ``None`` (the default) means "don't know" — used by pull/polling
        powermeters; the health loop falls back to reusing the control loop's
        last read or, when idle, a single bounded probe. Push powermeters
        override this to report their own connection/validity state with no
        I/O.
        """
        return None

    async def wait_for_message(self, timeout=5):
        pass

    async def wait_for_next_message(self, timeout=5):
        """Block until a *new* measurement arrives (push-based powermeters).

        Unlike ``wait_for_message`` (which returns immediately once data has
        been received *at least once*), this method waits for the *next*
        update, ensuring callers always get fresh data.  Polling-based
        powermeters leave the default no-op.
        """

    # --- Lifecycle (no-op by default, override for push-based powermeters) ---

    async def start(self):
        pass

    async def stop(self):
        pass

    def reset(self):
        pass


#: Default timeout shared by all HTTP-polling powermeters.  The battery polls
#: roughly once per second and gives up on the CT long before a 10 s read
#: would return, so fail fast: a slow/unresponsive source should error
#: quickly and let the next poll retry rather than pin a request handler.
_DEFAULT_HTTP_TIMEOUT = ClientTimeout(total=2, connect=1)


class HttpPollingPowermeter(Powermeter):
    """Base for polling-based HTTP powermeters with shared session lifecycle.

    Subclasses pass ``base_url`` (e.g. ``http://192.168.1.1`` or
    ``http://192.168.1.1:8080``) and use :meth:`_get_json` to fetch JSON
    from relative paths.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        self.session = aiohttp.ClientSession(timeout=_DEFAULT_HTTP_TIMEOUT)

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def _get_json(self, path: str) -> Any:
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"{self._base_url}{path}"
        async with self.session.get(url) as resp:
            return await resp.json(content_type=None)
