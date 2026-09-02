"""Base-vs-head comparison of evaluation results: seed merging, the
cross-scenario aggregate, the two one-line verdicts and their text / Markdown
renderings (the CI PR comment)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass

from .eval_metrics import (
    EVENT_WINDOW_S,
    FEEDIN_CT_PER_KWH,
    OSC_BAND_W,
    RETAIL_CT_PER_KWH,
    SETTLE_BAND_W,
    SETTLE_HOLD_S,
    STEADY_EXCLUDE_S,
)

# Metrics shown in tables, in order. Every metric improves downward.
_REPORT_METRICS = [
    "settle_mean_s",
    "settle_p95_s",
    "unsettled_events",
    "overshoot_mean_w",
    "overshoot_max_w",
    "band_crossings_per_h",
    "grid_p2p_w",
    "grid_rms_w",
    "steady_rms_w",
    "mean_abs_grid_w",
    "share_imbalance_w",
    "avoidable_import_wh",
    "avoidable_export_wh",
    "cost_regret_ct",
    "battery_travel_w_per_h",
]

# Relative weights for the priority verdict, encoding what a self-consumption
# controller is judged on: cost regret is the money north-star; avoidable
# import is paid at retail while export earns only feed-in (so ~4x); sign-flip
# overshoot and hunting are do-no-harm; battery travel and share imbalance
# (issue #523) are cycle life; settle time merely enables the rest.
_METRIC_WEIGHTS: dict[str, float] = {
    "cost_regret_ct": 4.0,
    "avoidable_import_wh": 4.0,
    "avoidable_export_wh": 1.0,
    "overshoot_max_w": 3.0,
    "band_crossings_per_h": 2.0,
    "battery_travel_w_per_h": 2.0,
    "share_imbalance_w": 2.0,
    "grid_p2p_w": 1.5,
    "grid_rms_w": 1.0,
    "overshoot_mean_w": 1.0,
    "mean_abs_grid_w": 1.0,
    "steady_rms_w": 0.5,
    "settle_mean_s": 0.5,
    "unsettled_events": 0.5,
    "settle_p95_s": 0.3,
}

# Do-no-harm guardrails: regressing one past _GUARDRAIL_TOLERANCE is flagged
# however good the weighted score looks, so a smoother controller can't trade
# self-consumption for stability. Overshoot flips the grid sign (worse than no
# battery); band crossings and peak-to-peak are sustained hunting; avoidable
# import is self-consumption missed at the retail tariff and free to fix; cost
# regret is real money lost whichever component moved. Avoidable export is
# deliberately not one: it conflates over-discharge with the legitimate choice
# to export rather than pay a charge round-trip, and misses DC-only packs.
_GUARDRAIL_METRICS = (
    "overshoot_max_w",
    "band_crossings_per_h",
    "grid_p2p_w",
    "avoidable_import_wh",
    "cost_regret_ct",
)
_GUARDRAIL_TOLERANCE = 0.05

# One line per `_REPORT_METRICS` entry, in the same order, rendered as a
# collapsible glossary in the CI PR comment. Definitions: `_compute_metrics`.
_METRIC_GLOSSARY = [
    (
        "settle_mean_s",
        f"Mean seconds after a load/PV step for grid power to return inside the "
        f"±{SETTLE_BAND_W:g} W settle band and hold for {SETTLE_HOLD_S:g} s "
        f"(reaction speed).",
    ),
    (
        "settle_p95_s",
        "95th-percentile settle time — the slow tail of reactions.",
    ),
    (
        "unsettled_events",
        f"Number of disturbance events that never settled within the "
        f"{EVENT_WINDOW_S / 60:g}-minute measurement window.",
    ),
    (
        "overshoot_mean_w",
        "Mean overshoot (W): how far grid power swings past zero to the "
        "opposite sign after an event.",
    ),
    (
        "overshoot_max_w",
        "Worst-case overshoot (W) across all events.",
    ),
    (
        "band_crossings_per_h",
        f"Sign flips per hour across the ±{OSC_BAND_W:g} W hysteresis band — "
        f"oscillation / hunting frequency.",
    ),
    (
        "grid_p2p_w",
        "Sustained peak-to-peak grid swing (95th - 5th percentile) over the "
        "whole run — oscillation amplitude. Non-zero whenever the loop keeps "
        "hunting, including continuous oscillation the step-response metrics "
        "(settle/overshoot) miss.",
    ),
    (
        "grid_rms_w",
        "RMS grid power (W) over the *whole* run, transients included — the L2 "
        "tracking error: how cleanly the loop held zero, penalising big "
        "excursions (overshoot, swings) far harder than a small steady offset. "
        "Pairs with battery_travel_w_per_h as the control-effort term.",
    ),
    (
        "steady_rms_w",
        f"RMS grid power (W) during steady state (excluding the "
        f"{STEADY_EXCLUDE_S:g} s after each event) — residual jitter when "
        f"nothing is changing.",
    ),
    (
        "mean_abs_grid_w",
        "Mean absolute grid power (W) over the whole run — overall tracking accuracy.",
    ),
    (
        "share_imbalance_w",
        "Time-weighted watts misallocated between batteries sharing a phase (sum "
        "of each battery's deviation from the even fair share) — 0 when the pool "
        "splits load evenly, higher when one battery is left lopsided (issue "
        "#523). 0 for scenarios with at most one battery per phase.",
    ),
    (
        "avoidable_import_wh",
        "Energy imported from the grid (Wh) the battery could have supplied "
        "(it had charge and discharge headroom) — missed self-consumption.",
    ),
    (
        "avoidable_export_wh",
        "Energy exported to the grid (Wh) an AC-chargeable battery could have "
        "absorbed (it had room and charge headroom) — missed charging.",
    ),
    (
        "cost_regret_ct",
        f"Money north-star: electricity bill (eurocents, import @ "
        f"{RETAIL_CT_PER_KWH:g} ct/kWh, export @ {FEEDIN_CT_PER_KWH:g} ct/kWh) "
        f"over what a perfect-foresight optimal battery would have paid on the "
        f"same load. Ungameable (both grid directions cost); 0 = matched the "
        f"optimum. The single number that says how much the controller left on "
        f"the table.",
    ),
    (
        "battery_travel_w_per_h",
        "Total absolute change in battery setpoints per hour (W/h) — control "
        "effort / actuator wear; lower is smoother.",
    ),
]

# The three figures a scenario's collapsed header shows: (key, label, unit).
_HEADLINE_METRICS = (
    ("settle_mean_s", "settle", "s"),
    ("overshoot_max_w", "overshoot", "W"),
    ("steady_rms_w", "RMS", "W"),
)


def _metric_ndp(key: str) -> int:
    """Rounding precision: 2 dp for eurocent costs, whose per-scenario regret
    clusters at fractions of a cent (1 dp would quantize the guardrail's 5%
    test into noise); 1 dp for everything else."""
    return 2 if key.endswith("_ct") else 1


def _mean_value(values: list, ndp: int = 1):
    """Average a homogeneous list of result values across seeds.

    Recurses into nested lists (a battery's trace, the list of per-battery
    traces) so traces average element-by-element; string lists (battery
    labels, identical across seeds) pass through as the first value. *ndp* is
    the rounding precision for numeric values (see :func:`_metric_ndp`)."""
    v0 = values[0]
    if isinstance(v0, bool):
        return v0
    if isinstance(v0, (int, float)):
        return round(sum(float(v) for v in values) / len(values), ndp)
    if isinstance(v0, list):
        if v0 and isinstance(v0[0], str):
            return v0  # labels are identical across seeds
        return [_mean_value([v[i] for v in values], ndp) for i in range(len(v0))]
    return v0  # strings / anything else: identical across seeds


def _merge_seeds(per_seed: list[dict]) -> dict:
    """Collapse one scenario's per-seed results into a single averaged row.

    Every numeric metric (and every trace, element-wise) becomes the mean over
    the seeds. A lone seed is returned unchanged (keeps its ``seed`` field);
    a merged row carries ``seeds`` / ``n_seeds`` instead."""
    if len(per_seed) == 1:
        return per_seed[0]
    merged: dict = {
        "scenario": per_seed[0]["scenario"],
        "seeds": [r["seed"] for r in per_seed],
        "n_seeds": len(per_seed),
    }
    for key in per_seed[0]:
        if key in ("scenario", "seed"):
            continue
        merged[key] = _mean_value([r[key] for r in per_seed], _metric_ndp(key))
    return merged


def _aggregate(results: list[dict]) -> dict:
    """Collapse a result list into one synthetic "AGGREGATE" row: each reported
    metric's unweighted mean across the scenarios that carry it (a base
    produced before a metric existed simply doesn't contribute that key).

    Means are scale-mixing on purpose: they're a rough headline, while the
    per-metric relative deltas (and :func:`_overall_summary`) are the
    unit-independent read on direction.
    """
    agg: dict = {"scenario": "AGGREGATE", "n_scenarios": len(results)}
    for key in _REPORT_METRICS:
        vals = [float(r[key]) for r in results if key in r]
        if vals:
            agg[key] = round(sum(vals) / len(vals), _metric_ndp(key))
    return agg


def _compare_aggregates(
    base: list[dict] | None, head: list[dict]
) -> tuple[dict | None, dict]:
    """Aggregate base and head over the scenarios they **share**, so the
    verdicts compare like for like when a PR adds or drops a scenario. Without
    a baseline, head is aggregated over all its scenarios."""
    if not base:
        return None, _aggregate(head)
    shared = {r["scenario"] for r in base} & {r["scenario"] for r in head}
    base_agg = _aggregate([r for r in base if r["scenario"] in shared])
    head_agg = _aggregate([r for r in head if r["scenario"] in shared])
    return base_agg, head_agg


def _paired(
    base_agg: dict, head_agg: dict, keys: Iterable[str]
) -> Iterator[tuple[str, float, float]]:
    """``(key, base, head)`` for each of *keys* present on both sides."""
    for key in keys:
        if key in base_agg and key in head_agg:
            yield key, float(base_agg[key]), float(head_agg[key])


def _overall_change(
    base_agg: dict, head_agg: dict
) -> tuple[int, int, int, float | None]:
    """Count improved / regressed / unchanged aggregate metrics and the mean
    relative change across them (``None`` when nothing is comparable).

    Lower is better, so a negative mean is an overall improvement. A metric
    whose base is 0 counts toward the direction tallies but has no defined
    relative change, so it stays out of the percentage."""
    improved = regressed = unchanged = 0
    deltas: list[float] = []
    for _key, bv, hv in _paired(base_agg, head_agg, _REPORT_METRICS):
        if hv < bv:
            improved += 1
        elif hv > bv:
            regressed += 1
        else:
            unchanged += 1
        if bv != 0:
            deltas.append((hv - bv) / abs(bv))
    mean_pct = sum(deltas) / len(deltas) * 100.0 if deltas else None
    return improved, regressed, unchanged, mean_pct


def _overall_summary(base_agg: dict, head_agg: dict) -> str:
    """One-line verdict for the aggregate: how many metrics moved each way and
    the mean relative change (lower is better)."""
    improved, regressed, unchanged, mean_pct = _overall_change(base_agg, head_agg)
    if mean_pct is None:
        trend = "no comparable metrics"
    elif mean_pct < 0:
        trend = f"mean {mean_pct:.1f}% (better)"
    elif mean_pct > 0:
        trend = f"mean +{mean_pct:.1f}% (worse)"
    else:
        trend = "mean 0% (unchanged)"
    return (
        f"{improved} improved, {regressed} regressed, {unchanged} unchanged "
        f"across {improved + regressed + unchanged} metrics — {trend}"
    )


def _weighted_overall(base_agg: dict, head_agg: dict) -> float | None:
    """Mean relative change weighted by :data:`_METRIC_WEIGHTS`; negative is an
    improvement, ``None`` when no comparable metric has a defined relative
    change."""
    num = den = 0.0
    for key, bv, hv in _paired(base_agg, head_agg, _METRIC_WEIGHTS):
        if bv == 0:
            continue
        num += _METRIC_WEIGHTS[key] * (hv - bv) / abs(bv)
        den += _METRIC_WEIGHTS[key]
    if den == 0:
        return None
    return num / den * 100.0


def _guardrail_regressions(base_agg: dict, head_agg: dict) -> list[str]:
    """Guardrail metrics that regressed past :data:`_GUARDRAIL_TOLERANCE`, as
    ``metric +N%`` — or ``metric 0→N`` when the base was zero, which is the
    worst breach (nothing became something) and has no relative change."""
    out: list[str] = []
    for key, bv, hv in _paired(base_agg, head_agg, _GUARDRAIL_METRICS):
        if bv == 0:
            if hv > 0:
                out.append(f"{key} 0→{hv:g}")
        elif (hv - bv) / bv > _GUARDRAIL_TOLERANCE:
            out.append(f"{key} +{(hv - bv) / bv * 100.0:.0f}%")
    return out


def _priority_summary(base_agg: dict, head_agg: dict) -> str:
    """One-line priority-weighted verdict plus the guardrail status."""
    pct = _weighted_overall(base_agg, head_agg)
    if pct is None:
        score = "no comparable metrics"
    elif pct < 0:
        score = f"{pct:.1f}% (better)"
    elif pct > 0:
        score = f"+{pct:.1f}% (worse)"
    else:
        score = "0% (unchanged)"
    regressions = _guardrail_regressions(base_agg, head_agg)
    if regressions:
        guard = "⚠️ do-no-harm guardrail regressed: " + ", ".join(regressions)
    else:
        guard = "✅ no do-no-harm guardrail regressions"
    return f"priority-weighted {score} — {guard}"


def _seed_label(res: dict) -> str:
    """``seed N`` for a single run, ``mean of N seeds`` once merged."""
    if res.get("n_seeds"):
        return f"mean of {res['n_seeds']} seeds"
    return f"seed {res.get('seed', '?')}"


def _fmt_delta(base: float, head: float) -> str:
    if base == head:
        return "="
    if base == 0:
        return f"{head - base:+g}"
    return f"{(head - base) / abs(base) * 100.0:+.0f}%"


@dataclass(frozen=True)
class _MetricRow:
    """One table row. *base* and *delta* are ``None`` when the base has no
    value for this metric (produced before it existed)."""

    key: str
    base: float | None
    head: float | None
    delta: str | None

    @property
    def direction(self) -> int:
        """-1 improved, +1 regressed, 0 unchanged or incomparable."""
        if self.base is None or self.head is None:
            return 0
        if float(self.head) < float(self.base):
            return -1
        if float(self.head) > float(self.base):
            return 1
        return 0


def _metric_rows(
    base: dict | None,
    head: dict,
    metrics: Sequence[str] = _REPORT_METRICS,
    fmt_delta: Callable[[float, float], str] = _fmt_delta,
) -> list[_MetricRow]:
    rows = []
    for key in metrics:
        hv = head.get(key)
        if base is not None and key in base and hv is not None:
            rows.append(
                _MetricRow(key, base[key], hv, fmt_delta(float(base[key]), float(hv)))
            )
        else:
            rows.append(_MetricRow(key, None, hv, None))
    return rows


def _headline(base: dict | None, head: dict) -> str:
    """``settle 10→12s, overshoot …W, RMS …W`` for a scenario's header."""
    parts = []
    for key, label, unit in _HEADLINE_METRICS:
        if base is None or key not in base:
            value = f"{head[key]}"
        else:
            value = f"{base[key]}→{head[key]}"
        parts.append(f"{label} {value}{unit}")
    return ", ".join(parts)


