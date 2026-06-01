"""Differential parity tests for the LoadBalancer C++ port.

These compile a host-gcc harness (``fixtures/balancer_parity_harness.cpp``)
that links the real ``esphome/components/ct002/balancer.cpp`` and drives a
single, *stateful* balancer instance through a command stream. The same stream
is replayed against the canonical Python ``LoadBalancer`` and the two are
compared poll-by-poll. Because one balancer processes the whole sequence, this
exercises the time-dependent machinery — saturation EMA, efficiency
deprioritization, probe/rotation, weight fade — not just one-shot phase splits,
which is where a port is most likely to drift.

The C++ side needs only a C++17 compiler; the test skips cleanly when none is
available, so it never blocks a pure-Python checkout.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from astrameter.ct002.balancer import BalancerConfig, ConsumerMode, LoadBalancer

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent
HARNESS_SRC = HERE / "fixtures" / "balancer_parity_harness.cpp"
BALANCER_SRC = REPO_ROOT / "esphome" / "components" / "ct002" / "balancer.cpp"

# Float (C++) vs double (Python) means sub-watt rounding noise is expected.
# Compare raw magnitudes against a half-watt-plus-slack tolerance rather than
# rounding each side and checking integer equality: a value sitting near an
# X.5 boundary can round to X on one stack and X+1 on the other despite a
# <0.001 W true difference, which would flake. 0.5 keeps the integer-watt
# intent; the small extra slack absorbs the rounding noise at the boundary.
WATT_TOL = 0.5 + 1e-3


def _find_compiler() -> str | None:
    for candidate in ("g++", "c++", "clang++"):
        if shutil.which(candidate):
            return candidate
    return None


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    compiler = _find_compiler()
    if compiler is None:
        pytest.skip("no C++ compiler (g++/clang++) on PATH")
    out = tmp_path_factory.mktemp("balancer_parity") / "harness"
    result = subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            f"-I{REPO_ROOT}",
            str(HARNESS_SRC),
            str(BALANCER_SRC),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"failed to build balancer parity harness:\n{result.stderr}")
    return out


# ---------------------------------------------------------------------------
# A small command-stream model driven against both stacks.
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class PyDriver:
    """Replays the command stream against the canonical Python LoadBalancer."""

    def __init__(self) -> None:
        self.clock = _Clock()
        self.balancer: LoadBalancer | None = None
        self.out: list[str] = []

    def feed(self, line: str) -> None:
        parts = line.split()
        cmd = parts[0]
        if cmd == "cfg":
            (
                fair,
                min_eff,
                rot,
                sat_threshold,
                sat_alpha,
                sat_min_target,
                sat_grace,
                sat_enabled,
            ) = parts[1:9]
            cfg = BalancerConfig(
                fair_distribution=bool(int(fair)),
                min_efficient_power=float(min_eff),
                efficiency_rotation_interval=float(rot),
                efficiency_saturation_threshold=float(sat_threshold),
            )
            self.balancer = LoadBalancer(
                cfg,
                saturation_alpha=float(sat_alpha),
                saturation_min_target=float(sat_min_target),
                saturation_decay_factor=0.995,
                saturation_grace_seconds=float(sat_grace),
                saturation_stall_timeout_seconds=60.0,
                saturation_enabled=bool(int(sat_enabled)),
                clock=self.clock,
                reset_fn=None,
            )
        elif cmd == "clock":
            self.clock.now = float(parts[1])
        elif cmd == "advance":
            self.clock.now += float(parts[1])
        elif cmd == "target":
            cid, mode, manual, grid, n = (
                parts[1],
                parts[2],
                parts[3],
                parts[4],
                int(parts[5]),
            )
            reports = {}
            i = 6
            for _ in range(n):
                rc, dev, phase, power = parts[i : i + 4]
                reports[rc] = {"device_type": dev, "phase": phase, "power": int(power)}
                i += 4
            res = self.balancer.compute_target(
                cid,
                ConsumerMode(mode, float(manual)),
                reports,
                float(grid),
                frozenset(),
                frozenset(),
                (),
            )
            self.out.append(f"{res[0]:.4f} {res[1]:.4f} {res[2]:.4f}")
        elif cmd == "sat":
            self.out.append(f"{self.balancer.get_saturation(parts[1]):.4f}")
        elif cmd == "last":
            lt = self.balancer.get_last_target(parts[1])
            self.out.append("none" if lt is None else f"{lt:.4f}")


def _run_cpp(harness: Path, lines: list[str]) -> list[str]:
    stdin = "\n".join(lines) + "\n"
    result = subprocess.run(
        [str(harness)],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def _run_py(lines: list[str]) -> list[str]:
    driver = PyDriver()
    for line in lines:
        driver.feed(line)
    return driver.out


def _compare(label: str, lines: list[str], cpp: list[str], py: list[str]) -> None:
    assert len(cpp) == len(py), (
        f"[{label}] output length mismatch: cpp={len(cpp)} py={len(py)}"
    )
    mismatches: list[str] = []
    for idx, (c, p) in enumerate(zip(cpp, py, strict=True)):
        cv = c.split()
        pv = p.split()
        if cv == ["none"] or pv == ["none"]:
            if cv != pv:
                mismatches.append(f"#{idx}: cpp={c!r} py={p!r}")
            continue
        for cval, pval in zip(cv, pv, strict=True):
            if abs(float(cval) - float(pval)) > WATT_TOL:
                mismatches.append(f"#{idx}: cpp={c!r} py={p!r}")
                break
    if mismatches:
        joined = "\n".join(mismatches[:20])
        # Dump the command stream so a failing (randomized) scenario is
        # reproducible straight from the assertion output.
        stream = "\n".join(lines)
        raise AssertionError(
            f"[{label}] LoadBalancer parity mismatches:\n{joined}\n"
            f"--- command stream ---\n{stream}"
        )


# ---------------------------------------------------------------------------
# Hand-written multi-poll scenarios (deterministic).
# ---------------------------------------------------------------------------

# Default config line used by simple scenarios (no efficiency machinery).
_CFG_DEFAULT = "cfg 0 0 900 0.4 0.15 20 90 0"
# Efficiency + saturation enabled, so deprioritization / probe / rotation /
# fade all engage across multiple polls.
_CFG_EFFICIENCY = "cfg 1 100 900 0.4 0.15 20 90 1"


def _report(cid: str, phase: str, power: int, dev: str = "HMA-2") -> str:
    return f"{cid} {dev} {phase} {power}"


def _target(
    cid: str,
    consumers: list[str],
    *,
    mode: str = "auto",
    manual: float = 0.0,
    grid: float = 0.0,
) -> str:
    return f"target {cid} {mode} {manual} {grid} {len(consumers)} " + " ".join(
        consumers
    )


def _scenario_steer_and_split() -> list[str]:
    lines = [_CFG_DEFAULT, "clock 1000"]
    # inactive across phases (incl. negative reported power)
    for phase, power in [("A", 200), ("B", -150), ("C", 0)]:
        lines.append(_target("a", [_report("a", phase, power)], mode="inactive"))
    # manual override, multi-phase pools
    lines.append(
        _target(
            "a",
            [_report("a", "A", 100), _report("b", "B", 0), _report("c", "C", 0)],
            mode="manual",
            manual=900,
        )
    )
    # auto: import/export, fair off (cfg has fair=0), multi-consumer & phase
    for grid in (300, -300, 901, -901):
        lines.append(
            _target(
                "a",
                [_report("a", "A", 0), _report("b", "B", 0), _report("c", "C", 0)],
                grid=grid,
            )
        )
    return lines


def _scenario_efficiency_lifecycle() -> list[str]:
    """Two batteries under low per-unit demand: drive deprioritization, then
    saturate the active one to provoke a probe/rotation, advancing the clock
    so the time-gated paths fire."""
    lines = [_CFG_EFFICIENCY, "clock 5000"]
    pool = [_report("x", "A", 0), _report("y", "A", 0)]
    # Low demand → one unit should be deprioritized over successive polls.
    for _ in range(6):
        lines.append(_target("x", pool, grid=120))
        lines.append(_target("y", pool, grid=120))
        lines.append("advance 1")
        lines.append("sat x")
        lines.append("sat y")
    # Now report the active unit as stuck at ~0 W while it's told to produce →
    # saturation climbs, eventually forcing a swap.
    pool_stuck = [_report("x", "A", 0), _report("y", "A", 0)]
    for _ in range(40):
        lines.append(_target("x", pool_stuck, grid=300))
        lines.append(_target("y", pool_stuck, grid=300))
        lines.append("advance 2")
        lines.append("sat x")
        lines.append("sat y")
        lines.append("last x")
        lines.append("last y")
    # Cross the rotation interval to exercise the timed rotation branch.
    lines.append("advance 950")
    lines.append(_target("x", pool, grid=200))
    lines.append(_target("y", pool, grid=200))
    return lines


def test_parity_steer_and_split(harness: Path) -> None:
    lines = _scenario_steer_and_split()
    _compare("steer_split", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_efficiency_lifecycle(harness: Path) -> None:
    lines = _scenario_efficiency_lifecycle()
    _compare("efficiency", lines, _run_cpp(harness, lines), _run_py(lines))


# ---------------------------------------------------------------------------
# Randomized differential fuzzing.
# ---------------------------------------------------------------------------


def _random_stream(seed: int, n_polls: int) -> list[str]:
    rng = random.Random(seed)
    fair = rng.choice([0, 1])
    min_eff = rng.choice([0, 0, 50, 100, 150])
    rot = rng.choice([20, 120, 900])
    sat_threshold = rng.choice([0.0, 0.3, 0.4])
    sat_enabled = rng.choice([0, 1])
    lines = [
        f"cfg {fair} {min_eff} {rot} {sat_threshold} 0.15 20 {90} {sat_enabled}",
        f"clock {rng.randint(1000, 9000)}",
    ]
    n_consumers = rng.randint(1, 3)
    cids = ["a", "b", "c"][:n_consumers]
    phases = {cid: rng.choice(["A", "B", "C"]) for cid in cids}
    devs = {cid: rng.choice(["HMA-2", "HME-4", "HMG-50"]) for cid in cids}
    for _ in range(n_polls):
        grid = rng.choice([-901, -300, -150, -1, 0, 1, 100, 300, 450, 901, 1500])
        powers = {cid: rng.choice([-200, -50, 0, 0, 50, 200]) for cid in cids}
        pool = [_report(cid, phases[cid], powers[cid], devs[cid]) for cid in cids]
        target_cid = rng.choice(cids)
        mode = rng.choice(["auto", "auto", "auto", "manual", "inactive"])
        manual = rng.choice([-300, 0, 250, 600])
        lines.append(_target(target_cid, pool, mode=mode, manual=manual, grid=grid))
        for cid in cids:
            lines.append(f"sat {cid}")
            lines.append(f"last {cid}")
        lines.append(f"advance {rng.choice([0.5, 1, 2, 5, 30, 60])}")
    return lines


@pytest.mark.parametrize("seed", range(40))
def test_parity_fuzz(harness: Path, seed: int) -> None:
    lines = _random_stream(seed, n_polls=25)
    _compare(f"fuzz[{seed}]", lines, _run_cpp(harness, lines), _run_py(lines))
