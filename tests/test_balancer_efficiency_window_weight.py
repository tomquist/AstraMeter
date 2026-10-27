"""Per-battery efficiency-window weight in the LoadBalancer.

The efficiency rotation deprioritizes some batteries at low demand (so the
active ones stay above ``min_efficient_power``) and rotates which one is active
for fair wear. ``efficiency_window_weight`` (a report-dict field, ``[0, 1]``,
neutral ``1.0``) biases that rotation: ``0.0`` keeps a battery deprioritized
while limiting — a backup that never leads, which the standby reserve still
hands what the batteries ahead cannot cover (see
``test_balancer_standby_reserve.py``) — ``1.0`` is full participation, and the active head holds its slot for
``efficiency_rotation_interval`` scaled by its weight — the last of those only
while a single battery is active, since above one slot a battery is active for
its own turn and its predecessors' too.

These poke the balancer internals (``_priority`` / ``_deprioritized`` /
``_last_rotation``) directly, matching the existing balancer unit tests. The
C++ mirror is covered by the differential parity suite.
"""

import time

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerReport,
    LoadBalancer,
    ProbeState,
)


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _make_balancer(clock, *, rotation_interval: float = 900.0) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            min_efficient_power=150,
            efficiency_rotation_interval=rotation_interval,
            # Follow demand instantly so the active-set decision is deterministic
            # (no EMA smoothing) for these unit assertions.
            efficiency_demand_alpha=1.0,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=False,
        clock=clock,
    )


def _report(power: float, eff_weight: float = 1.0) -> ConsumerReport:
    return ConsumerReport(
        phase="A",
        power=power,
        device_type="HMG-50",
        efficiency_window_weight=eff_weight,
    )


def test_zero_weight_battery_stays_deprioritized_while_limiting() -> None:
    """A 0-weight battery stays deprioritized while limiting (a standby)."""
    clock = _FakeClock()
    lb = _make_balancer(clock)
    # abs demand 200 over two units => per-consumer 100 < 150 => limiting,
    # one active slot. "a" (full weight) stays active; "b" (0) is parked.
    reports = {"a": _report(0.0, 1.0), "b": _report(0.0, 0.0)}
    for i in range(3):
        clock.advance(1.0)
        lb._compute_efficiency_deprioritized(reports, (i,), 200.0)
    assert lb._deprioritized == {"b"}
    # Order: the zero-weight unit is sunk to the back of the priority list.
    assert lb._priority[0] == "a"
    assert lb._priority[-1] == "b"


def test_zero_weight_battery_runs_when_all_needed() -> None:
    """When demand needs every battery (slots == n), the 0-weight one runs too."""
    clock = _FakeClock()
    lb = _make_balancer(clock)
    # abs demand 600 over two units => per-consumer 300 >= 150 => no limiting.
    reports = {"a": _report(0.0, 1.0), "b": _report(0.0, 0.0)}
    for i in range(3):
        clock.advance(1.0)
        lb._compute_efficiency_deprioritized(reports, (i,), 600.0)
    assert lb._deprioritized == set()