def _seeds_phrase(results: list[dict]) -> str:
    """`mean of N seeds` / `single seed` describing how a result set was run."""
    n = max((int(r.get("n_seeds", 1)) for r in results), default=1)
    return f"mean of {n} seeds" if n > 1 else "single seed"


def _seeds_caption(base: list[dict] | None, head: list[dict]) -> str:
    """One sentence on the seed count behind each side, or ``""`` when both ran a
    single seed (so the note only appears once figures are seed-averaged)."""
    hp = _seeds_phrase(head)
    bp = _seeds_phrase(base) if base else None
    if hp == "single seed" and (bp is None or bp == "single seed"):
        return ""
    if bp is None or bp == hp:
        return f"Metrics are the per-scenario {hp}."
    return f"Metrics are the per-scenario mean over seeds (base: {bp}, head: {hp})."


def render_text(results: list[dict]) -> str:
    lines = []
    # A single aggregate row up top makes an overall read possible without
    # eyeballing every scenario (skipped for a lone scenario — it'd just echo
    # that scenario's own numbers).
    if len(results) > 1:
        agg = _aggregate(results)
        lines.append(f"== AGGREGATE (mean across {agg['n_scenarios']} scenarios)")
        for key in _REPORT_METRICS:
            if key in agg:
                lines.append(f"  {key:<24} {agg[key]}")
        lines.append("")
    for res in results:
        lines.append(
            f"== {res['scenario']} ({_seed_label(res)}, "
            f"{res['duration_h']}h, {res['events_measured']} events)"
        )
        for key in _REPORT_METRICS:
            lines.append(f"  {key:<24} {res[key]}")
    return "\n".join(lines)


