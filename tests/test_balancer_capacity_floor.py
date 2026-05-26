"""Unit tests for the efficiency-mode capacity floor (issue #388).

These drive :class:`LoadBalancer` internals directly with a fake clock so the
cap-learning and slot-count logic can be checked precisely, independent of the
full simulator loop covered by ``tests/test_efficiency_e2e.py``.
"""

from __future__ import annotations

import time

from astrameter.ct002.balancer import (
    DEFAULT_OUTPUT_CAP,
    BalancerConfig,
    BalancerConsumerState,
    LoadBalancer,
)


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _make_balancer(
    clock: _FakeClock,
    *,
    min_efficient_power: float = 600,
    max_efficient_power: float = 0,
) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            min_efficient_power=min_efficient_power,
            max_efficient_power=max_efficient_power,
            efficiency_rotation_interval=900,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=True,
        clock=clock,
    )


def _reports(n: int, power: float = 0.0) -> dict:
    return {
        f"aabb0000000{i + 1}": {"phase": "A", "power": round(power)} for i in range(n)
    }


def _active_slots(lb: LoadBalancer) -> int:
    return len(lb._priority) - len(lb._deprioritized)


# --- U1: plateau learning -------------------------------------------------


def test_cap_learned_only_after_confirmed_plateau() -> None:
    lb = _make_balancer(_FakeClock())
    state = BalancerConsumerState()

    # Ramp-up: output rises steeply toward the 1105W setpoint — not a clip.
    lb._update_output_cap(state, last_target=1105, actual=400)
    assert state.clip_samples == 0
    assert state.output_cap == 0.0

    lb._update_output_cap(state, last_target=705, actual=800)
    assert state.clip_samples == 0
    assert state.output_cap == 0.0

    # Plateau: still commanded ~1105 (commanded = last_reported + last_target)
    # but output is flat at 800 — one clip sample, not yet confirmed.
    lb._update_output_cap(state, last_target=305, actual=800)
    assert state.clip_samples == 1
    assert state.output_cap == 0.0

    # Second consecutive clip sample confirms the ceiling.
    lb._update_output_cap(state, last_target=305, actual=800)
    assert state.output_cap == 800.0


def test_cap_not_learned_while_still_rising() -> None:
    lb = _make_balancer(_FakeClock())
    state = BalancerConsumerState()
    # Each sample shorts the setpoint but keeps rising fast → never a clip.
    for actual in (200, 450, 700):
        lb._update_output_cap(state, last_target=1105, actual=actual)
    assert state.clip_samples == 0
    assert state.output_cap == 0.0


# --- U2: default seed ------------------------------------------------------


def test_default_seed_engages_two_at_1105() -> None:
    lb = _make_balancer(_FakeClock())
    lb._compute_efficiency_deprioritized(_reports(3), sample_id=(0,), grid_total=1105.0)
    # Seed cap 800 → ceil(1105/800) == 2 active even before any learning.
    assert _active_slots(lb) == 2
    assert DEFAULT_OUTPUT_CAP == 800.0


# --- U3: explicit override -------------------------------------------------


def test_override_caps_each_battery_and_skips_learning() -> None:
    lb = _make_balancer(_FakeClock(), max_efficient_power=500)
    lb._compute_efficiency_deprioritized(_reports(3), sample_id=(0,), grid_total=1105.0)
    # ceil(1105/500) == 3 → all three batteries active.
    assert _active_slots(lb) == 3

    # Learning is skipped entirely under an explicit override.
    state = BalancerConsumerState()
    state.last_reported = 800
    lb._update_output_cap(state, last_target=305, actual=800)
    lb._update_output_cap(state, last_target=305, actual=800)
    assert state.output_cap == 0.0


# --- U4: opt-out -----------------------------------------------------------


def test_opt_out_disables_capacity_floor() -> None:
    lb = _make_balancer(_FakeClock(), max_efficient_power=-1)
    lb._compute_efficiency_deprioritized(_reports(3), sample_id=(0,), grid_total=1105.0)
    # Efficiency floor only: int(1105/600) == 1 active battery (legacy bug).
    assert _active_slots(lb) == 1

    # No learning side effects when disabled.
    state = BalancerConsumerState()
    state.last_reported = 800
    lb._update_output_cap(state, last_target=305, actual=800)
    lb._update_output_cap(state, last_target=305, actual=800)
    assert state.output_cap == 0.0
    assert state.clip_samples == 0


# --- U5: efficiency mode off ----------------------------------------------


def test_efficiency_disabled_keeps_all_active() -> None:
    lb = _make_balancer(_FakeClock(), min_efficient_power=0)
    result = lb._compute_efficiency_deprioritized(
        _reports(3), sample_id=(0,), grid_total=1105.0
    )
    assert result == {}
    assert lb._deprioritized == set()
