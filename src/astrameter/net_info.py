"""Reading the host's own network identity, without asking the network.

The Shelly HTTP surface and the mDNS records both have to name *this* host: a
MAC that stays the same across restarts, and the IPv4 address a battery on the
LAN can actually reach. Both are derived from the kernel's own view — the
default route and the interface behind it — rather than guessed.

Every reader takes its filesystem root as an argument so the tests drive them
off fixture trees with no network at all.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path

import ifaddr

from astrameter.config.logger import logger

#: Placeholder the kernel reports for an interface with no hardware address.
_NULL_MAC = "00:00:00:00:00:00"

#: RFC 5737 TEST-NET-1. ``connect()`` on a UDP socket sends nothing — it is a
#: routing-table lookup — and this address is reserved for documentation, so
#: nothing reaches a third party even if a future kernel did send something.
_ROUTE_PROBE_ADDR = "192.0.2.1"
_ROUTE_PROBE_PORT = 9


def default_route_interface(proc_root: str = "/proc") -> str | None:
    """Name of the interface carrying the default route, lowest metric first.

    Reads ``/proc/net/route``, whose default rows have both ``Destination`` and
    ``Mask`` set to ``00000000``. Deliberately *not* "the highest MAC on the
    box": inside a container that picks a random ``veth``, which is exactly the
    instability this module exists to avoid.
    """
    path = Path(proc_root) / "net" / "route"
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return None

    best: tuple[int, str] | None = None
    for line in lines[1:]:
        fields = line.split()
        # Iface Destination Gateway Flags RefCnt Use Metric Mask …
        if len(fields) < 8:
            continue
        iface, destination, mask, metric = fields[0], fields[1], fields[7], fields[6]
        if destination != "00000000" or mask != "00000000":
            continue
        try:
            parsed_metric = int(metric)
        except ValueError:
            continue
        if best is None or parsed_metric < best[0]:
            best = (parsed_metric, iface)
    return best[1] if best else None


def interface_mac(name: str, sys_root: str = "/sys") -> str | None:
    """The hardware address of *name*, or ``None`` when it has none."""
    path = Path(sys_root) / "class" / "net" / name / "address"
    try:
        raw = path.read_text().strip()
    except OSError as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return None
    if not raw or raw == _NULL_MAC:
        return None
    return raw


def local_ipv4() -> str | None:
    """The address this host would use to reach the LAN, or ``None``.

    A connected UDP socket asks the routing table which source address a packet
    would leave with; no packet is sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((_ROUTE_PROBE_ADDR, _ROUTE_PROBE_PORT))
        return str(sock.getsockname()[0])
    except OSError as exc:
        logger.debug("Could not determine the local IPv4 address: %s", exc)
        return None
    finally:
        sock.close()


def interface_for_ipv4(ip: str) -> str | None:
    """The interface carrying *ip*, or ``None`` when no adapter has it."""
    for adapter in ifaddr.get_adapters():
        for address in adapter.ips:
            if address.is_IPv4 and address.ip == ip:
                return str(adapter.nice_name)
    return None


def ipv4_for_interface(name: str) -> str | None:
    """The first IPv4 address configured on *name*, or ``None``."""
    for adapter in ifaddr.get_adapters():
        if adapter.nice_name != name:
            continue
        for address in adapter.ips:
            if address.is_IPv4:
                return str(address.ip)
    return None


def local_ipv4_set() -> frozenset[str]:
    """Every IPv4 address currently configured on this host.

    The announce watchdog compares this set between ticks: a change means the
    responder's sockets no longer match the interfaces that exist, so they have
    to be rebuilt.
    """
    found: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for address in adapter.ips:
            if address.is_IPv4:
                found.add(str(address.ip))
    return frozenset(found)


def announced_ipv4(mdns_host: str) -> str:
    """The IPv4 address to advertise, honouring an explicit *mdns_host*.

    *mdns_host* may be an address — for a host whose reachable LAN address is
    not the one the sockets bind, such as a virtual IP — or an interface name,
    which is resolved to that interface's first IPv4 address. An empty value
    derives the address from the default route. Falls back to loopback so
    callers always have a string to serve.
    """
    configured = mdns_host.strip()
    if configured:
        try:
            socket.inet_aton(configured)
        except OSError:
            resolved = ipv4_for_interface(configured)
            if resolved:
                return resolved
            logger.warning(
                "MDNS_HOST %r is neither an IPv4 address nor an interface with "
                "one; deriving the address from the default route instead",
                configured,
            )
        else:
            return configured
    return local_ipv4() or "127.0.0.1"


class AnnouncedAddress:
    """The single live copy of the advertised IPv4 address.

    Every surface that names this host's address reads it from here — the mDNS
    A record, ``WiFi.GetStatus``, the Gen1 status page and the dashboard — so a
    DHCP change moves all of them together instead of leaving one serving an
    address the others have already replaced.
    """

    __slots__ = ("_resolve", "_value")

    def __init__(self, initial: str, *, resolve: Callable[[], str]) -> None:
        self._value = initial
        self._resolve = resolve

    @property
    def value(self) -> str:
        """The address as last resolved. Cheap: readers call this per request."""
        return self._value

    def refresh(self) -> str | None:
        """Re-resolve; the new address, or ``None`` when it has not changed."""
        try:
            resolved = self._resolve()
        except OSError as exc:
            logger.debug("Could not refresh the announced address: %s", exc)
            return None
        if resolved == self._value:
            return None
        logger.info("Announced address changed from %s to %s", self._value, resolved)
        self._value = resolved
        return resolved
