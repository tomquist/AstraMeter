"""Regression test for GitHub issue #447 — run against both emulator backends.

This is the #376 PV-passthrough scenario pushed into the *saturation regime*
the #376 test never reaches.  In #376 the grid surplus is enormous, so a full
Venus D is simply parked at 0 W and its phase carries no instructed power.  In
#447 the surplus is only modestly above the charging battery's draw, so the
balancer keeps asking the full Venus D to take a (small) share of the charge.

The bug: the emulator recorded each consumer's instructed power using only the
*own-phase fragment* of the phase-split target.  Venus D's charge command is
split across phases A and C (Venus E sits on C), so the phase-A fragment alone
(~-470 W) is smaller in magnitude than Venus D's involuntary PV passthrough
(~+490 W).  ``reported + own_phase_fragment`` then comes out *positive*, and the
emulator broadcasts it as ``A_dchrg_power`` — a phantom discharge.

A real Venus E in whole-house "aggregate" mode (``rechrg_mode == 1``, see
``docs/marstek-firmware-behavior.md``) sums all phases' ``*_dchrg_power``
including phase A, so that phantom value throttles its charging — the oscillating
on/off the user reported.

The fix records the *full* instructed net power (``reported + sum(values)``),
which is exactly what the battery firmware integrates from the three phase
fields.  Then a battery commanded to charge never shows up as discharging,
regardless of phase split or involuntary passthrough.
"""

from __future__ import annotations

import _ct002_e2e_backend as be
import pytest
from _ct002_e2e_backend import E2E_UDP_PORT, EsphomeSim, HarnessClock, find_free_ports

from astrameter.ct002.ct002 import CT002
from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator

pytestmark = pytest.mark.esphome_e2e


@pytest.fixture(params=["python", "esphome"], autouse=True)
def _emulator_backend(request):
    if request.param == "esphome" and not be.have_esphome():
        pytest.skip("esphome CLI not on PATH; install with `uv tool install esphome`")
    be.ACTIVE_BACKEND = request.param
    yield
    be.ACTIVE_BACKEND = "python"


class _Issue447Harness:
    """Full Venus D (PV passthrough) + charging Venus E, near-saturation surplus."""

    def __init__(self) -> None:
        self.backend = be.ACTIVE_BACKEND
        self._esphome = EsphomeSim() if self.backend == "esphome" else None
        free_udp, http_port = find_free_ports(2)
        ct_port = E2E_UDP_PORT if self.backend == "esphome" else free_udp
        self.http_port = http_port
        self.clock = HarnessClock(
            on_change=(self._esphome.set_clock if self._esphome is not None else None)
        )

        # Modest surplus: ~3 kW total, only a little above Venus E's draw once it
        # ramps up.  This keeps the full Venus D in the charge pool (rather than
        # parked at 0 like #376), which is what exposes the own-phase-fragment
        # bookkeeping bug.
        self.load_model = LoadModel(
            base_load=[-1000.0, -1000.0, -1000.0],
            base_noise=0.0,
            loads=[],
        )

        ct_mac = "112233445566"
        self.venus_d = BatterySimulator(
            mac="02B250000001",
            phase="A",
            ct_mac=ct_mac,
            ct_host="127.0.0.1",
            ct_port=ct_port,
            max_charge_power=800,
            max_discharge_power=800,
            initial_soc=1.0,
            ramp_rate=400.0,
            poll_interval=0.3,
            min_power_threshold=5.0,
            startup_delay=0.0,
            inspection_count=0,
            max_dc_input=500,
            dc_input_power=490.0,
            idle_on_cross_phase_discharge=True,
            discharge_idle_mode="aggregate",
        )
        self.venus_e = BatterySimulator(
            mac="02B250000002",
            phase="C",
            ct_mac=ct_mac,
            ct_host="127.0.0.1",
            ct_port=ct_port,
            max_charge_power=2500,
            max_discharge_power=2500,
            initial_soc=0.5,
            ramp_rate=400.0,
            poll_interval=0.3,
            min_power_threshold=5.0,
            startup_delay=0.0,
            inspection_count=0,
            idle_on_cross_phase_discharge=True,
            discharge_idle_mode="aggregate",
        )
        self.batteries = [self.venus_d, self.venus_e]

        self.powermeter = PowermeterSimulator(
            batteries=self.batteries,
            load_model=self.load_model,
            host="127.0.0.1",
            port=http_port,
        )

        if self.backend == "python":
            self.ct002 = CT002(
                udp_port=ct_port,
                ct_mac=ct_mac,
                active_control=True,
                fair_distribution=True,
                min_efficient_power=0,
                clock=self.clock,
                reset_fn=None,
                consumer_ttl=100000,
            )

            async def update_readings(_addr, _fields=None, _consumer_id=None):
                grid = self.powermeter.compute_grid()
                return [grid["phase_a"], grid["phase_b"], grid["phase_c"]]

            self.ct002.before_send = update_readings
        else:
            self.ct002 = None

    async def start(self) -> None:
        await self.powermeter.start()
        if self.backend == "python":
            await self.ct002.start()
        else:
            self._esphome.spawn()
            self._esphome.set_dedupe(0)
            self._esphome.set_cfg("min_efficient_power", 0)
            self._esphome.set_cfg("fair_distribution", 1)
            self._esphome.set_clock(self.clock())

    async def stop(self) -> None:
        if self.backend == "python":
            await self.ct002.stop()
        else:
            self._esphome.stop()
        await self.powermeter.stop()

    async def _step_battery(self, b: BatterySimulator) -> None:
        if self.backend == "python":
            await b.step(b.poll_interval)
            return
        b._step_index += 1
        b._drain_pending_power_targets()
        b._update_power(b.poll_interval)
        b._update_soc(b.poll_interval)
        grid = self.powermeter.compute_grid()
        self._esphome.set_grid(grid["phase_a"], grid["phase_b"], grid["phase_c"])
        await b._send_request()

    async def step(self, n: int = 1) -> None:
        for _ in range(n):
            max_dt = max(b.poll_interval for b in self.batteries)
            for b in self.batteries:
                await self._step_battery(b)
            self.clock.advance(max_dt)

    # -- backend-agnostic emulator-state accessors -------------------------

    def phase_dchrg(self, phase: str) -> float:
        """Aggregated *_dchrg_power for a phase (positive instructed power)."""
        if self.backend == "python":
            return self.ct002._collect_reports_by_phase()[phase]["dchrg_power"]
        consumers = self._esphome.dump()["consumers"].values()
        return sum(
            c["last_instructed"]
            for c in consumers
            if c["phase"] == phase and c["last_instructed"] > 0
        )

    def last_instructed(self, mac: str) -> float:
        if self.backend == "python":
            consumer = self.ct002._consumers.get(mac.lower())
            assert consumer is not None
            return consumer.last_instructed_power
        entry = self._esphome.dump()["consumers"].get(mac.lower())
        assert entry is not None, f"no consumer {mac} in dump"
        return entry["last_instructed"]


