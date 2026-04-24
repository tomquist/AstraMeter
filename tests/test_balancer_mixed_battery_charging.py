"""Tests for issue #338: mixed DC + AC batteries under solar surplus.

Scenario from the report (users c00LhaNd86 and Matze6989): a Marstek
Venus (can charge **and** discharge via AC) runs next to a Marstek
B2500 (a DC battery — discharges via AC, but cannot charge via AC at
all).  Both report to the same AstraMeter CT002 emulator fed by a
Shelly Pro 3EM.  Under solar surplus the Venus stayed in standby
indefinitely; pointing the Marstek app directly at the Shelly made the
Venus charge immediately.  Discharging, where the B2500 can
participate, worked fine.

Root cause: ``LoadBalancer._compute_auto_target`` split the grid
reading evenly across every reporting storage unit (fair-share plus a
``_balance_correction`` that pushed each consumer toward the average
of reported powers).  Neither mechanism knew the B2500 was
charge-blind, so under surplus each battery was told to absorb half
of the real feed-in — often below the Venus's inverter start-up
threshold (~300-500 W), so the Venus never woke up.

Fix: real Marstek batteries advertise their model in the CT002 request
(``Consumer.device_type``).  The only AC-coupled family is the Venus
(prefixes ``HMG`` and ``VNS``).  ``_compute_auto_target`` now excludes
every other reporter from charge distribution under ``grid_total < 0``
and steers them to 0 W; the Venus receives the full surplus.
Positive grid (discharge) behaviour is unchanged.

The tests here cover:

 * the legacy deadlock when ``device_type`` is missing (proves the
   default-to-DC safety policy is in effect),
 * the fix path with recognised Venus / B2500 prefixes,
 * discharge unaffected,
 * the degenerate all-DC-under-surplus case.
"""

from __future__ import annotations

import logging
import time

import pytest

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    LoadBalancer,
)


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class DCOnlyBattery:
    """B2500-style battery: discharges via AC, cannot charge via AC.

    Any negative (charge) command is clamped to 0.  Positive commands
    ramp up like a normal inverter within ``max_discharge``.
    """

    def __init__(
        self,
        mac: str,
        *,
        max_discharge: int = 800,
        ramp: float = 300.0,
        device_type: str = "HMJ-1",
    ) -> None:
        self.mac = mac
        self.max_discharge = max_discharge
        self.ramp = ramp
        self.device_type = device_type
        self.power = 0.0

    def step(self, target_delta: float, reported_power: float) -> None:
        desired = reported_power + target_delta
        desired = max(0, min(self.max_discharge, desired))
        delta = desired - self.power
        if delta > self.ramp:
            delta = self.ramp
        elif delta < -self.ramp:
            delta = -self.ramp
        self.power += delta


class ACBatteryWithStartupThreshold:
    """Venus-style battery with a start-up threshold below which it stays idle.

    Real Marstek Venus inverters will not transition out of standby
    until the commanded change exceeds a few hundred watts — 400 W is
    a conservative mid-point of what users report.  Once activated the
    battery ramps normally; if the commanded magnitude drops back below
    ``startup_min`` for long enough the battery returns to standby.
    The commanded magnitude is derived from the CT-style
    ``current + delta`` protocol, matching
    :class:`astrameter.simulator.battery.BatterySimulator`.
    """

    def __init__(
        self,
        mac: str,
        *,
        max_charge: int = 2500,
        max_discharge: int = 800,
        ramp: float = 300.0,
        startup_min: float = 400.0,
        device_type: str = "HMG-50",
    ) -> None:
        self.mac = mac
        self.max_charge = max_charge
        self.max_discharge = max_discharge
        self.ramp = ramp
        self.startup_min = startup_min
        self.device_type = device_type
        self.power = 0.0
        self._active = False

    def step(self, target_delta: float, reported_power: float) -> None:
        desired = reported_power + target_delta
        desired = max(-self.max_charge, min(self.max_discharge, desired))
        if not self._active:
            if abs(desired) < self.startup_min:
                return
            self._active = True
        delta = desired - self.power
        if delta > self.ramp:
            delta = self.ramp
        elif delta < -self.ramp:
            delta = -self.ramp
        self.power += delta
        if self._active and abs(self.power) < 20 and abs(desired) < self.startup_min:
            self._active = False
            self.power = 0.0


