import asyncio
import json
import logging
import socket
from ipaddress import IPv4Network

from astrameter.config import ClientFilter
from astrameter.powermeter import Powermeter, ThrottledPowermeter
from astrameter.request_dedupe import RequestDeduplicator
from astrameter.shelly.shelly import Shelly


class DummyPowermeter(Powermeter):
    def __init__(self):
        self.call_count = 0

    async def get_powermeter_watts(self):
        self.call_count += 1
        return [1.0]

    async def get_powermeter_watts_raw(self):
        # Same physical reading as get_powermeter_watts; do not bump call_count so
        # throttling/dedupe tests that only observe get_powermeter_watts stay stable.
        return [1.0]


class FailingPowermeter(Powermeter):
    async def get_powermeter_watts(self):
        raise TimeoutError("Connection timeout to host http://192.168.2.17/")


class GatedPowermeter(Powermeter):
    """A meter whose read parks until ``gate`` is set — models a slow/throttled
    read or WAIT_FOR_NEXT_MESSAGE holding the handler so concurrent polls pile
    up behind it."""

    def __init__(self):
        self.gate = asyncio.Event()
        self.call_count = 0

    async def get_powermeter_watts(self):
        self.call_count += 1
        await self.gate.wait()
        return [42.0]


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, data: bytes, addr: tuple) -> None:
        self.sent.append((data, addr))


async def test_dedupe_window_drops_rapid_duplicates():
    # Drive the handler directly with a fake transport and a fake clock so
    # the test is independent of wall-clock time and real UDP delivery.
    dummy = DummyPowermeter()
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])
    shelly = Shelly(
        [(dummy, cf, False)],
        udp_port=0,
        device_id="test",
        dedupe_time_window=10.0,
    )
    clock = _FakeClock()
    shelly._dedup = RequestDeduplicator(10.0, clock=clock)

    transport = _FakeTransport()
    req = json.dumps(
        {"id": 1, "src": "cli", "method": "EM.GetStatus", "params": {"id": 0}}
    ).encode()
    addr = ("127.0.0.1", 54321)

    # First request: accepted and a response is sent.
    await shelly._handle_request(transport, req, addr)
    assert len(transport.sent) == 1
    assert dummy.call_count == 1

    # Second request within the window: dropped. Same source IP, different
    # port (mirroring real Shelly batteries which use ephemeral ports).
    clock.now = 1.0
    await shelly._handle_request(transport, req, ("127.0.0.1", 54322))
    assert len(transport.sent) == 1
    assert dummy.call_count == 1

    # After the window elapses, requests are answered again.
    clock.now = 11.5
    await shelly._handle_request(transport, req, ("127.0.0.1", 54323))
    assert len(transport.sent) == 2
    assert dummy.call_count == 2


async def test_concurrent_polls_from_one_battery_coalesce_over_udp():
    """End-to-end burst prevention over real UDP: a battery firing several
    polls while a throttled read is in flight gets exactly ONE response, not one
    per poll — repeating the same reading would feed its zero-export loop the
    same stale error before the plant can respond, overshooting target."""
    dummy = DummyPowermeter()
    pm = ThrottledPowermeter(dummy, throttle_interval=0.3)
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])

    shelly = Shelly([(pm, cf, True)], udp_port=0, device_id="test")
    await shelly.start()
    port = shelly.udp_port
    assert port != 0, "Shelly should have bound to an actual port"
    try:
        # Prime the throttle so the burst parks on the throttle window rather
        # than racing the very first fetch.
        assert await _send_req(port, 99) == 99
        initial_calls = dummy.call_count

        responses = []
        timeouts = []

        async def send_req(i):
            try:
                responses.append(await _send_req(port, i, timeout=1.0))
            except TimeoutError:
                timeouts.append(i)

        await asyncio.gather(*(send_req(i) for i in range(3)))

        # Same source IP == same battery: exactly one poll is answered and the
        # duplicates are coalesced away instead of each drawing a response.
        assert len(responses) == 1
        assert len(timeouts) == 2
        # The one answered poll shares a single throttled fetch.
        assert dummy.call_count - initial_calls <= 1
    finally:
        await shelly.stop()


async def test_concurrent_polls_coalesce_to_a_single_response():
    """Four polls from one battery pile up in a parked read; only ONE response
    goes out when the reading lands — not a four-deep burst."""
    pm = GatedPowermeter()
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])
    shelly = Shelly([(pm, cf, False)], udp_port=0, device_id="test")
    transport = _FakeTransport()
    req = json.dumps(
        {"id": 1, "src": "cli", "method": "EM.GetStatus", "params": {"id": 0}}
    ).encode()
    addr = ("127.0.0.1", 54321)

    handlers = [
        asyncio.create_task(shelly._handle_request(transport, req, addr))
        for _ in range(4)
    ]
    await asyncio.sleep(0.05)
    assert transport.sent == []  # every handler parked on the read

    pm.gate.set()  # one fresh reading arrives
    await asyncio.gather(*handlers)

    assert len(transport.sent) == 1  # one response, not four
    assert pm.call_count == 1  # the meter was read once, not four times
    assert shelly._inflight_batteries == set()  # flag cleared for the next read


