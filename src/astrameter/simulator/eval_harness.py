"""Runs one scenario: CT002 (active control) → BatterySimulator (firmware
steering laws) → LoadModel → PowermeterSimulator, under a mock clock so hours
of simulated household activity take seconds of wall time."""

from __future__ import annotations

import asyncio
import errno
import math
import os
import random
import socket
from concurrent.futures import ProcessPoolExecutor, as_completed

from astrameter.ct002.balancer import split_balancer_knobs
from astrameter.ct002.ct002 import CT002

from .battery import BatterySimulator
from .eval_metrics import _chart_traces, _compute_metrics
from .eval_scenarios import build_scenarios
from .eval_spec import EvalWorld, Scenario, _Sample
from .load_model import Load, LoadModel
from .powermeter_sim import PowermeterSimulator

# Mock-time epoch all scenarios start from; a constant keeps runs bit-for-bit
# reproducible across machines.
_EPOCH = 1_750_000_000.0
_CT_MAC = "112233445566"
# Offset between the first polls of consecutive batteries, so no two units ever
# poll in the same instant.
_POLL_STAGGER_S = 0.131
# Mock time spans hours, so CT002 must never evict a battery as stale.
_CONSUMER_TTL_S = 10_000_000


class _EvalClock:
    """Monotonic settable mock clock (same shape as the e2e HarnessClock)."""

    def __init__(self, start: float) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def set(self, value: float) -> None:
        if value > self._now:
            self._now = value


