"""The standby reserve in efficiency mode.

With ``min_efficient_power`` above 0, a battery outside the active set (faded
fully out, which includes a 0 % efficiency window weight sunk to the tail)
stays at 0 W until every battery ahead of it is out of headroom in the
demand's direction — at its learned ceiling, saturated (full / empty), or
DC-only under a surplus.  Then the standbys take only the overflow, one at a
time, and give it back first when the demand falls, down to 0 W and never past
it.  With efficiency mode off nothing changes.

Most of these close the loop over a minimal plant (each battery adds the
reading it is sent to its output, within its limits), so they hold for the
behaviour rather than for one internal state.  The C++ mirror is held to the
same streams by ``tests/components/ct002/test_balancer_parity.py``.
"""

from __future__ import annotations

import logging
import time

import pytest

from astrameter.ct002.balancer import (
    SPILL_RELEASE_SECONDS,
    BalancerConfig,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
)

AUTO = ConsumerMode("auto")


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _Battery:
    """Adds each reading to its output, within its limits.

    ``full`` stops it charging, ``empty`` stops it discharging, and a DC-only
    device type (B2500) never charges at all.
    """

    def __init__(
        self,
        *,
        discharge: float = 800.0,
        charge: float = 2500.0,
        device_type: str = "VNSE3",
        window: float = 1.0,
        power: float = 0.0,
    ) -> None:
        self.discharge = discharge
        self.charge = 0.0 if device_type.startswith("HMJ") else charge
        self.device_type = device_type
        self.window = window
        self.power = power
        self.full = False
        self.empty = False

    def step(self, reading: float) -> None:
        hi = 0.0 if self.empty else self.discharge
        lo = 0.0 if self.full else -self.charge
        self.power = float(round(max(lo, min(hi, self.power + reading))))


class _Loop:
    def __init__(
        self,
        batteries: dict[str, _Battery],
        *,
        min_efficient_power: float = 2500.0,
        **cfg: float,
    ) -> None:
        self.clock = _FakeClock()
        self.lb = LoadBalancer(
            config=BalancerConfig(
                min_efficient_power=min_efficient_power,
                efficiency_rotation_interval=86400.0,
                **cfg,
            ),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            clock=self.clock,
        )
        self.bats = batteries
        self.tick_no = 0
        self.log: list[tuple[float, dict[str, float]]] = []
        self.held_grid: float | None = None

    def reports(self) -> dict[str, ConsumerReport]:
        return {
            cid: ConsumerReport(
                phase="A",
                power=round(b.power),
                device_type=b.device_type,
                efficiency_window_weight=b.window,
            )
            for cid, b in self.bats.items()
        }

    def tick(self, load: float, *, meter_every: int = 1) -> None:
        grid = load - sum(b.power for b in self.bats.values())
        # A slow meter repeats its reading, and its sample id with it.
        if self.held_grid is None or self.tick_no % meter_every == 0:
            self.held_grid = round(grid)
            self.sample = (self.tick_no,)
        reports = self.reports()
        readings = {
            cid: sum(
                self.lb.compute_target(
                    cid,
                    AUTO,
                    reports,
                    self.held_grid,
                    frozenset(),
                    frozenset(),
                    self.sample,
                )
            )
            for cid in self.bats
        }
        for cid, b in self.bats.items():
            b.step(readings[cid])
        self.clock.advance(1.0)
        self.tick_no += 1
        self.log.append(
            (
                load - sum(b.power for b in self.bats.values()),
                {cid: b.power for cid, b in self.bats.items()},
            )
        )

    def run(self, load: float, n: int, **kw: int) -> None:
        for _ in range(n):
            self.tick(load, **kw)

    def mean(self, cid: str, last: int = 30) -> float:
        return sum(p[cid] for _, p in self.log[-last:]) / last

    def mean_grid(self, last: int = 30) -> float:
        return sum(g for g, _ in self.log[-last:]) / last


def _pair(window_b: float = 1.0) -> dict[str, _Battery]:
    return {"a": _Battery(), "b": _Battery(window=window_b)}


def test_standby_takes_only_the_overflow_past_the_heads_ceiling() -> None:
    loop = _Loop(_pair())
    loop.run(600, 120)
    # Within the head's reach the standby stays at exactly 0 W.
    assert loop.mean("a") > 580
    assert all(p["b"] == 0.0 for _, p in loop.log[-60:])

    loop.run(1200, 240)
    # Past its 800 W the head holds there and the standby takes the rest.
    assert abs(loop.mean("a") - 800) < 30
    assert abs(loop.mean("b") - 400) < 40
    assert abs(loop.mean_grid()) < 30
    assert loop.lb._spilling == ["b"]