def test_low_weight_battery_arrives_behind_a_heavier_peer() -> None:
    """Weight decides the fill order of a fresh pool, over the id order.

    Only the *fill* — once both are in the order, the rotation owns it (see
    ``test_weight_sort_does_not_re_pin_the_head_every_tick``).
    """
    clock = _FakeClock()
    lb = _make_balancer(clock)
    # "a" sorts first by id but has the lower weight, so it must arrive behind
    # "b" and be the one deprioritized on the first limiting poll.
    reports = {"a": _report(0.0, 0.2), "b": _report(0.0, 1.0)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    assert lb._priority[0] == "b"
    assert lb._priority[-1] == "a"
    assert lb._deprioritized == {"a"}


def test_full_weight_head_rotates_after_full_interval() -> None:
    """A weight-1.0 head holds its slot for the whole rotation interval."""
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"a": _report(0.0, 1.0), "b": _report(0.0, 1.0)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    rot0 = lb._last_rotation

    # Half the interval: no rotation yet.
    clock.advance(450.0)
    lb._compute_efficiency_deprioritized(reports, (1,), 200.0)
    assert lb._last_rotation == rot0

    # Past the full interval: the head rotates out.
    clock.advance(500.0)
    lb._compute_efficiency_deprioritized(reports, (2,), 200.0)
    assert lb._last_rotation > rot0


def test_half_weight_head_rotates_after_half_interval() -> None:
    """A weight-0.5 head gives up its slot after ~half the interval."""
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"a": _report(0.0, 0.5), "b": _report(0.0, 0.5)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    rot0 = lb._last_rotation

    # Just under half the interval: not yet.
    clock.advance(440.0)
    lb._compute_efficiency_deprioritized(reports, (1,), 200.0)
    assert lb._last_rotation == rot0

    # Past half the interval (900 * 0.5 = 450): the head rotates out early.
    clock.advance(20.0)
    lb._compute_efficiency_deprioritized(reports, (2,), 200.0)
    assert lb._last_rotation > rot0


# A battery polls every second or so in the field.  The share each one ends up
# with is only a clean ratio when the poll step is small against the rotation
# interval: each handover costs a poll or two of probe settling, so at a coarse
# step that overhead is a visible slice of every turn and the measured ratio
# swings with where the run happens to stop: sweeping every horizon from 60 to
# 480 polls at a 60 s step, the same 0.5 / 1.0 pool reads anywhere from 1.60 to
# 2.40 on horizon alone.  10 s is fine-grained enough to be stable and still
# cheap to run.
_POLL_STEP_S = 10.0


def _run_rotation(
    lb: LoadBalancer,
    clock: _FakeClock,
    weights: dict[str, float],
    *,
    hours: float,
    load: float = 200.0,
) -> dict[str, float]:
    """Seconds each battery spends active over *hours* of polling.

    Feeds the balancer back what its own decision implies: whoever holds an
    active slot reports its share of *load* on the next poll, and a
    deprioritized battery reports 0.  A battery reporting 0 W while active
    reads as one that cannot follow its target, which sends the efficiency
    probe hunting instead of leaving the rotation to run.
    """
    power = dict.fromkeys(weights, load / len(weights))
    active_seconds = dict.fromkeys(weights, 0.0)
    for i in range(int(hours * 3600 / _POLL_STEP_S)):
        clock.advance(_POLL_STEP_S)
        reports = {cid: _report(power[cid], weights[cid]) for cid in weights}
        deprioritized = lb._compute_efficiency_deprioritized(reports, (i,), 0.0)
        active = [cid for cid in weights if cid not in deprioritized]
        for cid in weights:
            power[cid] = load / len(active) if cid in active else 0.0
        for cid in active:
            active_seconds[cid] += _POLL_STEP_S
    return active_seconds


def test_unequal_weights_split_active_time_in_proportion() -> None:
    """Issue #647: a half-weight battery gets half the active time, not one poll.

    Two batteries in a 1:2 capacity ratio at a demand only one of them should
    serve.  Weights 0.5 / 1.0 are meant to hand the small one half the active
    window the large one gets; before the fix the every-poll weight sort put the
    large one straight back at the head, leaving the small one a single poll per
    cycle — 240 s against 21360 s over this run, nearer 89:1 than 2:1.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)

    active = _run_rotation(lb, clock, {"small": 0.5, "large": 1.0}, hours=6)

    assert active["small"] > 0
    assert 1.8 <= active["large"] / active["small"] <= 2.2


def test_equal_weights_split_active_time_evenly() -> None:
    """The neutral case stays even — the fix must not skew a 1:1 pool."""
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)

    active = _run_rotation(lb, clock, {"a": 1.0, "b": 1.0}, hours=6)

    assert 0.9 <= active["a"] / active["b"] <= 1.1


def test_weight_sort_does_not_re_pin_the_head_every_tick() -> None:
    """The weight order is a fill order, not a permanent rank.

    Once the head rotates out it must stay out for the next battery's whole
    window; re-sorting by weight on every poll would hand the slot straight
    back to the heaviest battery.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"a": _report(0.0, 0.4), "b": _report(0.0, 1.0)}

    # First tick fills the order by weight: the heavier battery leads.
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    assert lb._priority[0] == "b"

    # "b" holds a full interval, then hands over to "a" ...
    clock.advance(901.0)
    lb._compute_efficiency_deprioritized(reports, (1,), 200.0)
    assert lb._priority[0] == "a"

    # ... and keeps it for its own (shorter) window instead of being displaced
    # on the very next poll.
    clock.advance(60.0)
    lb._compute_efficiency_deprioritized(reports, (2,), 200.0)
    assert lb._priority[0] == "a"