def _md_metric_table(base: dict | None, head: dict) -> list[str]:
    rows = ["| Metric | Base | Head | Δ |", "|---|---:|---:|---:|"]
    for row in _metric_rows(base, head):
        bv = "—" if row.base is None else row.base
        hv = "—" if row.head is None else row.head
        rows.append(f"| {row.key} | {bv} | {hv} | {row.delta or '—'} |")
    return rows


def render_markdown_compare(
    base: list[dict], head: list[dict], *, report_available: bool = False
) -> str:
    """Markdown before/after tables for the CI PR comment.

    The interactive grid-power charts live in the HTML report CI uploads as
    the ``steering-eval`` artifact; set *report_available* when one is being
    produced, so a plain ``--compare`` run doesn't promise a report that
    doesn't exist.
    """
    base_by = {r["scenario"]: r for r in base}
    base_agg, head_agg = _compare_aggregates(base, head)
    out = [
        "### Steering evaluation (base vs head)",
        "",
    ]
    # Lead with the aggregate so a reviewer sees the overall direction before
    # any per-scenario table — the whole point of the roll-up.
    if base_agg is not None:
        out.append(f"**Overall: {_overall_summary(base_agg, head_agg)}.**")
        out.append("")
        out.append(f"**Priority: {_priority_summary(base_agg, head_agg)}.**")
        out.append("")
    out += [
        "Lower is better for every metric. See "
        "`src/astrameter/simulator/eval_metrics.py` for definitions.",
        "",
    ]
    caption = _seeds_caption(base, head)
    if caption:
        out += [f"_{caption}_", ""]
    out += [
        f"#### Aggregate — mean across {head_agg['n_scenarios']} scenarios",
        "",
    ]
    out += _md_metric_table(base_agg, head_agg)
    out.append("")
    if report_available:
        out += [
            "📊 **Interactive grid-power charts** (zoom / hover / toggle series) "
            "are in the self-contained `steering-eval-report.html` report — see "
            "the link below (it opens directly in the browser).",
            "",
        ]
    out += [
        "<details><summary><b>What do these metrics mean?</b></summary>",
        "",
        "| Metric | Meaning |",
        "|---|---|",
    ]
    out.extend(f"| `{key}` | {desc} |" for key, desc in _METRIC_GLOSSARY)
    out.append("")
    out.append("</details>")
    out.append("")
    # Every per-scenario table sits behind one outer section so the comment
    # leads with the roll-up and verdicts.
    out.append(
        f"<details><summary><b>Per-scenario tables</b> "
        f"({len(head)} scenarios)</summary>"
    )
    out.append("")
    for res in head:
        b = base_by.get(res["scenario"])
        out.append(
            f"<details><summary><b>{res['scenario']}</b> — "
            f"{_headline(b, res)}</summary>"
        )
        out.append("")
        out.extend(_md_metric_table(b, res))
        out.append("")
        out.append("</details>")
    out.append("")
    out.append("</details>")
    missing = [
        r["scenario"]
        for r in base
        if r["scenario"] not in {h["scenario"] for h in head}
    ]
    if missing:
        out.append("")
        out.append(f"_Scenarios only in base: {', '.join(missing)}_")
    return "\n".join(out)
