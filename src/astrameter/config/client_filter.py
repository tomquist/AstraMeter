from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network

from astrameter.config.logger import logger

__all__ = ["ClientFilter"]


class ClientFilter:
    def __init__(self, netmasks: list[IPv4Network]):
        self.netmasks = netmasks

    def matches(self, client_ip) -> bool:
        try:
            client_ip_addr = IPv4Address(client_ip)
            for netmask in self.netmasks:
                if client_ip_addr in netmask:
                    return True
        except ValueError as e:
            logger.error(f"Error: {e}")
            return False
        return False
