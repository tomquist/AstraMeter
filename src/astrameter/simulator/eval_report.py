"""Self-contained interactive HTML report for the steering evaluation.

Bundles the per-scenario metrics tables and **zoomable, hover-able** base-vs-head
grid-power charts into a single offline HTML file, which CI uploads as the
``steering-eval`` artifact.  This replaces the Mermaid charts that used to sit
inline in the PR comment: a static image (Mermaid or matplotlib) can't show two
overlapping 1800-point traces clearly, whereas an interactive chart lets a
reviewer zoom into a spike, toggle a series, and read exact values at the
cursor.

The report is fully self-contained — the uPlot library (``report_assets/``,
MIT) and all trace data are inlined, so the downloaded ``.html`` opens straight
from disk with no network or CDN.

The page is built from a static template with a handful of ``__PLACEHOLDER__``
slots (rather than an f-string) so the embedded JavaScript's own braces don't
need escaping.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Sequence
from pathlib import Path

_ASSETS = Path(__file__).parent / "report_assets"

# Series colours (solid; the chart background is dark).  Base/"before" is a warm
# orange, head/"after" a cool blue — the blue-vs-orange contrast that survives
# the common forms of colour-blindness, reading as before -> after.
COLOR_BASE = "#E8A33D"
COLOR_HEAD = "#4C9AFF"
# Net house consumption overlaid (dashed) on the grid chart for context.
COLOR_CONSUMPTION = "#8B949E"

# Categorical palette for the per-battery output chart (one colour per battery,
# cycled if a scenario has more batteries than colours).  Chosen for contrast
# against each other and the dark background.
BATTERY_COLORS = (
    "#4C9AFF",  # blue
    "#E8A33D",  # orange
    "#3FB950",  # green
    "#F85149",  # red
    "#A371F7",  # purple
    "#56D4DD",  # cyan
)


def _asset(name: str) -> str:
    return (_ASSETS / name).read_text(encoding="utf-8")


def _esc(text: object) -> str:
    return html.escape(str(text))


def _metrics_table(
    base: dict | None,
    head: dict,
    report_metrics: Sequence[str],
    fmt_delta: Callable[[float, float], str],
) -> str:
    rows = ["<table><thead><tr><th>Metric</th><th>Base</th><th>Head</th>"]
    rows.append("<th>&Delta;</th></tr></thead><tbody>")
    for key in report_metrics:
        hv = head[key]
        # A base produced before this metric existed has no value to compare.
        if base is None or key not in base:
            rows.append(
                f"<tr><td>{_esc(key)}</td><td>&mdash;</td>"
                f"<td>{_esc(hv)}</td><td>&mdash;</td></tr>"
            )
            continue
        bv = base[key]
        delta = fmt_delta(float(bv), float(hv))
        # Every reported metric is lower-is-better.
        cls = ""
        if float(hv) < float(bv):
            cls = ' class="better"'
        elif float(hv) > float(bv):
            cls = ' class="worse"'
        rows.append(
            f"<tr><td>{_esc(key)}</td><td>{_esc(bv)}</td>"
            f"<td>{_esc(hv)}</td><td{cls}>{_esc(delta)}</td></tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _summary(base: dict | None, head: dict) -> str:
    def pair(key: str, unit: str) -> str:
        if base is None:
            return f"{_esc(key)} {_esc(head[key])}{unit}"
        return f"{_esc(key)} {_esc(base[key])}&rarr;{_esc(head[key])}{unit}"

    return ", ".join(
        (
            pair("settle_mean_s", "s"),
            pair("overshoot_max_w", "W"),
            pair("steady_rms_w", "W"),
        )
    )


def render_html_report(
    base: list[dict] | None,
    head: list[dict],
    *,
    report_metrics: Sequence[str],
    metric_glossary: Sequence[tuple[str, str]],
    fmt_delta: Callable[[float, float], str],
    aggregate: tuple[dict | None, dict] | None = None,
    aggregate_summary: str = "",
    note: str = "",
) -> str:
    """Return a self-contained HTML report comparing *base* and *head*.

    *base* may be ``None`` / empty (no baseline on the PR base branch), in which
    case each scenario renders head-only.

    *aggregate*, when given, is a ``(base_agg, head_agg)`` pair of synthetic
    roll-up rows (means across scenarios). It renders as a leading "Aggregate"
    section so the overall direction of a change is visible before any
    per-scenario table; *aggregate_summary* is a one-line verdict shown with it.
    *note* is an optional caption (e.g. how many seeds were averaged) shown
    under the page heading.
    """
    base_by = {r["scenario"]: r for r in (base or [])}

    glossary_rows = "".join(
        f"<tr><td><code>{_esc(k)}</code></td><td>{_esc(v)}</td></tr>"
        for k, v in metric_glossary
    )

    sections: list[str] = []
    if aggregate is not None:
        agg_base, agg_head = aggregate
        n = agg_head.get("n_scenarios", len(head))
        agg_parts = [f"<h2>Aggregate &mdash; mean across {_esc(n)} scenarios</h2>"]
        if aggregate_summary:
            agg_parts.append(f'<p class="summary">{_esc(aggregate_summary)}</p>')
        agg_parts.append(_metrics_table(agg_base, agg_head, report_metrics, fmt_delta))
        sections.append(f"<section>{''.join(agg_parts)}</section>")
    # Each chart is a generic {durationMin, series:[{label,color,data}, ...]}
    # so the same JS builder draws both the grid (base vs head) and the
    # per-battery output overlays.
    charts: dict[str, dict] = {}
    for idx, res in enumerate(head):
        b = base_by.get(res["scenario"])
        dur = round(float(res.get("duration_h", 0.0)) * 60, 3)
        parts = [
            f"<h2>{_esc(res['scenario'])}</h2>",
            f'<p class="summary">{_summary(b, res)}</p>',
            _metrics_table(b, res, report_metrics, fmt_delta),
        ]

        # Grid power: base vs head overlay.
        head_trace = res.get("grid_trace") or []
        if head_trace:
            gid = f"grid{idx}"
            series = []
            base_trace = (b or {}).get("grid_trace")
            if base_trace:
                series.append(
                    {"label": "base", "color": COLOR_BASE, "data": base_trace}
                )
            series.append({"label": "head", "color": COLOR_HEAD, "data": head_trace})
            consumption = res.get("consumption_trace")
            if consumption:
                # Same scripted load in both runs, so one (dashed) line.
                series.append(
                    {
                        "label": "consumption",
                        "color": COLOR_CONSUMPTION,
                        "data": consumption,
                        "dash": [6, 4],
                    }
                )
            charts[gid] = {"durationMin": dur, "series": series}
            parts.append(
                '<p class="cap">Grid power (W) &mdash; base vs head, with net '
                "house consumption (dashed)</p>"
                f'<div class="chart" id="{gid}"></div>'
            )

        # Per-battery output (head run): one line per battery.
        traces = res.get("battery_traces") or []
        labels = res.get("battery_labels") or []
        if traces:
            bid = f"batt{idx}"
            bseries = [
                {
                    "label": labels[i] if i < len(labels) else f"B{i + 1}",
                    "color": BATTERY_COLORS[i % len(BATTERY_COLORS)],
                    "data": traces[i],
                }
                for i in range(len(traces))
            ]
            charts[bid] = {"durationMin": dur, "series": bseries}
            parts.append(
                '<p class="cap">Battery output (W) &mdash; head, per battery</p>'
                f'<div class="chart" id="{bid}"></div>'
            )

        sections.append(f"<section>{''.join(parts)}</section>")

    legend = (
        f'<span class="key" style="color:{COLOR_BASE}">&#9632; base (before)</span> '
        f'<span class="key" style="color:{COLOR_HEAD}">&#9632; head (after)</span>'
        if base_by
        else f'<span class="key" style="color:{COLOR_HEAD}">&#9632; head</span>'
    )
    note_html = f"<p class='summary'>{_esc(note)}</p>" if note else ""
    body = (
        "<div class='wrap'>"
        "<h1>Steering evaluation &mdash; base vs head</h1>"
        f"<p class='summary'>{legend} &middot; each scenario shows grid power "
        "(base vs head) and the head run's per-battery output. Drag on a chart "
        "to zoom, double-click to reset, click a series in the legend to toggle "
        "it. Lower is better for every metric.</p>"
        f"{note_html}"
        "<details><summary><b>What do these metrics mean?</b></summary>"
        f"<table><thead><tr><th>Metric</th><th>Meaning</th></tr></thead>"
        f"<tbody>{glossary_rows}</tbody></table></details>"
        f"{''.join(sections)}"
        "</div>"
    )

    template = _TEMPLATE
    return (
        template.replace("__UPLOT_CSS__", _asset("uPlot.min.css"))
        .replace("__APP_CSS__", _APP_CSS)
        .replace("__UPLOT_JS__", _asset("uPlot.iife.min.js"))
        .replace("__BODY__", body)
        # JSON is injected last so a stray placeholder token in the data can't
        # be re-expanded. Series colours travel inside this JSON.  Escape '<'
        # (as the JSON-valid '<') so trace data can't break out of the
        # inline <script> via a "</script>" — e.g. a battery label.
        .replace("__CHARTS_JSON__", json.dumps(charts).replace("<", "\\u003c"))
    )


_APP_CSS = """
:root { color-scheme: dark; }
body { background:#0d1117; color:#c9d1d9; margin:0; padding:24px;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 40px 0 0; padding-bottom: 6px;
  border-bottom: 1px solid #21262d; }
.summary { color:#8b949e; font-size: 13px; margin: 6px 0 10px; }
.cap { color:#8b949e; font-size: 12px; margin: 16px 0 2px; }
.key { font-weight: 600; margin-right: 10px; }
table { border-collapse: collapse; font-size: 12.5px; margin: 10px 0; }
th, td { border: 1px solid #21262d; padding: 3px 10px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
th { color:#8b949e; font-weight: 600; }
td.better { color:#3fb950; } td.worse { color:#f85149; }
.chart { width: 100%; margin: 6px 0 2px; }
details > summary { cursor: pointer; color:#8b949e; margin: 6px 0; }
a { color:#58a6ff; }
.u-legend, .u-legend * { color:#c9d1d9 !important; font-size: 12px; }
.u-axis { color:#8b949e; }
"""

_APP_JS = """
(function () {
  var AXIS = "#8b949e", GRID = "#21262d";
  function build(id, spec) {
    var el = document.getElementById(id);
    if (!el || typeof uPlot === "undefined") return;
    var s = spec.series || [];
    if (!s.length || !s[0].data) return;
    var n = s[0].data.length, dur = spec.durationMin;
    var xs = new Array(n);
    for (var i = 0; i < n; i++) xs[i] = n > 1 ? (i / (n - 1)) * dur : 0;
    var data = [xs], series = [{}];
    s.forEach(function (ser) {
      data.push(ser.data);
      var def = { label: ser.label, stroke: ser.color, width: 1.25,
        points: { show: false } };
      if (ser.dash) def.dash = ser.dash;
      series.push(def);
    });
    function opts() {
      return {
        width: el.clientWidth || 900,
        height: 320,
        scales: { x: { time: false } },
        legend: { live: true },
        cursor: { drag: { x: true, y: false }, focus: { prox: 24 } },
        axes: [
          { label: "minutes", stroke: AXIS, grid: { stroke: GRID },
            ticks: { stroke: GRID } },
          { label: "watts", stroke: AXIS, grid: { stroke: GRID },
            ticks: { stroke: GRID } }
        ],
        series: series
      };
    }
    var u = new uPlot(opts(), data, el);
    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        u.setSize({ width: el.clientWidth || 900, height: 320 });
      }).observe(el);
    }
  }
  var CHARTS = __CHARTS_JSON__;
  Object.keys(CHARTS).forEach(function (id) { build(id, CHARTS[id]); });
})();
"""

_TEMPLATE = (
    "<!doctype html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
    "<title>Steering evaluation</title>\n"
    "<style>__UPLOT_CSS__</style>\n<style>__APP_CSS__</style>\n</head>\n"
    "<body>\n__BODY__\n"
    "<script>__UPLOT_JS__</script>\n"
    "<script>" + _APP_JS + "</script>\n"
    "</body>\n</html>\n"
)
