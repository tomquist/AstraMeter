"""Interactive load model for the simulator.

Provides a base load with noise, toggleable discrete loads, and
adjustable solar input.  All state is mutated in-place by the TUI /
HTTP control layer.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

PHASES = ("A", "B", "C")


def load_power_trace(path: str | Path) -> list[tuple[float, float]]:
    """Read a ``t_s,watts`` power trace into ``[(seconds, watts), ...]``.

    Lines starting with ``#`` (the attribution/license header), blank lines and
    a single ``t_s,watts`` column header are ignored, so the vendored CSVs under
    ``traces/`` (real household data, see ``traces/README.md``) load directly.
    Samples are returned sorted by time. Raises :class:`ValueError` if the file
    yields no valid samples (so a corrupt/empty fixture fails fast and clearly
    rather than as a late ``IndexError`` downstream).
    """
    points: list[tuple[float, float]] = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        a, _, b = line.partition(",")
        try:
            points.append((float(a), float(b)))
        except ValueError:
            continue  # header row (``t_s,watts``) or stray text
    if not points:
        raise ValueError(
            f"No valid trace samples found in {Path(path)!s} (expected: t_s,watts)"
        )
    points.sort(key=lambda p: p[0])
    return points


def load_net_trace(path: str | Path) -> list[tuple[float, float, float]]:
    """Read a ``t_s,load_w,pv_w`` trace into ``[(seconds, load, pv), ...]``.

    Same comment/blank/header handling as :func:`load_power_trace`, for the
    vendored real prosumer net-load CSVs (load + PV from one site, see
    ``traces/README.md``). Samples are returned sorted by time. Raises
    :class:`ValueError` if the file yields no valid samples.
    """
    points: list[tuple[float, float, float]] = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            points.append((float(parts[0]), float(parts[1]), float(parts[2])))
        except ValueError:
            continue  # header row (``t_s,load_w,pv_w``) or stray text
    if not points:
        raise ValueError(
            f"No valid trace samples found in {Path(path)!s} (expected: t_s,load_w,pv_w)"
        )
    points.sort(key=lambda p: p[0])
    return points


@dataclass
class Load:
    name: str
    power: float
    phase: str
    active: bool = False


@dataclass
class LoadModel:
    base_load: list[float] = field(default_factory=lambda: [100.0, 100.0, 100.0])
    base_noise: float = 20.0
    loads: list[Load] = field(default_factory=list)
    solar_power: float = 0.0
    solar_max: float = 2000.0
    solar_phases: list[str] = field(default_factory=lambda: ["A"])
    auto_mode: bool = False
    auto_interval: tuple[float, float] = (10.0, 30.0)

    def get_grid_contribution(self) -> list[float]:
        """Return ``[phase_a, phase_b, phase_c]`` watts (load + noise - solar).

        Battery output is *not* included here -- the powermeter simulator
        subtracts it separately so it can be displayed independently in
        the TUI.
        """
        result = [0.0, 0.0, 0.0]

        for i, phase in enumerate(PHASES):
            base = self.base_load[i] if i < len(self.base_load) else 0.0
            load_sum = sum(
                ld.power for ld in self.loads if ld.active and ld.phase == phase
            )
            solar = self._solar_on_phase(phase)
            # Only apply noise to phases that have base load or active loads
            if base > 0 or load_sum > 0:
                noise = random.uniform(-self.base_noise, self.base_noise)
            else:
                noise = 0.0
            result[i] = base + noise + load_sum - solar

        return result

    # -- mutations ---------------------------------------------------------

    def toggle_load(self, index: int) -> None:
        """Toggle load at *1-based* index (matching TUI key bindings)."""
        idx = index - 1
        if not (0 <= idx < len(self.loads)):
            raise IndexError(f"Load index out of range: {index}")
        self.loads[idx].active = not self.loads[idx].active

    def set_solar(self, watts: float) -> None:
        self.solar_power = max(0.0, min(watts, self.solar_max))

    def auto_step(self) -> None:
        """Randomly mutate loads and solar (called by auto-mode timer)."""
        for ld in self.loads:
            if random.random() < 0.3:
                ld.active = not ld.active
        self.solar_power = random.uniform(0, self.solar_max)

    # -- helpers -----------------------------------------------------------

    def _solar_on_phase(self, phase: str) -> float:
        if phase in self.solar_phases:
            return self.solar_power / len(self.solar_phases)
        return 0.0

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "loads": [
                {
                    "name": ld.name,
                    "power": ld.power,
                    "phase": ld.phase,
                    "active": ld.active,
                }
                for ld in self.loads
            ],
            "solar": {
                "current": round(self.solar_power, 1),
                "max": self.solar_max,
                "phases": self.solar_phases,
            },
            "auto_mode": self.auto_mode,
        }