class B2500PassThrough:
    """B2500 at 100 % SoC passing its DC solar input straight through as AC.

    When the B2500 is full it can no longer absorb its own DC input, so
    the excess flows out as AC — the unit reports positive power
    (apparent discharge) regardless of any CT command.  The balancer
    sees this as "B2500 is producing" while the real grid is still
    importing the surplus it can't absorb.  See issue #338 (follow-up
    from the repo owner): this scenario reproduces the deadlock
    *without* requiring any Venus startup-threshold assumption, because
    the balance-correction + sign-clamp interaction alone pins the
    Venus below the level needed to cancel the B2500's feed.
    """

    def __init__(
        self,
        mac: str,
        passthrough_w: int,
        *,
        device_type: str = "HMJ-1",
    ) -> None:
        self.mac = mac
        self.passthrough_w = passthrough_w
        self.device_type = device_type
        self.power = float(passthrough_w)

    def step(self, target_delta: float, reported_power: float) -> None:
        # Output is dictated by DC solar input, not the CT command.
        self.power = float(self.passthrough_w)


def _make_balancer(clock: _FakeClock) -> LoadBalancer:
    """Balancer with CT002 defaults (matching the out-of-the-box config)."""
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            balance_gain=0.2,
            balance_deadband=15,
            error_boost_threshold=150,
            error_boost_max=0.5,
            error_reduce_threshold=20,
            max_correction_per_step=80,
            # ``min_efficient_power=0`` disables efficiency deprioritization,
            # matching the out-of-the-box config the reporters are running.
            min_efficient_power=0,
            probe_min_power=80,
            efficiency_rotation_interval=900,
            efficiency_fade_alpha=0.15,
            efficiency_saturation_threshold=0.4,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=True,
        clock=clock,
    )


