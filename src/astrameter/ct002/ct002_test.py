import asyncio

import pytest

from astrameter.ct002.ct002 import CT002
from astrameter.ct002.protocol import build_payload


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