def test_full_head_hands_the_surplus_to_a_zero_window_backup() -> None:
    loop = _Loop(_pair(window_b=0.0))
    loop.run(-1000, 120)
    assert loop.mean("a") < -950
    assert all(p["b"] == 0.0 for _, p in loop.log[-60:])

    loop.bats["a"].full = True
    loop.run(-1000, 240)
    assert abs(loop.mean("b") + 1000) < 40
    assert abs(loop.mean_grid()) < 30
    # The backup never takes the lead: it covers from behind.
    assert loop.lb._priority[-1] == "b"


def test_empty_head_hands_the_load_to_a_zero_window_backup() -> None:
    loop = _Loop(_pair(window_b=0.0))
    loop.run(500, 120)
    assert all(p["b"] == 0.0 for _, p in loop.log[-60:])

    loop.bats["a"].empty = True
    loop.run(500, 240)
    assert abs(loop.mean("b") - 500) < 30
    assert abs(loop.mean_grid()) < 30
    assert loop.lb._priority[-1] == "b"


def test_overflow_is_given_back_last_in_first_out_without_cross_flow() -> None:
    loop = _Loop(_pair())
    loop.run(1200, 240)
    assert loop.mean("b") > 350
    start = len(loop.log)
    loop.run(700, 240)
    after = loop.log[start:]
    # The standby gives back first and settles at exactly 0 W ...
    assert all(p["b"] == 0.0 for _, p in after[-60:])
    assert abs(loop.mean("a") - 700) < 30
    # ... the head keeps carrying until the standby has nothing left ...
    first_zero = next(i for i, (_, p) in enumerate(after) if p["b"] <= 0.0)
    assert min(p["a"] for _, p in after[:first_zero]) > 700
    # ... the standby never crosses into charging, and the two never trade
    # power with each other.
    assert min(p["b"] for _, p in after) >= 0.0
    assert not any(
        (p["a"] > 15 and p["b"] < -15) or (p["a"] < -15 and p["b"] > 15)
        for _, p in loop.log
    )


def test_dc_only_standby_is_passed_over_under_a_surplus() -> None:
    loop = _Loop(
        {
            "a": _Battery(),
            "b": _Battery(device_type="HMJ-2"),
            "c": _Battery(),
        }
    )
    loop.run(-800, 120)
    assert loop.lb._priority[0] == "a"
    loop.bats["a"].full = True
    loop.run(-800, 300)
    # The next AC-chargeable standby takes the surplus; the B2500 cannot.
    assert abs(loop.mean("c") + 800) < 40
    assert abs(loop.mean("b")) < 1
    assert abs(loop.mean_grid()) < 30


def test_repeating_meter_reading_still_hands_an_emptied_head_over() -> None:
    """A meter repeating its value (and sample id) must not freeze the decision."""
    loop = _Loop(_pair(window_b=0.0))
    loop.run(400, 120, meter_every=10)
    assert loop.mean("a") > 350
    loop.bats["a"].empty = True
    loop.run(400, 300, meter_every=10)
    assert abs(loop.mean("b", last=60) - 400) < 40
    assert loop.mean_grid(last=60) < 40


def _spilling_trio() -> _Loop:
    """Head "a" pinned at 800 W, "b" spilling the rest of 1200 W, "c" idle."""
    loop = _Loop({"a": _Battery(), "b": _Battery(), "c": _Battery()})
    loop.run(1200, 240)
    assert loop.lb._priority[0] == "a"
    assert loop.lb._spilling == ["b"]
    assert loop.mean("b") > 350
    return loop


def test_a_spilling_standby_holds_through_a_probe() -> None:
    """A probe elsewhere in the pool must not steer the overflow to 0 W."""
    loop = _spilling_trio()
    lb = loop.lb
    now = loop.clock()
    lb._begin_probe("c", ("c",), ("a",), ("a",), now)
    reports = loop.reports()
    carried = reports["b"].power
    # Demand left over: the spiller holds what it carries.
    held = sum(
        lb.compute_target("b", AUTO, reports, 20.0, frozenset(), frozenset(), (1,))
    )
    assert held == 0.0
    assert "b" in lb._spilling
    # The pool asks for less: the spiller gives back first, never past 0 W
    # (the wire reading is ramp-paced, so read the intent behind it).
    lb.compute_target("b", AUTO, reports, -150.0, frozenset(), frozenset(), (2,))
    assert lb.get_last_intent("b") == carried - 150.0
    # The supports split the demand without the spiller's output.
    assert lb._spill_delivery(reports, 20.0) == carried


