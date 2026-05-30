"""Differential parity tests for the LoadBalancer C++ port.

These compile a tiny host-gcc harness (``fixtures/balancer_parity_harness.cpp``)
that links the real ``esphome/components/ct002/balancer.cpp`` and drives the
*same* scenarios through both the C++ port and the canonical Python
``LoadBalancer``. Asserting both produce the same per-phase target is a
data-driven cross-stack parity guard, complementing the hand-written
``host_balancer_test.cpp`` gtest cases.

The C++ side needs only a C++17 compiler (``g++``/``clang++``); the test skips
cleanly when none is available, so it never blocks a pure-Python checkout.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from astrameter.ct002.balancer import BalancerConfig, ConsumerMode, LoadBalancer

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent
HARNESS_SRC = HERE / "fixtures" / "balancer_parity_harness.cpp"
BALANCER_SRC = REPO_ROOT / "esphome" / "components" / "ct002" / "balancer.cpp"


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
    )
    if result.returncode != 0:
        pytest.fail(f"failed to build balancer parity harness:\n{result.stderr}")
    return out


def _build_balancer(fair: bool) -> LoadBalancer:
    """Mirror the harness's construction so single calls stay deterministic."""
    return LoadBalancer(
        BalancerConfig(fair_distribution=fair),
        saturation_alpha=0.15,
        saturation_min_target=20.0,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=False,
        clock=lambda: 0.0,
        reset_fn=None,
    )


def _py_target(scenario: dict) -> list[float]:
    balancer = _build_balancer(scenario["fair"])
    reports = {
        c["cid"]: {"device_type": c["dev"], "phase": c["phase"], "power": c["power"]}
        for c in scenario["consumers"]
    }
    mode = ConsumerMode(scenario["mode"], scenario["manual_value"])
    return balancer.compute_target(
        scenario["consumer_id"],
        mode,
        reports,
        float(scenario["grid_total"]),
        frozenset(),
        frozenset(),
        (),
    )


def _scenario_line(s: dict) -> str:
    parts = [
        s["mode"],
        s["consumer_id"],
        str(s["grid_total"]),
        str(s["manual_value"]),
        "1" if s["fair"] else "0",
        str(len(s["consumers"])),
    ]
    for c in s["consumers"]:
        parts += [c["cid"], c["dev"], c["phase"], str(c["power"])]
    return " ".join(parts)


def _c(cid: str, phase: str, power: float, dev: str = "HMA-2") -> dict:
    return {"cid": cid, "dev": dev, "phase": phase, "power": power}


def _scn(
    mode: str,
    consumer_id: str,
    consumers: list[dict],
    *,
    grid_total: float = 0.0,
    manual_value: float = 0.0,
    fair: bool = True,
) -> dict:
    return {
        "mode": mode,
        "consumer_id": consumer_id,
        "consumers": consumers,
        "grid_total": grid_total,
        "manual_value": manual_value,
        "fair": fair,
    }


