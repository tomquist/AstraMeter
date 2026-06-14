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
    _aggregate,
    _compare_aggregates,
    _fmt_delta,
    _guardrail_regressions,
    _merge_seeds,
    _overall_summary,
    _priority_summary,
    _run_all,
    _seed_label,
    _weighted_overall,
    build_scenarios,
    render_markdown_compare,
    render_text,
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
        # Isolate the harness smoke test from the suite's realistic-meter
        # default: a clean meter keeps these assertions deterministic.
        meter_latency_s=0.0,
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
    # Pretty-noisy house baseline (noise-rejection stress), single and two Venus.
    assert "single_venus_noisy" in scenarios
    assert "two_venus_noisy/fair" in scenarios
    assert "two_venus_noisy/eff" in scenarios
    # Real recorded-household-load stress (correlated drift + spikes over a
    # latency-delayed meter), single and two Venus (fair + eff).
    assert "single_venus_trace" in scenarios
    assert "two_venus_trace/fair" in scenarios
    assert "two_venus_trace/eff" in scenarios
    # The trace /eff variant raises min_efficient_power above the default eff
    # floor so the concentration swap actually triggers within the real load's
    # range (at the default floor it would duplicate /fair).
    assert (
        scenarios["two_venus_trace/eff"].ct_kwargs["min_efficient_power"]
        > scenarios["two_venus/eff"].ct_kwargs["min_efficient_power"]
    )
    # The real-trace scenarios opt into realistic meter latency (the field
    # condition the synthetic latency-free scenarios never cover).
    assert scenarios["single_venus_trace"].meter_latency_s > 0
    assert scenarios["two_venus_trace/fair"].meter_latency_s > 0
    # Everyday scenarios now default to a realistic (non-zero) meter delay — no
    # real meter is delay-free, so overshoot/settle is measured under latency.
    assert scenarios["single_venus_steps"].meter_latency_s > 0
    assert scenarios["two_venus/fair"].meter_latency_s > 0
    # Slow-meter variants: a fresh reading only every 10 s (coarse-sampling
    # stress), covering meters that emit a point ~once per 10 s.
    for slow in (
        "single_venus_steps_slow",
        "single_venus_solar_slow",
        "two_venus_slow/fair",
    ):
        assert slow in scenarios
        assert scenarios[slow].meter_interval_s == 10.0
        assert scenarios[slow].meter_latency_s > 0
    # SoC-saturation scenarios: a small pack started near an edge and driven
    # hard enough to actually hit empty / full (the handoff to grid).
    assert "single_venus_drain" in scenarios
    assert "single_venus_fill" in scenarios
    # Three-phase imbalance: one Venus on each of A/B/C (everything else is
    # single-phase A) — exercises per-phase target distribution.
    assert "phase_imbalance" in scenarios
    assert [b.phase for b in scenarios["phase_imbalance"].batteries] == ["A", "B", "C"]
    # Real PV net-load (recorded PV + load with cloud transients), cf. the
    # synthetic half-sine single_venus_solar.
    assert "single_venus_pv" in scenarios
    # The noisy variant carries a markedly louder baseline than the stepped one.
    assert (
        scenarios["single_venus_noisy"].base_noise
        > scenarios["single_venus_steps"].base_noise
    )
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
    # Every per-scenario table is collapsed behind one outer section so the
    # comment leads with the aggregate roll-up.
    assert "Per-scenario tables" in md
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


def _two_results() -> list[dict]:
    a = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    b = dict(a, scenario="tiny2", settle_mean_s=a["settle_mean_s"] + 4.0)
    return [a, b]


def test_aggregate_is_per_metric_mean_across_scenarios():
    results = _two_results()
    agg = _aggregate(results)
    assert agg["scenario"] == "AGGREGATE"
    assert agg["n_scenarios"] == 2
    # Every reported metric is the unweighted mean of the two scenarios.
    for key in _REPORT_METRICS:
        expected = (float(results[0][key]) + float(results[1][key])) / 2
        assert agg[key] == round(expected, 1), key


def test_aggregate_omits_metrics_absent_from_every_result():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    old = {k: v for k, v in res.items() if k != "grid_p2p_w"}
    # A base produced before grid_p2p_w existed contributes no value for it, so
    # the aggregate simply omits the key (renderers then show '—').
    assert "grid_p2p_w" not in _aggregate([old])
    assert "grid_p2p_w" in _aggregate([res])


