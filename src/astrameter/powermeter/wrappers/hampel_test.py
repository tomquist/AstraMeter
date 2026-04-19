"""Tests for HampelPowermeter."""

import pytest

from .hampel import HampelPowermeter


class FakePowermeter:
    """Minimal powermeter stub for testing wrappers."""

    def __init__(self, values: list[float] | None = None):
        self._values: list[float] = values if values is not None else [0.0]
        self.started = False
        self.stopped = False
        self.reset_count = 0

    def set(self, values: list[float]) -> None:
        self._values = values

    async def get_powermeter_watts(self) -> list[float]:
        return list(self._values)

    async def wait_for_message(self, timeout=5):
        pass

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def reset(self):
        self.reset_count += 1


async def _push(hp: HampelPowermeter, fake: FakePowermeter, values: list[float]):
    fake.set(values)
    return await hp.get_powermeter_watts()


class TestHampelPowermeter:
    @pytest.mark.asyncio
    async def test_warmup_passes_through(self):
        fake = FakePowermeter([100.0])
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0)
        for _ in range(4):
            result = await hp.get_powermeter_watts()
            assert result == [100.0]

    @pytest.mark.asyncio
    async def test_clean_signal_not_modified(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0)
        # Fill window with clean samples
        for v in [100.0, 102.0, 98.0, 101.0, 99.0]:
            await _push(hp, fake, [v])
        # Next clean sample — should pass through
        result = await _push(hp, fake, [100.5])
        assert result == [100.5]

    @pytest.mark.asyncio
    async def test_single_spike_replaced_by_median(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0)
        for v in [100.0, 102.0, 98.0, 101.0, 99.0]:
            await _push(hp, fake, [v])
        result = await _push(hp, fake, [10000.0])
        # Median of the window after appending 10000 is 100 (or close),
        # threshold is small, so the spike is replaced.
        assert result[0] == pytest.approx(100.0, abs=5.0)
        assert result[0] != 10000.0

    @pytest.mark.asyncio
    async def test_roll_forward_evicts_mutated_entry(self):
        """After a replacement, the mutated (median) entry is what rolls out,
        not the original outlier — so the outlier doesn't return to the window.
        """
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=3, n_sigma=3.0)
        # Fill window
        await _push(hp, fake, [100.0])
        await _push(hp, fake, [100.0])
        await _push(hp, fake, [100.0])
        # Inject spike — window is [100, 100, 10000] before detection; after
        # replacement the stored window is [100, 100, 100].
        await _push(hp, fake, [10000.0])
        # Next clean sample: window becomes [100, 100, 100] after evicting the
        # oldest 100, and the new sample 101 is within threshold.
        result = await _push(hp, fake, [101.0])
        assert result == [101.0]

    @pytest.mark.asyncio
    async def test_constant_signal_mad_zero_no_floor(self):
        """MAD=0 with min_threshold=0 → threshold is 0 → documented
        limitation: spikes pass through."""
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0, min_threshold=0.0)
        for _ in range(5):
            await _push(hp, fake, [0.0])
        result = await _push(hp, fake, [500.0])
        assert result == [500.0]

    @pytest.mark.asyncio
    async def test_min_threshold_floor_catches_spike_on_constant(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0, min_threshold=50.0)
        for _ in range(5):
            await _push(hp, fake, [0.0])
        result = await _push(hp, fake, [500.0])
        # Spike > 50 W floor → replaced with median (0)
        assert result == [0.0]

    @pytest.mark.asyncio
    async def test_min_threshold_floor_lets_small_changes_pass(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0, min_threshold=50.0)
        for _ in range(5):
            await _push(hp, fake, [0.0])
        # Change smaller than the floor → passes through
        result = await _push(hp, fake, [30.0])
        assert result == [30.0]

    @pytest.mark.asyncio
    async def test_negative_outlier_symmetric(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0)
        for v in [100.0, 102.0, 98.0, 101.0, 99.0]:
            await _push(hp, fake, [v])
        result = await _push(hp, fake, [-10000.0])
        assert result[0] == pytest.approx(100.0, abs=5.0)
        assert result[0] != -10000.0

    @pytest.mark.asyncio
    async def test_phase_ratio_preserved_on_replacement(self):
        # Constant total → MAD=0; use min_threshold to gate detection.
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0, min_threshold=50.0)
        for phases in [
            [60.0, 30.0, 10.0],
            [50.0, 30.0, 20.0],
            [40.0, 40.0, 20.0],
            [70.0, 20.0, 10.0],
            [55.0, 35.0, 10.0],
        ]:
            await _push(hp, fake, phases)
        # Spike with total=1000 and ratio 60:30:10
        result = await _push(hp, fake, [600.0, 300.0, 100.0])
        # Median of totals is 100. Replacement ratio = 100 / 1000 = 0.1 →
        # per-phase [60, 30, 10].
        assert result == pytest.approx([60.0, 30.0, 10.0])

    @pytest.mark.asyncio
    async def test_raw_total_near_zero_equal_split(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0, min_threshold=50.0)
        # Fill window with a nonzero stable total so median is nonzero
        for _ in range(5):
            await _push(hp, fake, [100.0, 0.0, 0.0])
        # Next sample: phases cancel to zero total, which is a large departure
        # from the median total of 100 → outlier. Replacement must equal-split.
        result = await _push(hp, fake, [1.0, -1.0, 0.0])
        # Total was 0 → equal-split fallback: [median/3] * 3
        assert result == pytest.approx([100.0 / 3] * 3)

    @pytest.mark.asyncio
    async def test_empty_raw_values_returns_empty(self):
        fake = FakePowermeter([])
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0)
        result = await hp.get_powermeter_watts()
        assert result == []

    @pytest.mark.asyncio
    async def test_window_one_always_passthrough(self):
        """window=1 is degenerate: median == sample, MAD always 0 → passthrough
        (when min_threshold=0)."""
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=1, n_sigma=3.0, min_threshold=0.0)
        for v in [100.0, 10000.0, -50.0, 0.0]:
            result = await _push(hp, fake, [v])
            assert result == [v]

    @pytest.mark.asyncio
    async def test_even_window_works(self):
        """With an even window, statistics.median returns the mean of the two
        middle values. Confirm no crash and sane behavior."""
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=4, n_sigma=3.0)
        for v in [100.0, 102.0, 98.0, 101.0]:
            result = await _push(hp, fake, [v])
            # Warmup: first 3 pass through; 4th fills window, median ≈ 100.5,
            # MAD small → 101 is close → passes through.
            assert result == [v]
        # Now inject a clear spike
        result = await _push(hp, fake, [10000.0])
        assert result[0] != 10000.0

    @pytest.mark.asyncio
    async def test_n_sigma_zero_relies_on_min_threshold(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=5, n_sigma=0.0, min_threshold=50.0)
        for v in [100.0, 100.0, 100.0, 100.0, 100.0]:
            await _push(hp, fake, [v])
        # n_sigma=0 → sigma term is 0; min_threshold=50 is the only gate.
        # 120 vs median 100 = delta 20 < 50 → pass through
        result = await _push(hp, fake, [120.0])
        assert result == [120.0]
        # 200 vs median 100 = delta 100 > 50 → rejected
        fake.set([200.0])
        result = await hp.get_powermeter_watts()
        assert result[0] == pytest.approx(100.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_warmup_poisoning_recovers(self):
        """A spike within the warmup window passes through, but subsequent
        outliers after warmup still get detected once the window re-fills
        with clean samples. Uses min_threshold so MAD=0 doesn't block."""
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=3, n_sigma=3.0, min_threshold=50.0)
        # Warmup with poisoning: first sample is a spike
        await _push(hp, fake, [10000.0])  # passes through (warmup)
        await _push(hp, fake, [100.0])  # passes through (warmup)
        await _push(hp, fake, [100.0])  # fills window; still has 10000
        await _push(hp, fake, [100.0])  # 10000 evicted; window=[100,100,100]
        # Now inject a real spike — should be rejected via min_threshold.
        result = await _push(hp, fake, [10000.0])
        assert result[0] != 10000.0

    @pytest.mark.asyncio
    async def test_reset_clears_window(self):
        fake = FakePowermeter()
        hp = HampelPowermeter(fake, window=3, n_sigma=3.0)
        for _ in range(3):
            await _push(hp, fake, [100.0])
        hp.reset()
        assert fake.reset_count == 1
        # After reset, the next 2 samples are warmup and pass through
        result = await _push(hp, fake, [10000.0])
        assert result == [10000.0]

    @pytest.mark.asyncio
    async def test_invalid_window_rejected(self):
        fake = FakePowermeter()
        with pytest.raises(ValueError):
            HampelPowermeter(fake, window=0)

    @pytest.mark.asyncio
    async def test_invalid_n_sigma_rejected(self):
        fake = FakePowermeter()
        with pytest.raises(ValueError):
            HampelPowermeter(fake, window=5, n_sigma=-1.0)

    @pytest.mark.asyncio
    async def test_invalid_min_threshold_rejected(self):
        fake = FakePowermeter()
        with pytest.raises(ValueError):
            HampelPowermeter(fake, window=5, min_threshold=-1.0)

    @pytest.mark.asyncio
    async def test_lifecycle_delegation(self):
        fake = FakePowermeter([100.0])
        hp = HampelPowermeter(fake, window=5, n_sigma=3.0)
        await hp.start()
        assert fake.started
        await hp.stop()
        assert fake.stopped
        await hp.wait_for_message(timeout=1)
        hp.reset()
        assert fake.reset_count == 1
