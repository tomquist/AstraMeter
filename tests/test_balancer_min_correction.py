"""A balance correction worth sending is never one the firmware ignores.

The deadband decides whether a battery is far enough off its share to correct;
the correction itself is the gain times that imbalance, which for an imbalance
just past the deadband is a few watts.  The battery firmware ignores a step
that small, so the battery stayed tens of watts off its share for good: with
distribution weights 10 / 0.1, the light battery charged 64 W next to the
other (discussion #741).  The correction is now at least the deadband, and
never more than the imbalance itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
)

AUTO = ConsumerMode("auto")
HEAVY, LIGHT = "aaaaaaaaaaaa", "bbbbbbbbbbbb"


def _balancer(**cfg: Any) -> LoadBalancer:
    return LoadBalancer(
        # Pacing, damping and prediction off, so a test reads the allocation.
        config=BalancerConfig(
            pace_base_step=0,
            osc_damp_max=0,
            grid_predict_trust=0,
            import_trim_w=0,
            concentrate_deadband=0,
            **cfg,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=60,
    )


def _sent(lb: LoadBalancer, heavy: int, light: int, grid: float) -> float:
    """The reading the light battery is sent next to a 10x heavier one."""
    reports = {
        HEAVY: ConsumerReport(device_type="VNSE3", phase="A", power=heavy, weight=10.0),
        LIGHT: ConsumerReport(device_type="VNSE3", phase="A", power=light, weight=0.1),
    }
    return sum(lb.compute_target(LIGHT, AUTO, reports, grid, frozenset(), frozenset()))


def test_stalled_battery_is_corrected_by_at_least_the_deadband() -> None:
    # 64 W charging against a share of about 13 W: a 51 W imbalance, which the
    # gain alone turns into a ~12 W step the firmware would ignore.
    sent = _sent(_balancer(), -1252, -64, 0)
    assert sent == pytest.approx(25, abs=0.5)


def test_correction_never_exceeds_the_imbalance() -> None:
    # 40 W charging against the same share: a 27 W imbalance, just past the
    # deadband, is corrected by the 25 W floor, not more.
    sent = _sent(_balancer(balance_deadband=25), -1289, -40, 0)
    assert 0 < sent <= 27.5


def test_no_floor_without_a_deadband() -> None:
    sent = _sent(_balancer(balance_deadband=0), -1252, -64, 0)
    assert 0 < sent < 20


def test_the_step_cap_still_wins() -> None:
    sent = _sent(_balancer(max_correction_per_step=10), -1252, -64, 0)
    assert sent == pytest.approx(10, abs=0.5)


def test_zero_gain_still_means_no_balancing() -> None:
    sent = _sent(_balancer(balance_gain=0), -1252, -64, 0)
    assert sent == pytest.approx(0, abs=0.5)
