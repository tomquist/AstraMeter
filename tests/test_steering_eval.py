"""Smoke tests for the steering-quality evaluation harness.

The full suite (``python -m astrameter.simulator.evaluation``) simulates
hours per scenario; here we run a tiny inline scenario end-to-end to keep
the harness itself covered by CI's pytest run.
"""

from __future__ import annotations

import asyncio

import pytest

from astrameter.simulator.eval_report import render_html_report
from astrameter.simulator.evaluation import (
    _METRIC_GLOSSARY,
    _REPORT_METRICS,
    GRAPH_POINTS,
    BatterySpec,
    Event,
    Scenario,
    _fmt_delta,
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
    # Venus families also have solar variants (load + PV day curve + clouds),
    # exercising the charge/export side of the loop, not just discharge.
    assert "two_venus_solar/fair" in scenarios
    assert "two_venus_solar/eff" in scenarios
    assert "mixed_cadence_solar/fair" in scenarios
    assert "mixed_cadence_solar/eff" in scenarios
    # Washing-machine spike-absorption stress (issue #473).
    assert "single_venus_washer" in scenarios
    # Venus D (VNSD-0 integer loop) variants, including a heterogeneous phase
    # sharing with a Venus C (HMG float ramp).
    assert "single_venus_d_steps" in scenarios
    assert "single_venus_d_washer" in scenarios
    assert "single_venus_d_solar" in scenarios
    assert "venus_d_plus_c/fair" in scenarios
    assert "venus_d_plus_c/eff" in scenarios
    assert scenarios["single_venus_d_steps"].batteries[0].device_type == "VNSD-0"
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
    # The interactive charts moved to the HTML artifact, never an embedded
    # (static, unreadable) Mermaid chart.
    assert "mermaid" not in md
    # The report pointer only appears when a report is actually produced, so a
    # plain --compare run doesn't promise a link that won't exist.
    assert "steering-eval-report.html" not in md
    md_with_report = render_markdown_compare([base], [res], report_available=True)
    assert "steering-eval-report.html" in md_with_report


def test_compare_tolerates_base_missing_a_new_metric():
    """A base produced before a metric existed (this PR adds grid_p2p_w) must
    not break the comparison — CI runs base (old code) vs head (new code), so
    the base rows lack newly added keys. Both renderers must degrade to '—'."""
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    assert "grid_p2p_w" in res
    base_old = {k: v for k, v in res.items() if k != "grid_p2p_w"}
    md = render_markdown_compare([base_old], [res])
    assert "grid_p2p_w" in md  # row still rendered (Base shows —)
    h = render_html_report(
        [base_old],
        [res],
        report_metrics=_REPORT_METRICS,
        metric_glossary=_METRIC_GLOSSARY,
        fmt_delta=_fmt_delta,
    )
    assert "grid_p2p_w" in h


def test_html_report_is_self_contained_and_interactive():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    base = dict(res, overshoot_max_w=res["overshoot_max_w"] + 100.0)
    h = render_html_report(
        [base],
        [res],
        report_metrics=_REPORT_METRICS,
        metric_glossary=_METRIC_GLOSSARY,
        fmt_delta=_fmt_delta,
    )
    # Self-contained: the uPlot library and CSS are inlined (no CDN/network).
    assert "uPlot" in h and "https://cdn." not in h
    assert ".uplot" in h  # vendored CSS
    # Per scenario: a grid chart (base vs head) and a per-battery output chart.
    assert 'id="grid0"' in h
    assert 'id="batt0"' in h
    assert "Battery output" in h
    assert res["battery_labels"][0] in h  # battery series labelled
    # Net house consumption is overlaid on the grid chart as a dashed line.
    assert '"label": "consumption"' in h
    assert '"dash"' in h
    assert res["scenario"] in h
    # Metrics table with colour-coded (lower-is-better) deltas.
    assert "overshoot_max_w" in h
    assert 'class="better"' in h or 'class="worse"' in h


def test_html_report_escapes_script_breakout_in_chart_data():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    # A label that would close the inline <script> if embedded verbatim.
    res = dict(res, battery_labels=["</script><b>x"])
    h = render_html_report(
        None,
        [res],
        report_metrics=_REPORT_METRICS,
        metric_glossary=_METRIC_GLOSSARY,
        fmt_delta=_fmt_delta,
    )
    # The raw breakout sequence must not survive into the chart JSON; '<' is
    # escaped to the JSON-valid <.
    assert "</script><b>x" not in h
    assert "\\u003c/script>" in h


def test_html_report_handles_missing_baseline():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    h = render_html_report(
        None,
        [res],
        report_metrics=_REPORT_METRICS,
        metric_glossary=_METRIC_GLOSSARY,
        fmt_delta=_fmt_delta,
    )
    # Head-only still renders the grid (head series) and per-battery charts.
    assert 'id="grid0"' in h
    assert 'id="batt0"' in h


def test_grid_trace_is_downsampled_to_fixed_length():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    assert len(res["grid_trace"]) == GRAPH_POINTS
    assert all(isinstance(v, float) for v in res["grid_trace"])


def test_battery_traces_one_per_battery_fixed_length():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    # One label and one fixed-length downsampled trace per battery.
    assert res["battery_labels"] == ["B1 HMG-50"]
    assert len(res["battery_traces"]) == 1
    assert len(res["battery_traces"][0]) == GRAPH_POINTS
    assert all(isinstance(v, float) for v in res["battery_traces"][0])


def test_consumption_trace_matches_grid_plus_battery_output():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    cons = res["consumption_trace"]
    assert len(cons) == GRAPH_POINTS
    # Energy-balance identity (within downsample rounding): consumption ==
    # grid + sum of battery outputs at each bucket.
    grid = res["grid_trace"]
    bat = res["battery_traces"]
    for k in range(GRAPH_POINTS):
        expected = grid[k] + sum(b[k] for b in bat)
        assert abs(cons[k] - expected) <= 1.0


def test_metric_glossary_covers_every_reported_metric():
    glossary_keys = [key for key, _ in _METRIC_GLOSSARY]
    assert glossary_keys == _REPORT_METRICS


@pytest.mark.parametrize(
    "name",
    [
        "single_venus_steps",
        "single_venus_washer",
        "single_venus_d_steps",
        "single_venus_d_washer",
        "single_venus_d_solar",
        "venus_d_plus_c/fair",
    ],
)
def test_full_scenario_definitions_build(name):
    import random

    sc = build_scenarios()[name]
    events = sc.build_events(random.Random(1))
    assert events and all(e.at >= 0 for e in events)


def test_washer_scenario_reproduces_sustained_oscillation():
    """The washing-machine scenario (issue #473) reproduces the field signature:
    a drum-tumble rhythm over a latency-delayed meter, so the loop hunts
    continuously instead of holding zero. It is scored on the sustained-
    oscillation aggregates (the step-response metrics read 0 for this failure
    mode); a balancer fix should drive these down — that's the baseline's point.
    We assert it *reproduces* hunting, not a specific (currently poor) score."""
    sc = build_scenarios()["single_venus_washer"]
    res = asyncio.run(run_scenario(sc, seed=1))
    # The latency-driven hunt makes the grid oscillate and mistrack.
    assert res["grid_p2p_w"] > 0
    assert res["band_crossings_per_h"] > 0
    assert res["mean_abs_grid_w"] > 0
    # All reported metrics are populated and non-negative.
    for key in _REPORT_METRICS:
        assert res[key] >= 0, key


def test_meter_latency_drives_sustained_oscillation():
    """Acting on a delayed meter reading turns a settling loop into one that
    hunts: the washing-machine scenario's grid swing (grid_p2p_w) is markedly
    larger with its meter latency than with the delay removed. This guards the
    latency model (issue #473) that reproduces the field oscillation."""
    import dataclasses

    sc = build_scenarios()["single_venus_washer"]
    assert sc.meter_latency_s > 0  # the scenario opts into delay
    delayed = asyncio.run(run_scenario(sc, seed=1))
    instant = asyncio.run(
        run_scenario(dataclasses.replace(sc, meter_latency_s=0.0), seed=1)
    )
    assert delayed["grid_p2p_w"] > instant["grid_p2p_w"]


class TestRampPacingRegression:
    """Issue #458 acceptance: bounded overshoot on the firmware plant.

    Runs the same load-step scenario with pacing on (defaults) and off and
    asserts the paced controller keeps the opposite-direction grid excursion
    bounded where the unpaced one overshoots by hundreds of watts.
    """

    @staticmethod
    def _step_scenario() -> Scenario:
        duration = 600.0

        def events(_rng) -> list[Event]:
            return [
                Event(
                    at=60.0,
                    label="step_on",
                    apply=lambda w: w.load_model.base_load.__setitem__(0, 1800.0),
                ),
                Event(
                    at=360.0,
                    label="step_off",
                    apply=lambda w: w.load_model.base_load.__setitem__(0, 300.0),
                ),
            ]

        return Scenario(
            name="pacing_regression",
            description="1.5 kW load step on the firmware plant",
            batteries=[BatterySpec()],
            duration_s=duration,
            base_load=[300.0, 0.0, 0.0],
            base_noise=0.0,
            build_events=events,
        )

    def test_paced_overshoot_bounded(self):
        paced = asyncio.run(run_scenario(self._step_scenario(), seed=5))
        unpaced = asyncio.run(
            run_scenario(self._step_scenario(), seed=5, overrides={"pace_base_step": 0})
        )
        # The unpaced firmware ramp overshoots the step by hundreds of watts;
        # pacing must keep the excursion within ~2 base steps.
        assert paced["overshoot_max_w"] < 110, paced
        assert unpaced["overshoot_max_w"] > 250, unpaced
        # Both must still settle every step event inside its window.
        assert paced["unsettled_events"] == 0, paced