def test_saturation_swap_is_not_undone_by_the_weight_order() -> None:
    """A saturated battery handed over must stay handed over.

    The swap puts a healthy battery in the active slot; re-ranking the pool by
    weight on the next poll would drag the saturated one — heavier here — back
    to the head, and it would be reinstated on every poll for good.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"heavy": _report(0.0, 1.0), "light": _report(0.0, 0.5)}

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    assert lb._priority[0] == "heavy"

    # "heavy" stops following its target; the swap hands the slot to "light".
    lb._get_consumer("heavy").saturation_score = 1.0
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (1,), 200.0)
    assert lb._priority[0] == "light"

    # It has to still be "light" on the polls that follow.
    for i in range(2, 5):
        clock.advance(1.0)
        lb._compute_efficiency_deprioritized(reports, (i,), 200.0)
        assert lb._priority[0] == "light"


def test_force_rotation_is_not_undone_by_the_weight_order() -> None:
    """A hand-forced rotation survives the next poll.

    ``force_rotation`` is the dashboard's "rotate now" button, so a pool whose
    weights differ must not snap straight back to the battery it rotated away
    from.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"heavy": _report(0.0, 1.0), "light": _report(0.0, 0.5)}

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    assert lb._priority[0] == "heavy"

    lb.force_rotation(set(reports))
    assert lb._priority[0] == "light"

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (1,), 200.0)
    assert lb._priority[0] == "light"


def test_three_batteries_split_active_time_by_weight() -> None:
    """Weights 1 / 0.5 / 0.25 give a 4:2:1 split of the rotation."""
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)

    active = _run_rotation(lb, clock, {"big": 1.0, "mid": 0.5, "small": 0.25}, hours=12)
    total = sum(active.values())
    share = {cid: secs / total for cid, secs in active.items()}

    # Ideal 0.571 / 0.286 / 0.143.  Each handover costs a little settling time,
    # which comes off the longest turn and slightly flatters the shortest.
    assert 0.53 <= share["big"] <= 0.61
    assert 0.25 <= share["mid"] <= 0.32
    assert 0.12 <= share["small"] <= 0.18
    assert share["big"] > share["mid"] > share["small"]


def test_weight_dropped_to_zero_parks_the_battery_on_the_next_poll() -> None:
    """Parking is the one thing the per-poll order still enforces.

    A battery already holding the head has to be sunk as soon as its weight
    reaches 0, rather than keeping the slot until its window happens to end.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    running = {"a": _report(0.0, 1.0), "b": _report(0.0, 1.0)}

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(running, (0,), 200.0)
    assert lb._priority[0] == "a"

    # "a" is parked mid-window; "b" must take over on the very next poll.
    parked = {"a": _report(0.0, 0.0), "b": _report(0.0, 1.0)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(parked, (1,), 200.0)
    assert lb._priority[0] == "b"
    assert lb._priority[-1] == "a"
    assert lb._deprioritized == {"a"}


def test_weights_do_not_skew_the_rotation_once_several_batteries_run() -> None:
    """Above one active slot the rotation is even, whatever the weights.

    A battery is active for its own turn *and* its predecessors', so scaling
    each turn by the head's weight would hand two batteries weighted the same
    very different shares as soon as a lighter third joins them.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)

    # 400 W over three batteries leaves two active slots (400/150 -> 2).
    active = _run_rotation(
        lb, clock, {"a": 1.0, "b": 1.0, "c": 0.25}, hours=12, load=400.0
    )
    total = sum(active.values())
    share = {cid: secs / total for cid, secs in active.items()}

    # The two equally weighted batteries wear equally, and the light one is
    # neither starved (the #647 bug) nor privileged.
    assert abs(share["a"] - share["b"]) < 0.03
    for cid in ("a", "b", "c"):
        assert 0.30 <= share[cid] <= 0.37