def test_a_spilling_standby_absorbs_the_error_during_a_fade() -> None:
    """Fading batteries get demand-split targets; the spiller integrates."""
    loop = _spilling_trio()
    lb = loop.lb
    lb._get_consumer("a").fade_weight = 0.5
    reports = loop.reports()
    carried = reports["b"].power
    out = sum(
        lb.compute_target("b", AUTO, reports, 60.0, frozenset(), frozenset(), (1,))
    )
    assert out > 0.0
    assert "b" in lb._spilling
    # A large drop takes it down to 0 W and no further.
    lb._get_consumer("a").fade_weight = 0.5
    lb.compute_target("b", AUTO, reports, -5000.0, frozenset(), frozenset(), (2,))
    assert lb.get_last_intent("b") == 0.0
    assert carried > 0


def test_head_hovering_at_its_ceiling_does_not_toggle_the_standby() -> None:
    loop = _Loop(_pair())
    loop.run(1200, 240)
    lb = loop.lb
    assert lb._spilling == ["b"]
    reports = loop.reports()
    # The head slips 40 W off its ceiling: inside the staying margin (2 x 25 W)
    # but outside the joining one.  The standby stays.
    dipped = dict(reports)
    dipped["a"] = ConsumerReport(phase="A", power=760, device_type="VNSE3")
    lb.compute_target("b", AUTO, dipped, 0.0, frozenset(), frozenset(), (1,))
    assert lb._spilling == ["b"]
    # Further off, the standby still carrying power is held for a while ...
    dipped["a"] = ConsumerReport(phase="A", power=600, device_type="VNSE3")
    lb.compute_target("b", AUTO, dipped, 0.0, frozenset(), frozenset(), (2,))
    assert lb._spilling == ["b"]
    loop.clock.advance(SPILL_RELEASE_SECONDS / 2)
    lb.compute_target("b", AUTO, dipped, 0.0, frozenset(), frozenset(), (3,))
    assert lb._spilling == ["b"]
    # ... and released once the head has shown headroom for long enough.
    loop.clock.advance(SPILL_RELEASE_SECONDS)
    lb.compute_target("b", AUTO, dipped, 0.0, frozenset(), frozenset(), (4,))
    assert lb._spilling == []


def test_a_small_error_against_the_overflow_is_not_unloaded_from_the_standby() -> None:
    loop = _Loop(_pair())
    loop.run(1200, 240)
    lb = loop.lb
    reports = loop.reports()
    # Meter noise against the spill direction: the regular share handles it,
    # so the standby does not take it all.
    carried = reports["b"].power
    lb.compute_target("b", AUTO, reports, -20.0, frozenset(), frozenset(), (1,))
    assert lb.get_last_intent("b") > carried - 20.0
    # A real drop is given back by the standby first, all of it: the head's
    # share of it is nothing.
    lb.compute_target("b", AUTO, reports, -120.0, frozenset(), frozenset(), (2,))
    assert lb.get_last_intent("b") < carried - 30.0
    lb.compute_target("a", AUTO, reports, -120.0, frozenset(), frozenset(), (2,))
    assert lb.get_last_intent("a") == reports["a"].power


def test_efficiency_mode_off_never_spills() -> None:
    """Equal weights, fair distribution: the reserve has no part in it."""
    loop = _Loop(_pair(), min_efficient_power=0.0)
    for load in (600, 1200, 1700, -900, 300):
        loop.run(load, 120)
        assert loop.lb._spilling == []
        assert loop.lb._spill_scale == {}
        assert loop.lb._spill_caps == {}
    # Both share the load, as before.
    loop.run(1200, 240)
    assert loop.mean("a") > 450
    assert loop.mean("b") > 450


def test_force_rotation_and_removal_clear_the_reserve() -> None:
    loop = _spilling_trio()
    loop.lb.remove_consumer("b")
    assert loop.lb._spilling == []
    assert loop.lb._spill_scale == {}

    loop = _spilling_trio()
    loop.lb.detach_from_auto_pool("b")
    assert loop.lb._spilling == []

    loop = _spilling_trio()
    loop.lb.force_rotation(set(loop.bats))
    assert loop.lb._spilling == []
    assert loop.lb._spill_dir == 0


def test_steer_log_names_a_spilling_standby(caplog: pytest.LogCaptureFixture) -> None:
    """A deprioritized battery delivering the overflow is not logged as idle."""
    loop = _Loop(_pair())
    with caplog.at_level(logging.DEBUG, logger="astrameter"):
        loop.run(1200, 240)
    assert loop.lb._spilling == ["b"]
    lines = [m for m in caplog.messages if m.startswith("CT002 steer b:")]
    assert "rotation=overflow" in lines[-1], lines[-1:]
