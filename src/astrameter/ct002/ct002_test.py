import asyncio

import pytest

from astrameter.ct002.ct002 import CT002, _values_finite
from astrameter.ct002.protocol import build_payload, parse_request


class _RecordingTransport:
    """Captures every datagram CT002 would have sent back over UDP."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append(data)


def _poll(mac: str, phase: str = "A", power: int = 432) -> bytes:
    return build_payload(["HMG-50", mac, "HME-4", "112233445566", phase, str(power)])


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


def _drive_polls(ct: CT002, clock: FakeClock, gaps: list[float]) -> _RecordingTransport:
    """Poll ``ct`` once per entry in *gaps*, waiting that long beforehand."""
    transport = _RecordingTransport()
    for gap in gaps:
        clock.now += gap
        asyncio.run(
            ct._handle_request(_poll("AABBCCDDEEFF"), ("10.0.0.5", 50000), transport)
        )
    return transport


def test_poll_interval_measures_the_battery_not_our_replies() -> None:
    """`poll_interval` is the battery's cadence even while dedupe drops polls.

    Measuring it after the dedupe gate would report our answer rate instead,
    which is what made a 0.6 s window look like it had been rounded up
    (issue #589).
    """
    clock = FakeClock()
    clock.now = 1000.0
    # Battery polls every 0.5 s; the window suppresses every other reply.
    ct = CT002(ct_mac="", dedupe_time_window=0.6, clock=clock)
    transport = _drive_polls(ct, clock, [0.0] + [0.5] * 8)

    consumer = ct._consumers["aabbccddeeff"]
    assert consumer.poll_interval == 0.5, "poll_interval must track every poll"
    # Every other poll is answered, so replies land 1.0 s apart.
    assert consumer.answer_interval == 1.0
    assert len(transport.sent) == 5  # polls 1, 3, 5, 7, 9


def test_answer_interval_matches_poll_interval_without_dedupe() -> None:
    clock = FakeClock()
    clock.now = 1000.0
    ct = CT002(ct_mac="", dedupe_time_window=0.0, clock=clock)
    transport = _drive_polls(ct, clock, [0.0] + [2.0] * 5)

    consumer = ct._consumers["aabbccddeeff"]
    assert consumer.poll_interval == 2.0
    assert consumer.answer_interval == 2.0
    assert len(transport.sent) == 6


def test_deduped_poll_advances_the_status_revision() -> None:
    """A suppressed poll still moves state, so it must move `rev` with it.

    The status API's revision is how a client decides whether to re-render;
    leaving it untouched would freeze a deduped battery at its last answered
    poll even though its liveness and poll interval just changed.
    """
    clock = FakeClock()
    clock.now = 1000.0
    ct = CT002(ct_mac="", dedupe_time_window=0.6, clock=clock)
    _drive_polls(ct, clock, [0.0])
    after_first = ct._rev

    clock.now += 0.1  # inside the window → suppressed
    _drive_polls(ct, clock, [0.0])
    assert ct._rev > after_first


def test_deduped_poll_refreshes_liveness() -> None:
    """A suppressed poll still keeps the consumer out of the eviction sweep."""
    clock = FakeClock()
    clock.now = 1000.0
    ct = CT002(ct_mac="", dedupe_time_window=100.0, clock=clock)
    # 40 s apart: past the adaptive fallback TTL, and every poll after the
    # first is suppressed by the wide window.
    _drive_polls(ct, clock, [0.0, 40.0])

    clock.now += 1.0
    ct._cleanup_consumers()
    assert "aabbccddeeff" in ct._consumers


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


def _mark_reported(ct: CT002, consumer_id: str, now: float) -> None:
    """Fake a consumer whose last poll arrived at *now*."""
    ct._get_consumer(consumer_id).timestamp = now


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
    _mark_reported(ct, "c1", clock.now)
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
    _mark_reported(ct, "c1", clock.now)
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


# ---------------------------------------------------------------------------
# Concurrent-poll coalescing: one response per consumer per meter reading.
#
# When the meter read parks the handler — WAIT_FOR_NEXT_MESSAGE awaits the next
# push, THROTTLE_INTERVAL sleeps out the throttle window, or a slow HTTP meter
# just takes a while — the battery keeps polling (~1/s) and every datagram is
# its own task.  Both settings share the same failure mode: the parked handlers
# all wake on the one fresh reading and each sends a delta, so the battery gets
# a burst of instructions it *adds* to its output.  These tests pin the fix at
# the request-handler level, so it covers every slow-read cause at once.
# ---------------------------------------------------------------------------


async def _gated_before_send(ct: CT002, gate: asyncio.Event) -> list[int]:
    """Wire a ``before_send`` that parks on *gate* (a slow meter read) and
    returns a fixed grid reading.  Returns a one-element call counter list."""
    calls = [0]

    async def before_send(_addr, _fields=None, _consumer_id=None):
        calls[0] += 1
        await gate.wait()
        return [150.0, 0.0, 0.0]

    ct.before_send = before_send
    return calls


async def test_concurrent_polls_coalesce_to_a_single_response() -> None:
    """Four polls from one battery pile up in a parked read; only ONE delta
    goes out when the reading lands — not a four-deep burst."""
    ct = CT002(ct_mac="", active_control=True, dedupe_time_window=0.0)
    gate = asyncio.Event()
    calls = await _gated_before_send(ct, gate)

    transport = _RecordingTransport()
    addr = ("192.168.178.134", 22222)
    mac = "02b250b26777"

    handlers = [
        asyncio.create_task(ct._handle_request(_poll(mac), addr, transport))
        for _ in range(4)
    ]
    # Let every task reach its await point / early return.
    await asyncio.sleep(0.05)
    assert transport.sent == []  # nothing answered while the read is parked

    # A single fresh reading arrives and wakes the parked handler.
    gate.set()
    await asyncio.gather(*handlers)

    assert len(transport.sent) == 1  # exactly one delta, not a burst of four
    assert calls[0] == 1  # meter + stateful balancer driven once, not four times
    assert ct._inflight_consumers == set()  # flag cleared for the next reading


async def test_coalescing_is_per_consumer() -> None:
    """Coalescing is keyed per battery: two batteries polling at once each get
    their own single response; one does not suppress the other."""
    ct = CT002(ct_mac="", active_control=True, dedupe_time_window=0.0)
    gate = asyncio.Event()
    await _gated_before_send(ct, gate)

    transport = _RecordingTransport()
    handlers = []
    for mac, addr in (
        ("02b250b26777", ("192.168.178.134", 22222)),
        ("02b250aaaaaa", ("192.168.178.135", 22222)),
    ):
        # Two concurrent polls per battery — the duplicate is coalesced away.
        handlers.append(
            asyncio.create_task(ct._handle_request(_poll(mac), addr, transport))
        )
        handlers.append(
            asyncio.create_task(ct._handle_request(_poll(mac), addr, transport))
        )

    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(*handlers)

    assert len(transport.sent) == 2  # one per battery, both answered
    assert ct._inflight_consumers == set()


async def test_poll_answered_again_after_burst_coalesced() -> None:
    """Dropping the duplicate polls is not a lock-out: once the in-flight
    handler responds, the next poll is served normally."""
    ct = CT002(ct_mac="", active_control=True, dedupe_time_window=0.0)
    gate = asyncio.Event()
    gate.set()  # reads resolve immediately here
    await _gated_before_send(ct, gate)

    transport = _RecordingTransport()
    addr = ("192.168.178.134", 22222)
    mac = "02b250b26777"

    # First poll completes end-to-end (gate already open).
    await ct._handle_request(_poll(mac), addr, transport)
    assert len(transport.sent) == 1
    assert ct._inflight_consumers == set()

    # A later poll (a fresh reading) is still answered — no permanent drop.
    await ct._handle_request(_poll(mac), addr, transport)
    assert len(transport.sent) == 2


# ---------------------------------------------------------------------------
# Non-finite meter readings (issue #548): a NaN/Inf sample must take the same
# zero-delta hold path as an unavailable meter and leave the stateful
# controller untouched, so control recovers on the next finite reading.
# Before the guard, one NaN poisoned the grid-state predictor permanently
# (every later innovation is NaN) and the pace clamp turned the NaN reading
# into a constant +pace_base_step command until restart.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_non_finite_reading_holds_and_control_recovers(bad: float) -> None:
    clock = FakeClock()
    ct = CT002(ct_mac="", active_control=True, clock=clock)
    grid = [300.0]

    async def before_send(_addr, _fields=None, _consumer_id=None):
        return [grid[0], 0.0, 0.0]

    ct.before_send = before_send
    transport = _RecordingTransport()
    addr = ("192.168.178.134", 22222)

    async def poll() -> list[str]:
        clock.now += 15.0
        await ct._handle_request(_poll("02b250b26777", power=0), addr, transport)
        fields, err = parse_request(transport.sent[-1])
        assert err is None
        return fields

    # Warm poll: import drives a positive (discharge) target.
    r = await poll()
    assert int(r[4]) > 0

    # The meter glitches: a non-finite sample answers with a zero-delta hold.
    grid[0] = bad
    r = await poll()
    assert [r[i] for i in (4, 5, 6, 7)] == ["0", "0", "0", "0"]

    # The meter recovers with an export reading: control resumes and steers
    # negative — a poisoned predictor kept this pinned at +pace_base_step.
    grid[0] = -300.0
    r = await poll()
    assert int(r[4]) < 0


def test_values_finite_helper() -> None:
    assert _values_finite([1, 2.5, "300"]) is True
    assert _values_finite([]) is True
    assert _values_finite([float("nan")]) is False
    assert _values_finite([1.0, float("inf")]) is False
    assert _values_finite(["abc"]) is False
    assert _values_finite([None]) is False
    assert _values_finite([10**400]) is False  # float() raises OverflowError
