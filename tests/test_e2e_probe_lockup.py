"""End-to-end regression: probe handoff under the log2 topology.

Scenario mirrors the user's log2 (2026-04-10):
    - Two batteries, both self-report phase ``B``.
    - Grid load is on a different phase (phase ``A``), so the only
      way to zero the grid is via cross-phase compensation (which
      the CT002 protocol allows by design).
    - Efficiency optimization is enabled.
    - A probe-based handoff is forced.

The exact "target pinned at 0, grid drifts ~97 W for 1.5 h" symptom
from the production log requires a *stale meter source*, which this
in-process harness can't easily reproduce (the simulator always
returns fresh values).  What this test DOES cover is the "happy
path" of the handoff itself: the new active battery must end up
covering the real demand within the deadband.
"""

from __future__ import annotations

import socket
import time

import pytest

from astrameter.ct002.ct002 import CT002
from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator


class _FakeClock:
    def __init__(self) -> None:
        self._now = time.time()

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _find_free_ports(n: int) -> list[int]:
    socks: list[socket.socket] = []
    ports: list[int] = []
    try:
        for i in range(n):
            s = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM if i == 0 else socket.SOCK_STREAM,
            )
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
            socks.append(s)
    finally:
        for s in socks:
            s.close()
    return ports


class _Harness:
    """Bespoke harness that places both batteries on phase B with the load
    on phase A, so the only way to zero the grid is via cross-phase
    compensation (which the CT002 protocol allows — see
    ``docs/ct002-ct003-protocol.md``).
    """

    def __init__(
        self,
        *,
        load_a: float = 94.0,
        min_efficient_power: int = 50,
        efficiency_rotation_interval: int = 20,
    ) -> None:
        ct_port, http_port = _find_free_ports(2)
        self.clock = _FakeClock()
        self.load_model = LoadModel(
            base_load=[load_a, 0.0, 0.0],
            base_noise=0.0,
            loads=[],
        )
        ct_mac = "112233445566"
        # Match real Marstek ramp behaviour: slower ramp + a real
        # startup delay so the candidate doesn't instantly jump from
        # 0W to the probe target.
        self.batteries: list[BatterySimulator] = [
            BatterySimulator(
                mac="24215EDB1936",
                phase="B",
                ct_mac=ct_mac,
                ct_host="127.0.0.1",
                ct_port=ct_port,
                max_charge_power=800,
                max_discharge_power=800,
                initial_soc=0.8,
                ramp_rate=5.0,
                poll_interval=3.0,
                min_power_threshold=5.0,
                startup_delay=10.0,
            ),
            BatterySimulator(
                mac="ACD929A74B20",
                phase="B",
                ct_mac=ct_mac,
                ct_host="127.0.0.1",
                ct_port=ct_port,
                max_charge_power=800,
                max_discharge_power=800,
                initial_soc=0.8,
                ramp_rate=5.0,
                poll_interval=3.0,
                min_power_threshold=5.0,
                startup_delay=10.0,
            ),
        ]
        self.powermeter = PowermeterSimulator(
            batteries=self.batteries,
            load_model=self.load_model,
            host="127.0.0.1",
            port=http_port,
        )
        self.ct002 = CT002(
            udp_port=ct_port,
            ct_mac=ct_mac,
            active_control=True,
            fair_distribution=True,
            smooth_target_alpha=0.9,
            deadband=5,
            min_efficient_power=min_efficient_power,
            efficiency_rotation_interval=efficiency_rotation_interval,
            probe_min_power=20,  # lower so the test's small loads can probe
            clock=self.clock,
        )

        async def update_readings(_addr, _fields=None, _consumer_id=None):
            grid = self.powermeter.compute_grid()
            return [grid["phase_a"], grid["phase_b"], grid["phase_c"]]

        self.ct002.before_send = update_readings

    async def start(self) -> None:
        await self.powermeter.start()
        await self.ct002.start()

    async def stop(self) -> None:
        await self.ct002.stop()
        await self.powermeter.stop()

    async def step(self, n: int = 1) -> None:
        for _ in range(n):
            for b in self.batteries:
                await b.step(b.poll_interval)
            self.clock.advance(max(b.poll_interval for b in self.batteries))

    def battery_powers(self) -> list[float]:
        return [b.current_power for b in self.batteries]

    def grid_total(self) -> float:
        g = self.powermeter.compute_grid()
        return g["phase_a"] + g["phase_b"] + g["phase_c"]


@pytest.mark.timeout(60)
class TestProbeLockup:
    async def test_grid_recovers_after_probe_handoff(self) -> None:
        """After an efficiency-rotation probe handoff, the grid must not
        stay pinned at the load magnitude — the new active battery
        should continue to cover it.
        """
        h = _Harness(
            load_a=94.0,
            min_efficient_power=50,
            efficiency_rotation_interval=9999,  # Manual rotation only
        )
        await h.start()
        try:
            # Warm-up: let one battery take over as the sole active one.
            await h.step(200)

            before_powers = h.battery_powers()
            active_idx = 0 if abs(before_powers[0]) > abs(before_powers[1]) else 1
            standby_idx = 1 - active_idx

            # Confirm only one battery is active.
            assert abs(before_powers[active_idx]) > 40.0, (
                f"Warm-up failed to concentrate demand on one battery. "
                f"Powers: {before_powers}"
            )
            assert abs(before_powers[standby_idx]) < 25.0, (
                f"Warm-up failed to deprioritize the other battery. "
                f"Powers: {before_powers}"
            )

            grid_warmup = abs(h.grid_total())
            assert grid_warmup < 30.0, (
                f"Grid should be near zero after warm-up. grid={grid_warmup:.1f}"
            )

            # Force a rotation directly — this is the deterministic
            # way to exercise the probe handoff path regardless of the
            # rotation-interval clock arithmetic.
            h.ct002.force_efficiency_rotation()
            # Step through the probe and handoff.  Allow enough steps
            # for the probe (~5s) + post-probe fade (~5s) + settling.
            for _ in range(150):
                await h.step()

            after_powers = h.battery_powers()
            grid_after = abs(h.grid_total())

            # Main assertion: the grid must still be close to zero.
            assert grid_after < 30.0, (
                f"Grid is uncompensated after probe handoff: "
                f"grid={grid_after:.1f} W. Powers: {after_powers}."
            )
            # The rotation must have actually happened — the previously
            # active battery must no longer be the sole contributor.
            new_active = 0 if abs(after_powers[0]) > abs(after_powers[1]) else 1
            assert new_active != active_idx, (
                f"Rotation didn't swap the active battery. "
                f"before={before_powers} after={after_powers}"
            )
        finally:
            await h.stop()
