"""End-to-end: the add-on's configuration path, from options.json to the wire.

Starts a stand-in Supervisor (REST + the Home Assistant websocket proxy), boots
AstraMeter the way ``astrameter --addon`` does — add-on options in, no config
file anywhere — and has a battery poll the CT002 emulator over UDP. The reading
that comes back must be the Home Assistant sensor's value.

That covers the wiring the unit tests can only check in pieces: the options are
read, the power source is built against the Supervisor proxy and authenticates
with SUPERVISOR_TOKEN, the emulator is configured from the same options, and a
real datagram gets a real answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
from dataclasses import replace

import pytest
from _ct002_e2e_backend import find_free_ports
from _fake_supervisor import DEFAULT_TOKEN, GRID_SENSOR, SupervisorState, build_app
from aiohttp import web

from astrameter.config.addon import AddonAppConfig, SupervisorClient, load_options
from astrameter.config.settings import CtSettings
from astrameter.ct002.protocol import RESPONSE_LABELS, build_payload, parse_request
from astrameter.main import async_main

SUPERVISOR_TOKEN = DEFAULT_TOKEN
BATTERY_MAC = "AABBCCDDEEFF"
CT_MAC = "112233445566"


class FakeSupervisor:
    """Runs the stand-in Supervisor (see ``_fake_supervisor``) for one test.

    On its own thread and event loop, because the add-on's Supervisor client is
    a blocking one — a test awaiting it on the main loop would otherwise
    deadlock against this server.
    """

    def __init__(self, watts: float = 321.0, mqtt: dict | None = None) -> None:
        self.state = SupervisorState(watts=watts, token=SUPERVISOR_TOKEN, mqtt=mqtt)
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None

    def start(self) -> None:
        started = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, args=(started,), daemon=True
        )
        self._thread.start()
        assert started.wait(10), "fake supervisor did not start"

    def _serve(self, started: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._start_site())
        started.set()
        loop.run_forever()

    async def _start_site(self) -> None:
        self._runner = web.AppRunner(build_app(self.state))
        await self._runner.setup()
        self.port = find_free_ports(2)[1]
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    def stop(self) -> None:
        if self._loop is None:
            return
        assert self._runner is not None
        asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(10)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def unauthorized(self) -> int:
        return self.state.unauthorized

    @property
    def mqtt(self) -> dict | None:
        return self.state.mqtt

    @mqtt.setter
    def mqtt(self, value: dict | None) -> None:
        self.state.mqtt = value


def poll_ct002(port: int, ct_mac: str = CT_MAC, timeout: float = 1.0) -> dict[str, str]:
    """Poll the emulator like a battery does and return the parsed reply.

    A battery addresses its CT by MAC, so *ct_mac* has to be the one the
    emulator was configured with or the poll is ignored.
    """
    request = build_payload(["HMG-50", BATTERY_MAC, "HME-4", ct_mac, "A", "0"])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(request, ("127.0.0.1", port))
        data, _ = sock.recvfrom(2048)
    fields, error = parse_request(data)
    assert error is None, error
    return dict(zip(RESPONSE_LABELS, fields, strict=False))


async def poll_until(
    port: int, predicate, ct_mac: str = CT_MAC, attempts: int = 60
) -> dict[str, str]:
    """Poll until a reply satisfies *predicate*.

    Both ends are still coming up: the emulator may not have bound its socket
    yet (no reply, or a refused datagram), and the first reply can be a hold —
    zeros — while the meter is waiting for its first Home Assistant state.
    """
    reply: dict[str, str] = {}
    for _ in range(attempts):
        try:
            reply = await asyncio.to_thread(poll_ct002, port, ct_mac)
        except OSError:  # not listening yet, or the reply timed out
            await asyncio.sleep(0.25)
            continue
        if predicate(reply):
            return reply
        await asyncio.sleep(0.25)
    raise AssertionError(f"emulator never reported the expected value: {reply}")


class PortOverrideAddonConfig(AddonAppConfig):
    """The add-on backend, listening on a free port.

    The CT's UDP port is not an add-on option (the emulator owns 12345), so
    this is the one setting a test has to place itself; everything else still
    comes from the add-on options.
    """

    def __init__(self, options, supervisor, udp_port: int) -> None:
        super().__init__(options, supervisor)
        self._udp_port = udp_port

    def ct(self, device_type: str) -> CtSettings:
        return replace(super().ct(device_type), udp_port=self._udp_port)


@contextlib.asynccontextmanager
async def running_addon(options: dict, supervisor: FakeSupervisor, udp_port: int):
    """Run AstraMeter with the add-on backend, as ``--addon`` does."""
    config = PortOverrideAddonConfig(
        options,
        SupervisorClient(base_url=supervisor.base_url, token=SUPERVISOR_TOKEN),
        udp_port,
    )
    # main() resolves the Supervisor lookups before the loop starts; a test
    # driving async_main directly has to keep them off its own loop.
    await asyncio.to_thread(config.prefetch)
    # The built-in web server has a fixed port and nothing to do with the
    # configuration path under test.
    general = replace(config.general(), enable_web_server=False)
    task = asyncio.create_task(
        async_main(config, general, ["ct002"], ["device-1"], skip_test=True)
    )
    try:
        yield config, general
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def addon_options(tmp_path, monkeypatch):
    """A realistic /data/options.json, read the way the add-on reads it."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", SUPERVISOR_TOKEN)
    udp_port = find_free_ports(1)[0]

    def write(**overrides) -> dict:
        options = {
            "power_input_alias": GRID_SENSOR,
            "power_output_alias": "",
            "device_types": "ct002",
            "throttle_interval": 0,
            "wait_for_next_message": False,
            "ct_mac": CT_MAC,
            "active_control": False,  # relay mode: the reply carries the meter
            "log_level": "info",
            **overrides,
        }
        path = tmp_path / "options.json"
        path.write_text(json.dumps(options), encoding="utf-8")
        return load_options(str(path))

    write.udp_port = udp_port  # type: ignore[attr-defined]
    return write