async def test_coalescing_is_per_battery():
    """Coalescing is keyed per battery IP: two batteries polling at once each
    get their own response; one does not suppress the other."""
    pm = GatedPowermeter()
    cf = ClientFilter([IPv4Network("127.0.0.0/8")])
    shelly = Shelly([(pm, cf, False)], udp_port=0, device_id="test")
    transport = _FakeTransport()
    req = json.dumps(
        {"id": 1, "src": "cli", "method": "EM.GetStatus", "params": {"id": 0}}
    ).encode()

    handlers = []
    for ip in ("127.0.0.1", "127.0.0.2"):
        for _ in range(2):  # two concurrent polls per battery
            handlers.append(
                asyncio.create_task(shelly._handle_request(transport, req, (ip, 5000)))
            )

    await asyncio.sleep(0.05)
    pm.gate.set()
    await asyncio.gather(*handlers)

    assert len(transport.sent) == 2  # one per battery, both answered
    assert shelly._inflight_batteries == set()


async def test_poll_answered_again_after_burst_coalesced():
    """Dropping duplicates is not a lock-out: once the in-flight handler
    responds, the next poll is served normally."""
    pm = GatedPowermeter()
    pm.gate.set()  # reads resolve immediately
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])
    shelly = Shelly([(pm, cf, False)], udp_port=0, device_id="test")
    transport = _FakeTransport()
    req = json.dumps(
        {"id": 1, "src": "cli", "method": "EM.GetStatus", "params": {"id": 0}}
    ).encode()
    addr = ("127.0.0.1", 54321)

    await shelly._handle_request(transport, req, addr)
    assert len(transport.sent) == 1
    assert shelly._inflight_batteries == set()

    await shelly._handle_request(transport, req, addr)
    assert len(transport.sent) == 2


async def test_inflight_flag_cleared_when_meter_read_fails():
    """A failing meter read must still clear the in-flight flag, or the battery
    would be locked out of every future response."""
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])
    shelly = Shelly([(FailingPowermeter(), cf, False)], udp_port=0, device_id="test")
    transport = _FakeTransport()
    req = json.dumps(
        {"id": 1, "src": "cli", "method": "EM.GetStatus", "params": {"id": 0}}
    ).encode()
    addr = ("127.0.0.1", 54321)

    await shelly._handle_request(transport, req, addr)
    assert transport.sent == []  # read failed, no response
    assert shelly._inflight_batteries == set()  # but the flag is cleared


async def _drive_failing_read(caplog, level):
    """Send one request to a Shelly backed by a failing meter and return the
    single captured log record (and the fake transport, to assert no reply)."""
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])
    shelly = Shelly([(FailingPowermeter(), cf, False)], udp_port=0, device_id="test")
    transport = _FakeTransport()
    req = json.dumps(
        {"id": 1, "src": "cli", "method": "EM.GetStatus", "params": {"id": 0}}
    ).encode()

    with caplog.at_level(level, logger="astrameter"):
        await shelly._handle_request(transport, req, ("127.0.0.1", 54321))

    records = [
        r for r in caplog.records if "Could not read meter values" in r.getMessage()
    ]
    assert len(records) == 1
    return records[0], transport


async def test_meter_read_failure_logs_one_liner_without_traceback(caplog):
    record, transport = await _drive_failing_read(caplog, logging.WARNING)
    # No response is sent to the battery when the meter read fails.
    assert transport.sent == []
    assert record.levelno == logging.WARNING
    # At the normal level the traceback is suppressed: just the one-liner.
    assert not record.exc_info
    assert "Connection timeout" in record.getMessage()


async def test_meter_read_failure_includes_traceback_at_debug(caplog):
    record, _ = await _drive_failing_read(caplog, logging.DEBUG)
    # At DEBUG the full traceback is attached.
    assert record.exc_info is not None
    assert record.exc_info[0] is TimeoutError


async def _send_req(port, request_id, timeout=2.0):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _ClientProtocol(),
        local_addr=("127.0.0.1", 0),
        family=socket.AF_INET,
    )
    try:
        req = {
            "id": request_id,
            "src": "cli",
            "method": "EM.GetStatus",
            "params": {"id": 0},
        }
        transport.sendto(json.dumps(req).encode(), ("127.0.0.1", port))
        data = await asyncio.wait_for(protocol.received, timeout=timeout)
        return json.loads(data.decode())["id"]
    finally:
        transport.close()


class _ClientProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: asyncio.Future = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, addr):
        if not self.received.done():
            self.received.set_result(data)

    def error_received(self, exc):
        if not self.received.done():
            self.received.set_exception(exc)
