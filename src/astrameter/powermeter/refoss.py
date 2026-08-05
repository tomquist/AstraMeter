"""Refoss / Meross energy monitors (EM01P, EM06P, EM16P) via local Open API."""

from __future__ import annotations

import aiohttp
from aiohttp import ClientTimeout

from .base import Powermeter


def parse_channels(raw: str) -> list[int]:
    """Parse a comma-separated CHANNELS value into positive channel ids."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("CHANNELS must list at least one channel id")
    channels: list[int] = []
    for part in parts:
        try:
            channel = int(part)
        except ValueError as exc:
            raise ValueError(
                f"Invalid CHANNELS entry {part!r}: expected an integer"
            ) from exc
        if channel < 1:
            raise ValueError(
                f"Invalid CHANNELS entry {channel}: channel ids start at 1"
            )
        channels.append(channel)
    return channels


class Refoss(Powermeter):
    """Reads a Refoss / Meross energy monitor over the local HTTP Open API.

    Polls ``/rpc/Em.Status.Get?id=65535`` (all CT channels) and returns the
    signed ``power`` field for each configured channel id — positive = import,
    negative = export. Meross-branded EM*P hardware uses the same Refoss API
    (see https://docs.refoss.net/open-api/).

    The device exposes cleartext HTTP only (no TLS). Use only on an explicitly
    trusted local network; do not expose the meter API beyond that LAN.
    """

    def __init__(self, ip: str, channels: list[int]):
        ip = ip.strip()
        if not ip:
            raise ValueError("IP is required")
        if not channels:
            raise ValueError("CHANNELS must list at least one channel id")
        self.ip = ip
        self.channels = list(channels)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        # Fail fast: the battery polls ~1/s, so a slow source should error
        # quickly and let the next poll retry rather than pin a handler.
        self.session = aiohttp.ClientSession(timeout=ClientTimeout(total=2, connect=1))

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_json(self, path: str):
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"http://{self.ip}{path}"
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json("/rpc/Em.Status.Get?id=65535")
        status = response.get("status")
        if not isinstance(status, list):
            raise ValueError("Refoss Em.Status.Get response missing status array")

        by_id: dict[int, float] = {}
        for entry in status:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            if "power" not in entry:
                raise ValueError(f"Refoss status entry missing power field: {entry!r}")
            try:
                channel_id = int(entry["id"])
                by_id[channel_id] = float(entry["power"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid Refoss status entry: {entry!r}") from exc

        watts: list[float] = []
        for channel_id in self.channels:
            if channel_id not in by_id:
                raise ValueError(
                    f"Refoss channel {channel_id} not present in Em.Status.Get "
                    f"(have {sorted(by_id)})"
                )
            watts.append(by_id[channel_id])
        return watts
