"""Unit tests for :class:`TargetSmoother`.

These exercise both the happy paths (EMA convergence, deadband decay,
sign-change catchup) and the regression case described in
``tests/test_balancer_probe_lockup.py`` — a stable sensor value trapping
the smoother on the "wrong" side of a zero-crossing.
"""

from __future__ import annotations

from astrameter.ct002.smoother import TargetSmoother


class TestTargetSmootherBasics:
    def test_first_sample_seeds_value(self) -> None:
        s = TargetSmoother(alpha=0.5)
        assert s.update(100.0, (100.0, 0.0, 0.0)) == 100.0
        assert s.value == 100.0

    def test_ema_converges_toward_raw(self) -> None:
        s = TargetSmoother(alpha=0.5)
        s.update(0.0, (0.0,))
        # Each new sample must be a distinct sample_id or the dedup fires.
        s.update(100.0, (100.0,))
        assert 49.0 < s.value < 51.0  # 0 + 0.5 * (100 - 0)
        s.update(100.0, (100.1,))
        assert 74.0 < s.value < 76.0

    def test_deadband_decays_toward_zero(self) -> None:
        s = TargetSmoother(alpha=0.5, deadband=10.0)
        s.update(100.0, (100.0,))
        s.update(2.0, (2.0,))  # inside deadband
        assert 49.0 < s.value < 51.0  # decayed by alpha
        s.update(3.0, (3.0,))  # still inside deadband
        assert 24.0 < s.value < 26.0

    def test_sign_flip_catchup(self) -> None:
        s = TargetSmoother(alpha=0.1)
        s.update(100.0, (100.0,))
        assert s.value == 100.0
        # Sign flip: catchup_alpha = min(0.5, 0.1*4) = 0.4
        s.update(-100.0, (-100.0,))
        # delta = 0.4 * (-100 - 100) = -80 → value = 100 + (-80) = 20
        assert 19.0 < s.value < 21.0

    def test_max_step_limits_delta(self) -> None:
        s = TargetSmoother(alpha=1.0, max_step=10.0)
        s.update(0.0, (0.0,))
        s.update(100.0, (100.0,))
        assert s.value == 10.0


class TestTargetSmootherLockupRegression:
    """Regression: a stable sensor value must not trap the smoother.

    The balancer uses ``smoothed_target`` to compute per-consumer targets.
    If the smoother stops advancing while the raw meter reading is stale
    (e.g. push-based powermeter hasn't forwarded a new state event yet),
    the entire control loop freezes at the last-known value.

    See the matching test in ``test_balancer_probe_lockup.py`` for the
    end-to-end manifestation that prompted the fix.
    """

    def test_identical_samples_still_advance_value_when_changing(self) -> None:
        """Two calls with the same ``sample_id`` but different ``raw_total``
        must still be allowed to advance the EMA — sample_id is meant to
        coalesce multi-consumer polls within one meter tick, not to cap
        the smoother at its first reading.
        """
        s = TargetSmoother(alpha=0.5)
        s.update(50.0, (0.0,))  # Seed value well away from zero.
        assert s.value == 50.0
        # Same sample_id but raw has moved: this is the "stale key, fresh
        # value" pattern that could mask a real meter change.  The
        # smoother must advance the EMA.
        s.update(100.0, (0.0,))
        assert s.value != 50.0, (
            "Smoother ignored a fresh raw_total because sample_id was "
            "identical — this is the lockup regression."
        )
        # 50 + 0.5 * (100 - 50) = 75
        assert 74.0 < s.value < 76.0

    def test_repeated_identical_call_within_tick_is_still_deduped(self) -> None:
        """Within a single meter tick, multiple consumers polling must not
        compound the EMA.  The dedup still has to fire when *both*
        ``raw_total`` and ``sample_id`` match.
        """
        s = TargetSmoother(alpha=0.5)
        s.update(0.0, (0.0,))
        s.update(100.0, (100.0,))  # Tick 1: advance to 50
        after_first = s.value
        # Tick 1 continued: a second consumer reads the same meter value
        # and calls update() with an identical (raw, sample_id) pair.  The
        # EMA must not compound.
        s.update(100.0, (100.0,))
        assert s.value == after_first, "Dedup within a tick failed — EMA compounded"

    def test_reseed_clears_state_and_next_update_seeds_directly(self) -> None:
        """After :meth:`reseed` the next update must set ``_value``
        directly to ``raw_total`` (bypassing EMA) so post-probe state
        can re-anchor in a single step.
        """
        s = TargetSmoother(alpha=0.1)
        s.update(0.0, (0.0,))
        s.update(50.0, (50.0,))
        assert s.value is not None
        assert s.value != 50.0  # EMA has dragged it somewhere in between

        s.reseed()
        assert s.value is None
        assert s._last_sample is None
        assert s._last_raw_total is None

        s.update(100.0, (100.0,))
        assert s.value == 100.0, (
            "First post-reseed update must seed directly, not EMA-smooth"
        )
