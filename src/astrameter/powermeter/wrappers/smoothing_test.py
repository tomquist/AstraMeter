"""Tests for SmoothedPowermeter and DeadbandPowermeter."""

import pytest

from .smoothing import DeadbandPowermeter, SmoothedPowermeter


class FakePowermeter:
    """Minimal powermeter stub for testing wrappers."""

    def __init__(self, values: list[float] | None = None):
        self._values: list[float] = values or [0.0]
        self.started = False
        self.stopped = False
        self.reset_count = 0

    def set(self, values: list[float]) -> None:
        self._values = values

    async def get_powermeter_watts(self) -> list[float]:
        return list(self._values)

    async def get_powermeter_watts_raw(self) -> list[float]:
        return list(self._values)

    async def wait_for_message(self, timeout=5):
        pass

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def reset(self):
        self.reset_count += 1


# ---------------------------------------------------------------------------
# SmoothedPowermeter
# ---------------------------------------------------------------------------


class TestSmoothedPowermeter:
    @pytest.mark.asyncio
    async def test_first_call_seeds_value(self):
        fake = FakePowermeter([100.0, 50.0, -30.0])
        sm = SmoothedPowermeter(fake, alpha=0.5)
        result = await sm.get_powermeter_watts()
        assert result == [100.0, 50.0, -30.0]
        assert sm.smoothed_value == 120.0

    @pytest.mark.asyncio
    async def test_ema_converges_toward_raw(self):
        fake = FakePowermeter([100.0])
        sm = SmoothedPowermeter(fake, alpha=0.5)

        # Seed
        await sm.get_powermeter_watts()
        assert sm.smoothed_value == 100.0

        # Change raw → 200, EMA should move toward it
        fake.set([200.0])
        await sm.get_powermeter_watts()
        # delta = 0.5 * (200 - 100) = 50 → new = 150
        assert sm.smoothed_value == 150.0

        # Another step — use two phases with same total to bypass dedup
        fake.set([120.0, 80.0])
        await sm.get_powermeter_watts()
        # delta = 0.5 * (200 - 150) = 25 → new = 175
        assert sm.smoothed_value == 175.0

    @pytest.mark.asyncio
    async def test_sign_change_catchup(self):
        fake = FakePowermeter([100.0])
        sm = SmoothedPowermeter(fake, alpha=0.1)

        await sm.get_powermeter_watts()
        assert sm.smoothed_value == 100.0

        # Sign flip: raw goes negative
        fake.set([-100.0])
        await sm.get_powermeter_watts()
        # catchup_alpha = max(0.1, min(0.5, 0.1 * 4)) = 0.4
        # delta = 0.4 * (-100 - 100) = -80 → new = 20
        assert sm.smoothed_value == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_sign_change_does_not_slow_high_alpha(self):
        fake = FakePowermeter([1.0])
        sm = SmoothedPowermeter(fake, alpha=1.0)

        await sm.get_powermeter_watts()
        assert sm.smoothed_value == 1.0

        # Sign flip: the catchup branch must not reduce alpha below the
        # configured value, otherwise large user-chosen alphas (e.g. 1.0)
        # respond slower across zero-crossings than they do anywhere else.
        fake.set([-99.0])
        await sm.get_powermeter_watts()
        # catchup_alpha = max(1.0, min(0.5, 4.0)) = 1.0
        # delta = 1.0 * (-99 - 1) = -100 → new = -99
        assert sm.smoothed_value == pytest.approx(-99.0)

    @pytest.mark.asyncio
    async def test_max_step_limits_delta(self):
        fake = FakePowermeter([100.0])
        sm = SmoothedPowermeter(fake, alpha=0.9, max_step=10)

        await sm.get_powermeter_watts()

        fake.set([1000.0])
        await sm.get_powermeter_watts()
        # Without max_step: delta = 0.9 * 900 = 810
        # With max_step=10: clamped to 10 → new = 110
        assert sm.smoothed_value == 110.0

    @pytest.mark.asyncio
    async def test_dedup_identical_values(self):
        fake = FakePowermeter([100.0])
        sm = SmoothedPowermeter(fake, alpha=0.5)

        await sm.get_powermeter_watts()
        assert sm.smoothed_value == 100.0

        # Same values → dedup hit, no EMA update
        result = await sm.get_powermeter_watts()
        assert sm.smoothed_value == 100.0
        assert result == [100.0]

    @pytest.mark.asyncio
    async def test_dedup_same_sample_different_total_advances_ema(self):
        """When sample_id matches but raw_total differs, EMA should advance."""
        fake = FakePowermeter([100.0, 50.0])
        sm = SmoothedPowermeter(fake, alpha=0.5)

        await sm.get_powermeter_watts()
        assert sm.smoothed_value == 150.0

        # Change values (different sample_id and different total)
        fake.set([120.0, 60.0])
        await sm.get_powermeter_watts()
        # delta = 0.5 * (180 - 150) = 15 → new = 165
        assert sm.smoothed_value == 165.0

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        fake = FakePowermeter([100.0])
        sm = SmoothedPowermeter(fake, alpha=0.5)

        await sm.get_powermeter_watts()
        assert sm.smoothed_value == 100.0

        sm.reset()
        assert sm.smoothed_value is None

        # Next call seeds again
        fake.set([200.0])
        await sm.get_powermeter_watts()
        assert sm.smoothed_value == 200.0

    @pytest.mark.asyncio
    async def test_proportional_phase_distribution(self):
        fake = FakePowermeter([60.0, 30.0, 10.0])  # total=100
        sm = SmoothedPowermeter(fake, alpha=0.5)

        # Seed
        await sm.get_powermeter_watts()

        # Change to different values
        fake.set([80.0, 40.0, 80.0])  # total=200
        result = await sm.get_powermeter_watts()
        # smoothed: 100 + 0.5*(200-100) = 150
        # ratio = 150/200 = 0.75
        assert result == pytest.approx([60.0, 30.0, 60.0])

    @pytest.mark.asyncio
    async def test_all_zero_returns_raw(self):
        fake = FakePowermeter([0.0, 0.0, 0.0])
        sm = SmoothedPowermeter(fake, alpha=0.5)

        result = await sm.get_powermeter_watts()
        assert result == [0.0, 0.0, 0.0]

    @pytest.mark.asyncio
    async def test_lifecycle_delegation(self):
        fake = FakePowermeter([100.0])
        sm = SmoothedPowermeter(fake, alpha=0.5)

        await sm.start()
        assert fake.started

        await sm.stop()
        assert fake.stopped

        await sm.wait_for_message(timeout=1)

        sm.reset()
        assert fake.reset_count == 1


