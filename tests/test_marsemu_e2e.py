"""End-to-end against real battery firmware: the CT002 emulator steers emulated
Marstek devices running their unmodified firmware images.

``marsemu`` runs the firmware (CPU, Wi-Fi module, power stage and battery
models).  The device's CT002 polls are handed to ``CT002._handle_request``
in-process and its reply goes straight back to the firmware, all in the
device's emulated time, so the loop is deterministic and runs faster than
real time.  The house sees what the device actually delivers: the grid
reading AstraMeter answers with is the load minus the battery's output.

Opt-in: skipped unless ``marsemu`` is importable and the firmware images are
present (they are not redistributable).  To run::

    uv run --with-editable /path/to/marsemu pytest tests/test_marsemu_e2e.py

with ``MARSEMU_FIRMWARE`` pointing at images laid out as
``<device type>/<version>/*.bin`` (e.g. ``HMA-1/V232/...bin``) and, for the
Venus E 3.0, ``MARSEMU_ARCHIVE`` at a firmware archive containing
``VNSE3-0/Control/151`` and ``VNSE3-0/BMS/115``.  Each test takes one to
three minutes.
"""

from __future__ import annotations

import asyncio
import glob
import os
from typing import Any

import pytest

from astrameter.ct002.ct002 import CT002

marsemu = pytest.importorskip("marsemu")

FIRMWARE = os.environ.get("MARSEMU_FIRMWARE", "")
ARCHIVE = os.environ.get("MARSEMU_ARCHIVE", "")
CT_MAC = "112233445566"
#: virtual epoch the CT002 clock starts at (device time 0)
T0 = 1_700_000_000.0


def _image(root: str, pattern: str) -> str:
    if not root:
        pytest.skip("firmware location not set (MARSEMU_FIRMWARE / MARSEMU_ARCHIVE)")
    files = sorted(glob.glob(os.path.join(root, pattern), recursive=True))
    if not files:
        pytest.skip(f"firmware not available: {pattern}")
    return files[0]


class _Sink:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, addr: Any) -> None:
        self.sent.append(bytes(data))


class _Harness:
    """One emulated device in a house, its CT002 polls answered by ``CT002``."""

    def __init__(self, device: Any, house: Any) -> None:
        self.device = device
        self.house = house
        self.ct002 = CT002(
            ct_mac=CT_MAC, active_control=True, clock=lambda: T0 + device.time
        )
        self.ct002.before_send = self._meter
        self._loop = asyncio.new_event_loop()
        self.phases: list[str] = []
        self.meter = marsemu.connect_house(device, house, meter=self._answer)
        marsemu.configure_self_consumption(device, ct_mac=CT_MAC)
        # emulated time since the reading last left the tolerance band
        self._tol = 0.0
        self._last_out = 0.0
        device.sim.tickers.append(self._watch)

    async def _meter(self, addr: Any, request: Any, consumer_id: str) -> list[float]:
        g = self.house.grid()
        return [g["A"], g["B"], g["C"]]

    def _answer(self, data: bytes, addr: tuple[str, int]) -> bytes | None:
        sink = _Sink()
        self._loop.run_until_complete(self.ct002._handle_request(data, addr, sink))
        return sink.sent[-1] if sink.sent else None

    def total(self) -> float:
        return float(sum(self.house.grid().values()))

    def _watch(self, now: float, dt: float) -> None:
        if abs(self.total()) > self._tol:
            self._last_out = now

    def settle(self, tolerance: float, hold: float, budget: float) -> bool:
        """Run until the total grid power has stayed within ``tolerance`` for
        ``hold`` seconds (emulated), at most ``budget`` seconds."""
        self._tol = tolerance
        self._last_out = self.device.time
        self.device.run(budget, until=lambda: self.device.time - self._last_out >= hold)
        return self.device.time - self._last_out >= hold

    def consumer(self) -> Any:
        consumers = self.ct002.status_snapshot().consumers
        assert len(consumers) == 1, consumers
        return consumers[0]

    def close(self) -> None:
        self.device.close()
        self._loop.close()


@pytest.mark.timeout(900)
@pytest.mark.parametrize("build", ["HMA-1/V232", "HMJ-2/V118"])
def test_b2500_nulls_the_grid_and_follows_a_load_step(build: str) -> None:
    """B2500 (DC-output steering): covers the house load from its battery and
    PV, and follows a load step."""
    image = _image(FIRMWARE, f"{build}/*.bin")
    device = marsemu.B2500(image, pv=[300, 300])
    h = _Harness(device, marsemu.House({"A": 200.0}))
    try:
        assert h.settle(tolerance=15, hold=20, budget=180), (
            f"grid not nulled: {h.house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert device.ac_power > 150
        h.house.loads["A"] = 450.0
        assert h.settle(tolerance=15, hold=20, budget=90), (
            f"load step not followed: {h.house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert device.ac_power > 400
        assert h.meter.polls > 50
        assert h.consumer().phase == "A"
    finally:
        h.close()


@pytest.mark.timeout(1200)
def test_venus_e3_detects_its_phase_and_nulls_the_total() -> None:
    """Venus E 3.0: the firmware's phase detection (inspection polls) runs
    against the CT002 and finds the phase it is on (B), then it nulls the
    total over all phases and follows a load step."""
    control = _image(ARCHIVE, "**/VNSE3-0/Control/151/*.bin")
    bms = _image(ARCHIVE, "**/VNSE3-0/BMS/115/*.bin")
    device = marsemu.VenusE3(control, bms)
    house = marsemu.House({"A": 100.0, "B": 600.0, "C": 50.0}, battery_phase="B")
    h = _Harness(device, house)
    try:
        assert h.settle(tolerance=25, hold=20, budget=240), (
            f"total not nulled: {house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert h.consumer().phase == "B"
        assert device.ac_power > 600
        house.loads["B"] = 300.0
        assert h.settle(tolerance=25, hold=20, budget=120), (
            f"load step not followed: {house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert 350 < device.ac_power < 500
    finally:
        h.close()
