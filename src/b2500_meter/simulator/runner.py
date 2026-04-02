"""Simulation runner — wires batteries, load model and HTTP server together."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from .battery import BatterySimulator
from .load_model import Load, LoadModel
from .powermeter_sim import PowermeterSimulator

logger = logging.getLogger("b2500_sim.runner")


@dataclass
class BatteryConfig:
    mac: str
    phase: str
    max_charge_power: int = 800
    max_discharge_power: int = 800
    capacity_wh: float = 2560.0
    initial_soc: float = 0.5
    ramp_rate: float = 200.0
    poll_interval: float = 1.0


@dataclass
class SimulationConfig:
    batteries: list[BatteryConfig]
    ct_mac: str = "112233445566"
    ct_host: str = "127.0.0.1"
    ct_port: int = 12345
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    base_load: list[float] = field(default_factory=lambda: [100.0, 100.0, 100.0])
    base_noise: float = 20.0
    loads: list[Load] = field(default_factory=list)
    solar_max: float = 2000.0
    solar_phases: list[str] = field(default_factory=lambda: ["A"])
    auto_mode: bool = False
    auto_interval: tuple[float, float] = (10.0, 30.0)
    log_interval: float = 5.0
    time_scale: float = 1.0


class SimulationRunner:
    """Orchestrates batteries, load model, and HTTP powermeter."""

    def __init__(self, config: SimulationConfig) -> None:
        self.config = config
        self.load_model = self._build_load_model()
        self.batteries = self._build_batteries()
        self.powermeter = PowermeterSimulator(
            batteries=self.batteries,
            load_model=self.load_model,
            host=config.http_host,
            port=config.http_port,
        )

    # -- build helpers -----------------------------------------------------

    def _build_load_model(self) -> LoadModel:
        cfg = self.config
        return LoadModel(
            base_load=list(cfg.base_load),
            base_noise=cfg.base_noise,
            loads=[Load(ld.name, ld.power, ld.phase) for ld in cfg.loads],
            solar_max=cfg.solar_max,
            solar_phases=list(cfg.solar_phases),
            auto_mode=cfg.auto_mode,
            auto_interval=cfg.auto_interval,
        )

    def _build_batteries(self) -> list[BatterySimulator]:
        cfg = self.config
        return [
            BatterySimulator(
                mac=bc.mac,
                phase=bc.phase,
                ct_mac=cfg.ct_mac,
                ct_host=cfg.ct_host,
                ct_port=cfg.ct_port,
                max_charge_power=bc.max_charge_power,
                max_discharge_power=bc.max_discharge_power,
                capacity_wh=bc.capacity_wh,
                initial_soc=bc.initial_soc,
                ramp_rate=bc.ramp_rate,
                poll_interval=bc.poll_interval,
                time_scale=cfg.time_scale,
            )
            for bc in cfg.batteries
        ]

    # -- run (headless / --no-tui) -----------------------------------------

    async def run_headless(self) -> None:
        """Run simulator without TUI.  Logs status periodically."""
        await self.powermeter.start()
        tasks = [asyncio.create_task(b.run()) for b in self.batteries]
        tasks.append(asyncio.create_task(self._log_loop()))
        if self.load_model.auto_mode:
            tasks.append(asyncio.create_task(self._auto_loop()))

        try:
            await self.powermeter.shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.powermeter.stop()

    async def _log_loop(self) -> None:
        ts = max(self.config.time_scale, 0.1)
        while True:
            await asyncio.sleep(self.config.log_interval / ts)
            grid = self.powermeter.compute_grid()
            parts = [
                f"grid=[{grid['phase_a']:.0f}, {grid['phase_b']:.0f}, {grid['phase_c']:.0f}]"
            ]
            for b in self.batteries:
                parts.append(
                    f"{b.mac[-4:]}:{b.phase}/{b.current_power:.0f}W/{b.soc * 100:.0f}%"
                )
            logger.info(" | ".join(parts))

    async def _auto_loop(self) -> None:
        ts = max(self.config.time_scale, 0.1)
        while True:
            lo, hi = self.load_model.auto_interval
            await asyncio.sleep(random.uniform(lo, hi) / ts)
            if self.load_model.auto_mode:
                self.load_model.auto_step()

    # -- config loading ----------------------------------------------------

    @staticmethod
    def from_config_file(path: str | Path) -> SimulationRunner:
        data = json.loads(Path(path).read_text())
        cfg = parse_config(data)
        validate_config(cfg)
        return SimulationRunner(cfg)


def parse_config(data: dict) -> SimulationConfig:
    ct = data.get("ct", {})
    http = data.get("http", {})
    pm = data.get("powermeter", {})

    loads: list[Load] = []
    for ld in pm.get("loads", []):
        loads.append(Load(name=ld["name"], power=ld["power"], phase=ld["phase"]))

    batteries: list[BatteryConfig] = []
    for bd in data.get("batteries", []):
        bc = BatteryConfig(
            mac=bd["mac"],
            phase=bd["phase"],
            max_charge_power=bd.get("max_charge_power", 800),
            max_discharge_power=bd.get("max_discharge_power", 800),
            capacity_wh=bd.get("capacity_wh", 2560.0),
            initial_soc=bd.get("initial_soc", 0.5),
            ramp_rate=bd.get("ramp_rate", 200.0),
            poll_interval=bd.get("poll_interval", 1.0),
        )
        batteries.append(bc)

    auto_interval_raw = data.get("auto_interval", [10, 30])

    return SimulationConfig(
        batteries=batteries,
        ct_mac=ct.get("mac", "112233445566"),
        ct_host=ct.get("host", "127.0.0.1"),
        ct_port=ct.get("port", 12345),
        http_host=http.get("host", "127.0.0.1"),
        http_port=http.get("port", 8080),
        base_load=pm.get("base_load", [100.0, 100.0, 100.0]),
        base_noise=pm.get("base_noise", 20.0),
        loads=loads,
        solar_max=pm.get("solar_max", 2000.0),
        solar_phases=pm.get("solar_phases", ["A"]),
        auto_mode=data.get("auto_mode", False),
        auto_interval=tuple(auto_interval_raw),
        log_interval=data.get("log_interval", 5.0),
        time_scale=data.get("time_scale", 1.0),
    )


def validate_config(cfg: SimulationConfig) -> None:
    """Raise ``ValueError`` on invalid configuration."""
    seen_macs: set[str] = set()
    for bc in cfg.batteries:
        if bc.phase not in ("A", "B", "C"):
            raise ValueError(f"Battery {bc.mac}: invalid phase {bc.phase!r}")
        if not (0.0 <= bc.initial_soc <= 1.0):
            raise ValueError(
                f"Battery {bc.mac}: initial_soc must be 0.0-1.0, got {bc.initial_soc}"
            )
        if bc.max_charge_power < 0 or bc.max_discharge_power < 0:
            raise ValueError(f"Battery {bc.mac}: power values must be >= 0")
        mac = bc.mac.upper()
        if len(mac) != 12 or not all(c in "0123456789ABCDEF" for c in mac):
            raise ValueError(f"Battery MAC must be 12 hex chars, got {bc.mac!r}")
        if mac in seen_macs:
            raise ValueError(f"Duplicate battery MAC: {bc.mac}")
        seen_macs.add(mac)

    for ld in cfg.loads:
        if ld.phase not in ("A", "B", "C"):
            raise ValueError(f"Load {ld.name!r}: invalid phase {ld.phase!r}")

    for phase in cfg.solar_phases:
        if phase not in ("A", "B", "C"):
            raise ValueError(f"Invalid solar phase {phase!r}")

    if cfg.time_scale <= 0:
        raise ValueError(f"time_scale must be positive, got {cfg.time_scale}")


def quick_config(
    num_batteries: int = 1,
    num_phases: int = 1,
    base_load: list[float] | None = None,
    initial_soc: float = 0.5,
    ct_host: str = "127.0.0.1",
    ct_port: int = 12345,
    http_port: int = 8080,
) -> SimulationConfig:
    """Build a SimulationConfig for quick-start CLI mode."""
    phases = ["A", "B", "C"][:num_phases]
    batteries = []
    for i in range(num_batteries):
        mac = f"02B250{i + 1:06X}"
        phase = phases[i % len(phases)]
        batteries.append(BatteryConfig(mac=mac, phase=phase, initial_soc=initial_soc))

    default_loads = [
        Load("LED lights", 30, "A"),
        Load("TV + entertainment", 80, "B" if num_phases >= 2 else "A"),
        Load("Router + NAS", 40, "A"),
        Load("Microwave", 800, "A"),
        Load("Washing machine", 400, "B" if num_phases >= 2 else "A"),
    ]

    if base_load is None:
        base_load = [300.0, 0.0, 0.0] if num_phases == 1 else [100.0, 100.0, 100.0]

    return SimulationConfig(
        batteries=batteries,
        ct_host=ct_host,
        ct_port=ct_port,
        http_port=http_port,
        base_load=base_load,
        loads=default_loads,
        solar_phases=phases,
    )
