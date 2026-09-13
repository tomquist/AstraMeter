"""Announcing the emulated device on the local network.

Consumers find a Shelly the way any mDNS client finds anything: they ask the
local network for ``_shelly._tcp`` (and some for ``_http._tcp``), read the
service's TXT record to decide whether it is the kind of device they want, and
follow the SRV and A records to an address and port.

A responder runs in this process rather than delegating to a system daemon.
That matters most inside a Home Assistant add-on, where there is no such daemon
to delegate to — which is why other implementations need the user to install a
helper first, and why this one needs nothing.

Two responders can share UDP 5353 on one host: the library sets ``SO_REUSEADDR``
(and ``SO_REUSEPORT`` where available) on every socket, multicast is delivered
to all of them, and ``cooperating_responders`` skips the conflict probe that
would otherwise make two responders fight over one name.
"""

from __future__ import annotations

import dataclasses
import socket
from typing import TYPE_CHECKING

from zeroconf import InterfaceChoice, IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from astrameter.config.logger import logger
from astrameter.net_info import local_ipv4_set

if TYPE_CHECKING:
    # Type-only: the emulator imports this module, so importing the emulator's
    # package back at runtime would make the two a cycle — and which of them
    # broke would depend on which was imported first.
    from astrameter.shelly.identity import ShellyIdentity
    from astrameter.shelly.rpc import ShellyProfile

#: The service types a Shelly answers to. ``_shelly._tcp`` is what a battery
#: looks for; ``_http._tcp`` is what generic browsers and some consumers use.
SHELLY_SERVICE_TYPE = "_shelly._tcp.local."
HTTP_SERVICE_TYPE = "_http._tcp.local."


@dataclasses.dataclass(frozen=True, slots=True)
class MdnsService:
    """One service registration, in a form a test can assert on directly."""

    type_: str
    name: str
    server: str
    port: int
    addresses: tuple[bytes, ...]
    properties: dict[str, str]


def parse_txt_overrides(raw: str) -> dict[str, str]:
    """``key=value`` pairs from a comma-separated string.

    A value containing a comma is not expressible. No key in the Shelly TXT set
    can contain one, so the grammar stays simple rather than quoted.
    """
    overrides: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            logger.warning("Ignoring MDNS_TXT entry %r: expected key=value", item)
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            logger.warning("Ignoring MDNS_TXT entry %r: empty key", item)
            continue
        overrides[key] = value.strip()
    return overrides


def txt_records(
    identity: ShellyIdentity, profile: ShellyProfile, overrides: dict[str, str]
) -> dict[str, str]:
    """The TXT set a consumer reads to decide what this device is.

    ``gen`` and ``ver`` deliberately agree with what the HTTP surface reports
    for the same device: a consumer that gates on either would otherwise get
    two answers from one device. ``app`` deliberately does *not* — it is a
    string consumers match rather than gate on, and this is the spelling with
    field evidence behind it.
    """
    mac = identity.mac
    records = {
        "id": identity.shelly_id,
        "mac": ":".join(mac[i : i + 2] for i in range(0, 12, 2)),
        "gen": str(profile.gen),
        "app": profile.mdns_app,
        "model": profile.model,
        "arch": profile.arch,
        "fw_id": profile.fw_id,
        "ver": profile.ver,
    }
    records.update(overrides)
    return records


def shelly_services(
    identity: ShellyIdentity,
    profile: ShellyProfile,
    port: int,
    announced_ip: str,
    txt_overrides: dict[str, str] | None = None,
) -> list[MdnsService]:
    """Both service registrations for one device.

    Pure, so a test can assert the exact records without opening a socket.
    """
    properties = txt_records(identity, profile, txt_overrides or {})
    addresses: tuple[bytes, ...]
    try:
        addresses = (socket.inet_aton(announced_ip),)
    except OSError:
        logger.warning(
            "Cannot advertise %r as an address record; announcing without one",
            announced_ip,
        )
        addresses = ()
    server = f"{identity.hostname}.local."
    return [
        MdnsService(
            type_=service_type,
            name=f"{identity.instance}.{service_type}",
            server=server,
            port=port,
            addresses=addresses,
            properties=dict(properties),
        )
        for service_type in (SHELLY_SERVICE_TYPE, HTTP_SERVICE_TYPE)
    ]


