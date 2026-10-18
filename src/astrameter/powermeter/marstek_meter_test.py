import asyncio
import logging
from typing import cast

import pytest

from astrameter.ct002.protocol import build_payload, parse_request
from astrameter.powermeter import marstek_meter
from astrameter.powermeter.marstek_meter import (
    CT_TYPES,
    READER_DEVICE_TYPE,
    MarstekMeter,
    default_reader_mac,
)

READER_MAC = "02a57e123456"
CT_MAC = "aabbccddeeff"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reply(
    phases: tuple[int, int, int] = (100, -200, 50),
    *,
    ct_type: str = "HME-4",
    ct_mac: str = CT_MAC,
    battery_mac: str = READER_MAC,
    counts: tuple[int, int, int, int] = (0, 0, 0, 0),
    energy: bool = False,
) -> bytes:
    fields = [ct_type, ct_mac, READER_DEVICE_TYPE, battery_mac]
    fields += [str(p) for p in phases] + [str(sum(phases))]
    fields += [str(c) for c in counts] + ["-50", "7"] + ["0"] * 10
    if energy:
        fields += ["78600", "0", "482600", "0"]
    return bytes(build_payload(fields))


def _meter(**kwargs: object) -> MarstekMeter:
    kwargs.setdefault("reader_mac", READER_MAC)
    return MarstekMeter("192.0.2.10", **kwargs)  # type: ignore[arg-type]


def _request_fields(meter: MarstekMeter) -> list[str]:
    fields, error = parse_request(meter.build_request())
    assert fields is not None, error
    return fields


def test_request_is_an_unassigned_idle_battery_that_does_not_participate() -> None:
    fields = _request_fields(_meter())
    assert fields == [
        READER_DEVICE_TYPE,
        READER_MAC,
        "HME-4",
        "000000000000",
        "0",
        "0",
        "0",
    ]


def test_cycles_through_ct_types_until_one_answers() -> None:
    meter = _meter()
    sent = [_request_fields(meter)[2] for _ in range(4)]
    assert sent == [*CT_TYPES, CT_TYPES[0]]

    meter._handle_reply(_reply(ct_type="TPM2-0"))

    assert _request_fields(meter)[2:4] == ["TPM2-0", CT_MAC]
    assert _request_fields(meter)[2] == "TPM2-0"


def test_configured_type_and_mac_are_sent_as_given() -> None:
    meter = _meter(ct_type="HME-3", mac="AA:BB:CC:DD:EE:FF")
    assert _request_fields(meter)[2:4] == ["HME-3", CT_MAC]


async def test_reads_the_phase_powers() -> None:
    meter = _meter()
    meter._handle_reply(_reply((120, -340, 5), energy=True))
    await meter.wait_for_message(timeout=0.1)
    assert await meter.get_powermeter_watts() == [120.0, -340.0, 5.0]


def test_ignores_replies_for_another_battery() -> None:
    meter = _meter()
    meter._handle_reply(_reply(battery_mac="112233445566"))
    assert meter.values is None


def test_ignores_another_ct_once_one_is_known() -> None:
    meter = _meter(mac=CT_MAC)
    meter._handle_reply(_reply((1, 2, 3), ct_mac="001122334455"))
    assert meter.values is None
    meter._handle_reply(_reply((1, 2, 3)))
    assert meter.values == [1.0, 2.0, 3.0]


@pytest.mark.parametrize("bad", ["", "12a", "1.5"])
def test_a_garbled_phase_power_is_no_reading(bad: str) -> None:
    meter = _meter()
    fields, _ = parse_request(_reply())
    assert fields is not None
    fields[5] = bad
    meter._handle_reply(bytes(build_payload(fields)))
    assert meter.values is None
    assert meter.stream_online() is False


@pytest.mark.parametrize(
    "ct_mac",
    [
        "000000000000",  # an emulator without CT_MAC echoes the wildcard
        "02b250123456",  # a CT registered by AstraMeter in the Marstek cloud
        "0a0b0c0d0e0f",  # the CT_MAC configured for the emulator
    ],
)
def test_refuses_replies_from_an_astrameter_emulator(
    ct_mac: str, caplog: pytest.LogCaptureFixture
) -> None:
    meter = _meter(own_ct_macs=["0A:0B:0C:0D:0E:0F"])
    with caplog.at_level(logging.ERROR, logger="astrameter"):
        meter._handle_reply(_reply(ct_mac=ct_mac))
        meter._handle_reply(_reply(ct_mac=ct_mac))
    assert meter.values is None
    assert len(caplog.records) == 1
    assert "not a real CT" in caplog.records[0].getMessage()


