#!/usr/bin/env python3
"""Reproduction for issue #522 — pace settings break saturation detection.

The reporter's posted debug log (issue522_logs.txt, alongside this file) is the
ground truth. Setup decoded from it:

  * Venus E3 (VNSE3-0, phase A) is FULL — it reports power=0 every poll.
  * Venus A  (VNSA-0,  phase B) is healthy and charging (ramps -341 -> -569 W).
  * ~932 W PV surplus, almost all on the full battery's phase A.
  * Their tuning:  PACE_BASE_STEP = 15,  MIN_TARGET_FOR_SATURATION = 20.

Two parts:

  PART A — log replay (ground truth). Feeds Venus E3's actual (=0) and the
  exact reading the balancer sent it (total_power = -15 every poll, read
  straight out of the log) through the real SaturationTracker with the
  reporter's config. The score never leaves 0: because |reading|=15 is below
  MIN_TARGET=20, every tick takes the "idle" decay branch, and the stall-timeout
  rescue (which would force the score to 1.0) also can't fire because it too
  requires |target| >= MIN_TARGET. So the full battery is never recognised as
  saturated and keeps its fair share of a surplus it cannot absorb.

  PART B — balancer end-to-end. Replays the same scenario through the real
  LoadBalancer and shows the resulting symptom and the sharp dependence on
  PACE_BASE_STEP vs MIN_TARGET_FOR_SATURATION.

Run: uv run python tests/issue_repros/repro_issue522.py
"""

from __future__ import annotations

import os
import re
import time

from astrameter.ct002.balancer import (
    BalancerConfig,
    BalancerConsumerState,
    ConsumerMode,
    LoadBalancer,
    SaturationTracker,
)

LOG_PATH = os.path.join(os.path.dirname(__file__), "issue522_logs.txt")

# Reporter's CT003 tuning.
MIN_TARGET = 20
SAT_ALPHA = 0.9
DECAY = 0.995
GRACE_S = 90.0
STALL_S = 60.0


class _Clock:
    def __init__(self, t: float | None = None) -> None:
        self._t = t if t is not None else time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


# ---------------------------------------------------------------------------
# PART A — replay Venus E3's real (actual, reading) stream from the log
# ---------------------------------------------------------------------------

_RESP = re.compile(r"response to.*fields=\[([^\]]*)\]")
_REQ = re.compile(r"parsed fields.*meter_mac=(\w+).*power=(-?\d+) consumer_id=(\w+)")


def parse_full_battery_stream(mac="18cedff579dd"):
    """Return [(reading_sent, actual_power)] for the full battery, from the log.

    reading_sent is the response 'total_power' field (== the balancer's stored
    last_target); actual_power is the most recent reported power for that mac.
    """
    stream = []
    last_actual = 0
    for ln in open(LOG_PATH):
        mq = _REQ.search(ln)
        if mq and mq.group(1) == mac:
            last_actual = int(mq.group(2))
            continue
        mr = _RESP.search(ln)
        if mr:
            f = [x.strip().strip("'") for x in mr.group(1).split(",")]
            # positional: [2]=meter_dev_type [3]=meter_mac [7]=total_power
            if len(f) > 7 and f[3] == mac:
                stream.append((int(f[7]), last_actual))
    return stream


def part_a() -> None:
    print("=" * 78)
    print("PART A — log replay of the FULL battery (Venus E3) through the real")
    print(f"          SaturationTracker (MIN_TARGET_FOR_SATURATION={MIN_TARGET})")
    print("=" * 78)
    stream = parse_full_battery_stream()
    readings = {r for r, _ in stream}
    print(f"  log polls for full battery: {len(stream)}")
    print(f"  distinct readings the balancer sent it: {sorted(readings)}  (all |.|<20)")
    print(f"  actual power reported: always {stream[0][1]} W\n")

    def replay(min_target: float) -> float:
        clock = _Clock(0.0)
        trk = SaturationTracker(
            alpha=SAT_ALPHA, min_target=min_target, decay_factor=DECAY,
            stall_timeout_seconds=STALL_S, enabled=True, clock=clock,
        )
        st = BalancerConsumerState()
        trk.set_grace(st, clock() + GRACE_S)  # startup grace, like reset_consumer
        # Replay the real stream at 2 Hz, then hold the last sample long enough
        # to clear grace (90 s) + stall window (60 s) and show the steady value.
        seq = stream + [stream[-1]] * 600
        for reading, actual in seq:
            trk.update(st, last_target=float(reading), actual=float(actual))
            clock.advance(0.5)
        return st.saturation_score

    sat_real = replay(MIN_TARGET)
    sat_if_detectable = replay(10)  # pretend reading were above min_target
    print(f"  Venus E3 saturation after ~5 min, reporter's config : {sat_real:.3f}")
    print("    -> never recognised as saturated; keeps its fair share -> surplus")
    print("       exported instead of transferred to Venus A.")
    print(f"  Same stream if the reading were >= min_target (min_target=10): "
          f"{sat_if_detectable:.3f}")
    print("    -> stall-timeout fires, battery correctly marked saturated (1.0).\n")