def test_overall_summary_reports_direction():
    base_agg = {"settle_mean_s": 10.0, "overshoot_max_w": 100.0}
    better = {"settle_mean_s": 8.0, "overshoot_max_w": 90.0}
    worse = {"settle_mean_s": 12.0, "overshoot_max_w": 110.0}
    assert "(better)" in _overall_summary(base_agg, better)
    assert "improved" in _overall_summary(base_agg, better)
    assert "(worse)" in _overall_summary(base_agg, worse)


def test_weighted_overall_import_outweighs_export():
    # Same-sized relative move on import vs export: import is weighted ~4x, so
    # an import improvement must move the weighted score more than an equal
    # export improvement, and an import regression dominates an export gain.
    base = {"avoidable_import_wh": 100.0, "avoidable_export_wh": 100.0}
    import_better = {"avoidable_import_wh": 90.0, "avoidable_export_wh": 100.0}
    export_better = {"avoidable_import_wh": 100.0, "avoidable_export_wh": 90.0}
    assert _weighted_overall(base, import_better) < _weighted_overall(
        base, export_better
    )
    # Import up 10%, export down 10%: net worse despite the symmetric trade.
    mixed = {"avoidable_import_wh": 110.0, "avoidable_export_wh": 90.0}
    assert _weighted_overall(base, mixed) > 0


def test_weighted_overall_none_when_nothing_comparable():
    assert _weighted_overall({}, {}) is None
    # A base metric of 0 has no defined relative change → excluded.
    assert _weighted_overall({"overshoot_max_w": 0.0}, {"overshoot_max_w": 5.0}) is None


def test_guardrail_regression_is_flagged():
    base = {"overshoot_max_w": 100.0, "band_crossings_per_h": 50.0, "grid_p2p_w": 40.0}
    # overshoot_max up 30% (past the 5% tolerance), the rest flat.
    head = {"overshoot_max_w": 130.0, "band_crossings_per_h": 50.0, "grid_p2p_w": 40.0}
    regressions = _guardrail_regressions(base, head)
    assert regressions == ["overshoot_max_w +30%"]
    summary = _priority_summary(base, head)
    assert "⚠️" in summary and "overshoot_max_w" in summary
    # A move inside the tolerance is not flagged.
    assert _guardrail_regressions(base, {**base, "overshoot_max_w": 103.0}) == []


def test_priority_summary_clean_when_no_guardrail_regresses():
    base = {"avoidable_import_wh": 100.0, "overshoot_max_w": 100.0}
    head = {"avoidable_import_wh": 80.0, "overshoot_max_w": 80.0}
    summary = _priority_summary(base, head)
    assert "✅" in summary and "(better)" in summary


def test_markdown_compare_leads_with_priority_verdict():
    results = _two_results()
    base = [dict(r, overshoot_max_w=r["overshoot_max_w"] + 50.0) for r in results]
    md = render_markdown_compare(base, results)
    assert "**Priority:" in md
    assert "priority-weighted" in md


def test_overall_summary_denominator_counts_only_compared_metrics():
    # Base carries only 2 of the reported metrics (e.g. an older base from
    # before others existed); the verdict's denominator must be the metrics
    # actually compared, not the full list.
    base = {"settle_mean_s": 10.0, "overshoot_max_w": 100.0}
    head = {"settle_mean_s": 8.0, "overshoot_max_w": 90.0}
    assert "across 2 metrics" in _overall_summary(base, head)


def test_compare_aggregates_uses_only_shared_scenarios():
    def mk(name: str, settle: float) -> dict:
        return {"scenario": name, "seed": 1, "settle_mean_s": settle}

    # 'z' is base-only, 'c' is head-only; both must be excluded so the two
    # aggregates are computed over the same {a, b} population.
    base = [mk("a", 10.0), mk("b", 20.0), mk("z", 999.0)]
    head = [mk("a", 8.0), mk("b", 16.0), mk("c", 1.0)]
    base_agg, head_agg = _compare_aggregates(base, head)
    assert base_agg is not None
    assert base_agg["n_scenarios"] == head_agg["n_scenarios"] == 2
    assert base_agg["settle_mean_s"] == 15.0  # (10+20)/2, no 'z'
    assert head_agg["settle_mean_s"] == 12.0  # (8+16)/2, no 'c'
    # No baseline: head aggregates over all its scenarios, base side is None.
    assert _compare_aggregates(None, head) == (None, _aggregate(head))


