"""End-to-end: real HA Core + a real Shelly emulator + a UDP battery poller.

The Shelly companion to ``test_e2e.py``. No mocks on the device path: the
integration sets up a real ``astrameter.shelly.shelly.Shelly`` emulator on an
ephemeral UDP port, a minimal Marstek-battery emulator polls it with Shelly
Gen2 RPC ``EM.GetStatus`` over loopback, and we assert the RPC response carries
the grid power and that native Shelly-battery entities appear with that value.

Unlike CT002 (which has a production ``BatterySimulator`` reused here), there is
no production Shelly battery simulator, so the tiny UDP poller lives with this
test.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from custom_components.astrameter import const
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.astrameter_e2e


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ResponseCollector(asyncio.DatagramProtocol):
    """Resolves the in-flight poll's future with the next datagram received."""

    def __init__(self) -> None:
        self.pending: asyncio.Future[bytes] | None = None

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if self.pending is not None and not self.pending.done():
            self.pending.set_result(data)


class ShellyBatterySimulator:
    """Minimal Marstek battery that polls a Shelly EM over UDP.

    Each :meth:`poll` sends one Shelly Gen2 RPC ``EM.GetStatus`` datagram to the
    emulator and returns ``total_act_power`` from the response (or ``None`` on
    timeout) — the same exchange a real battery drives against a Shelly meter.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._req_id = 0
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _ResponseCollector | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            _ResponseCollector,
            local_addr=("127.0.0.1", 0),
            family=socket.AF_INET,
        )
        self._transport = transport
        self._protocol = protocol

    async def poll(self, timeout: float = 2.0) -> float | None:
        assert self._transport is not None and self._protocol is not None
        self._req_id += 1
        self._protocol.pending = asyncio.get_running_loop().create_future()
        req = {
            "id": self._req_id,
            "src": "battery-sim",
            "method": "EM.GetStatus",
            "params": {"id": 0},
        }
        self._transport.sendto(json.dumps(req).encode(), (self._host, self._port))
        try:
            data = await asyncio.wait_for(self._protocol.pending, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        return json.loads(data.decode()).get("result", {}).get("total_act_power")

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()


async def _drive(
    hass: HomeAssistant, sim: ShellyBatterySimulator, steps: int = 12
) -> float | None:
    """Poll the Shelly emulator while varying the grid sensor in the background.

    A real grid sensor changes continuously; the push-based HAStatePowermeter's
    ``wait_for_next_message`` would otherwise stall on a static value until the
    2s cap, past the poller's recv timeout.
    """
    stop = asyncio.Event()

    async def _vary() -> None:
        i = 0
        while not stop.is_set():
            hass.states.async_set("sensor.grid_power", str(250 + (i % 2)))
            i += 1
            await asyncio.sleep(0.1)

    vary = asyncio.create_task(_vary())
    last: float | None = None
    try:
        for _ in range(steps):
            result = await sim.poll()
            if result is not None:
                last = result
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await vary
    return last


async def test_shelly_e2e_entities(hass: HomeAssistant, socket_enabled) -> None:
    port = _free_udp_port()
    hass.states.async_set("sensor.grid_power", "250")

    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id=f"shelly_{port}",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_SHELLY_PRO_3EM_NEW,
            const.CONF_UDP_PORT: port,
            const.CONF_DEVICE_ID: f"shelly_{port}",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.recoverable is False or entry.state.name == "LOADED"

    sim = ShellyBatterySimulator("127.0.0.1", port)
    await sim.start()
    try:
        total = await _drive(hass, sim)
    finally:
        sim.close()
    await hass.async_block_till_done()

    assert total is not None, "battery never received a Shelly EM.GetStatus response"
    assert float(total) == pytest.approx(250.0, abs=1.5)

    # A Shelly battery device and its primary grid-power sensor should now exist.
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    grid_ids = [
        e.entity_id
        for e in entries
        if e.unique_id.endswith("_grid_power_total") and "shelly_battery" in e.unique_id
    ]
    assert grid_ids, "shelly battery grid_power_total sensor not created"
    state = hass.states.get(grid_ids[0])
    assert state is not None, "shelly battery grid_power_total has no state"
    assert float(state.state) == pytest.approx(250.0, abs=1.5)

    # Shelly batteries are read-only: the per-battery entity set carries no
    # controllable switch (unlike a CT002 consumer's Active/Auto switches).
    switch_ids = [
        e.entity_id
        for e in entries
        if e.entity_id.startswith("switch.") and "shelly_battery" in e.unique_id
    ]
    assert not switch_ids, "shelly battery should expose no switch entities"

    # Unload cleanly.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