def _run_scenario(batteries, surplus_watts: float, ticks: int):
    """Drive the balancer for *ticks* seconds at 1 Hz under a fixed surplus.

    Each battery contributes its ``device_type`` to the report dict, so
    the fix's AC-allow-list can see it.  Returns
    ``(grid_trace, per_mac_power_trace)``.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock)
    grid_trace: list[float] = []
    power_trace: dict[str, list[float]] = {b.mac: [] for b in batteries}

    for tick in range(ticks):
        reports = {
            b.mac: {
                "phase": "A",
                "power": round(b.power),
                "device_type": b.device_type,
            }
            for b in batteries
        }
        # Grid = (load - solar) - battery_sum.  Here we model a clean
        # surplus-only case: zero house load, ``surplus_watts`` of solar,
        # so ``grid = -surplus_watts - sum(battery.power)``.
        grid_total = -surplus_watts - sum(b.power for b in batteries)
        grid_trace.append(grid_total)

        deltas: dict[str, float] = {}
        for b in batteries:
            phase_targets = lb.compute_target(
                consumer_id=b.mac,
                consumer_mode=ConsumerMode("auto"),
                all_reports=reports,
                grid_total=grid_total,
                inactive=frozenset(),
                manual=frozenset(),
                sample_id=(tick,),
            )
            deltas[b.mac] = phase_targets[0]

        for b in batteries:
            b.step(deltas[b.mac], reports[b.mac]["power"])
            power_trace[b.mac].append(b.power)

        clock.advance(1.0)

    return grid_trace, power_trace


# ---------------------------------------------------------------------------
# Fix path — recognised Marstek device_types
# ---------------------------------------------------------------------------


def test_b2500_excluded_from_charge_share_lets_venus_wake() -> None:
    """The canonical fix scenario: HMJ-1 B2500 + HMG-50 Venus under 600 W surplus.

    With the device-type prefix check in place the B2500 is excluded
    from charge distribution, so the Venus receives the full -600 W
    command on tick 0, clears its start-up threshold, and absorbs the
    surplus to near-zero grid.
    """
    b2500 = DCOnlyBattery("b2500_01", device_type="HMJ-1")
    venus = ACBatteryWithStartupThreshold("venus_01", device_type="HMG-50")

    grid, power = _run_scenario([b2500, venus], surplus_watts=600.0, ticks=200)

    venus_tail = power["venus_01"][-30:]
    b2500_tail = power["b2500_01"][-30:]
    grid_tail = grid[-30:]

    assert max(abs(p) for p in b2500_tail) < 1.0, (
        f"B2500 should remain at 0 W throughout (DC-only). Tail: {b2500_tail}"
    )
    assert min(venus_tail) < -500, (
        f"Venus should absorb most of the 600 W surplus. Tail: {venus_tail}"
    )
    avg_grid = sum(grid_tail) / len(grid_tail)
    assert abs(avg_grid) < 50, f"Grid should drain near 0 W, got {avg_grid:.0f} W"


@pytest.mark.parametrize(
    "venus_device_type",
    [
        "HMG-50",
        "hmg-50",
        "HMG",
        "VNSE3-X",
        "vnse3-x",
        "VNSA-1",
        "VNSD-2",
        "VNS",
    ],
)
def test_all_known_venus_prefixes_are_charge_capable(venus_device_type: str) -> None:
    """Every recognised Venus-family prefix must absorb the surplus.

    ``HMG`` covers the older HMG-* naming; ``VNS`` covers VNSE3, VNSA,
    VNSD, and any bare-prefix variant.  Lowercase and suffixed forms
    must all match — the lookup is case-insensitive and prefix-based.
    """
    b2500 = DCOnlyBattery("b2500", device_type="HMJ-1")
    venus = ACBatteryWithStartupThreshold("venus", device_type=venus_device_type)

    _, power = _run_scenario([b2500, venus], surplus_watts=600.0, ticks=200)

    assert min(power["venus"][-30:]) < -500, (
        f"Venus with device_type={venus_device_type!r} should charge; "
        f"tail was {power['venus'][-30:]}"
    )
    assert max(abs(p) for p in power["b2500"][-30:]) < 1.0


@pytest.mark.parametrize(
    "dc_device_type",
    [
        "HMA-X",
        "HMB-X",
        "HMJ-X",
        "HMK-X",
        "hma-x",
        "JUPITER-1",
        "UNKNOWN",
        "",
    ],
)
def test_non_venus_prefixes_are_treated_as_dc(dc_device_type: str) -> None:
    """B2500 family, Jupiter, and anything unrecognised default to DC.

    Paired with a recognised Venus, these must be held at 0 W under
    surplus while the Venus absorbs everything.  Empty / unknown
    device types share the same DC default (fail-closed policy).
    """
    dc = DCOnlyBattery("dc", device_type=dc_device_type)
    venus = ACBatteryWithStartupThreshold("venus", device_type="HMG-50")

    _, power = _run_scenario([dc, venus], surplus_watts=600.0, ticks=200)

    assert max(abs(p) for p in power["dc"][-30:]) < 1.0, (
        f"device_type={dc_device_type!r} should be excluded from charge; "
        f"tail was {power['dc'][-30:]}"
    )
    assert min(power["venus"][-30:]) < -500, (
        f"Venus should absorb the surplus while the DC sibling is held at 0. "
        f"Venus tail: {power['venus'][-30:]}"
    )


# ---------------------------------------------------------------------------
# B2500 pass-through at 100 % SoC
# ---------------------------------------------------------------------------


def test_b2500_passthrough_at_full_soc_does_not_pin_venus() -> None:
    """B2500 full + passing 500 W DC through as AC: Venus must absorb it.

    Without the fix the pre-fix balancer pins the Venus at ~-340 W: the
    balance correction treats the B2500's +500 W "output" as a peer
    behaviour the Venus should match toward, and the sign clamp then
    blocks Venus from being pushed negative enough to cancel the feed.
    The result is a sustained ~160 W export, independent of any
    inverter startup threshold.

    With the fix, the B2500 is recognised by prefix (``HMJ-1``) as
    DC-only and excluded from charge distribution; the Venus receives
    the full -500 W target, charges to -500 W, and pins the grid at 0.
    """
    b2500 = B2500PassThrough("b2500_full", passthrough_w=500)
    venus = ACBatteryWithStartupThreshold("venus", device_type="HMG-50")

    grid, power = _run_scenario([b2500, venus], surplus_watts=0.0, ticks=60)

    venus_tail = power["venus"][-20:]
    b2500_tail = power["b2500_full"][-20:]
    grid_tail = grid[-20:]

    # B2500 keeps pushing its 500 W pass-through regardless of commands.
    assert all(abs(p - 500.0) < 1.0 for p in b2500_tail), (
        f"B2500 pass-through output should stay at 500 W, tail was {b2500_tail}"
    )
    # Venus absorbs the full pass-through; grid at ~0.
    assert min(venus_tail) < -490, (
        f"Venus should converge near -500 W to cancel the pass-through, "
        f"tail was {venus_tail}"
    )
    avg_grid = sum(grid_tail) / len(grid_tail)
    assert abs(avg_grid) < 30, (
        f"Grid should converge near 0 W (Venus exactly cancels B2500), "
        f"got {avg_grid:.0f} W"
    )


# ---------------------------------------------------------------------------
# Discharge unaffected
# ---------------------------------------------------------------------------


def test_dc_discharge_still_shared_with_ac_sibling() -> None:
    """The gate is ``grid_total < 0`` only — imports still share across both.

    A B2500 + Venus pair facing 1 kW of house consumption should both
    discharge (the B2500 can do that fine) and together drain the grid
    to ~0.  Protects the user's observation that discharging was
    working with the original balancer.
    """
    b2500 = DCOnlyBattery("b2500", device_type="HMJ-1")
    venus = ACBatteryWithStartupThreshold("venus", device_type="HMG-50")

    grid, power = _run_scenario([b2500, venus], surplus_watts=-1000.0, ticks=200)

    tail_grid = grid[-30:]
    tail_b2500 = power["b2500"][-30:]
    tail_venus = power["venus"][-30:]
    avg_grid = sum(tail_grid) / len(tail_grid)
    assert abs(avg_grid) < 50, (
        f"Discharge across both batteries should drain the grid; got {avg_grid:.0f} W"
    )
    assert sum(tail_b2500) / len(tail_b2500) > 400, "B2500 should be discharging"
    assert sum(tail_venus) / len(tail_venus) > 400, "Venus should be discharging"


# ---------------------------------------------------------------------------
# Degenerate all-DC-under-surplus case
# ---------------------------------------------------------------------------


def test_all_dc_under_surplus_holds_zero_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two B2500s under surplus: nothing can absorb; log once and hold at 0.

    No recognised AC-chargeable battery is reporting, so the balancer
    cannot do anything useful with the surplus.  It should:
     * hold every consumer at 0 W (no stray charge commands),
     * surface an info-level notice listing the device_types it saw,
       so the user can diagnose the mix.
    The message is latched — only one line per transition into the
    state, not one per tick.
    """
    a = DCOnlyBattery("dc_a", device_type="HMJ-1")
    b = DCOnlyBattery("dc_b", device_type="HMA-2")

    with caplog.at_level(logging.INFO, logger="astrameter"):
        _, power = _run_scenario([a, b], surplus_watts=600.0, ticks=200)

    assert max(abs(p) for p in power["dc_a"]) < 1.0
    assert max(abs(p) for p in power["dc_b"]) < 1.0

    dc_messages = [
        rec.getMessage()
        for rec in caplog.records
        if "no AC-chargeable battery" in rec.getMessage()
    ]
    assert len(dc_messages) == 1, (
        f"Expected exactly one latched warning, got {len(dc_messages)}: {dc_messages}"
    )
    assert "600" in dc_messages[0], "Message should include the surplus magnitude"


