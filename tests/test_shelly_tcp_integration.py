"""The emulator's own rules for HTTP clients, and its start-up stages.

Three behaviours here are the ones a user would notice if they broke: a
smart-home integration must not appear on the dashboard as a battery, a client
that goes away must not leak per-client state on a LAN-facing port, and no
single failed bind may take the whole device down.
"""

from __future__ import annotations

import asyncio
import time
from ipaddress import IPv4Network
from typing import Any

import pytest

from astrameter.config.config_loader import ClientFilter
from astrameter.config.settings import ConfiguredPowermeter, ShellySettings
from astrameter.powermeter import Powermeter
from astrameter.shelly import rpc
from astrameter.shelly.identity import ShellyIdentity
from astrameter.shelly.rpc import ShellyRpcError
from astrameter.shelly.shelly import MAX_TCP_WAITERS_PER_BATTERY, Shelly

IDENTITY = ShellyIdentity(
    mac="B827EB364242",
    instance="ShellyPro3EM-B827EB364242",
    hostname="ShellyPro3EM-B827EB364242",
    shelly_id="shellypro3em-b827eb364242",
)
ANY_CLIENT = ClientFilter([IPv4Network("0.0.0.0/0")])


class StubMeter(Powermeter):
    """A meter under the test's control, including how long a read takes."""

    def __init__(
        self, values: list[float] | None = None, *, fail: Exception | None = None
    ) -> None:
        self.values = values if values is not None else [300.0, 0.0, -120.0]
        self.fail = fail
        self.reads = 0
        self.gate: asyncio.Event | None = None

    async def get_powermeter_watts(self) -> list[float]:
        self.reads += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return list(self.values)


def build_emulator(
    meter: StubMeter | None = None,
    *,
    settings: ShellySettings | None = None,
    owns_tcp: bool = True,
    client_filter: ClientFilter = ANY_CLIENT,
) -> Shelly:
    sources = [ConfiguredPowermeter(meter, client_filter, False)] if meter else []
    return Shelly(
        powermeters=sources,
        udp_port=0,
        device_id="shellypro3em-ec4609c439c1",
        device_type="shellypro3em_new",
        identity=IDENTITY,
        settings=settings or ShellySettings(tcp_port=0),
        owns_tcp=owns_tcp,
    )


async def test_a_meter_surface_marks_the_client_as_a_battery() -> None:
    emulator = build_emulator(StubMeter())
    await emulator._read_for_http("10.0.0.5", "em.getstatus")
    snapshot = emulator.status_snapshot()
    assert [battery.ip for battery in snapshot.batteries] == ["10.0.0.5"]
    assert snapshot.batteries[0].transport == "tcp"


async def test_a_composite_poll_does_not_make_a_battery() -> None:
    """The rule that keeps a smart-home integration off the battery list.

    Home Assistant polls ``Shelly.GetStatus`` every 60 s once it has added the
    device. Counting that as a battery would put the Home Assistant host on
    the dashboard, and publish an MQTT battery device for it.
    """
    emulator = build_emulator(StubMeter())
    events: list[tuple[str, dict[str, Any]]] = []
    emulator.event_listener = lambda _dev, ip, data: events.append((ip, data))

    await emulator._read_for_http("10.0.0.9", "shelly.getstatus")
    await emulator._read_for_http("10.0.0.9", "shelly.getcomponents")
    await emulator._read_for_http("10.0.0.9", "/shelly")

    assert emulator.status_snapshot().batteries == ()
    assert events == []


async def test_a_meter_surface_emits_the_grid_power_event() -> None:
    emulator = build_emulator(StubMeter())
    events: list[tuple[str, dict[str, Any]]] = []
    emulator.event_listener = lambda _dev, ip, data: events.append((ip, data))
    await emulator._read_for_http("10.0.0.5", "em.getstatus")
    assert len(events) == 1
    ip, payload = events[0]
    assert ip == "10.0.0.5"
    assert payload["grid_power"]["total"] == pytest.approx(180.0)
    assert payload["battery_count"] == 1


async def test_both_transports_send_the_same_event_shape() -> None:
    """One payload builder, so the two transports cannot drift apart."""
    emulator = build_emulator(StubMeter())
    events: list[dict[str, Any]] = []
    emulator.event_listener = lambda _dev, _ip, data: events.append(data)
    await emulator._read_for_http("10.0.0.5", "em.getstatus")
    emulator._emit_grid_power_event("10.0.0.6", [300.0, 0.0, -120.0], 1.0)
    assert set(events[0]) == set(events[1])


async def test_a_battery_on_both_transports_reports_both() -> None:
    emulator = build_emulator(StubMeter())
    emulator._track_battery_seen("10.0.0.5", "udp")
    emulator._track_battery_seen("10.0.0.5", "tcp")
    assert emulator.status_snapshot().batteries[0].transport == "udp+tcp"


