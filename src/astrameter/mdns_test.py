"""What the emulated device announces, and how the announcement is maintained.

The record contents are asserted directly off the pure builder, and the
advertiser is driven through a double, so nothing here opens a socket or joins
a multicast group.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from zeroconf import InterfaceChoice, ServiceInfo

from astrameter import mdns
from astrameter.mdns import (
    HTTP_SERVICE_TYPE,
    SHELLY_SERVICE_TYPE,
    MdnsAdvertiser,
    parse_txt_overrides,
    shelly_services,
    txt_records,
)
from astrameter.shelly import rpc
from astrameter.shelly.identity import ShellyIdentity

IDENTITY = ShellyIdentity(
    mac="B827EB364242",
    instance="ShellyPro3EM-B827EB364242",
    hostname="ShellyPro3EM-B827EB364242",
    shelly_id="shellypro3em-b827eb364242",
)
PROFILE = rpc.PROFILES["shellypro3em"]


class FakeZeroconf:
    """Stands in for the responder, recording what it was asked to publish."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.registered: list[ServiceInfo] = []
        self.updated: list[ServiceInfo] = []
        self.unregistered: list[ServiceInfo] = []
        self.interface_updates = 0
        self.closed = False
        self.cooperating: list[bool] = []
        self.zeroconf = self

    async def async_wait_for_start(self) -> None:
        return None

    async def async_register_service(
        self, info: ServiceInfo, cooperating_responders: bool = False
    ) -> None:
        self.registered.append(info)
        self.cooperating.append(cooperating_responders)

    async def async_update_service(self, info: ServiceInfo) -> None:
        self.updated.append(info)

    async def async_unregister_service(self, info: ServiceInfo) -> None:
        self.unregistered.append(info)

    async def async_update_interfaces(self, **kwargs: Any) -> None:
        self.interface_updates += 1

    async def async_close(self) -> None:
        self.closed = True


@pytest.fixture
def services() -> list[mdns.MdnsService]:
    return shelly_services(IDENTITY, PROFILE, 80, "192.168.1.50")


def test_both_service_types_are_announced(
    services: list[mdns.MdnsService],
) -> None:
    """A battery looks for one type; generic browsers use the other."""
    assert [service.type_ for service in services] == [
        SHELLY_SERVICE_TYPE,
        HTTP_SERVICE_TYPE,
    ]


def test_the_service_and_host_names(services: list[mdns.MdnsService]) -> None:
    """The instance label and the SRV target are separate strings.

    They happen to match by default, but they are computed independently so
    either can be pinned without moving the other.
    """
    for service in services:
        assert service.name == f"ShellyPro3EM-B827EB364242.{service.type_}"
        assert service.server == "ShellyPro3EM-B827EB364242.local."
        assert service.port == 80
        assert service.addresses == (socket.inet_aton("192.168.1.50"),)


def test_the_txt_record() -> None:
    """What a consumer reads to decide whether this is a device it wants."""
    assert txt_records(IDENTITY, PROFILE, {}) == {
        "id": "shellypro3em-b827eb364242",
        "mac": "B8:27:EB:36:42:42",
        "gen": "2",
        "app": "shellypro3em",
        "model": "SPEM-003CEBEU",
        "arch": "esp32",
        "fw_id": "20250924-062729/1.7.1-gd336f31",
        "ver": "1.7.1",
    }


def test_the_txt_generation_agrees_with_the_http_surface() -> None:
    """A consumer gating on ``gen`` must get one answer, not two.

    The HTTP surface reports this device as gen 2, so the discovery record has
    to say the same thing — otherwise a consumer that checks both sees a
    device disagreeing with itself.
    """
    records = txt_records(IDENTITY, PROFILE, {})
    assert records["gen"] == str(PROFILE.gen)
    assert records["ver"] == PROFILE.ver
    assert records["id"] == IDENTITY.shelly_id


