"""End-to-end: simulated batteries detect their phase through the emulator.

The batteries run the firmware's own phase detection
(:mod:`astrameter.simulator.phase_detection`) against the CT002 emulator over
real UDP: they start with no stored phase, report ``0``, probe, and must settle
on the phase the simulated meter has them wired to. This is the baseline the
harder scenarios build on.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import _ct002_e2e_backend as be
import pytest
from _ct002_e2e_backend import E2E_UDP_PORT, EsphomeSim, HarnessClock, PollScheduler

from astrameter.ct002.ct002 import CT002
from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator

pytestmark = pytest.mark.esphome_e2e

CT_TYPE = "HME-4"  # what the ESPHome e2e binary announces


@pytest.fixture(params=["python", "esphome"], autouse=True)
def _emulator_backend(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.param == "esphome" and not be.have_esphome():
        pytest.skip("esphome CLI not on PATH; install with `uv tool install esphome`")
    be.ACTIVE_BACKEND = request.param
    yield
    be.ACTIVE_BACKEND = "python"


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Harness:
    """Batteries, a three-phase meter and the emulator, on a mock clock."""

    def __init__(self, batteries: list[dict], base_load: list[float]) -> None:
        self.backend = be.ACTIVE_BACKEND
        self._esphome = EsphomeSim() if self.backend == "esphome" else None
        port = E2E_UDP_PORT if self._esphome is not None else _free_udp_port()
        self.clock = HarnessClock(
            on_change=self._esphome.set_clock if self._esphome is not None else None
        )
        self.batteries = [
            BatterySimulator(
                ct_mac="000000000000",
                ct_port=port,
                ct_dev_type=CT_TYPE,
                initial_soc=0.8,
                poll_interval=1.0,
                startup_delay=0.0,
                ramp_rate=400.0,
                min_power_threshold=5.0,
                phase_detection=True,
                **spec,
            )
            for spec in batteries
        ]
        self.load = LoadModel(base_load=base_load, base_noise=0.0, loads=[])
        self.meter = PowermeterSimulator(
            batteries=self.batteries, load_model=self.load, port=0
        )
        self.ct002: CT002 | None = None
        if self._esphome is None:
            self.ct002 = CT002(
                udp_port=port,
                ct_type=CT_TYPE,
                active_control=True,
                clock=self.clock,
                consumer_ttl=100000,
            )

            async def before_send(_addr, _request=None, _consumer_id=None):
                return self.grid()

            self.ct002.before_send = before_send
        self._scheduler = PollScheduler(self.batteries, self.clock, self._step_one)

    def grid(self) -> list[float]:
        g = self.meter.compute_grid()
        return [g["phase_a"], g["phase_b"], g["phase_c"]]

    async def start(self) -> None:
        if self.ct002 is not None:
            await self.ct002.start()
        else:
            assert self._esphome is not None
            self._esphome.spawn()
            self._esphome.set_dedupe(0)
            self._esphome.set_clock(self.clock())

    async def stop(self) -> None:
        if self.ct002 is not None:
            await self.ct002.stop()
        elif self._esphome is not None:
            self._esphome.stop()

    async def _step_one(self, b: BatterySimulator) -> None:
        if self._esphome is None:
            await b.step(b.poll_interval)
            return
        b._advance(b.poll_interval)
        self._esphome.set_grid(*self.grid())
        await b._send_request()

    async def step(self, n: int = 1) -> None:
        await self._scheduler.step(n)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("device", ["HMG-50", "VNSE3-0"])
@pytest.mark.parametrize("wired", ["A", "B", "C"])
async def test_single_battery_detects_its_phase(device: str, wired: str) -> None:
    h = _Harness(
        [{"mac": "24215EDB1931", "phase": wired, "meter_dev_type": device}],
        base_load=[300.0, 200.0, 150.0],
    )
    await h.start()
    try:
        b = h.batteries[0]
        assert b.reported_phase == "0"
        for _ in range(30):
            await h.step()
            if b.reported_phase != "0":
                break
        assert b.reported_phase == wired
        # And it steers from there: the three-phase total is held near zero.
        await h.step(40)
        assert abs(sum(h.grid())) < 30
    finally:
        await h.stop()
