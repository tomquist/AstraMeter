"""Unit tests for the load model and its real-trace loader."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from .load_model import load_net_trace, load_power_trace

_TRACE = Path(__file__).parent / "traces" / "rae_household.csv"
_NET_TRACE = Path(__file__).parent / "traces" / "cyprus_netload.csv"


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
    # The fixture is a multi-hour 1-second window: thousands of samples, evenly
    # spaced at 1 s, all non-negative watts.
    assert len(trace) > 3600
    assert trace[0][0] == 0.0
    assert all(b >= 0.0 for _, b in trace)
    spacings = {round(b[0] - a[0]) for a, b in itertools.pairwise(trace)}
    assert spacings == {1}
    # The window spans real dynamic range (quiet baseline + cooking spikes), so
    # it exercises both the steering band and the saturated-grid regime.
    watts = [w for _, w in trace]
    assert min(watts) < 900 and max(watts) > 2500


@pytest.mark.parametrize(
    "content",
    [
        "",  # empty file
        "# only a comment\n#\n\n",  # comments/blank only
        "t_s,watts\nnope,nope\nfoo,bar\n",  # header + invalid-only rows
    ],
)
def test_load_power_trace_raises_without_valid_rows(tmp_path, content):
    p = tmp_path / "bad.csv"
    p.write_text(content)
    with pytest.raises(ValueError):
        load_power_trace(p)


def test_load_net_trace_reads_three_columns(tmp_path):
    p = tmp_path / "n.csv"
    p.write_text("# c\nt_s,load_w,pv_w\n0,300,0\n30,250.5,1200\n60,400,900\n")
    assert load_net_trace(p) == [
        (0.0, 300.0, 0.0),
        (30.0, 250.5, 1200.0),
        (60.0, 400.0, 900.0),
    ]


def test_vendored_net_trace_loads():
    trace = load_net_trace(_NET_TRACE)
    assert len(trace) > 600  # multi-hour 30 s window
    assert trace[0][0] == 0.0
    spacings = {round(b[0] - a[0]) for a, b in itertools.pairwise(trace)}
    assert spacings == {30}
    # Real partly-cloudy midday: substantial PV, and load/PV both non-negative.
    assert max(pv for _, _, pv in trace) > 2000
    assert all(load >= 0 and pv >= 0 for _, load, pv in trace)


@pytest.mark.parametrize(
    "content",
    [
        "",  # empty file
        "# only a comment\n#\n\n",  # comments/blank only
        "t_s,load_w,pv_w\nx,y,z\n1,2\n",  # header, invalid + too-few-column rows
    ],
)
def test_load_net_trace_raises_without_valid_rows(tmp_path, content):
    p = tmp_path / "bad.csv"
    p.write_text(content)
    with pytest.raises(ValueError):
        load_net_trace(p)