# ---------------------------------------------------------------------------
# PART B — balancer end-to-end threshold sweep
# ---------------------------------------------------------------------------

PHASE_IDX = {"A": 0, "B": 1, "C": 2}
CFG = dict(
    fair_distribution=True, balance_gain=0.40, balance_deadband=30,
    max_correction_per_step=150, import_trim_w=8, min_efficient_power=150,
    efficiency_rotation_interval=900, efficiency_fade_alpha=0.8,
    efficiency_saturation_threshold=0.4,
)
SAT_KW = dict(
    saturation_alpha=SAT_ALPHA, saturation_min_target=MIN_TARGET,
    saturation_decay_factor=DECAY, saturation_grace_seconds=GRACE_S,
    saturation_stall_timeout_seconds=STALL_S, saturation_enabled=True,
)


class _SimBattery:
    def __init__(self, mac, phase, *, full):
        self.mac, self.phase, self.full, self.power = mac, phase, full, 0.0

    def step(self, delta, reported):
        desired = reported + delta
        self.power = max(0 if self.full else -800, min(800, desired))


def part_b_case(pace_base, pace_max):
    base = {"A": -1545.0, "B": 613.0, "C": 0.0}
    e = _SimBattery("18cedff579dd", "A", full=True)
    a = _SimBattery("bc2a3314c6bc", "B", full=False)
    bats = [e, a]
    clock = _Clock()
    lb = LoadBalancer(
        config=BalancerConfig(pace_base_step=pace_base, pace_max_step=pace_max, **CFG),
        clock=clock, **SAT_KW,
    )
    for tick in range(900):
        reports = {b.mac: {"phase": b.phase, "power": round(b.power)} for b in bats}
        grid = dict(base)
        for b in bats:
            grid[b.phase] -= b.power
        gt = sum(grid.values())
        tg = {
            b.mac: lb.compute_target(
                consumer_id=b.mac, consumer_mode=ConsumerMode("auto"),
                all_reports=reports, grid_total=gt, inactive=frozenset(),
                manual=frozenset(), sample_id=(tick,),
            )
            for b in bats
        }
        for b in bats:
            b.step(tg[b.mac][PHASE_IDX[b.phase]], reports[b.mac]["power"])
        clock.advance(0.5)
    grid = dict(base)
    for b in bats:
        grid[b.phase] -= b.power
    return lb.get_saturation(e.mac), a.power, sum(grid.values())


def part_b() -> None:
    print("=" * 78)
    print("PART B — balancer end-to-end. Venus E3 FULL, sweep PACE_BASE_STEP")
    print("=" * 78)
    print(f"  {'PACE_BASE_STEP':>14} | {'E(full) sat':>11} | {'A charge':>9} | "
          f"{'grid':>7} |")
    for pb in (15, 19, 20, 0):
        sat, ap, gr = part_b_case(float(pb), max(300.0, float(pb)))
        flag = "  <-- full battery never detected, surplus wasted" if sat < 0.4 else ""
        print(f"  {pb:>14g} | {sat:>11.3f} | {ap:>+9.0f} | {gr:>+7.0f} |{flag}")
    print("  Reporter ran 15 (< MIN_TARGET 20) -> broken; 0 (their workaround) "
          "-> fixed.")


def main() -> None:
    part_a()
    part_b()


if __name__ == "__main__":
    main()