# ---------------------------------------------------------------------------
# DeadbandPowermeter
# ---------------------------------------------------------------------------


class TestDeadbandPowermeter:
    @pytest.mark.asyncio
    async def test_values_within_deadband_return_zeros(self):
        fake = FakePowermeter([5.0, -3.0, 2.0])  # total=4
        db = DeadbandPowermeter(fake, deadband=20.0)

        result = await db.get_powermeter_watts()
        assert result == [0.0, 0.0, 0.0]

    @pytest.mark.asyncio
    async def test_values_outside_deadband_pass_through(self):
        fake = FakePowermeter([50.0, 30.0, -10.0])  # total=70
        db = DeadbandPowermeter(fake, deadband=20.0)

        result = await db.get_powermeter_watts()
        assert result == [50.0, 30.0, -10.0]

    @pytest.mark.asyncio
    async def test_zero_deadband_disables_gating(self):
        fake = FakePowermeter([1.0, -1.0, 0.5])  # total=0.5
        db = DeadbandPowermeter(fake, deadband=0.0)

        result = await db.get_powermeter_watts()
        assert result == [1.0, -1.0, 0.5]

    @pytest.mark.asyncio
    async def test_negative_total_within_deadband(self):
        fake = FakePowermeter([-5.0, -3.0, 2.0])  # total=-6
        db = DeadbandPowermeter(fake, deadband=20.0)

        result = await db.get_powermeter_watts()
        assert result == [0.0, 0.0, 0.0]

    @pytest.mark.asyncio
    async def test_exactly_at_threshold_returns_zeros(self):
        fake = FakePowermeter([19.9])
        db = DeadbandPowermeter(fake, deadband=20.0)

        result = await db.get_powermeter_watts()
        assert result == [0.0]

    @pytest.mark.asyncio
    async def test_lifecycle_delegation(self):
        fake = FakePowermeter([100.0])
        db = DeadbandPowermeter(fake, deadband=20.0)

        await db.start()
        assert fake.started

        await db.stop()
        assert fake.stopped

        await db.wait_for_message(timeout=1)

        db.reset()
        assert fake.reset_count == 1