def test_render_text_adds_aggregate_only_for_multiple_scenarios():
    multi = render_text(_two_results())
    assert "AGGREGATE (mean across 2 scenarios)" in multi
    # A single scenario would just echo its own numbers, so no aggregate.
    single = render_text([asyncio.run(run_scenario(_tiny_scenario(), seed=3))])
    assert "AGGREGATE" not in single


def test_markdown_compare_leads_with_aggregate_rollup():
    results = _two_results()
    base = [dict(r, overshoot_max_w=r["overshoot_max_w"] + 50.0) for r in results]
    md = render_markdown_compare(base, results)
    assert "**Overall:" in md
    assert "Aggregate — mean across 2 scenarios" in md
    # The overall verdict and the aggregate table both precede any per-scenario
    # collapsible, so the roll-up is the first thing a reviewer sees.
    assert md.index("Aggregate — mean across") < md.index("<details>")


def test_html_report_renders_aggregate_section():
    results = _two_results()
    base = [dict(r, overshoot_max_w=r["overshoot_max_w"] + 50.0) for r in results]
    base_agg, head_agg = _aggregate(base), _aggregate(results)
    h = render_html_report(
        base,
        results,
        report_metrics=_REPORT_METRICS,
        metric_glossary=_METRIC_GLOSSARY,
        fmt_delta=_fmt_delta,
        aggregate=(base_agg, head_agg),
        aggregate_summary=_overall_summary(base_agg, head_agg),
    )
    assert "Aggregate &mdash; mean across 2 scenarios" in h
    assert "improved" in h  # the one-line verdict is rendered


def test_merge_seeds_averages_metrics_and_traces():
    r1 = {
        "scenario": "x",
        "seed": 1,
        "settle_mean_s": 10.0,
        "unsettled_events": 0,
        "grid_trace": [0.0, 10.0],
        "battery_traces": [[1.0, 3.0]],
        "battery_labels": ["B1 HMG-50"],
    }
    r2 = dict(
        r1,
        seed=2,
        settle_mean_s=20.0,
        unsettled_events=1,
        grid_trace=[2.0, 20.0],
        battery_traces=[[3.0, 5.0]],
    )
    merged = _merge_seeds([r1, r2])
    assert merged["seeds"] == [1, 2] and merged["n_seeds"] == 2
    assert "seed" not in merged  # the single-seed field is replaced
    # Scalars and traces are the element-wise mean over seeds.
    assert merged["settle_mean_s"] == 15.0
    assert merged["unsettled_events"] == 0.5
    assert merged["grid_trace"] == [1.0, 15.0]
    assert merged["battery_traces"] == [[2.0, 4.0]]
    # Labels (identical across seeds) pass through.
    assert merged["battery_labels"] == ["B1 HMG-50"]


def test_merge_seeds_single_seed_is_passthrough():
    r = {"scenario": "x", "seed": 1, "settle_mean_s": 10.0}
    # One seed: nothing to average, the original row (with its seed) is returned.
    assert _merge_seeds([r]) is r


def test_seed_label_reads_single_or_averaged():
    assert _seed_label({"seed": 3}) == "seed 3"
    assert _seed_label({"n_seeds": 4, "seeds": [1, 2, 3, 4]}) == "mean of 4 seeds"


def test_markdown_compare_notes_seed_averaging():
    res = asyncio.run(run_scenario(_tiny_scenario(), seed=3))
    head = [dict(res, n_seeds=3, seeds=[1, 2, 3])]
    md = render_markdown_compare([res], head)
    # The reader is told the head figures are seed-averaged (not a single draw).
    assert "Metrics are the per-scenario" in md
    assert "mean of 3 seeds" in md