async def test_no_phantom_dchrg_for_charging_full_battery() -> None:
    h = _Issue447Harness()
    await h.start()
    try:
        # Warm-up: let the balancer drive Venus E up toward the surplus.
        await h.step(40)

        venus_e_powers: list[float] = []
        max_a_dchrg = 0.0
        worst_d_instructed = float("-inf")
        for _ in range(60):
            await h.step(1)
            venus_e_powers.append(h.venus_e.current_power)
            max_a_dchrg = max(max_a_dchrg, h.phase_dchrg("A"))
            worst_d_instructed = max(
                worst_d_instructed, h.last_instructed(h.venus_d.mac)
            )
        avg_venus_e = sum(venus_e_powers) / len(venus_e_powers)

        # 1. The core invariant: a full Venus D that the balancer is driving to
        #    charge must never be broadcast as discharging on phase A, even
        #    though it physically passes PV through as positive power.
        assert max_a_dchrg == 0.0, (
            f"[{h.backend}] A_dchrg_power must stay 0 while Venus D is instructed "
            f"to charge; saw phantom discharge up to {max_a_dchrg:.0f} W"
        )

        # 2. Equivalently: Venus D's recorded instructed power is a charge (<= 0),
        #    never a phantom positive (the passthrough leaking through).
        assert worst_d_instructed <= 0.0, (
            f"[{h.backend}] Venus D instructed power should be a charge (<=0); "
            f"worst was {worst_d_instructed:.0f} W"
        )

        # 3. User-visible symptom: with no phantom discharge to throttle it,
        #    Venus E charges hard into the surplus instead of oscillating.
        assert avg_venus_e < -2000.0, (
            f"[{h.backend}] Venus E should charge steadily into the surplus; got "
            f"avg={avg_venus_e:.0f}, samples={venus_e_powers}"
        )

        # 4. Sanity: Venus D really is doing involuntary PV passthrough.
        assert h.venus_d.current_power > 0, (
            f"[{h.backend}] Venus D should be passing PV through; got "
            f"current_power={h.venus_d.current_power}"
        )
    finally:
        await h.stop()
