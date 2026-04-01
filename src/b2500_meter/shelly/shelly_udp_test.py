import asyncio
import json
import socket
from ipaddress import IPv4Network

from b2500_meter.config import ClientFilter
from b2500_meter.powermeter import Powermeter, ThrottledPowermeter
from b2500_meter.shelly.shelly import Shelly


class DummyPowermeter(Powermeter):
    def __init__(self):
        self.call_count = 0

    async def get_powermeter_watts(self):
        self.call_count += 1
        return [1.0]


async def test_multiple_requests_with_throttling():
    dummy = DummyPowermeter()
    pm = ThrottledPowermeter(dummy, throttle_interval=0.2)
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])

    shelly = Shelly([(pm, cf)], udp_port=0, device_id="test")
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


async def _send_req(port, request_id):
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
        data = await asyncio.wait_for(protocol.received, timeout=2.0)
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
