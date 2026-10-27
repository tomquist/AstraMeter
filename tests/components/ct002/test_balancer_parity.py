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
from collections.abc import Callable
from pathlib import Path

import pytest

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
)

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
        self.min_power = (0.0, 0.0)
        self.min_power_overrides: dict[str, tuple[float, float]] = {}

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
            # Optional trailing global MIN_DC_OUTPUT (absent = disabled) and
            # ramp-pacing pair (absent = dataclass defaults, pacing on).
            min_dc = float(parts[9]) if len(parts) > 9 else 0.0
            pace_kwargs = (
                {
                    "pace_base_step": float(parts[10]),
                    "pace_max_step": float(parts[11]),
                }
                if len(parts) > 11
                else {}
            )
            # Optional trailing deadband-concentration threshold (after the
            # pace pair; absent = BalancerConfig default, matching the C++ harness).
            if len(parts) > 12:
                pace_kwargs["concentrate_deadband"] = float(parts[12])
            # Optional trailing steady-import trim (after concentration; absent =
            # disabled so existing fixtures keep their pre-trim behaviour).
            if len(parts) > 13:
                pace_kwargs["import_trim_w"] = float(parts[13])
            # Optional trailing efficiency demand-smoothing alpha (after the
            # trim; absent = BalancerConfig default, matching the C++ harness).
            if len(parts) > 14:
                pace_kwargs["efficiency_demand_alpha"] = float(parts[14])
            cfg = BalancerConfig(
                fair_distribution=bool(int(fair)),
                min_efficient_power=float(min_eff),
                efficiency_rotation_interval=float(rot),
                efficiency_saturation_threshold=float(sat_threshold),
                min_dc_output=min_dc,
                min_charge_power=self.min_power[0],
                min_discharge_power=self.min_power[1],
                **pace_kwargs,
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
        elif cmd == "minpow":
            self.min_power = (float(parts[1]), float(parts[2]))
        elif cmd == "override":
            self.min_power_overrides[parts[1]] = (float(parts[2]), float(parts[3]))
        elif cmd == "idle":
            snap = self.balancer.snapshot_consumer(parts[1])
            self.out.append("1" if snap is not None and snap.min_power_idle else "0")
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
                rc, dev, phase, power, md, eww, weight = parts[i : i + 7]
                min_charge, min_discharge = self.min_power_overrides.get(
                    rc, (-1.0, -1.0)
                )
                reports[rc] = ConsumerReport(
                    min_charge_power=None if min_charge < 0 else min_charge,
                    min_discharge_power=None if min_discharge < 0 else min_discharge,
                    device_type=dev,
                    phase=phase,
                    power=int(power),
                    weight=float(weight),
                    min_dc_output=None if float(md) < 0 else float(md),
                    efficiency_window_weight=float(eww),
                )
                i += 7
            res = self.balancer.compute_target(
                cid,
                ConsumerMode(mode, float(manual)),
                reports,
                float(grid),
                frozenset(),
                frozenset(),
                # sample_id mirrors the meter reading (as in production, where
                # it is the grid values): a changed grid is a fresh sample, so
                # the grid-state predictor's meter-correction / trust-adaptation
                # branch is exercised in the differential comparison.
                (float(grid),),
            )
            self.out.append(f"{res[0]:.4f} {res[1]:.4f} {res[2]:.4f}")
        elif cmd == "sat":
            self.out.append(f"{self.balancer.get_saturation(parts[1]):.4f}")
        elif cmd == "last":
            lt = self.balancer.get_last_target(parts[1])
            self.out.append("none" if lt is None else f"{lt:.4f}")
        elif cmd == "intent":
            li = self.balancer.get_last_intent(parts[1])
            self.out.append("none" if li is None else f"{li:.4f}")


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


def _report(
    cid: str,
    phase: str,
    power: int,
    dev: str = "HMA-2",
    min_dc: float = -1.0,
    eff_weight: float = 1.0,
    weight: float = 1.0,
) -> str:
    # min_dc < 0 encodes "unset" (inherit global / None). The efficiency-window
    # and distribution weight tokens are always emitted (default 1.0 = neutral).
    return f"{cid} {dev} {phase} {power} {min_dc} {eff_weight} {weight}"


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


def _scenario_efficiency_window_weight() -> list[str]:
    """Per-battery efficiency-window weight: a backup unit (0) stays
    deprioritized while limiting, a half-weight head rotates out at half the
    interval, unequal non-zero weights hand each unit a proportional turn, and a
    fresh pool fills heaviest-first even when that contradicts id order. Both
    stacks must agree poll-by-poll on the resulting targets."""
    lines = [_CFG_EFFICIENCY, "clock 5000"]
    # "y" is the backup (weight 0); under low demand it must stay
    # deprioritized while "x" carries the load.
    parked = [
        _report("x", "A", 0, eff_weight=1.0),
        _report("y", "A", 0, eff_weight=0.0),
    ]
    for _ in range(6):
        lines.append(_target("x", parked, grid=120))
        lines.append(_target("y", parked, grid=120))
        lines.append("advance 1")
        lines.append("last x")
        lines.append("last y")
    # Two half-weight units: the active head should rotate out after ~half the
    # rotation interval (900 * 0.5 = 450). Cross that boundary and keep polling.
    half = [_report("x", "A", 0, eff_weight=0.5), _report("y", "A", 0, eff_weight=0.5)]
    for _ in range(3):
        lines.append(_target("x", half, grid=120))
        lines.append(_target("y", half, grid=120))
        lines.append("advance 1")
    lines.append("advance 460")
    for _ in range(4):
        lines.append(_target("x", half, grid=120))
        lines.append(_target("y", half, grid=120))
        lines.append("advance 1")
        lines.append("last x")
        lines.append("last y")
    # Unequal non-zero weights (issue #647): the lighter unit holds the head for
    # its own shorter window rather than being re-ranked behind the heavier one
    # on every poll, so the two stacks have to agree on a rotation order that no
    # longer follows from the weights alone.
    uneven = [
        _report("x", "A", 0, eff_weight=0.5),
        _report("y", "A", 0, eff_weight=1.0),
    ]
    for _ in range(3):
        lines.append(_target("x", uneven, grid=120))
        lines.append(_target("y", uneven, grid=120))
        lines.append("advance 1")
    # Past "y"'s full window, then past "x"'s half window, then past a second
    # full one: three handovers, each read back on both stacks.
    for step in (910, 460, 910):
        lines.append(f"advance {step}")
        for _ in range(3):
            lines.append(_target("x", uneven, grid=120))
            lines.append(_target("y", uneven, grid=120))
            lines.append("advance 1")
            lines.append("last x")
            lines.append("last y")
    # A brand-new pool whose id order contradicts its weight order: "a" sorts
    # first by id but must arrive behind the heavier "b". Dropping x/y from the
    # reports retires them, so both stacks fill the order from scratch and have
    # to break the tie the same way round.
    fresh = [
        _report("a", "A", 0, eff_weight=0.5),
        _report("b", "A", 0, eff_weight=1.0),
    ]
    for _ in range(4):
        lines.append(_target("a", fresh, grid=120))
        lines.append(_target("b", fresh, grid=120))
        lines.append("advance 1")
        lines.append("last a")
        lines.append("last b")
    # Three units reporting 80 W against a 10 W grid: demand 250 over a
    # min_efficient_power of 100 keeps *two* slots active, and a real reported
    # power keeps the stall detector out of the rotation. Cycling far enough for
    # the 0.25-weight unit to take the head twice is what discriminates: above
    # one slot its turn must be a full interval on both stacks, not a quarter of
    # one, or the two rotations drift apart at that handover.
    trio = [
        _report("p", "A", 80, eff_weight=1.0),
        _report("q", "A", 80, eff_weight=1.0),
        _report("r", "A", 80, eff_weight=0.25),
    ]
    # The demand estimate is an EMA that only advances when the sample changes,
    # so the grid alternates to keep it climbing to the ~250 that limits three
    # units to two slots. Warm it up first, then two full windows bring the
    # 0.25-weight unit to the head; the 400 s step is the discriminator, sitting
    # between the quarter-window (225 s) an ungated head would rotate on and the
    # full one it has to hold once more than one battery is active.
    grids = (10, 12)
    for warm in range(24):
        for cid in ("p", "q", "r"):
            lines.append(_target(cid, trio, grid=grids[warm % 2]))
        lines.append("advance 1")
    for idx, step in enumerate((910, 910, 400, 910, 910, 400)):
        lines.append(f"advance {step}")
        for k in range(3):
            for cid in ("p", "q", "r"):
                lines.append(_target(cid, trio, grid=grids[(idx + k) % 2]))
            lines.append("advance 1")
            for cid in ("p", "q", "r"):
                lines.append(f"last {cid}")
    # Every battery parked: the sink has no one to promote, so a 0 weight
    # reaches the head and both stacks have to agree on what an all-zero pool
    # does. This pins that agreement, not the full-window fallback itself —
    # with these reports the probe and saturation machinery owns the ordering
    # here, so removing the fallback from one stack alone does not part them.
    # The fallback is pinned on the Python side by
    # test_all_zero_weight_pool_rotates_on_the_normal_interval.
    parked_all = [
        _report("m", "A", 0, eff_weight=0.0),
        _report("n", "A", 0, eff_weight=0.0),
    ]
    for _ in range(6):
        lines.append(_target("m", parked_all, grid=120))
        lines.append(_target("n", parked_all, grid=120))
        lines.append("advance 30")
        lines.append("last m")
        lines.append("last n")
    lines.append("advance 910")
    for _ in range(3):
        lines.append(_target("m", parked_all, grid=120))
        lines.append(_target("n", parked_all, grid=120))
        lines.append("advance 1")
        lines.append("last m")
        lines.append("last n")
    return lines


def _scenario_min_dc_output() -> list[str]:
    """Exercise the MIN_DC_OUTPUT floor: global on a DC battery + Venus, and a
    per-device override on the Venus, across surplus/discharge."""
    lines = ["cfg 1 0 900 0.4 0.15 20 90 1 25", "clock 2000"]
    pool = [_report("a", "A", 0, "HMA-2"), _report("b", "B", 0, "HMG-50")]
    for grid in (-600, -300, 0, 300, -150):
        lines.append(_target("a", pool, grid=grid))
        lines.append(_target("b", pool, grid=grid))
        lines.append("last a")
        lines.append("last b")
        lines.append("advance 1")
    # Per-device override floors the Venus too.
    pool2 = [_report("a", "A", 0, "HMA-2"), _report("b", "B", 0, "HMG-50", 40)]
    for grid in (-600, 200):
        lines.append(_target("b", pool2, grid=grid))
        lines.append("last b")
        lines.append("advance 1")
    return lines


def _scenario_min_power() -> list[str]:
    """Exercise MIN_CHARGE_POWER / MIN_DISCHARGE_POWER (issue #721): a pool of
    three on one phase whose demand sweeps through every minimum in both
    directions, so batteries drop out one at a time, resume past the
    hysteresis, and one battery's own override is judged on its own."""
    lines = [
        "minpow 60 50",
        "cfg 1 0 900 0.4 0.15 20 90 1 0 30 100 0 15",
        "override c -1 120",
        "clock 2000",
    ]
    # (grid, output of a, b, c) per tick: idle standby, a step up through the
    # minimums, back down, into a surplus and out again.
    ticks = [
        (19, 0, 0, 0),
        (70, 0, 0, 0),
        (40, 30, 0, 0),
        (130, 0, 0, 0),
        (60, 70, 0, 0),
        (400, 0, 0, 0),
        (100, 150, 150, 0),
        (-120, 150, 150, 0),
        (-30, 60, 0, 0),
        (-100, 0, 0, 0),
        (-200, 0, 0, 0),
        (-50, -100, -100, 0),
        (-400, 0, 0, 0),
        (40, -60, -60, -60),
        (300, 0, 0, 0),
    ]
    for grid, *outputs in ticks:
        for _ in range(2):
            pool = [
                _report(cid, "A", power, "HMG-50")
                for cid, power in zip("abc", outputs, strict=True)
            ]
            for cid in "abc":
                lines.append(_target(cid, pool, grid=grid))
                lines.append(f"idle {cid}")
                lines.append(f"last {cid}")
            lines.append("advance 1")
    return lines


def _scenario_import_trim() -> list[str]:
    """Hold a steady small grid import for many polls so the steady-import trim
    engages (dwell crosses IMPORT_TRIM_DWELL), then drop into export so the dwell
    resets — both branches must match between the Python and C++ balancers."""
    # Single Venus, fair on, pacing on, concentration off (0), trim = 15.
    lines = ["cfg 1 0 900 0.4 0.15 20 90 1 0 30 100 0 15", "clock 2000"]
    pool = [_report("a", "A", 0, "HMG-50")]
    # A small import that jitters every poll (so each reading is a *fresh* sample,
    # which the trim requires) but stays well inside the trim band, long enough to
    # cross the dwell threshold and keep firing.
    for i in range(14):
        lines.append(_target("a", pool, grid=50 + (i % 5) * 6))
        lines.append("last a")
        lines.append("intent a")
        lines.append("advance 1")
    # Export resets the dwell; the trim must go silent.
    for i in range(4):
        lines.append(_target("a", pool, grid=-200 - i))
        lines.append("last a")
        lines.append("advance 1")
    return lines


def _scenario_power_ceilings() -> list[str]:
    """Two batteries capped at 800 W next to an uncapped one (issue #655).

    The capped pair holds flat while pushed until both stacks learn their
    ceiling; the third keeps ramping, and the tracking share and the
    water-filled balance target must then agree poll for poll.  A charge swing
    exercises the other direction, and one capped unit finally exceeds its
    ceiling so both stacks drop it.  In between, the pool sits settled past
    the ceiling TTL: a ceiling that still binds must survive it on both.
    """
    lines = ["cfg 1 0 900 0.4 0.15 20 90 0", "clock 3000"]
    big = 700
    for step in range(14):
        grid = 1500 - 60 * step
        pool = [
            _report("a", "A", 800, "VNSE3"),
            _report("b", "A", 800, "VNSE3"),
            _report("c", "A", big, "VNSE3"),
        ]
        for cid in ("a", "b", "c"):
            lines.append(_target(cid, pool, grid=grid))
            lines.append(f"last {cid}")
        lines.append("advance 1")
        big += 60
    settled = [
        _report("a", "A", 800, "VNSE3"),
        _report("b", "A", 800, "VNSE3"),
        _report("c", "A", 1700, "VNSE3"),
    ]
    for _ in range(12):
        for cid in ("a", "b", "c"):
            lines.append(_target(cid, settled, grid=0))
            lines.append(f"last {cid}")
        lines.append("advance 120")
    for step in range(4):
        pool = [
            _report("a", "A", 600 - 150 * step, "VNSE3"),
            _report("b", "A", 800, "VNSE3"),
            _report("c", "A", 1200, "VNSE3"),
        ]
        for cid in ("a", "b", "c"):
            lines.append(_target(cid, pool, grid=-400))
        lines.append("advance 1")
    pool = [
        _report("a", "A", 900, "VNSE3"),
        _report("b", "A", 800, "VNSE3"),
        _report("c", "A", 1500, "VNSE3"),
    ]
    for _ in range(3):
        for cid in ("a", "b", "c"):
            lines.append(_target(cid, pool, grid=300))
        lines.append("advance 1")
    return lines


def _scenario_skewed_weights() -> list[str]:
    """Distribution weights of 10 and 0.1 (discussion #741).

    The light unit starts tens of watts off its share; the heavy unit then
    runs into its charge ceiling, which both stacks learn, and the light one
    takes the rest.  A swing to load turns the shares around, the heavy unit
    then stops following, and a DC-only light unit closes the scenario.
    """
    lines = ["cfg 1 0 900 0.4 0.15 20 90 1 0 0 0", "clock 5000"]

    def pool(a: int, b: int, dev_b: str = "VNSE3") -> list[str]:
        return [
            _report("a", "A", a, "VNSE3", weight=10.0),
            _report("b", "A", b, dev_b, weight=0.1),
        ]

    def tick(a: int, b: int, grid: float, dev_b: str = "VNSE3") -> None:
        for cid in ("a", "b"):
            lines.append(_target(cid, pool(a, b, dev_b), grid=grid))
            lines.append(f"last {cid}")
            lines.append(f"sat {cid}")
        lines.append("advance 1")

    # Stalled overflow under a modest surplus.
    for _ in range(3):
        tick(-1252, -64, -10)
    # The priority unit holds at 2500 W charge while the surplus grows.
    for _ in range(10):
        tick(-2500, 0, -1000)
    tick(-2500, -600, -400)
    # Swing to load: the overflow is discharging nothing it should.
    for _ in range(3):
        tick(300, -600, 900)
    # The priority unit stops following (full) under a surplus.
    for _ in range(12):
        tick(0, 0, -800)
    # A DC-only light unit.
    for _ in range(3):
        tick(400, 60, -20, dev_b="HMA-2")
    return lines


def test_parity_skewed_weights(harness: Path) -> None:
    lines = _scenario_skewed_weights()
    _compare("skewed_weights", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_power_ceilings(harness: Path) -> None:
    lines = _scenario_power_ceilings()
    _compare("power_ceilings", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_import_trim(harness: Path) -> None:
    lines = _scenario_import_trim()
    _compare("import_trim", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_min_power(harness: Path) -> None:
    lines = _scenario_min_power()
    _compare("min_power", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_min_dc_output(harness: Path) -> None:
    lines = _scenario_min_dc_output()
    _compare("min_dc_output", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_steer_and_split(harness: Path) -> None:
    lines = _scenario_steer_and_split()
    _compare("steer_split", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_efficiency_lifecycle(harness: Path) -> None:
    lines = _scenario_efficiency_lifecycle()
    _compare("efficiency", lines, _run_cpp(harness, lines), _run_py(lines))


def test_parity_efficiency_window_weight(harness: Path) -> None:
    lines = _scenario_efficiency_window_weight()
    _compare(
        "efficiency_window_weight", lines, _run_cpp(harness, lines), _run_py(lines)
    )


# ---------------------------------------------------------------------------
# Closed-loop streams: the standby reserve (efficiency mode).
#
# The reserve only engages once a battery ahead is out of headroom — at a
# learned ceiling, saturated, or DC-only under a surplus — which takes reports
# that follow the balancer's own commands over many polls.  These streams are
# recorded by steering a minimal plant with the Python driver, then replayed
# through both stacks, so the comparison runs over realistic trajectories.
# ---------------------------------------------------------------------------


class _Plant:
    """Adds each reading to its output, within its limits, in whole watts."""

    def __init__(
        self,
        dev: str = "VNSE3",
        *,
        discharge: float = 800.0,
        charge: float = 2500.0,
        window: float = 1.0,
        phase: str = "A",
    ) -> None:
        self.dev = dev
        self.discharge = discharge
        self.charge = 0.0 if dev.startswith(("HMA", "HMJ", "HMK")) else charge
        self.window = window
        self.phase = phase
        self.power = 0
        self.full = False
        self.empty = False

    def step(self, reading: float) -> None:
        hi = 0.0 if self.empty else self.discharge
        lo = 0.0 if self.full else -self.charge
        self.power = round(max(lo, min(hi, self.power + reading)))


def _closed_loop_stream(
    cfg: str,
    plants: dict[str, _Plant],
    schedule: list[tuple[float, int]],
    *,
    meter_every: int = 1,
    events: dict[int, Callable[[dict[str, _Plant]], None]] | None = None,
) -> tuple[list[str], int]:
    """Record a closed-loop stream; also return how many polls spilled.

    The meter carries a few watts of noise, as a real one does.  A noiseless
    meter settles the loop onto exact cancellations, where the two stacks'
    float and double arithmetic leave residues of opposite sign (around
    1e-8 W) that the deadband concentration and the oscillation damper then
    branch on — a precision artefact of the comparison, not a divergence of
    the logic.
    """
    driver = PyDriver()
    noise = random.Random(len(plants) * 1000 + len(schedule))
    lines = [cfg, "clock 10000"]
    for line in lines:
        driver.feed(line)
    tick = 0
    spilled = 0
    grid = 0.0
    for load, n in schedule:
        for _ in range(n):
            if events and tick in events:
                events[tick](plants)
            # A slow meter repeats its reading, and with it its sample id.
            if tick % meter_every == 0:
                grid = round(
                    load
                    - sum(b.power for b in plants.values())
                    + noise.choice((-6.3, -3.7, -1.4, 1.6, 3.4, 5.8))
                )
            pool = [
                _report(cid, b.phase, b.power, b.dev, eff_weight=b.window)
                for cid, b in plants.items()
            ]
            readings = {}
            for cid in plants:
                line = _target(cid, pool, grid=grid)
                lines.append(line)
                driver.feed(line)
                readings[cid] = sum(float(v) for v in driver.out[-1].split())
                assert driver.balancer is not None
                spilled += bool(driver.balancer._spilling)
            for cid in plants:
                lines += [f"last {cid}", f"intent {cid}", f"sat {cid}"]
            for cid, b in plants.items():
                b.step(readings[cid])
            lines.append("advance 1")
            driver.feed("advance 1")
            tick += 1
    return lines, spilled


# Efficiency mode at a floor no single battery reaches, with no timed rotation:
# one head, the rest standby.  Saturation detection on.
_CFG_RESERVE = "cfg 1 2500 86400 0.4 0.15 20 90 1"


def _set(cid: str, **attrs: bool) -> Callable[[dict[str, _Plant]], None]:
    def apply(plants: dict[str, _Plant]) -> None:
        for name, value in attrs.items():
            setattr(plants[cid], name, value)

    return apply


def _reserve_streams() -> dict[str, tuple[list[str], int]]:
    return {
        # The head's 800 W ceiling: overflow, last-in-first-out give-back,
        # overflow again, then a surplus.
        "overflow_lifo": _closed_loop_stream(
            _CFG_RESERVE,
            {"a": _Plant(), "b": _Plant()},
            [(600, 60), (1200, 150), (700, 100), (1200, 60), (-900, 60)],
        ),
        # A 0 % window backup behind a head that empties, then fills.
        "backup_takes_over": _closed_loop_stream(
            _CFG_RESERVE,
            {"a": _Plant(), "b": _Plant(window=0.0)},
            [(500, 180), (-1000, 60), (-1000, 120)],
            events={60: _set("a", empty=True), 180: _set("a", empty=False, full=True)},
        ),
        # A DC-only standby first in line under a surplus is passed over.
        "dc_standby_surplus": _closed_loop_stream(
            _CFG_RESERVE,
            {"a": _Plant(), "b": _Plant("HMJ-2"), "c": _Plant()},
            [(-800, 180), (1500, 120)],
            events={60: _set("a", full=True)},
        ),
        # Rotation every two minutes while the head's ceiling binds: probes and
        # fades run with a spilling standby.
        "handoffs": _closed_loop_stream(
            "cfg 1 2500 120 0.4 0.15 20 90 1",
            {"a": _Plant(), "b": _Plant(), "c": _Plant()},
            [(1200, 420)],
        ),
        # A meter repeating its reading for ten polls while the head empties.
        "repeating_meter": _closed_loop_stream(
            _CFG_RESERVE,
            {"a": _Plant(), "b": _Plant(window=0.0)},
            [(400, 240)],
            meter_every=10,
            events={80: _set("a", empty=True)},
        ),
    }


@pytest.mark.parametrize(
    "name",
    [
        "overflow_lifo",
        "backup_takes_over",
        "dc_standby_surplus",
        "handoffs",
        "repeating_meter",
    ],
)
def test_parity_standby_reserve(harness: Path, name: str) -> None:
    lines, spilled = _reserve_streams()[name]
    # The stream has to exercise what it is named for.
    assert spilled > 0, f"{name}: the standby reserve never engaged"
    _compare(f"reserve[{name}]", lines, _run_cpp(harness, lines), _run_py(lines))


def _random_reserve_stream(seed: int) -> tuple[list[str], int]:
    """A closed-loop stream over a random fleet, floor, schedule and faults.

    Mixed device types (a DC-only B2500 among them), discharge limits that a
    ceiling is learned at, efficiency window weights including 0, a random
    load / surplus schedule, a battery emptying or filling part-way, and
    sometimes a slow meter.
    """
    rng = random.Random(10_000 + seed)
    min_eff = rng.choice([400, 1200, 2500])
    rot = rng.choice([120, 900, 86400])
    cfg = f"cfg 1 {min_eff} {rot} 0.4 0.15 20 90 1"
    plants: dict[str, _Plant] = {}
    for cid in ("a", "b", "c")[: rng.randint(2, 3)]:
        plants[cid] = _Plant(
            rng.choice(["VNSE3", "VNSE3", "HMG-50", "HMJ-2"]),
            discharge=rng.choice([600.0, 800.0, 1200.0]),
            charge=rng.choice([1200.0, 2500.0]),
            window=rng.choice([1.0, 1.0, 0.5, 0.0]),
        )
    schedule = [
        (rng.choice([-1500, -700, 300, 700, 1100, 1600, 2200]), rng.randint(30, 90))
        for _ in range(rng.randint(3, 5))
    ]
    fault_at = rng.randint(20, 120)
    fault_cid = rng.choice(list(plants))
    fault = rng.choice(["empty", "full"])
    return _closed_loop_stream(
        cfg,
        plants,
        schedule,
        meter_every=rng.choice([1, 1, 3]),
        events={fault_at: _set(fault_cid, **{fault: True})},
    )


@pytest.mark.parametrize("seed", range(16))
def test_parity_fuzz_standby_reserve(harness: Path, seed: int) -> None:
    lines, _ = _random_reserve_stream(seed)
    _compare(f"reserve_fuzz[{seed}]", lines, _run_cpp(harness, lines), _run_py(lines))


def test_reserve_fuzz_engages_the_reserve() -> None:
    """The randomized reserve streams are not all idle ones."""
    engaged = sum(_random_reserve_stream(seed)[1] > 0 for seed in range(16))
    assert engaged >= 8


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
    min_dc = rng.choice([0, 0, 25, 50])
    # Vary ramp pacing too: off, defaults (omitted), constant cap, tight cap.
    pace = rng.choice(["", "", " 0 0", " 50 200", " 50 50", " 30 120"])
    # Deadband concentration is the trailing cfg token, so it needs an explicit
    # pace pair before it; force one when emitting it. ``None`` omits the token
    # (exercises the default), while an explicit ``0`` exercises the disabled
    # (plain-split) path — distinct now that the default is on.
    conc = rng.choice([None, None, 0, 40, 60])
    if conc is not None and not pace:
        pace = " 50 200"
    conc_tok = f" {conc}" if conc is not None else ""
    # Steady-import trim is the next trailing token (after concentration), so it
    # needs both the pace pair and a concentration token present; force them when
    # exercising it.
    trim = rng.choice([0, 0, 15, 30])
    trim_tok = ""
    if trim:
        if not pace:
            pace = " 50 200"
        if not conc_tok:
            conc_tok = " 0"
        trim_tok = f" {trim}"
    # Efficiency demand-smoothing alpha is the next trailing token (after the
    # trim), so it needs the pace pair, a concentration token and a trim token
    # present; force them when emitting it. ``None`` omits it (exercises the
    # default); ``1.0`` exercises the disabled (react-every-sample) path.
    demand_alpha = rng.choice([None, None, 1.0, 0.1, 0.3])
    demand_tok = ""
    if demand_alpha is not None:
        if not pace:
            pace = " 50 200"
        if not conc_tok:
            conc_tok = " 0"
        if not trim_tok:
            trim_tok = " 0"
        demand_tok = f" {demand_alpha}"
    lines = [
        f"cfg {fair} {min_eff} {rot} {sat_threshold} 0.15 20 {90} {sat_enabled} "
        f"{min_dc}{pace}{conc_tok}{trim_tok}{demand_tok}",
        f"clock {rng.randint(1000, 9000)}",
    ]
    n_consumers = rng.randint(1, 3)
    cids = ["a", "b", "c"][:n_consumers]
    phases = {cid: rng.choice(["A", "B", "C"]) for cid in cids}
    devs = {
        cid: rng.choice(["HMA-2", "HME-4", "HMG-50", "HMN-1", "VNSD"]) for cid in cids
    }
    overrides = {cid: rng.choice([-1, -1, -1, 25, 80]) for cid in cids}
    # Per-consumer efficiency-window weight: mostly neutral, sometimes parked (0)
    # or partial, to exercise the weight-driven sink/rotation across stacks.
    eff_weights = {cid: rng.choice([1.0, 1.0, 1.0, 0.0, 0.5, 0.25]) for cid in cids}
    # Distribution weights, drawn from their own generator so the streams the
    # draws above produce stay as they were: mostly neutral, sometimes a
    # strongly skewed split or a parked unit.
    wrng = random.Random(seed + 1_000_000)
    weights = {cid: wrng.choice([1.0, 1.0, 1.0, 10.0, 0.1, 0.5, 0.0]) for cid in cids}
    for _ in range(n_polls):
        grid = rng.choice([-901, -300, -150, -1, 0, 1, 100, 300, 450, 901, 1500])
        powers = {cid: rng.choice([-200, -50, 0, 0, 50, 200]) for cid in cids}
        pool = [
            _report(
                cid,
                phases[cid],
                powers[cid],
                devs[cid],
                overrides[cid],
                eff_weights[cid],
                weights[cid],
            )
            for cid in cids
        ]
        target_cid = rng.choice(cids)
        mode = rng.choice(["auto", "auto", "auto", "manual", "inactive"])
        manual = rng.choice([-300, 0, 250, 600])
        lines.append(_target(target_cid, pool, mode=mode, manual=manual, grid=grid))
        for cid in cids:
            lines.append(f"sat {cid}")
            lines.append(f"last {cid}")
            lines.append(f"intent {cid}")
        lines.append(f"advance {rng.choice([0.5, 1, 2, 5, 30, 60])}")
    return lines


@pytest.mark.parametrize("seed", range(40))
def test_parity_fuzz(harness: Path, seed: int) -> None:
    lines = _random_stream(seed, n_polls=25)
    _compare(f"fuzz[{seed}]", lines, _run_cpp(harness, lines), _run_py(lines))