@pytest.fixture
def supervisor():
    server = FakeSupervisor(watts=321.0)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.mark.timeout(60)
async def test_addon_serves_the_home_assistant_reading_over_udp(
    addon_options, supervisor
):
    options = addon_options()

    async with running_addon(options, supervisor, addon_options.udp_port):
        reply = await poll_until(
            addon_options.udp_port, lambda r: r["A_phase_power"] != "0"
        )

    assert reply["A_phase_power"] == "321"
    assert reply["meter_mac_code"].lower() == CT_MAC.lower()
    assert reply["meter_dev_type"] == "HME-4"
    # Everything talked to the Supervisor with the add-on's own token.
    assert supervisor.unauthorized == 0


@pytest.mark.timeout(60)
async def test_add_on_options_reach_the_running_emulator(addon_options, supervisor):
    """A tuning option set in the add-on UI changes what the battery is told."""
    options = addon_options(power_offset="100", ct_mac="AABBCCDDEE01")

    async with running_addon(options, supervisor, addon_options.udp_port):
        reply = await poll_until(
            addon_options.udp_port,
            lambda r: r["A_phase_power"] != "0",
            ct_mac="AABBCCDDEE01",
        )

    # 321 W from Home Assistant plus the add-on's power_offset.
    assert reply["A_phase_power"] == "421"
    assert reply["meter_mac_code"].lower() == "aabbccddee01"


async def test_supervisor_endpoints_answer_the_add_on(supervisor):
    """The Supervisor lookups the add-on makes at startup, against a real server."""
    supervisor.mqtt = {
        "host": "core-mosquitto",
        "port": 1883,
        "username": "addons",
        "password": "secret",
        "ssl": False,
    }
    client = SupervisorClient(base_url=supervisor.base_url, token=SUPERVISOR_TOKEN)

    assert await asyncio.to_thread(client.home_assistant_ready) is True
    assert await asyncio.to_thread(client.addon_slug) == "a0ef98c5_b2500_meter"
    service = await asyncio.to_thread(client.mqtt_service)
    assert service["host"] == "core-mosquitto"
    assert supervisor.unauthorized == 0


async def test_a_wrong_token_is_not_silently_accepted(supervisor):
    client = SupervisorClient(base_url=supervisor.base_url, token="wrong")
    assert await asyncio.to_thread(client.home_assistant_ready) is False
    assert supervisor.unauthorized == 1
