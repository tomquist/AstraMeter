"""End-to-end simulation tests for the efficiency optimization feature.

These tests create a full stack: CT002 → BatterySimulators → LoadModel → PowermeterSim,
and verify that the efficiency optimization correctly concentrates power on fewer
batteries at low demand and distributes to all at high demand.

All tests use **deterministic stepping** — no ``asyncio.sleep``, no random
jitter.  The test harness drives one simulation iteration at a time via
``BatterySimulator.step()`` with a controllable fake clock injected into
the balancer so that time-dependent logic (rotation, saturation grace,
probe deadlines) is fully reproducible.
"""

from __future__ import annotations

import _ct002_e2e_backend as be
import pytest
from _ct002_e2e_backend import E2E_UDP_PORT, EsphomeSim, HarnessClock, find_free_ports

from astrameter.ct002.ct002 import CT002
from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import Load, LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator

pytestmark = pytest.mark.esphome_e2e


# Every test runs once per emulator backend. The python backend always runs;
# esphome skips without the CLI. See tests/_ct002_e2e_backend.py.
@pytest.fixture(params=["python", "esphome"], autouse=True)
def _emulator_backend(request):
    if request.param == "esphome" and not be.have_esphome():
        pytest.skip("esphome CLI not on PATH; install with `uv tool install esphome`")
    be.ACTIVE_BACKEND = request.param
    yield
    be.ACTIVE_BACKEND = "python"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _SimHarness:
    """Wire CT002 + batteries + powermeter sim for deterministic E2E tests.

    Batteries are **not** started as background tasks.  Instead the test
    calls :meth:`step` or :meth:`step_until` to advance the simulation
    one iteration at a time.
    """

    def __init__(
        self,
        num_batteries: int = 2,
        base_load: list[float] | None = None,
        loads: list[Load] | None = None,
        min_efficient_power: int = 0,
        efficiency_rotation_interval: int = 900,
        poll_interval: float = 0.3,
        poll_intervals: list[float] | None = None,
        base_noise: float = 0.0,
        startup_delay: float = 2.0,
        startup_delays: list[float] | None = None,
        min_power_threshold: float = 5.0,
        min_power_thresholds: list[float] | None = None,
        **ct_kwargs,
    ):
        self.backend = be.ACTIVE_BACKEND
        self._esphome = EsphomeSim() if self.backend == "esphome" else None
        free_udp, http_port = find_free_ports(2)
        # ESPHome binary listens on a fixed port (its YAML); the in-process
        # Python CT002 can take an ephemeral one.
        ct_port = E2E_UDP_PORT if self.backend == "esphome" else free_udp
        self.ct_port = ct_port
        self.http_port = http_port
        self.clock = HarnessClock(
            on_change=(self._esphome.set_clock if self._esphome is not None else None)
        )

        if base_load is None:
            base_load = [200.0, 0.0, 0.0]

        self.load_model = LoadModel(
            base_load=list(base_load),
            base_noise=base_noise,
            loads=[Load(ld.name, ld.power, ld.phase) for ld in (loads or [])],
        )

        ct_mac = "112233445566"
        self.batteries: list[BatterySimulator] = []
        for i in range(num_batteries):
            mac = f"02B250{i + 1:06X}"
            battery_poll_interval = (
                poll_intervals[i] if poll_intervals is not None else poll_interval
            )
            battery_startup_delay = (
                startup_delays[i] if startup_delays is not None else startup_delay
            )
            battery_min_power_threshold = (
                min_power_thresholds[i]
                if min_power_thresholds is not None
                else min_power_threshold
            )
            self.batteries.append(
                BatterySimulator(
                    mac=mac,
                    phase="A",
                    ct_mac=ct_mac,
                    ct_host="127.0.0.1",
                    ct_port=ct_port,
                    max_charge_power=800,
                    max_discharge_power=800,
                    initial_soc=0.8,
                    ramp_rate=400.0,
                    poll_interval=battery_poll_interval,
                    min_power_threshold=battery_min_power_threshold,
                    startup_delay=battery_startup_delay,
                )
            )

        self.powermeter = PowermeterSimulator(
            batteries=self.batteries,
            load_model=self.load_model,
            host="127.0.0.1",
            port=http_port,
        )

        # Balancer/saturation settings for this scenario. For the Python
        # backend they go to the CT002 constructor; for the ESPHome backend
        # they're pushed via `cfg` control commands at start() (the binary's
        # compile-time config is otherwise fixed).
        self._scenario_cfg: dict[str, float] = {
            "min_efficient_power": float(min_efficient_power),
            "efficiency_rotation_interval": float(efficiency_rotation_interval),
            "fair_distribution": 1.0,
        }
        for k, val in ct_kwargs.items():
            self._scenario_cfg[k] = float(
                1 if val is True else 0 if val is False else val
            )

        if self.backend == "python":
            self.ct002 = CT002(
                udp_port=ct_port,
                ct_mac=ct_mac,
                active_control=True,
                fair_distribution=True,
                min_efficient_power=min_efficient_power,
                efficiency_rotation_interval=efficiency_rotation_interval,
                clock=self.clock,
                reset_fn=None,
                consumer_ttl=100000,  # avoid eviction during long mock-time sims
                **ct_kwargs,
            )

            async def update_readings(_addr, _fields=None, _consumer_id=None):
                grid = self.powermeter.compute_grid()
                return [grid["phase_a"], grid["phase_b"], grid["phase_c"]]

            self.ct002.before_send = update_readings
        else:
            self.ct002 = None

    async def start(self):
        await self.powermeter.start()
        if self.backend == "python":
            await self.ct002.start()
        else:
            self._esphome.spawn()
            self._esphome.set_dedupe(0)  # sims poll fast in mock time; no dedup
            for key, val in self._scenario_cfg.items():
                self._esphome.set_cfg(key, val)
            self._esphome.set_clock(self.clock())

    async def stop(self):
        if self.backend == "python":
            await self.ct002.stop()
        else:
            self._esphome.stop()
        await self.powermeter.stop()

    # -- stepping ----------------------------------------------------------

    async def _step_battery(self, b: BatterySimulator) -> None:
        """One battery iteration. The Python emulator reads grid live via
        before_send; the ESPHome binary needs the grid injected, so we
        replicate BatterySimulator.step() and inject the grid AFTER the
        battery ramps but BEFORE it polls — matching Python's timing."""
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
        """Step all batteries *n* times.  Advances the clock by each
        battery's ``poll_interval`` (max across batteries) per step."""
        for _ in range(n):
            max_dt = max(b.poll_interval for b in self.batteries)
            for b in self.batteries:
                await self._step_battery(b)
            self.clock.advance(max_dt)

    async def step_until(
        self,
        condition,
        *,
        max_steps: int = 200,
    ) -> int:
        """Step until *condition()* is true.  Returns step count."""
        for i in range(max_steps):
            await self.step()
            if condition():
                return i + 1
        raise AssertionError(f"Condition not met after {max_steps} steps")

    # -- observation helpers -----------------------------------------------

    def battery_powers(self) -> list[float]:
        return [b.current_power for b in self.batteries]

    def grid_total(self) -> float:
        grid = self.powermeter.compute_grid()
        return grid["phase_a"] + grid["phase_b"] + grid["phase_c"]

    def active_battery_count(self, threshold: float = 15.0) -> int:
        return sum(1 for p in self.battery_powers() if abs(p) > threshold)

    def active_battery_indexes(self, threshold: float = 15.0) -> list[int]:
        return [i for i, p in enumerate(self.battery_powers()) if abs(p) > threshold]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEfficiencyE2E:
    """End-to-end tests for efficiency optimization with simulated batteries."""

    async def test_low_demand_concentrates_power(self):
        """At 200W with 2 batteries and threshold=150, only 1 battery should be active."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step(10)

            max_power = max(abs(p) for p in h.battery_powers())
            assert max_power > 150, (
                f"Active battery power {max_power}W is too low. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_high_demand_uses_all_batteries(self):
        """At 600W with 2 batteries and threshold=150, both should be active."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[600.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count(30.0) == 2)
        finally:
            await h.stop()

    async def test_demand_increase_activates_second_battery(self):
        """When demand rises from low to high, second battery activates."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[150.0, 0.0, 0.0],
            min_efficient_power=150,
            loads=[Load("BigLoad", 500.0, "A")],
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count(30.0) == 1)

            h.load_model.toggle_load(1)

            await h.step_until(lambda: h.active_battery_count(30.0) == 2)
        finally:
            await h.stop()

    async def test_disabled_feature_uses_all_batteries(self):
        """With min_efficient_power=0 (default), both batteries share load equally."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 2)
        finally:
            await h.stop()

    async def test_priority_rotation_switches_active_battery(self):
        """After rotation interval, the other battery joins via probe."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step(10)

            active_before = h.active_battery_indexes()
            assert len(active_before) == 1
            standby_idx = 1 - active_before[0]

            # Advance clock past rotation interval
            h.clock.advance(8)

            # Step until the standby battery is probed
            await h.step_until(
                lambda: abs(h.battery_powers()[standby_idx]) > 15,
                max_steps=100,
            )

            grid = abs(h.grid_total())
            # The Venus-class steering controller accelerates its correction
            # under a sustained error, so a probe joining transiently overshoots
            # the grid more than the old deadbeat plant before it settles. This
            # bounds that transient (the run settles back toward zero after).
            assert grid < 150, (
                f"Grid should stay stable during probe rotation. "
                f"Grid={grid:.0f}W powers={h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_probe_keeps_grid_near_zero_during_slow_rotation(self):
        """During a slow probe, the previous battery keeps covering demand."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            startup_delay=6.0,
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step(10)
            powers_before = h.battery_powers()
            active_before = 0 if abs(powers_before[0]) > abs(powers_before[1]) else 1
            standby = 1 - active_before

            # Trigger rotation
            h.clock.advance(8)

            # Sample only during the startup-delay window.
            # startup_delay=6.0 / dt=0.3 → 20 steps to clear.
            # Sample first 10 steps to stay well within the window.
            grid_errors: list[float] = []
            backup_powers: list[float] = []
            max_probe_power = 0.0
            for _ in range(10):
                await h.step()
                powers = h.battery_powers()
                grid_errors.append(abs(h.grid_total()))
                backup_powers.append(abs(powers[active_before]))
                max_probe_power = max(max_probe_power, abs(powers[standby]))

            # Backup battery should stay online covering demand
            min_backup = min(backup_powers)
            assert min_backup > 100, (
                f"Previous battery should remain online during probe. "
                f"Min backup={min_backup:.0f}W Powers: {h.battery_powers()}"
            )
            # Probe battery should still be in startup delay
            assert max_probe_power < 10, (
                f"Promoted battery should still be in startup delay. "
                f"Max probe power={max_probe_power:.0f}W"
            )
            max_grid = max(grid_errors)
            # Measured headroom (both backends, paced and unpaced): <10 W.
            # 30 matches the settle convention used elsewhere in this suite.
            assert max_grid < 30, (
                f"Grid should stay near zero during probe, max={max_grid:.0f}W. "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_probe_handles_mixed_poll_intervals(self):
        """Residual backup coverage tolerates probe lag from slower polling."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            poll_intervals=[0.9, 0.3],
            startup_delays=[6.0, 6.0],
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step(10)

            h.clock.advance(8)

            # Sample across the startup-delay window and past it. The
            # Venus-class controller ramps with acceleration (slower initial
            # response than a deadbeat plant) and the faithful input gate
            # debounces the candidate's first grid samples, so a probe starting
            # up under a slower poll cadence leaves a larger residual grid error
            # transiently — and on the C++ emulator the candidate can briefly be
            # driven to charge — before coverage catches up.
            grid_errors: list[float] = []
            for _ in range(12):
                await h.step()
                grid_errors.append(abs(h.grid_total()))

            # Coverage stays with the pool throughout the slow-poll probe: the
            # demand is served either by the previous battery (probe still
            # ramping / not yet committed, as on the Python backend here) or by
            # the candidate once it has cleanly taken over. Ramp pacing (#458)
            # lets the candidate ramp up without being whipsawed into a charge
            # transient, so on the C++ backend the probe now *commits* within
            # the window and the candidate covers the full load — previously it
            # was driven to charge and the probe was rejected, leaving the old
            # battery active. Either outcome keeps the demand covered, which is
            # what this test guards (with the bounded/settled grid asserts
            # below), so assert pool-level coverage rather than a specific unit.
            total_discharge = sum(p for p in h.battery_powers() if p > 0)
            assert total_discharge > 100, (
                f"Demand should remain covered by the pool. "
                f"Powers: {h.battery_powers()}"
            )
            # No runaway: the error stays bounded (a true coverage failure grows
            # without bound, like the stale-meter lockup) ...
            assert max(grid_errors) < 500, (
                f"Mixed poll intervals should not blow up grid error "
                f"(max={max(grid_errors):.0f}W). Powers: {h.battery_powers()}"
            )
            # ... and it converges back toward the deadband once coverage
            # catches up (a clean monotonic descent, e.g. [45, 30, 7] W). The
            # stronger default oscillation damping lets the slow-poll probe take
            # a cycle longer to settle, so the tail of that descent sits a little
            # higher than the old <30 W bound — still nowhere near the sustained
            # 100-250 W a genuinely failed/hunting handoff holds (cf. the bounded
            # bursts in test_probe_acceptance_avoids_large_export_spike).
            assert max(grid_errors[-3:]) < 60, (
                f"Mixed poll intervals should settle (last errors "
                f"{[round(e) for e in grid_errors[-3:]]}W). Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_probe_acceptance_avoids_large_export_spike(self):
        """Successful probe handoff should not temporarily double total output."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            startup_delay=2.0,
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step(10)
            standby = 1 - h.active_battery_indexes()[0]
            h.clock.advance(8)

            total_outputs: list[float] = []
            grid_errors: list[float] = []
            probe_accepted = False
            for _ in range(60):
                await h.step()
                powers = h.battery_powers()
                total_outputs.append(sum(max(p, 0.0) for p in powers))
                grid_errors.append(abs(h.grid_total()))
                if abs(powers[standby]) > 15:
                    probe_accepted = True

            assert probe_accepted, (
                f"Expected promoted battery to join. Powers: {h.battery_powers()}"
            )
            # The Venus-class steering controller accelerates under a sustained
            # error, and its >50 W spike filter debounces the outgoing battery's
            # first sight of the export jump, so a probe joining overshoots for a
            # couple of regulation cycles before settling. Bound the *sustained*
            # behavior (a buggy handoff doubles output / leaves a large grid
            # error for many cycles, not 2-3 samples) and require it to settle.
            # The tighter default ramp pacing winds the outgoing unit down a
            # little more gradually, so each handoff's bounded export burst spans
            # ~3 samples per probe cycle (two cycles in this window) before
            # settling — still a transient, not the many-cycle doubling a broken
            # handoff would show.
            doubled = sum(1 for t in total_outputs if t >= 400)
            assert doubled <= 5, (
                f"Probe acceptance kept output doubled for {doubled} samples; "
                f"totals={[round(t) for t in total_outputs]}"
            )
            large_grid = sum(1 for e in grid_errors if e >= 170)
            assert large_grid <= 7, (
                f"Probe acceptance kept a large grid error for {large_grid} samples; "
                f"errors={[round(e) for e in grid_errors]}"
            )
            assert max(total_outputs[-3:]) < 250 and max(grid_errors[-3:]) < 30, (
                f"Handoff did not settle: totals={[round(t) for t in total_outputs[-3:]]} "
                f"grid errors={[round(e) for e in grid_errors[-3:]]}"
            )
        finally:
            await h.stop()

    async def test_probe_respects_80w_inverter_floor(self):
        """Probe should use a meaningful command when batteries ignore tiny targets."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            probe_min_power=80,
            efficiency_rotation_interval=7,
            startup_delay=0.0,
            min_power_thresholds=[80.0, 80.0],
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count(80.0) == 1)
            await h.step(10)
            standby = 1 - h.active_battery_indexes(threshold=80.0)[0]
            h.clock.advance(8)

            probe_joined = False
            grid_errors: list[float] = []
            for _ in range(40):
                await h.step()
                powers = h.battery_powers()
                grid_errors.append(abs(h.grid_total()))
                if abs(powers[standby]) >= 70:
                    probe_joined = True
                    break

            assert probe_joined, (
                f"Probe should use enough command to clear an 80W inverter floor. "
                f"Powers: {h.battery_powers()}"
            )
            max_grid = max(grid_errors)
            assert max_grid < 120, (
                f"80W probe floor should not destabilize the grid; "
                f"max={max_grid:.0f}W. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_probe_rejection_keeps_backup_covering_demand(self):
        """Rejected probe should not create a noticeable demand gap."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            startup_delay=0.0,
            efficiency_fade_alpha=1.0,
            saturation_grace_seconds=5.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step(10)
            active_before = h.active_battery_indexes()[0]
            standby = 1 - active_before
            h.batteries[standby].startup_delay = 20.0
            h.clock.advance(8)

            grid_errors: list[float] = []
            large_gap_samples = 0
            for _ in range(40):
                await h.step()
                grid = abs(h.grid_total())
                grid_errors.append(grid)
                if grid > 100:
                    large_gap_samples += 1

            assert abs(h.battery_powers()[standby]) < 25, (
                f"Promoted battery should still be rejected. Powers: {h.battery_powers()}"
            )
            assert large_gap_samples == 0, (
                f"Rejected probe should not leave the grid under-covered. "
                f"max_grid={max(grid_errors):.0f}W powers={h.battery_powers()}"
            )
            max_grid = max(grid_errors)
            # Measured headroom (both backends, paced and unpaced): <1 W.
            # 30 matches the settle convention used elsewhere in this suite.
            assert max_grid < 30, (
                f"Rejected probe should not leave a large grid gap; max={max_grid:.0f}W. "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_grid_converges_near_zero(self):
        """With efficiency optimization, grid import/export should converge near zero."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step_until(lambda: abs(h.grid_total()) < 25)

            grid = abs(h.grid_total())
            assert grid < 25, (
                f"Grid should converge near zero, got {grid}W. "
                f"Battery powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_three_batteries_partial_activation(self):
        """With 3 batteries and 350W demand (threshold=150), 2 should be active."""
        h = _SimHarness(
            num_batteries=3,
            base_load=[350.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 2)
        finally:
            await h.stop()

    async def test_smooth_transition_no_overshoot(self):
        """During demand increase, no single battery should overshoot excessively."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            loads=[Load("BigLoad", 500.0, "A")],
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count(30.0) == 1)

            h.load_model.toggle_load(1)

            # Sample powers during the transition
            max_power_seen = 0.0
            for _ in range(30):
                await h.step()
                for p in h.battery_powers():
                    max_power_seen = max(max_power_seen, abs(p))

            # No single battery should *overshoot* the BigLoad transiently
            # (e.g. swing toward the full ~700 W demand and settle back). With
            # deadband concentration the most-active battery also absorbs the
            # small near-zero residual (≤ concentrate_deadband above its fair
            # share), so its settled share sits a bit over the 500 W load — that
            # is the designed distribution, not a transient excursion (peak ==
            # final). The threshold guards against a battery hogging the whole
            # demand, which is well above this.
            assert max_power_seen < 560, (
                f"Overshoot detected: max battery power {max_power_seen:.0f}W "
                f"during transition. Final powers: {h.battery_powers()}"
            )

            await h.step_until(lambda: h.active_battery_count(30.0) == 2)
        finally:
            await h.stop()

    async def test_saturated_battery_triggers_rotation(self):
        """When the active battery is saturated, it gets swapped out quickly."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            saturation_stall_timeout_seconds=4.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            powers = h.battery_powers()
            active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
            other_idx = 1 - active_idx
            h.batteries[active_idx].max_charge_power = 0
            h.batteries[active_idx].max_discharge_power = 0

            # Advance clock past stall timeout to trigger saturation detection
            h.clock.advance(5.0)

            await h.step_until(
                lambda: abs(h.battery_powers()[other_idx]) > 50,
                max_steps=100,
            )
        finally:
            await h.stop()

    async def test_initially_empty_battery_swaps_without_timed_rotation(self):
        """An empty prioritized battery should be swapped out before timed rotation."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_rotation_interval=9999,
            efficiency_saturation_threshold=0.4,
            saturation_stall_timeout_seconds=4.0,
        )
        h.batteries[0].soc = 0.0
        h.batteries[1].soc = 1.0
        await h.start()
        try:
            # Advance clock past stall timeout
            h.clock.advance(5.0)

            await h.step_until(
                lambda: abs(h.battery_powers()[1]) > 50,
                max_steps=150,
            )
            assert abs(h.battery_powers()[0]) < 20, (
                f"Empty battery should remain near zero. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    async def test_saturation_recovery_after_swap(self):
        """After forced swap, original battery recovers when constraint is lifted."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_rotation_interval=10,
            efficiency_saturation_threshold=0.4,
            saturation_decay_factor=0.8,
            saturation_stall_timeout_seconds=4.0,
        )
        await h.start()
        try:
            await h.step_until(lambda: h.active_battery_count() == 1)
            powers = h.battery_powers()
            active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
            other_idx = 1 - active_idx

            # Saturate the active battery
            h.batteries[active_idx].max_charge_power = 0
            h.batteries[active_idx].max_discharge_power = 0
            h.clock.advance(5.0)

            # Wait for swap
            await h.step_until(
                lambda: abs(h.battery_powers()[other_idx]) > 50,
                max_steps=100,
            )

            # Restore the original battery
            h.batteries[active_idx].max_charge_power = 800
            h.batteries[active_idx].max_discharge_power = 800
            # Advance clock past rotation interval to allow it back
            h.clock.advance(11)

            # Wait for restored battery to become active
            await h.step_until(
                lambda: abs(h.battery_powers()[active_idx]) > 25,
                max_steps=150,
            )

            # Wait for grid to settle
            await h.step_until(
                lambda: abs(h.grid_total()) < 30,
                max_steps=100,
            )
        finally:
            await h.stop()

    async def test_load_sign_reversal_does_not_cause_false_saturation(self):
        """When load flips sign (discharge->charge), active battery must not
        be falsely detected as saturated while it ramps to the new direction.

        Reproduces the real-world ping-pong observed when solar production
        changes cause the grid target to flip sign.
        """
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=9999,  # no timed rotation
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            saturation_stall_timeout_seconds=60.0,
        )
        await h.start()
        try:
            # Let system settle with one battery discharging
            await h.step_until(lambda: h.active_battery_count() == 1)
            await h.step(10)

            active_idx = h.active_battery_indexes()[0]
            other_idx = 1 - active_idx
            assert h.battery_powers()[active_idx] > 100

            # Flip load sign: go from 200W discharge to -200W (solar excess)
            h.load_model.base_load[0] = -200.0

            # Step through the sign reversal -- the active battery must NOT
            # be swapped out due to false saturation.
            # 60 steps at 0.3s/step = 18s, plenty for old bug to trigger.
            rotations = 0
            for _ in range(60):
                await h.step()
                powers_after = h.battery_powers()
                # Detect rotation: other battery becomes sole active
                if (
                    abs(powers_after[other_idx]) > 15
                    and abs(powers_after[active_idx]) < 15
                ):
                    rotations += 1

            # The battery should have reversed direction (now charging)
            assert h.battery_powers()[active_idx] < -50, (
                f"Active battery should be charging after sign reversal. "
                f"Powers: {h.battery_powers()}"
            )
            # No false-saturation rotation should have occurred
            assert rotations == 0, (
                f"Battery was falsely rotated {rotations} time(s) during "
                f"sign reversal. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()