async def test_an_unmatched_client_is_told_no_data_is_available() -> None:
    """A ``NETMASK`` that does not cover the caller is a configuration error.

    The UDP path drops the datagram; an HTTP client needs an answer, and the
    cause is indistinguishable from an outage from its side.
    """
    emulator = build_emulator(
        StubMeter(), client_filter=ClientFilter([IPv4Network("192.168.99.0/24")])
    )
    with pytest.raises(ShellyRpcError) as raised:
        await emulator._read_for_http("10.0.0.5", "em.getstatus")
    assert raised.value.code == 1


async def test_a_raising_meter_becomes_the_documented_error() -> None:
    """Any read failure is code 1, exactly as on the UDP path.

    Without this the composite methods would answer 500 and a smart-home
    integration could not add the device at all during a meter outage.
    """
    emulator = build_emulator(StubMeter(fail=TimeoutError("no answer")))
    with pytest.raises(ShellyRpcError) as raised:
        await emulator._read_for_http("10.0.0.5", "em.getstatus")
    assert raised.value.code == 1
    assert raised.value.message == rpc.NO_POWER_DATA


async def test_an_empty_reading_is_not_served_as_zero() -> None:
    """A meter that has not produced its first reading is unavailable.

    Serving zeros instead would read as a real measurement of no load, on the
    value a battery steers on.
    """
    emulator = build_emulator(StubMeter([]))
    with pytest.raises(ShellyRpcError) as raised:
        await emulator._read_for_http("10.0.0.5", "em.getstatus")
    assert raised.value.code == 1


async def test_a_single_phase_reading_is_not_an_error() -> None:
    """Short but non-empty is a legitimate single-phase meter."""
    emulator = build_emulator(StubMeter([250.0]))
    assert await emulator._read_for_http("10.0.0.5", "em.getstatus") == [250.0]


async def test_over_the_cap_a_meter_surface_is_shed() -> None:
    """A backlog is shed rather than queued without limit."""
    meter = StubMeter()
    meter.gate = asyncio.Event()
    emulator = build_emulator(meter)

    parked = [
        asyncio.create_task(emulator._read_for_http("10.0.0.5", "em.getstatus"))
        for _ in range(MAX_TCP_WAITERS_PER_BATTERY)
    ]
    await asyncio.sleep(0)
    with pytest.raises(ShellyRpcError) as raised:
        await emulator._read_for_http("10.0.0.5", "em.getstatus")
    assert raised.value.code == 1
    assert raised.value.retry_after == 1

    meter.gate.set()
    await asyncio.gather(*parked)


async def test_over_the_cap_a_composite_still_answers() -> None:
    """The cap must not turn into a failed device setup.

    An over-cap composite takes no slot and does not wait: it degrades to a
    200 with the measurements nulled, which is what keeps a smart-home
    integration able to add the device under load.
    """
    meter = StubMeter()
    meter.gate = asyncio.Event()
    emulator = build_emulator(meter)

    parked = [
        asyncio.create_task(emulator._read_for_http("10.0.0.5", "em.getstatus"))
        for _ in range(MAX_TCP_WAITERS_PER_BATTERY)
    ]
    await asyncio.sleep(0)
    assert await emulator._read_for_http("10.0.0.5", "shelly.getstatus") is None
    # The refusal took no slot, so it cannot itself deepen the queue.
    assert emulator._battery_waiters["10.0.0.5"] == MAX_TCP_WAITERS_PER_BATTERY

    meter.gate.set()
    await asyncio.gather(*parked)


async def test_one_clients_burst_is_answered_from_one_reading() -> None:
    """Serialised per client, as the UDP path already coalesces.

    A battery runs a closed-loop controller against the value we report, so
    answering a burst from one pre-adjustment reading several times winds it
    past target.
    """
    meter = StubMeter()
    emulator = build_emulator(meter)
    await asyncio.gather(
        *(emulator._read_for_http("10.0.0.5", "em.getstatus") for _ in range(3))
    )
    assert meter.reads == 3
    assert emulator._battery_waiters["10.0.0.5"] == 0


async def test_per_client_state_is_pruned_once_a_client_goes_quiet() -> None:
    """Remotely reachable state must not grow without bound.

    A client that only ever calls the reading-free methods never appears in
    the battery list, so a sweep driven from that list would never reach it —
    which on a LAN-facing port is a leak anyone could trigger.
    """
    emulator = build_emulator(StubMeter())
    for index in range(20):
        await emulator._read_for_http(f"10.0.0.{index}", "shelly.getstatus")
    assert len(emulator._http_client_last_touch) == 20
    assert emulator.status_snapshot().batteries == ()

    emulator._prune_http_client_state(time.time() + 600)
    assert emulator._http_client_last_touch == {}
    assert dict(emulator._battery_waiters) == {}
    assert dict(emulator._battery_locks) == {}


async def test_pruning_leaves_a_client_with_a_request_in_flight() -> None:
    """Dropping a held lock would let a second waiter straight through."""
    meter = StubMeter()
    meter.gate = asyncio.Event()
    emulator = build_emulator(meter)
    parked = asyncio.create_task(emulator._read_for_http("10.0.0.5", "em.getstatus"))
    await asyncio.sleep(0)

    emulator._prune_http_client_state(time.time() + 600)
    assert "10.0.0.5" in emulator._http_client_last_touch

    meter.gate.set()
    await parked


