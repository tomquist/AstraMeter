from astrameter.request_dedupe import RequestDeduplicator


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_window_zero_allows_everything() -> None:
    clock = FakeClock()
    dedup: RequestDeduplicator[str] = RequestDeduplicator(0.0, clock=clock)
    for _ in range(5):
        assert dedup.should_process("a") is True


def test_repeat_within_window_is_dropped() -> None:
    clock = FakeClock()
    dedup: RequestDeduplicator[str] = RequestDeduplicator(1.0, clock=clock)
    assert dedup.should_process("a") is True
    clock.now += 0.5
    assert dedup.should_process("a") is False


def test_repeat_after_window_passes() -> None:
    clock = FakeClock()
    dedup: RequestDeduplicator[str] = RequestDeduplicator(1.0, clock=clock)
    assert dedup.should_process("a") is True
    clock.now += 1.5
    assert dedup.should_process("a") is True


def test_different_keys_are_independent() -> None:
    clock = FakeClock()
    dedup: RequestDeduplicator[str] = RequestDeduplicator(10.0, clock=clock)
    assert dedup.should_process("a") is True
    assert dedup.should_process("b") is True
    clock.now += 1.0
    assert dedup.should_process("a") is False
    assert dedup.should_process("b") is False


def test_purge_drops_stale_entries() -> None:
    clock = FakeClock()
    dedup: RequestDeduplicator[str] = RequestDeduplicator(1.0, clock=clock)
    dedup.should_process("old")
    clock.now += 100.0
    dedup.should_process("new")
    dedup.purge_older_than(max_age_seconds=10.0)
    # "old" is 100s old and should be purged; "new" was just recorded.
    clock.now += 0.1
    assert dedup.should_process("old") is True  # record removed → fresh
    assert dedup.should_process("new") is False  # still within window


def test_purge_on_empty_is_noop() -> None:
    dedup: RequestDeduplicator[str] = RequestDeduplicator(1.0)
    dedup.purge_older_than(10.0)  # should not raise
