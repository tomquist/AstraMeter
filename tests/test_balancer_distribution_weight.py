"""Per-battery distribution weight in the LoadBalancer fair-share split.

Mirrors the C++ host test ``LoadBalancer.AutoSplitHonoursDistributionWeight``.
A battery's ``weight`` rides along in its report dict; the balancer biases the
proportional split by it while leaving the neutral (all-1.0) case identical to
the unweighted behaviour.
"""

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    LoadBalancer,
)


def _make_balancer(*, fair_distribution: bool = True) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=fair_distribution,
            balance_gain=0.2,
            balance_deadband=15,
            max_correction_per_step=80,
            min_efficient_power=0,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=True,
    )


def _report(power: float, weight: float = 1.0, phase: str = "A") -> dict:
    return {"phase": phase, "power": power, "device_type": "HMA-2", "weight": weight}


def test_fair_share_honours_weight():
    """With balancing off, the raw fair-share split follows the weight ratio."""
    lb = _make_balancer(fair_distribution=False)
    reports = {"a": _report(0.0, weight=1.5), "b": _report(0.0, weight=1.0)}
    a_out = lb.compute_target(
        "a", ConsumerMode("auto"), reports, 500.0, frozenset(), frozenset()
    )
    b_out = lb.compute_target(
        "b", ConsumerMode("auto"), reports, 500.0, frozenset(), frozenset()
    )
    # share = eff_part(1.0) * weight; total = 2.5 → a: 500*1.5/2.5 = 300, b: 200.
    assert a_out[0] == 300.0
    assert b_out[0] == 200.0


def test_zero_weight_takes_no_share():
    """Weight 0.0 means the battery is parked at 0 W; the rest absorb the load."""
    lb = _make_balancer(fair_distribution=False)
    reports = {"a": _report(0.0, weight=0.0), "b": _report(0.0, weight=1.0)}
    a_out = lb.compute_target(
        "a", ConsumerMode("auto"), reports, 400.0, frozenset(), frozenset()
    )
    b_out = lb.compute_target(
        "b", ConsumerMode("auto"), reports, 400.0, frozenset(), frozenset()
    )
    assert a_out[0] == 0.0
    assert b_out[0] == 400.0


def test_neutral_weight_matches_equal_split():
    """Default weight 1.0 (and an absent weight key) split demand evenly."""
    weighted = {"a": _report(0.0), "b": _report(0.0)}
    # An absent "weight" key must behave exactly like the neutral default.
    bare = {
        "a": {"phase": "A", "power": 0, "device_type": "HMA-2"},
        "b": {"phase": "A", "power": 0, "device_type": "HMA-2"},
    }
    for reports in (weighted, bare):
        lb = _make_balancer(fair_distribution=False)
        out = lb.compute_target(
            "a", ConsumerMode("auto"), reports, 400.0, frozenset(), frozenset()
        )
        assert out[0] == 200.0


def test_balance_correction_targets_weighted_share():
    """Two equally-loaded batteries get nudged toward the weighted ratio.

    Both report 250 W; with weights 1.5/1.0 the heavier battery's target sits
    above its current output and the lighter one's below, so the correction is
    positive for "a" and negative for "b".
    """
    lb = _make_balancer(fair_distribution=True)
    reports = {"a": _report(250.0, weight=1.5), "b": _report(250.0, weight=1.0)}
    a_out = lb.compute_target(
        "a", ConsumerMode("auto"), reports, 500.0, frozenset(), frozenset()
    )
    b_out = lb.compute_target(
        "b", ConsumerMode("auto"), reports, 500.0, frozenset(), frozenset()
    )
    # Weighted target for "a" (300) > its 250 reported → pushed up; "b" pushed down.
    assert a_out[0] > 250.0
    assert b_out[0] < 250.0
