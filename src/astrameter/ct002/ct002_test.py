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
