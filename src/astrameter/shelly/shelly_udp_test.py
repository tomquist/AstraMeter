import asyncio
import json
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