def test_txt_overrides_replace_defaults() -> None:
    """The escape hatch for a battery that wants a different value."""
    records = txt_records(IDENTITY, PROFILE, {"gen": "3", "app": "Pro3EM"})
    assert records["gen"] == "3"
    assert records["app"] == "Pro3EM"
    # Everything else is untouched.
    assert records["model"] == "SPEM-003CEBEU"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ("gen=3", {"gen": "3"}),
        ("gen=3,app=Pro3EM", {"gen": "3", "app": "Pro3EM"}),
        (" gen = 3 , app = Pro3EM ", {"gen": "3", "app": "Pro3EM"}),
        ("gen=3,,", {"gen": "3"}),
        ("novalue", {}),
        ("=3", {}),
        ("key=with=equals", {"key": "with=equals"}),
    ],
)
def test_parse_txt_overrides(raw: str, expected: dict[str, str]) -> None:
    assert parse_txt_overrides(raw) == expected


def test_an_unusable_address_still_announces_the_service(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Better a service with no address record than no service at all.

    A consumer can still resolve the host name through another responder.
    """
    services = shelly_services(IDENTITY, PROFILE, 80, "not-an-address")
    assert all(service.addresses == () for service in services)


async def test_the_advertiser_registers_both_services(
    services: list[mdns.MdnsService],
) -> None:
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    assert await advertiser.start() is True
    assert advertiser.registered
    fake = advertiser._zc
    assert isinstance(fake, FakeZeroconf)
    assert [info.type for info in fake.registered] == [
        SHELLY_SERVICE_TYPE,
        HTTP_SERVICE_TYPE,
    ]


async def test_registration_cooperates_with_another_responder(
    services: list[mdns.MdnsService],
) -> None:
    """Another responder on this host is expected, not a name conflict.

    Home Assistant runs its own; without this the conflict probe would treat
    it as a clash and rename or refuse our service.
    """
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    await advertiser.start()
    fake = advertiser._zc
    assert isinstance(fake, FakeZeroconf)
    assert fake.cooperating == [True, True]


async def test_the_advertiser_binds_ipv4_across_all_interfaces(
    services: list[mdns.MdnsService],
) -> None:
    """The bind selection is separate from the address advertised.

    Binding the *announced* address would be wrong: it may be an address this
    host does not hold, which yields a responder with no sockets at all.
    """
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    await advertiser.start()
    fake = advertiser._zc
    assert isinstance(fake, FakeZeroconf)
    assert fake.kwargs["interfaces"] is InterfaceChoice.All


async def test_refresh_re_announces_the_new_address(
    services: list[mdns.MdnsService],
) -> None:
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    await advertiser.start()
    await advertiser.refresh("10.0.0.7")
    fake = advertiser._zc
    assert isinstance(fake, FakeZeroconf)
    assert len(fake.updated) == 2
    assert all(
        info.addresses == [socket.inet_aton("10.0.0.7")] for info in fake.updated
    )


async def test_refresh_before_start_is_a_no_op(
    services: list[mdns.MdnsService],
) -> None:
    """The emulator's watchdog ticks whether or not a responder came up."""
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    await advertiser.refresh("10.0.0.7")
    await advertiser.stop()


async def test_interfaces_are_rebuilt_only_when_the_address_set_changes(
    services: list[mdns.MdnsService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interface that disappears and returns leaves stale sockets behind."""
    addresses = {"10.0.0.1"}
    monkeypatch.setattr(mdns, "local_ipv4_set", lambda: frozenset(addresses))
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    await advertiser.start()
    fake = advertiser._zc
    assert isinstance(fake, FakeZeroconf)

    assert await advertiser.refresh_interfaces() is False
    assert fake.interface_updates == 0

    addresses.add("10.0.0.2")
    assert await advertiser.refresh_interfaces() is True
    assert fake.interface_updates == 1
    # Unchanged again: rebuilding on every tick would be needless churn.
    assert await advertiser.refresh_interfaces() is False


async def test_stop_sends_goodbyes_before_closing(
    services: list[mdns.MdnsService],
) -> None:
    """A consumer that hears the goodbye drops us at once.

    Without it the device lingers in every cache until its records expire,
    which looks like an unreachable meter rather than a stopped one.
    """
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    await advertiser.start()
    fake = advertiser._zc
    assert isinstance(fake, FakeZeroconf)
    await advertiser.stop()
    assert len(fake.unregistered) == 2
    assert fake.closed
    assert not advertiser.registered


async def test_stop_is_safe_without_a_start(
    services: list[mdns.MdnsService],
) -> None:
    advertiser = MdnsAdvertiser(services, zc_factory=FakeZeroconf)  # type: ignore[arg-type]
    await advertiser.stop()
