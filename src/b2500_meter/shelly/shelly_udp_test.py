import asyncio
import json
import socket
from ipaddress import IPv4Network

from b2500_meter.config import ClientFilter
from b2500_meter.powermeter import Powermeter, ThrottledPowermeter
from b2500_meter.shelly.shelly import Shelly


class DummyPowermeter(Powermeter):
    def get_powermeter_watts(self):
        return [1.0]


async def test_multiple_requests_with_throttling():
    pm = ThrottledPowermeter(DummyPowermeter(), throttle_interval=0.2)
    cf = ClientFilter([IPv4Network("127.0.0.1/32")])

    # Find a free port
    tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tmp.bind(("", 0))
    port = tmp.getsockname()[1]
    tmp.close()

    shelly = Shelly([(pm, cf)], udp_port=port, device_id="test")
    await shelly.start()
    try:
        responses = []
        errors = []

        async def send_req(i):
            req = {
                "id": i,
                "src": "cli",
                "method": "EM.GetStatus",
                "params": {"id": 0},
            }
            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _ClientProtocol(),
                local_addr=("127.0.0.1", 0),
                family=socket.AF_INET,
            )
            try:
                transport.sendto(json.dumps(req).encode(), ("127.0.0.1", port))
                data = await asyncio.wait_for(protocol.received, timeout=2.0)
                resp = json.loads(data.decode())
                responses.append(resp["id"])
            except TimeoutError:
                errors.append(f"timeout for request id={i}")
            finally:
                transport.close()

        await asyncio.gather(*(send_req(i) for i in range(3)))

        assert errors == []
        assert sorted(responses) == [0, 1, 2]
    finally:
        await shelly.stop()


class _ClientProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self._loop = asyncio.get_event_loop()
        self.received: asyncio.Future = self._loop.create_future()

    def datagram_received(self, data, addr):
        if not self.received.done():
            self.received.set_result(data)

    def error_received(self, exc):
        if not self.received.done():
            self.received.set_exception(exc)
