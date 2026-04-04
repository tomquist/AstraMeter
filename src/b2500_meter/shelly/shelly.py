from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from b2500_meter.config import ClientFilter
from b2500_meter.config.logger import logger
from b2500_meter.powermeter import Powermeter

BATTERY_INACTIVE_TIMEOUT_SECONDS = 120
POLL_INTERVAL_EMA_ALPHA = 0.3


class _ShellyProtocol(asyncio.DatagramProtocol):
    def __init__(self, shelly: Shelly):
        self.shelly = shelly
        self._tasks: set[asyncio.Task] = set()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        task = asyncio.create_task(
            self.shelly._safe_handle_request(self.transport, data, addr)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class Shelly:
    def __init__(
        self,
        powermeters: list[tuple[Powermeter, ClientFilter]],
        udp_port: int,
        device_id,
    ):
        self._udp_port = udp_port
        self._device_id = device_id
        self._powermeters = powermeters
        self._transport = None
        self._protocol: _ShellyProtocol | None = None
        self._battery_last_seen: dict[str, float] = {}
        self._battery_poll_interval: dict[str, float] = {}
        self._inactive_batteries: set[str] = set()
        self._stopped = asyncio.Event()
        self._inactive_check_task = None
        self.event_listener: Callable[[str, str, dict[str, Any]], None] | None = None

    def _calculate_derived_values(self, power):
        decimal_point_enforcer = 0.001
        if abs(power) < 0.1:
            return decimal_point_enforcer

        return round(
            power
            + (decimal_point_enforcer if power == round(power) or power == 0 else 0),
            1,
        )

    def _create_em_response(self, request_id, powers):
        if len(powers) == 1:
            powers = [powers[0], 0, 0]
        elif len(powers) != 3:
            powers = [0, 0, 0]

        a = self._calculate_derived_values(powers[0])
        b = self._calculate_derived_values(powers[1])
        c = self._calculate_derived_values(powers[2])

        total_act_power = round(sum(powers), 3)
        total_act_power = total_act_power + (
            0.001
            if total_act_power == round(total_act_power) or total_act_power == 0
            else 0
        )

        return {
            "id": request_id,
            "src": self._device_id,
            "dst": "unknown",
            "result": {
                "a_act_power": a,
                "b_act_power": b,
                "c_act_power": c,
                "total_act_power": total_act_power,
            },
        }

    def _create_em1_response(self, request_id, powers):
        total_power = round(sum(powers), 3)
        total_power = total_power + (
            0.001 if total_power == round(total_power) or total_power == 0 else 0
        )

        return {
            "id": request_id,
            "src": self._device_id,
            "dst": "unknown",
            "result": {
                "act_power": total_power,
            },
        }

    def _track_battery_seen(self, addr) -> float | None:
        battery_ip = addr[0]
        now = time.time()

        first_seen = battery_ip not in self._battery_last_seen
        was_inactive = battery_ip in self._inactive_batteries

        # Compute EMA-smoothed poll interval
        poll_interval: float | None = None
        if not first_seen:
            raw_interval = now - self._battery_last_seen[battery_ip]
            prev = self._battery_poll_interval.get(battery_ip)
            if prev is None:
                self._battery_poll_interval[battery_ip] = round(raw_interval, 1)
            else:
                self._battery_poll_interval[battery_ip] = round(
                    POLL_INTERVAL_EMA_ALPHA * raw_interval
                    + (1 - POLL_INTERVAL_EMA_ALPHA) * prev,
                    1,
                )
            poll_interval = self._battery_poll_interval[battery_ip]

        self._battery_last_seen[battery_ip] = now
        if was_inactive:
            self._inactive_batteries.remove(battery_ip)

        if first_seen:
            logger.info(
                "Battery detected on Shelly UDP port %s: %s",
                self._udp_port,
                battery_ip,
            )
        elif was_inactive:
            logger.info(
                "Battery reconnected on Shelly UDP port %s after inactivity: %s",
                self._udp_port,
                battery_ip,
            )

        return poll_interval

    def _log_inactive_batteries(self):
        now = time.time()
        newly_inactive_batteries = []

        for battery_ip, last_seen in self._battery_last_seen.items():
            if (
                now - last_seen >= BATTERY_INACTIVE_TIMEOUT_SECONDS
                and battery_ip not in self._inactive_batteries
            ):
                self._inactive_batteries.add(battery_ip)
                newly_inactive_batteries.append(battery_ip)

        for battery_ip in newly_inactive_batteries:
            logger.info(
                "Battery inactive on Shelly UDP port %s for >= %ss: %s",
                self._udp_port,
                BATTERY_INACTIVE_TIMEOUT_SECONDS,
                battery_ip,
            )
            self._call_event_listener(battery_ip, {"_removed": True})

    def _call_event_listener(self, battery_ip: str, data: dict[str, Any]) -> None:
        if not self.event_listener:
            return
        try:
            self.event_listener(self._device_id, battery_ip, data)
        except Exception as exc:
            logger.warning("event_listener failed for %s: %s", battery_ip, exc)

    async def _safe_handle_request(self, transport, data, addr):
        try:
            await self._handle_request(transport, data, addr)
        except Exception:
            logger.exception("Error handling Shelly request from %s", addr)

    async def _handle_request(self, transport, data, addr):
        poll_interval = self._track_battery_seen(addr)

        try:
            request_str = data.decode()
        except UnicodeDecodeError:
            logger.debug("Ignoring non-UTF-8 datagram from %s:%s", addr[0], addr[1])
            return

        logger.debug(f"Received UDP message: {request_str}")
        logger.debug(f"From: {addr[0]}:{addr[1]}")

        try:
            request = json.loads(request_str)
            logger.debug(f"Parsed request: {json.dumps(request, indent=2)}")
            if isinstance(request.get("params", {}).get("id"), int):
                powermeter = None
                for pm, client_filter in self._powermeters:
                    if client_filter.matches(addr[0]):
                        powermeter = pm
                        break
                if powermeter is None:
                    logger.warning(f"No powermeter found for client {addr[0]}")
                    return

                powers = await powermeter.get_powermeter_watts()

                if request.get("method") == "EM.GetStatus":
                    response = self._create_em_response(request["id"], powers)
                elif request.get("method") == "EM1.GetStatus":
                    response = self._create_em1_response(request["id"], powers)
                else:
                    return

                response_json = json.dumps(response, separators=(",", ":"))
                logger.debug(f"Sending response: {response_json}")
                response_data = response_json.encode()
                transport.sendto(response_data, addr)

                battery_ip = addr[0]
                if len(powers) == 1:
                    grid_l1, grid_l2, grid_l3 = powers[0], 0.0, 0.0
                elif len(powers) >= 3:
                    grid_l1, grid_l2, grid_l3 = (
                        float(powers[0]),
                        float(powers[1]),
                        float(powers[2]),
                    )
                else:
                    grid_l1, grid_l2, grid_l3 = 0.0, 0.0, 0.0
                self._call_event_listener(
                    battery_ip,
                    {
                        "grid_power": {
                            "l1": grid_l1,
                            "l2": grid_l2,
                            "l3": grid_l3,
                            "total": grid_l1 + grid_l2 + grid_l3,
                        },
                        "active": battery_ip not in self._inactive_batteries,
                        "poll_interval": poll_interval,
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                        "battery_count": len(self._battery_last_seen),
                    },
                )
        except json.JSONDecodeError:
            logger.error("Error: Invalid JSON")
        except Exception:
            logger.exception("Error processing message")

    async def _inactive_check_loop(self):
        try:
            while True:
                await asyncio.sleep(1.0)
                self._log_inactive_batteries()
        except asyncio.CancelledError:
            pass

    async def start(self):
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _ShellyProtocol(self),
            local_addr=("0.0.0.0", self._udp_port),
        )
        self._transport = transport
        self._protocol = protocol
        self._stopped.clear()
        self._inactive_check_task = asyncio.create_task(self._inactive_check_loop())
        bound = self._transport.get_extra_info("sockname")
        if bound:
            self._udp_port = bound[1]
        logger.info(f"Shelly emulator listening on UDP port {self._udp_port}...")

    @property
    def udp_port(self) -> int:
        return self._udp_port

    async def wait(self):
        await self._stopped.wait()

    async def stop(self):
        if self._inactive_check_task:
            self._inactive_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._inactive_check_task
            self._inactive_check_task = None
        # Close transport first to stop new datagrams from spawning tasks,
        # then cancel and await any in-flight handler tasks.
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._protocol:
            for task in list(self._protocol._tasks):
                task.cancel()
            await asyncio.gather(*self._protocol._tasks, return_exceptions=True)
        self._protocol = None
        self._stopped.set()
