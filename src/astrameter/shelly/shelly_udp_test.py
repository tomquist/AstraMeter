import asyncio
import json
import socket
from ipaddress import IPv4Network

from astrameter.config import ClientFilter
from astrameter.powermeter import Powermeter, ThrottledPowermeter
from astrameter.request_dedupe import RequestDeduplicator
from astrameter.shelly.shelly import Shelly, _ShellyProtocol


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


async def test_multiple_requests_with_throttling():
    dummy = DummyPowermeter()
    pm = ThrottledPowermeter(dummy, throttle_interval=0.2)
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])

    shelly = Shelly([(pm, cf, True)], udp_port=0, device_id="test")
    await shelly.start()
    port = shelly.udp_port
    assert port != 0, "Shelly should have bound to an actual port"
    try:
        # Prime the throttle with an initial fetch so subsequent
        # concurrent requests coalesce on the same reading.
        resp = await _send_req(port, 99)
        assert resp == 99
        initial_calls = dummy.call_count

        responses = []
        errors = []

        async def send_req(i):
            try:
                resp_id = await _send_req(port, i)
                responses.append(resp_id)
            except TimeoutError:
                errors.append(f"timeout for request id={i}")

        await asyncio.gather(*(send_req(i) for i in range(3)))

        assert errors == []
        assert sorted(responses) == [0, 1, 2]
        # All 3 concurrent requests should coalesce into a single fetch.
        assert dummy.call_count - initial_calls <= 1, (
            f"Expected at most 1 powermeter fetch for 3 concurrent requests, "
            f"got {dummy.call_count - initial_calls}"
        )
    finally:
        await shelly.stop()


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
    def __init__(self):
        self.received: asyncio.Future = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, addr):
        if not self.received.done():
            self.received.set_result(data)

    def error_received(self, exc):
        if not self.received.done():
            self.received.set_exception(exc)


def _make_shelly(netmask="127.0.0.1/32"):
    dummy = DummyPowermeter()
    cf = ClientFilter([IPv4Network(netmask)])
    return Shelly([(dummy, cf, False)], udp_port=0, device_id="test")


async def test_error_received_is_ignored():
    # Transient UDP errors (e.g. ICMP port unreachable) must be swallowed,
    # not raised, so the endpoint stays open for the other batteries.
    shelly = _make_shelly()
    proto = _ShellyProtocol(shelly)
    proto.connection_made(_FakeTransport())
    assert proto.error_received(OSError("port unreachable")) is None


async def test_connection_lost_with_error_rebinds_and_keeps_serving():
    # An unexpected transport loss should trigger a rebind so the emulator
    # keeps answering instead of going permanently deaf (issue #404).
    shelly = _make_shelly()
    await shelly.start()
    try:
        bound_port = shelly.udp_port
        protocol = shelly._protocol
        assert protocol is not None

        # Simulate asyncio closing the endpoint after a socket error.
        protocol.connection_lost(OSError("boom"))
        assert shelly._rebind_task is not None
        await shelly._rebind_task

        # Rebound to the same port with a fresh protocol object.
        assert shelly.udp_port == bound_port
        assert shelly._transport is not None
        assert shelly._protocol is not None
        assert shelly._protocol is not protocol

        # The fresh endpoint still answers requests over real UDP.
        assert await _send_req(bound_port, 7) == 7
    finally:
        await shelly.stop()


async def test_connection_lost_clean_does_not_rebind():
    # A clean close (exc is None, e.g. our own stop()) must not rebind.
    shelly = _make_shelly()
    await shelly.start()
    protocol = shelly._protocol
    assert protocol is not None
    protocol.connection_lost(None)
    assert shelly._rebind_task is None
    await shelly.stop()


async def test_stop_cancels_pending_rebind():
    # stop() must flag closing and cancel any in-flight rebind task.
    shelly = _make_shelly()
    await shelly.start()
    protocol = shelly._protocol
    assert protocol is not None
    protocol.connection_lost(OSError("boom"))
    assert shelly._rebind_task is not None
    await shelly.stop()
    assert shelly._rebind_task is None
    assert shelly._closing is False
