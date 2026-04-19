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


def test_dropped_request_does_not_refresh_timestamp() -> None:
    # The window is measured from the last *accepted* request. A dropped
    # repeat within the window must not slide the window forward.
    clock = FakeClock()
    dedup: RequestDeduplicator[str] = RequestDeduplicator(1.0, clock=clock)
    assert dedup.should_process("a") is True  # accepted at t=0.0
    clock.now = 0.6
    assert dedup.should_process("a") is False  # dropped; must not refresh
    # At t=1.05 we're past the original accept (t=0.0) but would still be
    # within 1.0s of the dropped attempt (t=0.6) if it had refreshed.
    clock.now = 1.05
    assert dedup.should_process("a") is True


def test_non_finite_window_is_treated_as_disabled() -> None:
    inf_dedup: RequestDeduplicator[str] = RequestDeduplicator(float("inf"))
    for _ in range(3):
        assert inf_dedup.should_process("a") is True

    nan_dedup: RequestDeduplicator[str] = RequestDeduplicator(float("nan"))
    for _ in range(3):
        assert nan_dedup.should_process("a") is True


def test_negative_window_is_treated_as_disabled() -> None:
    dedup: RequestDeduplicator[str] = RequestDeduplicator(-5.0)
    for _ in range(3):
        assert dedup.should_process("a") is True
