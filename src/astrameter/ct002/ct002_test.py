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
