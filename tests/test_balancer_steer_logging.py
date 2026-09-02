"""The per-consumer DEBUG line that explains each command (discussion #625).

Reconstructing an allocation from a user's log used to mean guessing: the
CT002 response carries only the final reading, so a manual target, a
consumer the efficiency layer never promoted, and a genuinely small auto
share all look identical on the wire.  Worse, a paced command hides the
intent behind it -- a loop asking for +1000 W and a loop asking for +30 W
put the same 30 W on the wire.  These tests pin the fields that tell those
cases apart.
"""

from __future__ import annotations

import logging

import pytest

from astrameter.config.logger import logger
from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
)

MANUAL, AUTO, IDLE = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"


def _reports() -> dict:
    return {
        MANUAL: ConsumerReport(device_type="HMJ-2", phase="A", power=400),
        AUTO: ConsumerReport(device_type="HMJ-2", phase="A", power=350),
        IDLE: ConsumerReport(device_type="HMJ-2", phase="A", power=0),
    }


def _steer(caplog: pytest.LogCaptureFixture, **cfg) -> dict[str, str]:
    """Drive one poll of each mode; return the steer line per consumer."""
    clock = [1000.0]
    lb = LoadBalancer(
        config=BalancerConfig(fair_distribution=True, **cfg),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=60,
        clock=lambda: clock[0],
    )
    reports = _reports()
    manual, inactive = frozenset({MANUAL}), frozenset({IDLE})
    with caplog.at_level(logging.DEBUG, logger="astrameter"):
        for cid in reports:
            if cid in inactive:
                mode = ConsumerMode("inactive")
            elif cid in manual:
                mode = ConsumerMode("manual", 800.0)
            else:
                mode = ConsumerMode("auto")
            lb.compute_target(cid, mode, reports, 1000.0, inactive, manual, (0, 1000.0))
    lines = [m for m in caplog.messages if m.startswith("CT002 steer ")]
    return {line.split()[2].rstrip(":"): line for line in lines}


def test_the_line_matches_the_firmware_format_byte_for_byte(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One reader parses support logs from both stacks, so the format is shared.

    ``format_steer_log`` in ``esphome/components/ct002/balancer.cpp`` renders
    the same fields, in this order, at this precision;
    ``host_balancer_test.cpp`` asserts the C++ side against the literal below.
    Python is canonical -- change it here first, then mirror.
    """
    lines = _steer(caplog)
    assert lines[MANUAL] == (
        "CT002 steer aaaaaaaaaaaa: mode=manual=800 rotation=active weight=1.00 "
        "grid=1000 ctrl=- share=- reported=400 intent=800 send=400 "
        "unpaced=400 pace_cap=0 sat=0.00"
    )


def test_every_consumer_gets_a_line_naming_its_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lines = _steer(caplog)
    assert set(lines) == {MANUAL, AUTO, IDLE}
    # The distinction the wire cannot make: all three can send the same bytes.
    assert "mode=manual=800" in lines[MANUAL]
    assert "mode=auto" in lines[AUTO]
    assert "mode=inactive" in lines[IDLE]
    assert "rotation=active" in lines[AUTO]


def test_the_auto_line_separates_intent_from_what_pacing_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow ramp is either a small share or a tight pace cap -- say which."""
    lines = _steer(caplog, pace_base_step=30, pace_max_step=100)
    fields = dict(f.split("=", 1) for f in lines[AUTO].split() if "=" in f)
    # The loop wanted the whole 1000 W error; pacing put 30 W on the wire.
    assert float(fields["unpaced"]) == pytest.approx(1000, abs=1)
    assert float(fields["send"]) == pytest.approx(30, abs=1)
    assert float(fields["pace_cap"]) == pytest.approx(30, abs=1)
    # ...and the grid figure the loop acted on is recorded next to the meter's,
    # so a predictor that disagrees with the meter is visible rather than
    # inferred.
    assert float(fields["grid"]) == pytest.approx(1000, abs=1)
    assert float(fields["ctrl"]) == pytest.approx(1000, abs=1)


def test_allocation_figures_are_blank_where_they_do_not_apply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Manual and inactive consumers never reach the allocator.

    They must not inherit the previous consumer's ``ctrl``/``share`` -- that
    would read as a real figure and send the next reader down a false trail.
    """
    lines = _steer(caplog)
    for cid in (MANUAL, IDLE):
        assert "ctrl=-" in lines[cid], lines[cid]
        assert "share=-" in lines[cid], lines[cid]


def test_nothing_is_computed_when_debug_logging_is_off() -> None:
    """The guard, not just the output.

    ``logger.debug`` discards the line at INFO, but Python evaluates its
    arguments first -- a dozen renderings per consumer per poll, plus the
    probe-participant set, for output nobody reads.  Dropping the level check
    would leave every assertion above green while a normal install paid for
    diagnostics it never sees, so pin it: with DEBUG off, the line must not be
    built at all.
    """
    clock = [1000.0]
    lb = LoadBalancer(
        config=BalancerConfig(fair_distribution=True),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=60,
        clock=lambda: clock[0],
    )
    reports = _reports()

    def explode(_value: float | None) -> str:  # pragma: no cover - never runs
        raise AssertionError("steer log rendered a field with DEBUG disabled")

    # ``_diag_num`` is reachable only from the logging path -- the control path
    # never renders. (``_probe_participants`` would be the wrong canary: the
    # saturation gate calls it too, so it fires on a healthy poll.)
    lb._diag_num = explode  # type: ignore[method-assign]
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        for cid in reports:
            lb.compute_target(
                cid,
                ConsumerMode("auto"),
                reports,
                1000.0,
                frozenset(),
                frozenset(),
                (0, 1000.0),
            )
    finally:
        logger.setLevel(previous)


def test_a_consumer_the_efficiency_layer_idled_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The field that matters most, and the one a wire trace cannot supply.

    With ``MIN_EFFICIENT_POWER`` set, the efficiency layer idles surplus
    batteries at low demand.  Such a consumer is steered to zero and is
    byte-identical on the wire to one switched off by hand, to one parked on a
    manual target of 0, and to one given a zero distribution weight -- the
    ambiguity that made a real report (discussion #625) unreadable.
    """
    clock = [1000.0]
    lb = LoadBalancer(
        config=BalancerConfig(fair_distribution=True, min_efficient_power=200),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=60,
        clock=lambda: clock[0],
    )
    reports = {
        cid: ConsumerReport(device_type="HMJ-2", phase="A", power=50)
        for cid in ("dddddddddddd", "eeeeeeeeeeee", "ffffffffffff")
    }
    # Demand well under one battery's efficient slice, so the layer idles two.
    with caplog.at_level(logging.DEBUG, logger="astrameter"):
        for poll in range(3):
            for cid in reports:
                lb.compute_target(
                    cid,
                    ConsumerMode("auto"),
                    reports,
                    60.0,
                    frozenset(),
                    frozenset(),
                    (poll, 60.0),
                )
            clock[0] += 3.0
    idled = {cid for cid in reports if cid in lb._deprioritized}
    assert idled, "expected the efficiency layer to idle at least one battery"
    lines = [m for m in caplog.messages if m.startswith("CT002 steer ")]
    for cid in idled:
        mine = [line for line in lines if cid in line]
        assert mine and "rotation=deprioritized" in mine[-1], mine[-1:]
