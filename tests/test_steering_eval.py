"""Smoke tests for the steering-quality evaluation harness.

The full suite (``python -m astrameter.simulator.evaluation``) simulates
hours per scenario; here we run a tiny inline scenario end-to-end to keep
the harness itself covered by CI's pytest run.
"""

from __future__ import annotations

import asyncio

import pytest

from astrameter.simulator.evaluation import (
    _METRIC_GLOSSARY,
    _REPORT_METRICS,
    GRAPH_POINTS,
    BatterySpec,
    Event,
    Scenario,
    build_scenarios,
    render_markdown_compare,
    run_scenario,
)


def _tiny_scenario() -> Scenario:
    duration = 240.0

    def events(_rng) -> list[Event]:
        return [
            Event(
                at=60.0,
                label="step_on",
                apply=lambda w: w.load_model.base_load.__setitem__(0, 800.0),
            ),
            Event(
                at=150.0,
                label="step_off",
                apply=lambda w: w.load_model.base_load.__setitem__(0, 200.0),
            ),
        ]

    return Scenario(
        name="tiny",
        description="smoke",
        batteries=[BatterySpec(poll_interval=1.0)],
        duration_s=duration,
        base_load=[200.0, 0.0, 0.0],
        base_noise=0.0,
        build_events=events,
    )


def test_run_scenario_produces_metrics():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    assert res["scenario"] == "tiny"
    assert res["samples"] > 200
    # Both scripted steps are large enough to measure.
    assert res["events_measured"] == 2
    for key in (
        "settle_mean_s",
        "overshoot_max_w",
        "band_crossings_per_h",
        "steady_rms_w",
        "import_wh",
        "export_wh",
        "avoidable_import_wh",
        "avoidable_export_wh",
        "battery_travel_w_per_h",
    ):
        assert res[key] >= 0, key
    # The battery covers the initial 200 W base load well before the end.
    assert res["settle_mean_s"] < res["duration_h"] * 3600


def test_run_scenario_is_deterministic():
    a = asyncio.run(run_scenario(_tiny_scenario(), seed=7))
    b = asyncio.run(run_scenario(_tiny_scenario(), seed=7))
    assert a == b


def test_overrides_reach_the_balancer():
    # An absurd deadband makes the balance-correction path inert; the run
    # must still complete and produce metrics (knob plumbed through CT002).
    res = asyncio.run(
        run_scenario(_tiny_scenario(), seed=3, overrides={"balance_deadband": 500.0})
    )
    assert res["samples"] > 200


def test_scenario_registry_shape():
    scenarios = build_scenarios()
    # Multi-battery scenarios exist in both balancer modes.
    assert "two_venus/fair" in scenarios
    assert "two_venus/eff" in scenarios
    assert "mixed_venus_b2500/fair" in scenarios
    assert "mixed_venus_b2500/eff" in scenarios
    assert scenarios["two_venus/eff"].ct_kwargs["min_efficient_power"] > 0
    for sc in scenarios.values():
        assert sc.duration_s > 0 and sc.batteries


def test_markdown_compare_renders():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    base = dict(res, overshoot_max_w=res["overshoot_max_w"] + 100.0)
    md = render_markdown_compare([base], [res])
    assert "| overshoot_max_w |" in md
    assert "tiny" in md
    # The collapsible metric glossary is included with a row per metric.
    assert "What do these metrics mean?" in md
    for key in _REPORT_METRICS:
        assert f"| `{key}` |" in md
    # Each scenario embeds a Mermaid grid-power chart with a base and head line.
    assert "```mermaid" in md
    assert "xychart-beta" in md
    assert md.count("    line [") == 2


def test_grid_trace_is_downsampled_to_fixed_length():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    assert len(res["grid_trace"]) == GRAPH_POINTS
    assert all(isinstance(v, float) for v in res["grid_trace"])


def test_metric_glossary_covers_every_reported_metric():
    glossary_keys = [key for key, _ in _METRIC_GLOSSARY]
    assert glossary_keys == _REPORT_METRICS


@pytest.mark.parametrize("name", ["single_venus_steps"])
def test_full_scenario_definitions_build(name):
    import random

    sc = build_scenarios()[name]
    events = sc.build_events(random.Random(1))
    assert events and all(e.at >= 0 for e in events)