def test_run_all_runs_seeds_in_parallel_and_merges():
    # End-to-end through the process pool: one real (short) scenario over two
    # seeds collapses to a single seed-averaged row.
    results = _run_all(["single_venus_washer"], [1, 2], {})
    assert len(results) == 1
    r = results[0]
    assert r["scenario"] == "single_venus_washer"
    assert r["n_seeds"] == 2 and r["seeds"] == [1, 2]
    assert "seed" not in r
    for key in _REPORT_METRICS:
        assert key in r


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
        "single_venus_trace",
        "two_venus_trace/fair",
        "two_venus_trace/eff",
        "single_venus_steps_slow",
        "single_venus_solar_slow",
        "two_venus_slow/fair",
        "single_venus_drain",
        "single_venus_fill",
        "phase_imbalance",
        "single_venus_pv",
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


def test_noisy_scenario_has_no_labeled_events_but_scores_aggregates():
    """The pretty-noisy house baseline has no scripted load steps (so the
    step-response metrics read 0), and the loud baseline noise drives the loop:
    it is scored on the sustained-oscillation aggregates, all of which stay
    populated and non-negative."""
    sc = build_scenarios()["single_venus_noisy"]
    assert sc.build_events(__import__("random").Random(1)) == []
    res = asyncio.run(run_scenario(sc, seed=1))
    assert res["events_measured"] == 0
    # The noise keeps the grid moving, so the oscillation aggregates are live.
    assert res["grid_p2p_w"] > 0
    assert res["mean_abs_grid_w"] > 0
    for key in _REPORT_METRICS:
        assert res[key] >= 0, key


def test_trace_scenario_replays_real_load_and_scores_aggregates():
    """The real-trace scenario drives the base load from a recorded household
    trace (no scripted load steps, so the step-response metrics read 0) and is
    scored on the sustained-oscillation/energy aggregates, all populated and
    non-negative. Different seeds slice a different window of the recording, so
    the load actually differs between seeds (genuine structural diversity, not
    just re-randomised noise)."""
    sc = build_scenarios()["single_venus_trace"]
    res = asyncio.run(run_scenario(sc, seed=1))
    assert res["events_measured"] == 0
    assert res["grid_p2p_w"] > 0
    assert res["mean_abs_grid_w"] > 0
    for key in _REPORT_METRICS:
        assert res[key] >= 0, key
    # Two seeds slice different offsets into the recording, so the consumption
    # trace (the scripted load itself) differs between them.
    res2 = asyncio.run(run_scenario(sc, seed=2))
    assert res["consumption_trace"] != res2["consumption_trace"]


def test_trace_eff_variant_concentrates_unlike_fair():
    """The two-Venus trace /eff variant raises min_efficient_power so efficiency
    optimization actually engages on a real load: during a calm stretch it
    concentrates onto one Venus and idles the second (which only cuts in on
    peaks), so its per-battery split is markedly *unequal* where /fair always
    splits evenly. Whether concentration happens depends on the window's load
    level, so seed 2 is used — a calm-window slice where it clearly engages
    (busy-window seeds keep both active, which is the correct load-dependent
    behaviour). Guards against /eff silently degenerating into a copy of /fair."""
    sc = build_scenarios()
    fair = asyncio.run(run_scenario(sc["two_venus_trace/fair"], seed=2))
    eff = asyncio.run(run_scenario(sc["two_venus_trace/eff"], seed=2))

    def imbalance(res):
        b0, b1 = res["battery_traces"]
        return sum(abs(a - b) for a, b in zip(b0, b1, strict=True)) / len(b0)

    def min_battery_idle_fraction(res):
        b0, b1 = res["battery_traces"]
        idle = sum(1 for a, b in zip(b0, b1, strict=True) if min(abs(a), abs(b)) < 20)
        return idle / len(b0)

    # fair splits evenly (tiny imbalance, never idles a unit); eff concentrates.
    assert imbalance(eff) > 5 * imbalance(fair)
    assert imbalance(eff) > 100.0
    assert min_battery_idle_fraction(fair) < 0.1
    assert min_battery_idle_fraction(eff) > 0.5


