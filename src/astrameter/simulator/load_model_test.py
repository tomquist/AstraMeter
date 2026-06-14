"""Unit tests for the load model and its real-trace loader."""

from __future__ import annotations

import itertools
from pathlib import Path

from .load_model import load_power_trace

_TRACE = Path(__file__).parent / "traces" / "uci_household.csv"


def test_load_power_trace_skips_comments_and_header(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text(
        "# a comment\n"
        "#\n"
        "t_s,watts\n"
        "0,100\n"
        "\n"  # blank line
        "60,250.5\n"
        "120,90\n"
    )
    assert load_power_trace(p) == [(0.0, 100.0), (60.0, 250.5), (120.0, 90.0)]


def test_load_power_trace_sorts_by_time(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("120,3\n0,1\n60,2\n")
    assert load_power_trace(p) == [(0.0, 1.0), (60.0, 2.0), (120.0, 3.0)]


def test_vendored_household_trace_loads():
    trace = load_power_trace(_TRACE)
    # The fixture is a multi-hour 1-minute window: hundreds of samples, evenly
    # spaced at 60 s, all non-negative watts.
    assert len(trace) > 300
    assert trace[0][0] == 0.0
    assert all(b >= 0.0 for _, b in trace)
    spacings = {round(b[0] - a[0]) for a, b in itertools.pairwise(trace)}
    assert spacings == {60}
    # The window spans real dynamic range (quiet baseline + cooking spikes), so
    # it exercises both the steering band and the saturated-grid regime.
    watts = [w for _, w in trace]
    assert min(watts) < 600 and max(watts) > 2500
