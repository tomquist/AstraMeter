import pytest

from astrameter.ct002.ct002 import CT002


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_dedup_uses_consumer_id_key_and_injected_clock() -> None:
    clock = FakeClock()
    ct = CT002(dedupe_time_window=1.0, clock=clock)

    # Same consumer within the window → dropped.
    assert ct._dedup.should_process("consumer-A") is True
    clock.now += 0.5
    assert ct._dedup.should_process("consumer-A") is False

    # Different consumers are independent, even within the window.
    assert ct._dedup.should_process("consumer-B") is True

    # After the window elapses, the same consumer is accepted again.
    clock.now += 1.0
    assert ct._dedup.should_process("consumer-A") is True


def test_dedup_window_zero_disables() -> None:
    ct = CT002(dedupe_time_window=0.0)
    for _ in range(3):
        assert ct._dedup.should_process("consumer-A") is True


def test_set_consumer_efficiency_window_weight_accepts_valid_range() -> None:
    ct = CT002()
    for value in (0.0, 0.25, 0.5, 1.0):
        ct.set_consumer_efficiency_window_weight("c1", value)
        assert ct._get_consumer("c1").efficiency_window_weight == value


def test_set_consumer_efficiency_window_weight_rejects_out_of_range() -> None:
    ct = CT002()
    ct.set_consumer_efficiency_window_weight("c1", 0.5)
    for bad in (-0.1, 1.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            ct.set_consumer_efficiency_window_weight("c1", bad)
    # The rejected writes left the last valid value untouched.
    assert ct._get_consumer("c1").efficiency_window_weight == 0.5


def test_consumer_efficiency_window_weight_defaults_to_one() -> None:
    ct = CT002()
    assert ct._get_consumer("c1").efficiency_window_weight == 1.0


def test_overrides_survive_consumer_eviction() -> None:
    """A battery that goes silent past its TTL is evicted, but its user-set
    control state is re-seeded onto the fresh consumer when it returns."""
    clock = FakeClock()
    ct = CT002(consumer_ttl=10, clock=clock)

    # User sets a manual override and tweaks the distribution weight.
    ct.set_consumer_manual_target("c1", 150.0)
    ct.set_consumer_auto_target("c1", False)  # manual mode
    ct.set_consumer_distribution_weight("c1", 2.0)
    ct.set_consumer_active("c1", False)

    # Mark it as having reported, then let it fall silent past the TTL.
    clock.now = 5.0
    ct._get_consumer("c1").timestamp = clock.now
    clock.now += 11.0
    ct._cleanup_consumers()
    assert "c1" not in ct._consumers  # evicted
    assert "c1" in ct._consumer_overrides  # but the override is retained

    # Battery returns — a fresh consumer is created and re-seeded.
    revived = ct._get_consumer("c1")
    assert revived.manual_target == 150.0
    assert revived.manual_enabled is True
    assert revived.distribution_weight == 2.0
    assert revived.active is False


def test_override_tracks_latest_value_through_eviction() -> None:
    """Returning a battery to auto mode is also remembered, so it doesn't come
    back stuck in a stale manual override after an eviction."""
    clock = FakeClock()
    ct = CT002(consumer_ttl=10, clock=clock)

    ct.set_consumer_manual_target("c1", 150.0)
    ct.set_consumer_auto_target("c1", False)
    # User changes their mind and switches back to automatic control.
    ct.set_consumer_auto_target("c1", True)

    clock.now = 5.0
    ct._get_consumer("c1").timestamp = clock.now
    clock.now += 11.0
    ct._cleanup_consumers()

    revived = ct._get_consumer("c1")
    assert revived.manual_enabled is False  # auto mode preserved, not manual


def test_no_override_leaves_fresh_consumer_at_defaults() -> None:
    """A consumer never touched by a setter is created with plain defaults."""
    ct = CT002()
    consumer = ct._get_consumer("c1")
    assert consumer.manual_enabled is False
    assert consumer.manual_target == 0.0
    assert consumer.active is True
    assert consumer.distribution_weight == 1.0
    assert "c1" not in ct._consumer_overrides