def _reserve_udp_port() -> socket.socket:
    """Claim a free UDP port and hold it: the returned socket keeps the port
    out of the pool until closed. Asking for port 0 and closing straight away
    only names a port, and parallel workers were handed the same one."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
    except OSError:
        sock.close()
        raise
    return sock


async def _start_ct002(
    ct002: CT002, batteries: list[BatterySimulator], attempts: int = 10
) -> None:
    """Bind the CT listener on a free port and point *batteries* at it.

    The reservation cannot be handed to the listener atomically (CT002 binds
    the wildcard address, which conflicts with the loopback hold), so it is
    released a moment before the real bind and a lost race costs one retry.
    """
    for attempt in range(attempts):
        with _reserve_udp_port() as reservation:
            ct002.udp_port = int(reservation.getsockname()[1])
        for battery in batteries:
            battery.ct_port = ct002.udp_port
        try:
            await ct002.start()
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or attempt == attempts - 1:
                raise
        else:
            return


async def run_scenario(
    scenario: Scenario,
    seed: int = 1,
    overrides: dict[str, float] | None = None,
) -> dict:
    """Run *scenario* deterministically and return its metrics dict."""
    # The LoadModel draws noise from the global ``random``; seed it so each
    # run is reproducible.  Event schedules use an independent stream so
    # adding noise samples never shifts the scripted timeline.
    random.seed(seed)
    rng = random.Random(seed + 1)
    events = sorted(scenario.build_events(rng), key=lambda e: e.at)

    clock = _EvalClock(_EPOCH)
    load_model = LoadModel(
        base_load=list(scenario.base_load),
        base_noise=scenario.base_noise,
        loads=[Load(ld.name, ld.power, ld.phase) for ld in scenario.loads],
    )
    batteries = [
        BatterySimulator(
            mac=f"02B250{i + 1:06X}",
            phase=spec.phase,
            ct_mac=_CT_MAC,
            ct_host="127.0.0.1",
            # Real port assigned by _start_ct002 once the CT has bound it.
            ct_port=0,
            meter_dev_type=spec.device_type,
            max_charge_power=spec.max_charge_power,
            max_discharge_power=spec.max_discharge_power,
            capacity_wh=spec.capacity_wh,
            initial_soc=spec.initial_soc,
            ramp_rate=spec.ramp_rate,
            poll_interval=spec.poll_interval,
            min_power_threshold=spec.min_power_threshold,
            startup_delay=spec.startup_delay,
            max_dc_input=spec.max_dc_input,
            initial_power=spec.initial_power,
        )
        for i, spec in enumerate(scenario.batteries)
    ]
    powermeter = PowermeterSimulator(batteries=batteries, load_model=load_model, port=0)
    world = EvalWorld(load_model=load_model, batteries=batteries)

    ct_kwargs: dict[str, float] = dict(scenario.ct_kwargs)
    ct_kwargs.update(overrides or {})
    balancer, other_kwargs = split_balancer_knobs(ct_kwargs)
    ct002 = CT002(
        udp_port=0,  # assigned by _start_ct002 below
        ct_mac=_CT_MAC,
        active_control=True,
        balancer=balancer,
        clock=clock,
        consumer_ttl=_CONSUMER_TTL_S,
        dedupe_time_window=0.0,
        **other_kwargs,
    )

    samples: list[_Sample] = []
    # The controller reads the meter at the meter's own cadence (stale in
    # between, like a real powermeter poll); metrics record the true grid.
    # ``grid_history`` keeps recent true readings so a refresh can serve the
    # value as it was ``meter_latency_s`` ago (transport/measurement delay).
    meter_cache: dict[str, float] = {}
    meter_read_at = [-math.inf]
    grid_history: list[tuple[float, dict[str, float]]] = []

    async def before_send(
        _addr: tuple[str, int],
        _request: object = None,
        _consumer_id: str | None = None,
    ) -> list[float]:
        now = clock() - _EPOCH
        # Draw the house load once; derive both the true grid and the raw
        # consumption from that same sample (get_grid_contribution re-draws
        # noise each call, so a second call would decorrelate them).
        contribution = load_model.get_grid_contribution()
        true_grid = powermeter.compute_grid_from(contribution)
        grid_history.append((now, true_grid))
        # Drop history older than what the delayed read can still need.
        horizon = now - scenario.meter_latency_s - scenario.meter_interval_s - 1.0
        while grid_history[0][0] < horizon:
            grid_history.pop(0)
        if now - meter_read_at[0] >= scenario.meter_interval_s:
            # Serve the reading as it was meter_latency_s ago (zero-order hold
            # on the history: the most recent sample at or before target_t).
            target_t = now - scenario.meter_latency_s
            delayed = grid_history[0][1]
            for ht, hg in grid_history:
                if ht <= target_t:
                    delayed = hg
                else:
                    break
            meter_cache.clear()
            meter_cache.update(delayed)
            meter_read_at[0] = now
        samples.append(
            _Sample(
                t=now,
                grid=true_grid["phase_a"] + true_grid["phase_b"] + true_grid["phase_c"],
                consumption=contribution[0] + contribution[1] + contribution[2],
                powers=tuple(b.current_power for b in batteries),
                socs=tuple(b.soc for b in batteries),
                dc_input=sum(b.dc_input_power for b in batteries),
            )
        )
        return [
            meter_cache["phase_a"],
            meter_cache["phase_b"],
            meter_cache["phase_c"],
        ]

    ct002.before_send = before_send
    await _start_ct002(ct002, batteries)
    try:
        # Event-driven schedule: each battery polls on its own cadence
        # (staggered starts), scripted events fire in between.
        next_poll = [0.5 + i * _POLL_STAGGER_S for i in range(len(batteries))]
        marks: list[tuple[float, str]] = []
        event_idx = 0
        while True:
            i = min(range(len(batteries)), key=lambda k: next_poll[k])
            t_next = next_poll[i]
            if t_next > scenario.duration_s:
                break
            while event_idx < len(events) and events[event_idx].at <= t_next:
                ev = events[event_idx]
                clock.set(_EPOCH + ev.at)
                ev.apply(world)
                if ev.label:
                    marks.append((ev.at, ev.label))
                event_idx += 1
            clock.set(_EPOCH + t_next)
            await batteries[i].step(batteries[i].poll_interval)
            next_poll[i] = t_next + batteries[i].poll_interval
    finally:
        await ct002.stop()

    return {
        **_compute_metrics(scenario, seed, samples, marks),
        **_chart_traces(scenario, samples),
    }


def _run_one(name: str, seed: int, overrides: dict[str, float]) -> dict:
    """Process-pool worker: rebuilds the registry from *name* (event closures
    aren't picklable) and gets its own global ``random`` state, which is what
    keeps concurrent seeds deterministic."""
    scenarios = build_scenarios()
    return asyncio.run(run_scenario(scenarios[name], seed=seed, overrides=overrides))


def _run_tasks(
    tasks: list[tuple[str, int]], overrides: dict[str, float]
) -> dict[tuple[str, int], dict]:
    """Run every (scenario, seed) task, in parallel across CPU cores.

    The simulation is CPU-bound, so processes (not asyncio) are what actually
    parallelize it; one task runs inline to avoid pool overhead."""
    if len(tasks) <= 1:
        return {t: _run_one(t[0], t[1], overrides) for t in tasks}
    workers = min(len(tasks), os.cpu_count() or 1)
    out: dict[tuple[str, int], dict] = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_one, name, seed, overrides): (name, seed)
            for name, seed in tasks
        }
        for fut in as_completed(futures):
            out[futures[fut]] = fut.result()
    return out