def _service_info(service: MdnsService) -> ServiceInfo:
    return ServiceInfo(
        type_=service.type_,
        name=service.name,
        server=service.server,
        port=service.port,
        addresses=list(service.addresses),
        properties=dict(service.properties),
    )


class MdnsAdvertiser:
    """Keeps a set of service registrations live for as long as it is running.

    Holds no timer of its own: the emulator owns the tick that refreshes the
    advertised address, because that same tick has to keep running when mDNS is
    switched off — otherwise the HTTP surface would go on serving an address
    that has changed.
    """

    def __init__(
        self,
        services: list[MdnsService],
        *,
        interfaces: InterfaceChoice | list[str] = InterfaceChoice.All,
        zc_factory: type[AsyncZeroconf] = AsyncZeroconf,
    ) -> None:
        self._services = services
        self._interfaces = interfaces
        self._zc_factory = zc_factory
        self._zc: AsyncZeroconf | None = None
        self._infos: list[ServiceInfo] = []
        self._addresses = frozenset[str]()

    @property
    def registered(self) -> bool:
        return bool(self._infos)

    async def start(self) -> bool:
        """Register both services. ``False`` when the responder cannot run.

        Failing here is not fatal to the emulator: a consumer can still be
        pointed at the device by address, so the HTTP surface stays up and the
        emulator retries on its next tick.
        """
        zc = self._zc_factory(interfaces=self._interfaces, ip_version=IPVersion.V4Only)
        infos = [_service_info(service) for service in self._services]
        for info in infos:
            # Another responder on this host is expected, not a conflict: skip
            # the probe that would otherwise treat it as a name clash.
            await zc.async_register_service(info, cooperating_responders=True)
        self._zc = zc
        self._infos = infos
        self._addresses = local_ipv4_set()
        logger.info(
            "Announcing %s over mDNS on port %s",
            self._services[0].name if self._services else "nothing",
            self._services[0].port if self._services else 0,
        )
        return True

    async def refresh(self, announced_ip: str) -> None:
        """Re-announce with a new address record."""
        if self._zc is None or not self._infos:
            return
        try:
            packed = socket.inet_aton(announced_ip)
        except OSError:
            logger.warning("Cannot announce %r as an address record", announced_ip)
            return
        for info in self._infos:
            info.addresses = [packed]
            await self._zc.async_update_service(info)
        logger.info("Re-announced the emulated Shelly at %s", announced_ip)

    async def refresh_interfaces(self) -> bool:
        """Rebuild the sockets when the host's address set changed.

        An interface that disappears and returns — a reconnecting network, a
        container restart on the host — leaves the responder listening on
        sockets that no longer match reality.
        """
        if self._zc is None:
            return False
        current = local_ipv4_set()
        if current == self._addresses:
            return False
        self._addresses = current
        try:
            await self._zc.zeroconf.async_wait_for_start()
            await self._zc.async_update_interfaces(
                interfaces=self._interfaces, ip_version=IPVersion.V4Only
            )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Could not rebuild the mDNS sockets: %s", exc)
            return False
        logger.info("Rebuilt the mDNS sockets after a network change")
        return True

    async def stop(self) -> None:
        """Send goodbyes, then close, tolerating a partial start."""
        if self._zc is None:
            return
        for info in self._infos:
            try:
                await self._zc.async_unregister_service(info)
            except (OSError, RuntimeError) as exc:
                logger.debug("Could not unregister %s: %s", info.name, exc)
        try:
            await self._zc.async_close()
        except (OSError, RuntimeError) as exc:
            logger.debug("Could not close the mDNS responder: %s", exc)
        self._zc = None
        self._infos = []
