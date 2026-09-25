"""Regression for issue #652 — a Hampel-filtered CT sits out the sweep.

A Venus on firmware 1.50 re-runs its CT inspection every ~35 minutes: it polls
with phase ``'0'``, takes itself off the CT and sweeps its own output to nearly
full discharge and then nearly full charge, watching the CT reading follow.

With ``HAMPEL_WINDOW`` set, the reporter's log showed the emulator answering
that whole sweep with the *frozen* pre-sweep reading (+34 W while the house was
exporting 1712 W) — the battery was testing against a CT that never moved.  The
filter then adopted the sweep's export readings as its new normal just as the
sweep ended and rejected the true import readings that followed, so the resumed
control loop steered against an inverted grid sign and drove the battery to full
charge while the house imported 2.1 kW.

The numbers below are the ones from that log (22:15:43-22:16:20).
"""

from __future__ import annotations

from typing import Any

from astrameter.ct002 import CT002
from astrameter.ct002.protocol import build_payload, parse_request
from astrameter.powermeter.base import Powermeter
from astrameter.powermeter.wrappers import HampelPowermeter

CT_MAC = "112233445566"
BATTERY_MAC = "AABBCCDDEEFF"
# The reporter's settings.
HAMPEL_WINDOW = 7
HAMPEL_MIN_THRESHOLD = 500.0

# Readings from the log, in watts (positive = import).
STEADY_GRID = 34.0
SWEEP_EXPORT_GRID = -1712.0
POST_SWEEP_IMPORT_GRID = 2103.0
# What the battery reported at each stage.
STEADY_OUTPUT = 271
SWEEP_OUTPUT = 1993
POST_SWEEP_OUTPUT = -1993


class _FakeMeter(Powermeter):
    """Single-phase source whose reading the test sets directly."""

    def __init__(self) -> None:
        self.value = 0.0

    async def get_powermeter_watts(self) -> list[float]:
        return [self.value, 0.0, 0.0]


class _CaptureTransport:
    def __init__(self) -> None:
        self.sent: bytes | None = None

    def sendto(self, data: bytes, _addr: Any) -> None:
        self.sent = data


class _Rig:
    """A CT002 reading through a real Hampel filter, wired like main.py."""

    def __init__(self, **kwargs: Any) -> None:
        self.meter = _FakeMeter()
        self.chain = HampelPowermeter(
            self.meter,
            window=HAMPEL_WINDOW,
            min_threshold=HAMPEL_MIN_THRESHOLD,
        )
        self.resets = 0
        self.device = CT002(ct_mac=CT_MAC, reset_fn=self._reset, **kwargs)

        async def before_send(
            _addr: Any, _request: Any, _consumer_id: str
        ) -> list[float]:
            return await self.chain.get_powermeter_watts()

        self.device.before_send = before_send

    def _reset(self) -> None:
        self.resets += 1
        self.chain.reset()

    async def poll(self, phase: str, reported_power: int, grid: float) -> float:
        """Drive one battery poll with the meter reading *grid*.

        Returns the value the CT put in the grid field — the raw relay reading
        in inspection mode, the steering delta under active control.
        """
        self.meter.value = grid
        transport = _CaptureTransport()
        request = build_payload(
            ["HMG-50", BATTERY_MAC, "HME-4", CT_MAC, phase, str(reported_power)]
        )
        await self.device._handle_request(request, ("1.1.1.1", 12345), transport)
        assert transport.sent is not None, "CT002 sent no response"
        fields, error = parse_request(transport.sent)
        assert error is None, error
        assert fields is not None
        return float(fields[4])

    async def prime(self) -> None:
        """Fill the Hampel window with the steady pre-sweep grid."""
        for _ in range(HAMPEL_WINDOW):
            await self.poll("A", STEADY_OUTPUT, STEADY_GRID)


async def test_sweep_reading_reaches_the_battery() -> None:
    """The battery must see the export its own sweep caused.

    Relay-only (``active_control=False``) so the grid field carries the reading
    itself: before the fix the filter answered -1712 W of export with the
    pre-sweep +34 W, which is a CT that does not respond to a 2 kW swing.
    """
    rig = _Rig(active_control=False)
    await rig.prime()

    served = await rig.poll("0", SWEEP_OUTPUT, SWEEP_EXPORT_GRID)

    assert served == SWEEP_EXPORT_GRID, (
        f"inspection poll was answered with {served} W instead of the "
        f"{SWEEP_EXPORT_GRID} W the meter actually read"
    )


async def test_post_sweep_control_is_not_inverted() -> None:
    """Leaving inspection, the first command must follow the real grid.

    The house is importing 2.1 kW with the battery parked at full charge, so
    the only correct direction is *discharge more* (a positive delta).  Before
    the fix the filter still held the sweep's export readings, rejected the
    import, and the loop answered with a negative delta — charge harder.
    """
    rig = _Rig(active_control=True)
    await rig.prime()

    # The sweep: discharge hard, so the house exports for long enough that an
    # un-reset window (4 of its 7 slots) would carry the export as its median.
    for _ in range(HAMPEL_WINDOW // 2 + 1):
        await rig.poll("0", SWEEP_OUTPUT, SWEEP_EXPORT_GRID)

    delta = await rig.poll("A", POST_SWEEP_OUTPUT, POST_SWEEP_IMPORT_GRID)

    assert delta > 0, (
        f"post-sweep command was {delta} W: the battery is charging at "
        f"{POST_SWEEP_OUTPUT} W while the house imports "
        f"{POST_SWEEP_IMPORT_GRID} W, so it must be told to discharge"
    )


async def test_filters_are_reset_around_the_sweep_only() -> None:
    """Ordinary polls leave the filter alone; inspection polls reset it."""
    rig = _Rig(active_control=False)
    await rig.prime()
    assert rig.resets == 0, "steady-state polls must not reset the filter"

    await rig.poll("0", SWEEP_OUTPUT, SWEEP_EXPORT_GRID)
    await rig.poll("0", SWEEP_OUTPUT, SWEEP_EXPORT_GRID)
    assert rig.resets == 2, "every inspection poll keeps the filter out of the way"

    await rig.poll("A", POST_SWEEP_OUTPUT, POST_SWEEP_IMPORT_GRID)
    assert rig.resets == 3, "the first committed-phase poll clears the sweep's state"

    await rig.poll("A", POST_SWEEP_OUTPUT, POST_SWEEP_IMPORT_GRID)
    assert rig.resets == 3, "and nothing resets it again once the sweep is over"


async def test_genuine_spike_is_still_rejected() -> None:
    """The filter still does its job between sweeps.

    The reporter's meter emits ~1.7 kW single-sample spikes every couple of
    minutes — the reason they enabled Hampel at all.  Resetting around a sweep
    must not turn the filter off for the rest of the time.
    """
    rig = _Rig(active_control=False)
    await rig.prime()

    served = await rig.poll("A", STEADY_OUTPUT, 1800.0)

    assert served == STEADY_GRID, f"spike was relayed as {served} W"