# Deterministic scenarios exercising the steer-to-zero, manual-override and
# auto paths, including export (negative) directions and multi-phase splits.
SCENARIOS: list[dict] = [
    # --- inactive: steer the consumer's reported output to zero ---
    _scn("inactive", "a", [_c("a", "A", 200)]),
    _scn("inactive", "a", [_c("a", "B", 350)]),
    _scn("inactive", "a", [_c("a", "C", -125)]),
    _scn("inactive", "a", [_c("a", "A", 0)]),
    # --- manual override: target = manual_value - reported, split by phase ---
    _scn("manual", "a", [_c("a", "A", 100)], manual_value=400),
    _scn("manual", "a", [_c("a", "B", 0)], manual_value=250),
    _scn("manual", "a", [_c("a", "C", 500)], manual_value=-200),
    _scn("manual", "a", [_c("a", "A", 100), _c("b", "B", 0)], manual_value=300),
    _scn(
        "manual",
        "a",
        [_c("a", "A", 0), _c("b", "B", 0), _c("c", "C", 0)],
        manual_value=900,
    ),
    _scn(
        "manual",
        "a",
        [_c("a", "A", 0), _c("b", "A", 0), _c("c", "B", 0)],
        manual_value=600,
    ),
    # --- auto: single + multi consumer, import and export ---
    _scn("auto", "a", [_c("a", "A", 0)], grid_total=300, fair=False),
    _scn("auto", "a", [_c("a", "A", 0)], grid_total=-300, fair=False),
    _scn(
        "auto",
        "a",
        [_c("a", "A", 0), _c("b", "A", 0)],
        grid_total=400,
        fair=False,
    ),
    _scn(
        "auto",
        "a",
        [_c("a", "A", 0), _c("b", "B", 0)],
        grid_total=400,
        fair=False,
    ),
    _scn(
        "auto",
        "a",
        [_c("a", "A", 0), _c("b", "B", 0), _c("c", "C", 0)],
        grid_total=900,
        fair=False,
    ),
    _scn(
        "auto",
        "a",
        [_c("a", "A", 0), _c("b", "B", 0)],
        grid_total=-200,
        fair=False,
    ),
    # --- auto: default fair distribution ---
    _scn("auto", "a", [_c("a", "A", 0)], grid_total=300, fair=True),
    _scn(
        "auto",
        "a",
        [_c("a", "A", 0), _c("b", "A", 0)],
        grid_total=600,
        fair=True,
    ),
    _scn(
        "auto",
        "a",
        [_c("a", "A", 0), _c("b", "B", 0)],
        grid_total=600,
        fair=True,
    ),
    # --- auto: asymmetric reported power feeds balance correction ---
    _scn(
        "auto",
        "a",
        [_c("a", "A", 50), _c("b", "A", 0)],
        grid_total=400,
        fair=False,
    ),
    _scn(
        "auto",
        "b",
        [_c("a", "A", 50), _c("b", "A", 0)],
        grid_total=400,
        fair=False,
    ),
    # --- auto: mixed AC/DC device types under export (DC-clamp path) ---
    _scn(
        "auto",
        "a",
        [_c("a", "A", 0, dev="HMA-2"), _c("g", "A", 0, dev="HMG-50")],
        grid_total=-300,
        fair=False,
    ),
    _scn(
        "auto",
        "g",
        [_c("a", "A", 0, dev="HMA-2"), _c("g", "A", 0, dev="HMG-50")],
        grid_total=-300,
        fair=False,
    ),
    # --- auto: three consumers, asymmetric phases and reports ---
    _scn(
        "auto",
        "a",
        [_c("a", "A", 100), _c("b", "B", 0), _c("c", "C", -50)],
        grid_total=450,
        fair=False,
    ),
]


def _run_harness(harness: Path, scenarios: list[dict]) -> list[list[float]]:
    stdin = "\n".join(_scenario_line(s) for s in scenarios) + "\n"
    result = subprocess.run(
        [str(harness)], input=stdin, capture_output=True, text=True, check=True
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == len(scenarios), (
        f"harness emitted {len(lines)} lines for {len(scenarios)} scenarios"
    )
    return [[float(x) for x in ln.split()] for ln in lines]


def test_balancer_parity(harness: Path) -> None:
    """C++ and Python LoadBalancer agree on per-phase targets, wire-rounded.

    Compared at integer-watt granularity (what reaches the wire): the C++ port
    uses 32-bit float while Python uses double, so sub-watt rounding noise is
    tolerated, but any genuine algorithmic divergence shows up as a mismatch.
    """
    cpp_results = _run_harness(harness, SCENARIOS)
    mismatches: list[str] = []
    for scenario, cpp in zip(SCENARIOS, cpp_results, strict=True):
        py = _py_target(scenario)
        for phase, (pv, cv) in enumerate(zip(py, cpp, strict=True)):
            if abs(round(pv) - round(cv)) > 0:
                mismatches.append(
                    f"{_scenario_line(scenario)!r} phase {'ABC'[phase]}: "
                    f"python={pv:.4f} cpp={cv:.4f}"
                )
    assert not mismatches, "LoadBalancer parity mismatches:\n" + "\n".join(mismatches)