async def test_pruning_tolerates_a_client_with_no_lock_entry() -> None:
    """A client refused before the cap check has only a touch entry.

    Deleting from a ``defaultdict`` raises on a missing key, and this sweep
    runs in the same task as the battery eviction — so getting it wrong would
    silently stop that too.
    """
    emulator = build_emulator(
        StubMeter(), client_filter=ClientFilter([IPv4Network("192.168.99.0/24")])
    )
    with pytest.raises(ShellyRpcError):
        await emulator._read_for_http("10.0.0.5", "em.getstatus")
    assert "10.0.0.5" in emulator._http_client_last_touch
    assert "10.0.0.5" not in emulator._battery_locks

    emulator._prune_http_client_state(time.time() + 600)
    assert emulator._http_client_last_touch == {}


async def test_the_http_surface_comes_up_and_serves() -> None:
    emulator = build_emulator(StubMeter())
    await emulator.start()
    try:
        snapshot = emulator.status_snapshot()
        assert snapshot.tcp_running is True
        assert snapshot.tcp_port and snapshot.tcp_port > 0
        assert snapshot.mac == "B827EB364242"
        assert snapshot.shelly_id == "shellypro3em-b827eb364242"
        assert snapshot.mdns_hostname == "ShellyPro3EM-B827EB364242"
    finally:
        await emulator.stop()


async def test_a_failed_udp_bind_still_brings_up_the_http_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stage may abort a later one.

    The privileged UDP port is exactly the bind that fails on a non-root
    install, and it used to take the whole device — HTTP and discovery
    included — down with it.
    """
    from astrameter.shelly import shelly as shelly_module

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(shelly_module.UdpServer, "serve", refuse)
    emulator = build_emulator(StubMeter())
    await emulator.start()
    try:
        snapshot = emulator.status_snapshot()
        assert snapshot.running is False
        assert snapshot.tcp_running is True
    finally:
        await emulator.stop()


async def test_the_eviction_sweep_runs_without_a_udp_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Liveness is shared, so its sweep cannot be tied to the UDP bind.

    Otherwise an HTTP-only battery would be tracked forever: never evicted,
    never marked inactive, never emitting its removal event.
    """
    from astrameter.shelly import shelly as shelly_module

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(shelly_module.UdpServer, "serve", refuse)
    emulator = build_emulator(StubMeter())
    await emulator.start()
    try:
        assert emulator._inactive_check_task is not None
        assert not emulator._inactive_check_task.done()
    finally:
        await emulator.stop()


async def test_a_disabled_http_port_still_announces_and_watches() -> None:
    """Discovery is the point even with no HTTP surface.

    Some consumers discover over mDNS and then poll the UDP port, so the
    announcement has to survive ``TCP_PORT = -1`` — and the address watchdog
    has to keep running, or the advertised address freezes.
    """
    emulator = build_emulator(
        StubMeter(), settings=ShellySettings(tcp_port=-1, mdns_enabled=False)
    )
    await emulator.start()
    try:
        snapshot = emulator.status_snapshot()
        assert snapshot.tcp_running is False
        assert snapshot.tcp_port is None
        assert emulator._announce_task is not None
        assert not emulator._announce_task.done()
    finally:
        await emulator.stop()


async def test_a_non_owner_reports_no_http_or_mdns_fields() -> None:
    """Every default install has one: the pair is two devices, one identity."""
    emulator = build_emulator(StubMeter(), owns_tcp=False)
    await emulator.start()
    try:
        snapshot = emulator.status_snapshot()
        assert snapshot.tcp_port is None
        assert snapshot.tcp_running is False
        assert snapshot.mdns_registered is False
        assert snapshot.mac is None
        assert snapshot.shelly_id is None
        assert snapshot.mdns_hostname is None
        assert snapshot.mdns_instance is None
        assert snapshot.mdns_ip is None
    finally:
        await emulator.stop()


async def test_stop_is_safe_after_a_partial_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrameter.shelly import shelly as shelly_module

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(shelly_module.UdpServer, "serve", refuse)
    emulator = build_emulator(
        StubMeter(), settings=ShellySettings(tcp_port=-1, mdns_enabled=False)
    )
    await emulator.start()
    await emulator.stop()
    await emulator.stop()


async def test_the_two_transports_agree_on_the_same_reading() -> None:
    """One method, one number, whichever way it was asked.

    The encodings differ — the UDP path rounds to one decimal and nudges the
    last digit — so this compares the values, not the bytes.
    """
    emulator = build_emulator(StubMeter())
    udp = emulator._create_em1_response(1, [300.0, 0.0, -120.0])
    identity = IDENTITY
    ctx = rpc.RequestContext(
        identity=identity,
        profile=rpc.PROFILES["shellypro3em"],
        udp_port=1010,
        announced_ip="192.168.1.50",
        settings=rpc.SettingsStore(),
        energy=rpc.EnergyCounters(now=time.time),
        now=time.time(),
        started_at=time.time(),
    )
    tcp = rpc.em1_get_status(ctx, {}, [300.0, 0.0, -120.0])
    assert abs(tcp["act_power"] - udp["result"]["act_power"]) < 0.01