def test_heavier_battery_joining_a_running_pool_waits_its_turn() -> None:
    """Arrivals go to the back, even a heavy one meeting a lighter head.

    The weight fills a *fresh* order; it must not let a late arrival preempt a
    battery that is part-way through its turn.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    running = {"light": _report(0.0, 0.5), "mid": _report(0.0, 0.5)}

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(running, (0,), 200.0)
    assert lb._priority[0] == "light"

    joined = dict(running) | {"heavy": _report(0.0, 1.0)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(joined, (1,), 200.0)
    assert lb._priority[0] == "light"
    assert lb._priority[-1] == "heavy"


def test_unparking_a_battery_puts_it_back_in_the_rotation() -> None:
    """Raising a weight off 0 returns the battery to the rotation."""
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    parked = {"a": _report(0.0, 1.0), "b": _report(0.0, 0.0)}

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(parked, (0,), 200.0)
    assert lb._deprioritized == {"b"}

    # Un-parked, "b" is no longer pinned to the tail and takes the head at the
    # end of "a"'s window.
    running = {"a": _report(0.0, 1.0), "b": _report(0.0, 1.0)}
    clock.advance(901.0)
    lb._compute_efficiency_deprioritized(running, (1,), 200.0)
    assert lb._priority[0] == "b"
    assert lb._deprioritized == {"a"}


def test_rejected_probe_leaves_its_candidate_at_the_back() -> None:
    """A probe rejection sinks the battery that failed it; that has to stick.

    ``_reject_probe`` rewrites the order deliberately, putting the candidate
    last.  Re-ranking by weight on the next poll would pull it back to the head
    when it happens to be the heaviest — re-promoting a battery that had just
    failed to deliver.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"heavy": _report(0.0, 1.0), "light": _report(0.0, 0.5)}

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    assert lb._priority[0] == "heavy"

    now = clock()
    lb._probe_state = ProbeState(
        candidate_id="heavy",
        active_ids=("heavy",),
        backup_ids=("light",),
        restore_active_ids=("light",),
        deadline=now + 30.0,
        started_at=now,
    )
    lb._reject_probe(now, "test")
    assert lb._priority == ["light", "heavy"]

    # The next polls must leave the failed candidate where the rejection put it.
    for i in range(1, 4):
        clock.advance(1.0)
        lb._compute_efficiency_deprioritized(reports, (i,), 200.0)
        assert lb._priority[0] == "light"


def test_departing_battery_leaves_the_rotation_order_alone() -> None:
    """A battery dropping out takes its slot with it and nothing else.

    The other half of ``_sync_pool``'s reconciliation: the survivors keep their
    order and the departed id is dropped from ``_deprioritized`` too, rather
    than the pool being refilled by weight from scratch.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    three = {
        "heavy": _report(0.0, 1.0),
        "mid": _report(0.0, 0.5),
        "light": _report(0.0, 0.25),
    }

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(three, (0,), 200.0)
    assert lb._priority == ["heavy", "mid", "light"]

    # "heavy" goes silent mid-window: "mid" inherits the head in place, and the
    # order behind it is untouched.
    two = {"mid": _report(0.0, 0.5), "light": _report(0.0, 0.25)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(two, (1,), 200.0)
    assert lb._priority == ["mid", "light"]
    assert "heavy" not in lb._deprioritized


def test_all_zero_weight_pool_rotates_on_the_normal_interval() -> None:
    """Every battery parked still rotates at the interval, not at every poll.

    The sink cannot promote anyone when nothing outranks anyone, so a 0 weight
    reaches the head — the one case where it can.  Taken literally that is a 0
    threshold and the slot changes hands on every poll the probe machinery
    leaves free; before the fallback this pool moved roughly every 46 s against
    a 900 s interval.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"a": _report(0.0, 0.0), "b": _report(0.0, 0.0)}

    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    first = lb._priority[0]

    # Ten minutes of polling, well inside one window: the head holds.
    for i in range(1, 600):
        clock.advance(1.0)
        lb._compute_efficiency_deprioritized(reports, (i,), 200.0)
        assert lb._priority[0] == first

    # Past the full interval it hands over like any other pool.
    clock.advance(400.0)
    lb._compute_efficiency_deprioritized(reports, (600,), 200.0)
    assert lb._priority[0] != first
