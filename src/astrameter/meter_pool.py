"""Picking the power source that answers a given client, and reading it.

Both emulators are handed the same list of configured sources and the address
of the battery polling them, and both have to answer from the one whose
``NETMASK`` covers that address — pausing first, when that source asked to be
read on its own cadence, for something newer than the value the previous poll
already served.
"""

from __future__ import annotations

from astrameter.config.logger import logger
from astrameter.config.settings import ConfiguredPowermeter
from astrameter.powermeter import Powermeter
from astrameter.powermeter.wrappers.health import HealthTrackingPowermeter

#: How long a poll waits for a pushed reading before serving the last one.
#: Batteries poll about once a second, so a longer wait would answer after the
#: poll it is answering has already been retried.
FRESH_MESSAGE_TIMEOUT_S = 2.0


def powermeter_name(powermeter: Powermeter) -> str:
    """The meter class behind the health wrapper every configured meter gets."""
    inner = (
        powermeter.wrapped_powermeter
        if isinstance(powermeter, HealthTrackingPowermeter)
        else powermeter
    )
    return type(inner).__name__


def powermeter_for(
    pool: list[ConfiguredPowermeter], client_ip: str
) -> ConfiguredPowermeter | None:
    """The first source in *pool* whose client filter covers *client_ip*."""
    for configured in pool:
        if configured.client_filter.matches(client_ip):
            return configured
    return None


async def read_fresh(configured: ConfiguredPowermeter) -> list[float]:
    """Per-phase watts, waiting out one push first when the source asked for it.

    A timed-out wait serves the cached value rather than raising: a meter that
    has gone quiet is a stale reading, not a failed poll.
    """
    powermeter = configured.powermeter
    if configured.wait_for_next_message:
        try:
            await powermeter.wait_for_next_message(timeout=FRESH_MESSAGE_TIMEOUT_S)
        except TimeoutError:
            logger.debug(
                "Powermeter %s produced no fresh message within %.0fs; "
                "serving last known value",
                powermeter_name(powermeter),
                FRESH_MESSAGE_TIMEOUT_S,
            )
    return await powermeter.get_powermeter_watts()