def test_pv_net_load_scenario_charges_from_real_solar():
    """The real-PV net-load scenario drives PV + load together from a recorded
    Cyprus prosumer (partly-cloudy day). Net export charges the battery (SoC
    rises from its 0.4 start) and the loop tracks the real solar variability
    well (low mean abs grid). Confirms the bidirectional path runs on real PV
    rather than the synthetic half-sine."""
    sc = build_scenarios()["single_venus_pv"]
    res = asyncio.run(run_scenario(sc, seed=2))
    # Export-dominated midday: the pack charges up from the 0.4 start.
    assert res["soc_max"] > 0.45
    # The loop tracks the net-load (battery absorbs the surplus) without large
    # residual grid on average.
    assert res["mean_abs_grid_w"] < 120
    # The battery is actually working (charging), not idle.
    assert min(res["battery_traces"][0]) < -20


def test_phase_imbalance_nulls_each_phase_independently():
    """With one Venus per phase and asymmetric per-phase loads, the active-
    control loop distributes a target to each unit so every phase is nulled —
    not just the aggregate. Each battery discharges to cover its own phase, and
    the total grid converges (low mean abs grid), confirming per-phase
    distribution rather than single-phase steering."""
    sc = build_scenarios()["phase_imbalance"]
    res = asyncio.run(run_scenario(sc, seed=1))
    # The pool tracks the (3-phase) load well overall.
    assert res["mean_abs_grid_w"] < 150
    # All three units (one per phase) are discharging to cover their phase.
    means = [sum(t) / len(t) for t in res["battery_traces"]]
    assert len(means) == 3
    assert all(m > 100 for m in means)


def test_soc_saturation_scenarios_hit_the_edges():
    """The drain/fill scenarios actually push the pack into empty/full
    saturation, exercising the handoff to the grid. Once saturated the battery
    can't help, so the *avoidable* energy is a small fraction of the total grid
    exchange (most of it is unavoidable physics, correctly excluded)."""
    sc = build_scenarios()
    drain = asyncio.run(run_scenario(sc["single_venus_drain"], seed=1))
    fill = asyncio.run(run_scenario(sc["single_venus_fill"], seed=1))
    # Drain empties the pack; grid import takes over and is mostly unavoidable.
    assert drain["soc_min"] < 0.05
    assert drain["import_wh"] > 100
    assert drain["avoidable_import_wh"] < 0.3 * drain["import_wh"]
    # Fill tops out the pack; surplus is exported and mostly unavoidable.
    assert fill["soc_max"] > 0.95
    assert fill["export_wh"] > 100
    assert fill["avoidable_export_wh"] < 0.3 * fill["export_wh"]


def test_slow_meter_variant_tracks_worse_than_default():
    """A slow meter (fresh reading only every ~10 s) makes the loop act on
    badly stale data, so it mistracks far more than the same scenario on the
    realistic ~1 s default meter. The slow variant exists to cover meters that
    emit a point only ~once per 10 s; here we assert it is meaningfully harder,
    not a specific (poor) score."""
    sc = build_scenarios()
    fast = asyncio.run(run_scenario(sc["single_venus_steps"], seed=1))
    slow = asyncio.run(run_scenario(sc["single_venus_steps_slow"], seed=1))
    assert sc["single_venus_steps_slow"].meter_interval_s == 10.0
    # Coarse 10 s sampling drives a large grid swing the 1 s meter avoids.
    assert slow["grid_p2p_w"] > 2 * fast["grid_p2p_w"]


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
            # Isolate the ramp-pacing regression from meter latency (the suite
            # default): this test asserts controller-pacing overshoot bounds.
            meter_latency_s=0.0,
            build_events=events,
        )

    def test_paced_overshoot_bounded(self):
        # Isolate ramp pacing: disable the adaptive grid-state predictor (on by
        # default), which independently bounds the windup this test attributes
        # to pacing — leaving it on would make the unpaced run look bounded too.
        paced = asyncio.run(
            run_scenario(
                self._step_scenario(), seed=5, overrides={"grid_predict_trust": 0}
            )
        )
        unpaced = asyncio.run(
            run_scenario(
                self._step_scenario(),
                seed=5,
                overrides={"pace_base_step": 0, "grid_predict_trust": 0},
            )
        )
        # The unpaced firmware ramp overshoots the step by hundreds of watts;
        # pacing must keep the excursion within ~2 base steps.
        assert paced["overshoot_max_w"] < 110, paced
        assert unpaced["overshoot_max_w"] > 250, unpaced
        # Both must still settle every step event inside its window.
        assert paced["unsettled_events"] == 0, paced
