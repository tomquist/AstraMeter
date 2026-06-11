"""Regression test for GitHub issue #376 — run against both emulator backends.

A Venus D-like battery (full SoC, PV passthrough -> AC) on phase A reports
positive ``power`` to the CT002 emulator while a Venus E-like battery on
phase C is charging.  The user-visible bug was Venus E stopping charging the
moment Venus D enabled "feed excess to grid": the emulator misattributed
Venus D's passthrough output as an instructed discharge (``A_dchrg_power``),
distorting the response state the batteries steer on.

AstraMeter must keep instructing Venus E to charge across many ticks (i.e.
not broadcast Venus D's passthrough as a discharge signal). The same
behaviour is asserted against both the in-process Python CT002 and the
compiled ESPHome host binary; the per-consumer ``last_instructed_power`` and
the per-phase ``dchrg_power`` are read from the emulator's state on both
sides (directly in Python, via the test-hooks ``dump`` command on ESPHome).
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


class _Issue376Harness:
    """Venus D-like (PV passthrough) + Venus E-like, on both backends."""

    def __init__(self) -> None:
        self.backend = be.ACTIVE_BACKEND
        self._esphome = EsphomeSim() if self.backend == "esphome" else None
        free_udp, http_port = find_free_ports(2)
        ct_port = E2E_UDP_PORT if self.backend == "esphome" else free_udp
        self.http_port = http_port
        self.clock = HarnessClock(
            on_change=(self._esphome.set_clock if self._esphome is not None else None)
        )

        # Strong export on all phases (mirrors the Tasmota readings in the
        # log attached to issue #376).
        self.load_model = LoadModel(
            base_load=[-7000.0, -7800.0, -3900.0],
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
            dc_input_power=500.0,
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


async def test_venus_e_keeps_charging_during_venus_d_pv_passthrough() -> None:
    h = _Issue376Harness()
    await h.start()
    try:
        # Warm-up: let the balancer settle. The faithful battery model's
        # input-conditioning gate debounces the initial large export for a
        # cycle, so the steering takes a little longer to ramp in than the
        # bare law did; give it enough cycles to reach steady state.
        await h.step(80)

        venus_e_powers: list[float] = []
        for _ in range(10):
            await h.step(1)
            venus_e_powers.append(h.venus_e.current_power)
        avg_venus_e = sum(venus_e_powers) / len(venus_e_powers)

        # 1. Venus E is still charging hard, not idle (the user-visible bug).
        assert avg_venus_e < -500.0, (
            f"[{h.backend}] Venus E should be charging despite Venus D's PV "
            f"passthrough; got avg={avg_venus_e:.0f}, samples={venus_e_powers}"
        )

        # 2. A_dchrg_power must be 0 — Venus D's positive output must not be
        #    broadcast as a discharge signal.
        assert h.phase_dchrg("A") == 0, (
            f"[{h.backend}] A_dchrg_power should be 0 (Venus D was instructed to charge)"
        )

        # 3. Sanity: Venus D *was* instructed to charge.
        assert h.last_instructed(h.venus_d.mac) < 0.0, (
            f"[{h.backend}] Venus D should have been instructed to charge "
            f"(negative target on phase A)"
        )

        # 4. Sanity: Venus D is in fact passing PV through to AC.
        assert h.venus_d.current_power > 0, (
            f"[{h.backend}] Venus D should be doing PV passthrough; got "
            f"current_power={h.venus_d.current_power}"
        )
    finally:
        await h.stop()
