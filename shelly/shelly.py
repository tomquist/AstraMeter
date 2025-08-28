import socket
import threading
import json
from typing import List, Tuple
from config import ClientFilter
from powermeter import Powermeter
from config.logger import logger


class Shelly:
    def __init__(
        self,
        powermeters: List[Tuple[Powermeter, ClientFilter]],
        udp_port: int,
        device_id,
    ):
        self._udp_port = udp_port
        self._device_id = device_id
        self._powermeters = powermeters
        self._udp_thread = None
        self._stop = False
        self._value_mutex = threading.Lock()

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

    def udp_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", self._udp_port))
        logger.info(f"Shelly emulator listening on UDP port {self._udp_port}...")

        try:
            while not self._stop:
                data, addr = sock.recvfrom(1024)
                request_str = data.decode()
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
                            continue

                        powers = powermeter.get_powermeter_watts()
                        logger.debug(f"Powers: {powers}")
                        if request.get("method") == "EM.GetStatus":
                            response = self._create_em_response(request["id"], powers)
                        elif request.get("method") == "EM1.GetStatus":
                            response = self._create_em1_response(request["id"], powers)
                        else:
                            continue

                        response_json = json.dumps(response, separators=(",", ":"))
                        logger.debug(f"Sending response: {response_json}")
                        response_data = response_json.encode()
                        sock.sendto(response_data, addr)
                except json.JSONDecodeError:
                    logger.error(f"Error: Invalid JSON")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")

        finally:
            sock.close()

    def start(self):
        if self._udp_thread:
            return
        self._stop = False
        self._udp_thread = threading.Thread(target=self.udp_server)
        self._udp_thread.start()

    def join(self):
        if self._udp_thread:
            self._udp_thread.join()

    def stop(self):
        self._stop = True
        if self._udp_thread:
            self._udp_thread.join()
            self._udp_thread = None