# ---------------------------------------------------------------------------
# Legacy / regression protection for the original deadlock
# ---------------------------------------------------------------------------


def test_unknown_device_type_deadlock_persists() -> None:
    """Protects the default-to-DC safety policy.

    The original bug reproduction used empty ``device_type`` strings:
    both batteries look like unknown DC batteries to the balancer, so
    nobody is asked to charge and the grid keeps feeding back the
    surplus.  This is the expected fail-closed behaviour for consumers
    we can't identify.  This also exercises the ``all_dc_under_surplus``
    branch; the one-shot info-log emitted there is asserted separately
    by ``test_all_dc_under_surplus_holds_zero_and_logs``.
    """
    b2500 = DCOnlyBattery("b2500_legacy", device_type="")
    venus = ACBatteryWithStartupThreshold("venus_legacy", device_type="")

    grid, power = _run_scenario([b2500, venus], surplus_watts=600.0, ticks=200)

    assert max(abs(p) for p in power["venus_legacy"][-30:]) < 1.0
    assert max(abs(p) for p in power["b2500_legacy"][-30:]) < 1.0
    avg_grid = sum(grid[-30:]) / 30
    assert avg_grid < -550, (
        f"With unknown device_types nobody charges; expected grid ~= -600 W, got "
        f"{avg_grid:.0f} W"
    )
