"""End-to-end: simulated batteries detect their phase through the emulator.

The batteries run the firmware's own phase detection
(:mod:`astrameter.simulator.phase_detection`) against the CT002 emulator over
real UDP: they start with no stored phase, report ``0``, probe, and must settle
on the phase the simulated meter has them wired to, in the situations a real
install puts them in: next to a battery that already steers, behind a meter
that fails or updates slowly, at low charge, and on a CT003.
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
DEVICES = ["HMG-50", "VNSE3-0"]


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
    """Batteries, a three-phase meter and the emulator, on a mock clock.

    Each battery spec is passed to :class:`BatterySimulator`, except
    ``boot_at``: the battery stays silent until the clock has run that many
    seconds. ``meter_ok`` makes the meter fail (the Python emulator's
    ``before_send`` raises; the ESPHome sensor goes stale), ``meter_period``
    makes it deliver a new reading only every so many seconds, and
    ``total_only`` makes it report the three-phase total on phase A, as a
    meter without per-phase values does.
    """

    def __init__(
        self,
        batteries: list[dict],
        base_load: list[float],
        *,
        ct_type: str = CT_TYPE,
        meter_period: float = 0.0,
        total_only: bool = False,
    ) -> None:
        self.backend = be.ACTIVE_BACKEND
        if self.backend == "esphome" and ct_type != CT_TYPE:
            pytest.skip(f"the ESPHome e2e binary announces {CT_TYPE} only")
        self._esphome = EsphomeSim() if self.backend == "esphome" else None
        port = E2E_UDP_PORT if self._esphome is not None else _free_udp_port()
        self.clock = HarnessClock(
            on_change=self._esphome.set_clock if self._esphome is not None else None
        )
        self._t0 = self.clock()
        self._boot_at: dict[str, float] = {}
        self.batteries = []
        for spec in batteries:
            spec = dict(spec)
            boot_at = float(spec.pop("boot_at", 0.0))
            defaults = {
                "ct_mac": "000000000000",
                "ct_port": port,
                "ct_dev_type": ct_type,
                "initial_soc": 0.8,
                "poll_interval": 1.0,
                "startup_delay": 0.0,
                "ramp_rate": 400.0,
                "min_power_threshold": 5.0,
                "phase_detection": True,
            }
            b = BatterySimulator(**{**defaults, **spec})
            self._boot_at[b.mac] = boot_at
            self.batteries.append(b)
        self.meter_ok = True
        self.meter_period = meter_period
        self.total_only = total_only
        self._reading: list[float] | None = None
        self._reading_at = 0.0
        self.load = LoadModel(base_load=base_load, base_noise=0.0, loads=[])
        self.meter = PowermeterSimulator(
            batteries=self.batteries, load_model=self.load, port=0
        )
        self.ct002: CT002 | None = None
        if self._esphome is None:
            self.ct002 = CT002(
                udp_port=port,
                ct_type=ct_type,
                active_control=True,
                clock=self.clock,
                consumer_ttl=100000,
            )

            async def before_send(_addr, _request=None, _consumer_id=None):
                if not self.meter_ok:
                    raise ValueError("meter unavailable (test)")
                return self.reading()

            self.ct002.before_send = before_send
        self._scheduler = PollScheduler(self.batteries, self.clock, self._step_one)

    def grid(self) -> list[float]:
        """The true per-phase grid power."""
        g = self.meter.compute_grid()
        return [g["phase_a"], g["phase_b"], g["phase_c"]]

    def reading(self) -> list[float]:
        """What the meter reports: the grid, held for ``meter_period`` and
        folded onto phase A when ``total_only``."""
        now = self.clock()
        if self._reading is None or now - self._reading_at >= self.meter_period:
            g = self.grid()
            self._reading = [sum(g), 0.0, 0.0] if self.total_only else g
            self._reading_at = now
        return list(self._reading)

    def elapsed(self) -> float:
        return self.clock() - self._t0

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
        if self.elapsed() < self._boot_at[b.mac]:
            return
        if self._esphome is None:
            await b.step(b.poll_interval)
            return
        b._advance(b.poll_interval)
        if self.meter_ok:
            self._esphome.set_grid(*self.reading())
        else:
            self._esphome.sensor_stale()
        await b._send_request()

    async def step(self, n: int = 1) -> None:
        await self._scheduler.step(n)

    async def run_until_detected(self, max_steps: int = 120) -> None:
        """Step until no battery is detecting any more (or *max_steps*)."""
        for _ in range(max_steps):
            if all(not b.detector.active for b in self.batteries):
                return
            await self.step()

    def phases(self) -> list[str]:
        return [b.reported_phase for b in self.batteries]


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


async def _settle_and_check_grid(h: _Harness, steps: int = 60) -> None:
    """After detection the batteries steer the three-phase total to ~0."""
    await h.step(steps)
    assert abs(sum(h.grid())) < 50, h.grid()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize(("steering", "wired"), [("A", "C"), ("C", "B"), ("A", "A")])
async def test_detects_next_to_a_steering_battery(
    device: str, steering: str, wired: str
) -> None:
    """A battery joins one that already steers, on another phase or its own.

    The steering battery reacts to the newcomer's probe; with a real CT only a
    battery on the probed phase would.
    """
    h = _Harness(
        [
            {
                "mac": "24215EDB1931",
                "phase": steering,
                "stored_phase": steering,
                "meter_dev_type": device,
            },
            {
                "mac": "ACD929A74B20",
                "phase": wired,
                "meter_dev_type": device,
                "boot_at": 30.0,
            },
        ],
        base_load=[400.0, 200.0, 300.0],
    )
    await h.start()
    try:
        await h.step(30)
        await h.run_until_detected(300)
        assert h.phases() == [steering, wired]
    finally:
        await h.stop()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("outage_s", [1, 5])
async def test_meter_outage_during_probe(device: str, outage_s: int) -> None:
    """The meter drops out while the battery probes, then comes back."""
    h = _Harness(
        [{"mac": "24215EDB1931", "phase": "B", "meter_dev_type": device}],
        base_load=[300.0, 200.0, 150.0],
    )
    await h.start()
    try:
        b = h.batteries[0]
        for _ in range(30):
            await h.step()
            if b.detector._probing or not b.detector.active:
                break
        assert b.detector._probing, "probe never started"
        h.meter_ok = False
        await h.step(outage_s)
        h.meter_ok = True
        await h.run_until_detected(400)
        assert h.phases() == ["B"]
        await _settle_and_check_grid(h)
    finally:
        await h.stop()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("device", DEVICES)
async def test_meter_not_ready_at_startup(device: str) -> None:
    """The emulator answers before its meter has delivered anything.

    Phase A carries more than the detection threshold, so a baseline taken
    from a substitute reading would see A move when the real one arrives.
    """
    h = _Harness(
        [{"mac": "24215EDB1931", "phase": "C", "meter_dev_type": device}],
        base_load=[700.0, 0.0, 50.0],
    )
    h.meter_ok = False
    await h.start()
    try:
        await h.step(5)
        h.meter_ok = True
        await h.run_until_detected(400)
        assert h.phases() == ["C"]
        await _settle_and_check_grid(h)
    finally:
        await h.stop()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("period", [5.0, 10.0])
async def test_slow_meter(device: str, period: float) -> None:
    """A meter that delivers a reading only every few seconds."""
    h = _Harness(
        [{"mac": "24215EDB1931", "phase": "B", "meter_dev_type": device}],
        base_load=[300.0, 200.0, 150.0],
        meter_period=period,
    )
    await h.start()
    try:
        await h.run_until_detected(400)
        assert h.phases() == ["B"]
    finally:
        await h.stop()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("device", DEVICES)
async def test_low_charge_probes_by_charging(device: str) -> None:
    """At or below 50 % the probe charges instead of discharging."""
    h = _Harness(
        [
            {
                "mac": "24215EDB1931",
                "phase": "A",
                "meter_dev_type": device,
                "initial_soc": 0.3,
            }
        ],
        base_load=[300.0, 200.0, 150.0],
    )
    await h.start()
    try:
        b = h.batteries[0]
        lowest = 0.0
        for _ in range(400):
            if not b.detector.active:
                break
            await h.step()
            lowest = min(lowest, b.target_power)
        assert lowest < 0, "the probe should charge"
        assert h.phases() == ["A"]
    finally:
        await h.stop()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize(("total_only", "expected"), [(False, "B"), (True, "A")])
async def test_ct003(device: str, total_only: bool, expected: str) -> None:
    """CT003, with a per-phase meter and with a total-only one.

    A total-only meter puts everything on phase A, so A is what any battery
    can detect there, wherever it is wired.
    """
    h = _Harness(
        [{"mac": "24215EDB1931", "phase": "B", "meter_dev_type": device}],
        base_load=[300.0, 200.0, 150.0],
        ct_type="HME-3",
        total_only=total_only,
    )
    await h.start()
    try:
        await h.run_until_detected(400)
        assert h.phases() == [expected]
        await _settle_and_check_grid(h)
    finally:
        await h.stop()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("steered", DEVICES)
@pytest.mark.parametrize(("ct_type", "total_only"), [(CT_TYPE, False), ("HME-3", True)])
async def test_battery_stuck_in_detection_does_not_hold_back_the_steered_one(
    steered: str, ct_type: str, total_only: bool
) -> None:
    """Issue #732: one battery never finds its phase and idles at ~0 W.

    Its charge gate is closed (at 30 % SoC detection needs the charge flag), so
    it keeps reporting phase 0. The battery next to it must still cover the
    whole load: counted in the steering pool, the idle one was handed a share
    and pulled its neighbour toward 0 W, leaving about a quarter of the load on
    the grid. The CT003 / total-only variant is the reporter's setup.
    """
    h = _Harness(
        [
            {
                "mac": "24215EDB1931",
                "phase": "A",
                "stored_phase": "A",
                "meter_dev_type": steered,
            },
            {
                "mac": "CCC837B39DBE",
                "phase": "A",
                "meter_dev_type": "VNSE3-0",
                "initial_soc": 0.3,
            },
        ],
        base_load=[600.0, 0.0, 0.0],
        ct_type=ct_type,
        total_only=total_only,
    )
    h.batteries[1].charge_allowed = False
    await h.start()
    try:
        await h.step(150)
        assert h.phases() == ["A", "0"]
        assert abs(h.batteries[1].current_power) < 1
        assert abs(sum(h.grid())) < 30, h.grid()
    finally:
        await h.stop()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize(
    ("neighbour", "wired", "delay_ticks"), [("A", "A", 0), ("A", "B", 2)]
)
async def test_slow_probe_next_to_a_steering_battery(
    device: str, neighbour: str, wired: str, delay_ticks: int
) -> None:
    """A probe that ramps slowly (50 W/s) is not cancelled by a neighbour.

    AstraMeter sees the probe as a grid change; steering the neighbour against
    it kept the probing battery's phase from ever moving by the threshold (same
    phase), or moved the neighbour's own phase too (another phase, HMG-50 gives
    up), and detection failed three times. The steered batteries now steer on
    the grid without the probe the detecting battery reports.
    """
    phys = {"ramp_rate": 50.0, "power_update_delay_ticks": delay_ticks}
    h = _Harness(
        [
            {
                "mac": "24215EDB1931",
                "phase": neighbour,
                "stored_phase": neighbour,
                "meter_dev_type": "HMG-50",
                "max_discharge_power": 2500,
                "max_charge_power": 2500,
                **phys,
            },
            {
                "mac": "CCC837B39DBE",
                "phase": wired,
                "meter_dev_type": device,
                "boot_at": 60.0,
                **phys,
            },
        ],
        base_load=[600.0, 300.0, 0.0],
    )
    await h.start()
    try:
        await h.step(60)
        await h.run_until_detected(400)
        assert h.phases() == [neighbour, wired]
        assert h.batteries[1].detector.attempts == 0, "needed a retry"
    finally:
        await h.stop()