def test_rejects_a_mac_shared_with_the_emulated_ct() -> None:
    with pytest.raises(ValueError, match="emulated CT"):
        _meter(mac=CT_MAC, own_ct_macs=[CT_MAC.upper()])


def test_requires_an_ip() -> None:
    with pytest.raises(ValueError, match="IP"):
        MarstekMeter("")


async def test_a_stale_reading_is_an_outage() -> None:
    clock = _Clock()
    meter = _meter(clock=clock, max_age_seconds=10)
    with pytest.raises(ValueError, match="No reply"):
        await meter.get_powermeter_watts()
    assert meter.stream_online() is False

    meter._handle_reply(_reply())
    assert meter.stream_online() is True
    clock.now += 11
    assert meter.stream_online() is False
    with pytest.raises(ValueError, match="over 10 s"):
        await meter.get_powermeter_watts()


class _SentRequests:
    def __init__(self) -> None:
        self.requests: list[bytes] = []

    def sendto(self, data: bytes) -> None:
        self.requests.append(data)


async def test_a_waiting_reader_asks_at_once_but_no_faster_than_the_ct_answers() -> (
    None
):
    clock = _Clock()
    meter = _meter(clock=clock)
    sent = _SentRequests()
    meter._transport = sent  # type: ignore[assignment]

    first = asyncio.create_task(meter.wait_for_next_message(timeout=1))
    await asyncio.sleep(0)
    assert len(sent.requests) == 1

    clock.now += 0.4
    second = asyncio.create_task(meter.wait_for_next_message(timeout=1))
    await asyncio.sleep(0)
    assert len(sent.requests) == 1, "asked again within the CT's pause"

    meter._handle_reply(_reply())
    await asyncio.gather(first, second)

    clock.now += 0.5
    third = asyncio.create_task(meter.wait_for_next_message(timeout=1))
    await asyncio.sleep(0)
    assert len(sent.requests) == 2
    meter._handle_reply(_reply())
    await third


def test_warns_once_about_batteries_still_on_the_real_ct(
    caplog: pytest.LogCaptureFixture,
) -> None:
    meter = _meter()
    with caplog.at_level(logging.WARNING, logger="astrameter"):
        meter._handle_reply(_reply(counts=(1, 0, 1, 0)))
        meter._handle_reply(_reply(counts=(1, 0, 1, 0)))
    assert [r.getMessage() for r in caplog.records] == [
        "Marstek meter: 2 battery/batteries still use the real CT at 192.0.2.10 "
        "directly. Pair them with AstraMeter's CT instead, or both will steer them."
    ]


def test_reader_mac_is_stable_and_reserved() -> None:
    assert default_reader_mac() == default_reader_mac()
    assert default_reader_mac().startswith("02a57e")
    assert len(default_reader_mac()) == 12


@pytest.mark.parametrize("ip", ["127.0.0.1", "0.0.0.0"])
async def test_refuses_to_poll_this_machine(ip: str) -> None:
    meter = MarstekMeter(ip)
    with pytest.raises(ValueError, match="this machine"):
        await meter.start()


class _FakeCt(asyncio.DatagramProtocol):
    """A CT that answers only requests naming its type, as the firmware does."""

    def __init__(self, ct_type: str) -> None:
        self.ct_type = ct_type
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # A cast, not an isinstance check: before Python 3.12 the selector
        # loop's UDP transport is not a DatagramTransport subclass.
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        fields, _ = parse_request(data)
        if fields is None or fields[2] != self.ct_type:
            return
        assert self.transport is not None
        self.transport.sendto(
            _reply((10, 20, 30), ct_type=self.ct_type, battery_mac=fields[1]), addr
        )


async def test_detects_the_ct_over_a_real_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    ct_transport, _ = await loop.create_datagram_endpoint(
        lambda: _FakeCt("HME-3"), local_addr=("127.0.0.1", 0)
    )
    monkeypatch.setattr(
        marstek_meter, "CT_PORT", ct_transport.get_extra_info("sockname")[1]
    )
    monkeypatch.setattr(marstek_meter, "_is_own_address", lambda peer, local: False)
    meter = MarstekMeter("127.0.0.1", poll_interval=0.1)
    await meter.start()
    try:
        await meter.wait_for_message(timeout=5)
        assert await meter.get_powermeter_watts() == [10.0, 20.0, 30.0]
        assert _request_fields(meter)[2:4] == ["HME-3", CT_MAC]
    finally:
        await meter.stop()
        ct_transport.close()
