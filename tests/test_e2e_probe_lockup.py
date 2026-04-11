"""End-to-end regression: probe handoff under the log2 topology.

Covers two scenarios:

1. *Happy path* — probe handoff between two consumers with a
   live meter.  The grid must return to the deadband after the
   handoff.

2. *Stale-meter lockup* — what actually happened in the user's log:
   the push-based powermeter (HomeWizard) stops delivering new
   measurements part-way through the probe window, the
   ``before_send`` callback keeps serving the last-known values,
   and the balancer is forced to drive the rotation blind.  This
   is the reproduction of the 1.5-hour uncompensated-grid bug.
"""

from __future__ import annotations

import contextlib
import socket
import time

import pytest

from astrameter.ct002.ct002 import CT002
from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator


class _FakeClock:
    def __init__(self) -> None:
        self._now = time.time()

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _reserve_free_ports(n: int) -> list[tuple[int, socket.socket]]:
    """Reserve *n* ephemeral ports and return ``(port, socket)`` pairs.

    The reservation sockets are **kept open** by the caller: they must
    be closed one at a time *immediately* before the corresponding
    service binds to the port, so the TOCTOU window between the
    reservation closing and the service re-binding is minimized to a
    single function call.  Closing the reservation sockets eagerly
    (as a plain ``_find_free_ports`` would) leaves a wide race window
    during which another process (or a parallel pytest worker under
    ``pytest-xdist``) can grab the port and cause a spurious bind
    failure.
    """
    pairs: list[tuple[int, socket.socket]] = []
    try:
        for i in range(n):
            s = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM if i == 0 else socket.SOCK_STREAM,
            )
            s.bind(("127.0.0.1", 0))
            pairs.append((s.getsockname()[1], s))
    except Exception:
        for _, s in pairs:
            s.close()
        raise
    return pairs


class _Harness:
    """Bespoke harness that places both batteries on phase B with the load
    on phase A, so the only way to zero the grid is via cross-phase
    compensation (which the CT002 protocol allows — see
    ``docs/ct002-ct003-protocol.md``).
    """

    def __init__(
        self,
        *,
        load_a: float = 94.0,
        min_efficient_power: int = 50,
        efficiency_rotation_interval: int = 20,
    ) -> None:
        port_reservations = _reserve_free_ports(2)
        ct_port, self._ct_port_sock = port_reservations[0]
        http_port, self._http_port_sock = port_reservations[1]
        # Lifecycle flags so ``stop()`` is safe to call before ``start()``.
        self._started_powermeter = False
        self._started_ct002 = False
        self.clock = _FakeClock()
        # When non-None, `before_send` returns this frozen snapshot
        # instead of the live grid reading.  Simulates a push-based
        # powermeter (HomeWizard / HA websocket) whose connection has
        # silently half-opened mid-stream.
        self.frozen_grid: list[float] | None = None
        # When True, `before_send` raises ``ValueError("stale")``
        # instead of returning a value.  Simulates a push-based
        # powermeter that HAS a staleness check (the fix for the
        # lockup) — the CT002 emulator must handle this gracefully.
        self.powermeter_raises_stale: bool = False
        self.load_model = LoadModel(
            base_load=[load_a, 0.0, 0.0],
            base_noise=0.0,
            loads=[],
        )
        ct_mac = "112233445566"
        # Match real Marstek ramp behaviour: slower ramp + a real
        # startup delay so the candidate doesn't instantly jump from
        # 0W to the probe target.
        self.batteries: list[BatterySimulator] = [
            BatterySimulator(
                mac="24215EDB1936",
                phase="B",
                ct_mac=ct_mac,
                ct_host="127.0.0.1",
                ct_port=ct_port,
                max_charge_power=800,
                max_discharge_power=800,
                initial_soc=0.8,
                ramp_rate=5.0,
                poll_interval=3.0,
                min_power_threshold=5.0,
                startup_delay=10.0,
            ),
            BatterySimulator(
                mac="ACD929A74B20",
                phase="B",
                ct_mac=ct_mac,
                ct_host="127.0.0.1",
                ct_port=ct_port,
                max_charge_power=800,
                max_discharge_power=800,
                initial_soc=0.8,
                ramp_rate=5.0,
                poll_interval=3.0,
                min_power_threshold=5.0,
                startup_delay=10.0,
            ),
        ]
        self.powermeter = PowermeterSimulator(
            batteries=self.batteries,
            load_model=self.load_model,
            host="127.0.0.1",
            port=http_port,
        )
        self.ct002 = CT002(
            udp_port=ct_port,
            ct_mac=ct_mac,
            active_control=True,
            fair_distribution=True,
            smooth_target_alpha=0.9,
            deadband=5,
            min_efficient_power=min_efficient_power,
            efficiency_rotation_interval=efficiency_rotation_interval,
            probe_min_power=20,  # lower so the test's small loads can probe
            clock=self.clock,
        )

        async def update_readings(_addr, _fields=None, _consumer_id=None):
            if self.powermeter_raises_stale:
                raise ValueError("HomeWizard measurement is stale (test)")
            if self.frozen_grid is not None:
                return list(self.frozen_grid)
            grid = self.powermeter.compute_grid()
            return [grid["phase_a"], grid["phase_b"], grid["phase_c"]]

        self.ct002.before_send = update_readings

    def freeze_meter_at_current_reading(self) -> None:
        """Simulate a push-based powermeter going stale.  From this
        call onward the CT002 emulator sees a frozen snapshot of the
        grid — the simulator's *real* grid continues to evolve based
        on battery outputs and loads."""
        grid = self.powermeter.compute_grid()
        self.frozen_grid = [
            grid["phase_a"],
            grid["phase_b"],
            grid["phase_c"],
        ]

    def unfreeze_meter(self) -> None:
        self.frozen_grid = None

    async def start(self) -> None:
        # Hand the reserved ports off to the real services one at a
        # time: close the reservation socket immediately before its
        # service binds so the TOCTOU window is a single function
        # call (rather than the full harness construction time that
        # the eager-close pattern would leave exposed).
        #
        # Also unwind cleanly on partial-start failure: if
        # ``ct002.start()`` raises after ``powermeter.start()``
        # already succeeded, tear the powermeter back down before
        # re-raising so the test doesn't leak a listening HTTP
        # server across test runs.  ``_started_*`` flags make this
        # cleanup idempotent with :meth:`stop`.
        self._started_powermeter = False
        self._started_ct002 = False
        try:
            self._http_port_sock.close()
            await self.powermeter.start()
            self._started_powermeter = True
            self._ct_port_sock.close()
            await self.ct002.start()
            self._started_ct002 = True
        except BaseException:
            if self._started_powermeter and not self._started_ct002:
                with contextlib.suppress(Exception):
                    await self.powermeter.stop()
                self._started_powermeter = False
            raise

    async def stop(self) -> None:
        if self._started_ct002:
            with contextlib.suppress(Exception):
                await self.ct002.stop()
            self._started_ct002 = False
        if self._started_powermeter:
            with contextlib.suppress(Exception):
                await self.powermeter.stop()
            self._started_powermeter = False
        # If `start()` was never reached, the reservation sockets are
        # still open — close them so the test doesn't leak fds.  If
        # `start()` did run, the sockets are already closed and
        # ``contextlib.suppress(OSError)`` makes the double-close a
        # no-op.
        for sock in (self._ct_port_sock, self._http_port_sock):
            with contextlib.suppress(OSError):
                sock.close()

    async def step(self, n: int = 1) -> None:
        for _ in range(n):
            for b in self.batteries:
                await b.step(b.poll_interval)
            self.clock.advance(max(b.poll_interval for b in self.batteries))

    def battery_powers(self) -> list[float]:
        return [b.current_power for b in self.batteries]

    def grid_total(self) -> float:
        g = self.powermeter.compute_grid()
        return g["phase_a"] + g["phase_b"] + g["phase_c"]


@pytest.mark.timeout(60)
class TestProbeLockup:
    async def test_grid_recovers_after_probe_handoff(self) -> None:
        """After an efficiency-rotation probe handoff, the grid must not
        stay pinned at the load magnitude — the new active battery
        should continue to cover it.
        """
        h = _Harness(
            load_a=94.0,
            min_efficient_power=50,
            efficiency_rotation_interval=9999,  # Manual rotation only
        )
        await h.start()
        try:
            # Warm-up: let one battery take over as the sole active one.
            await h.step(200)

            before_powers = h.battery_powers()
            active_idx = 0 if abs(before_powers[0]) > abs(before_powers[1]) else 1
            standby_idx = 1 - active_idx

            # Confirm only one battery is active.
            assert abs(before_powers[active_idx]) > 40.0, (
                f"Warm-up failed to concentrate demand on one battery. "
                f"Powers: {before_powers}"
            )
            assert abs(before_powers[standby_idx]) < 25.0, (
                f"Warm-up failed to deprioritize the other battery. "
                f"Powers: {before_powers}"
            )

            grid_warmup = abs(h.grid_total())
            assert grid_warmup < 30.0, (
                f"Grid should be near zero after warm-up. grid={grid_warmup:.1f}"
            )

            # Force a rotation directly — this is the deterministic
            # way to exercise the probe handoff path regardless of the
            # rotation-interval clock arithmetic.
            h.ct002.force_efficiency_rotation()
            # Step through the probe and handoff.  Allow enough steps
            # for the probe (~5s) + post-probe fade (~5s) + settling.
            for _ in range(150):
                await h.step()

            after_powers = h.battery_powers()
            grid_after = abs(h.grid_total())

            # Main assertion: the grid must still be close to zero.
            assert grid_after < 30.0, (
                f"Grid is uncompensated after probe handoff: "
                f"grid={grid_after:.1f} W. Powers: {after_powers}."
            )
            # The rotation must have actually happened — the previously
            # active battery must no longer be the sole contributor.
            new_active = 0 if abs(after_powers[0]) > abs(after_powers[1]) else 1
            assert new_active != active_idx, (
                f"Rotation didn't swap the active battery. "
                f"before={before_powers} after={after_powers}"
            )
        finally:
            await h.stop()

    async def test_stale_meter_during_probe_causes_persistent_lockup(
        self,
    ) -> None:
        """Documents the log2 failure *in the absence of powermeter
        staleness detection*.

        The harness's in-test ``before_send`` does NOT implement
        staleness detection — when ``frozen_grid`` is set it silently
        returns the cached values.  This deliberately exercises the
        path where the emulator has no way to know the meter has
        gone quiet: the balancer computes ``target ≈ 0`` because its
        meter source is pinned at ~0, and the real grid drifts to the
        full magnitude of the load (what the user observed for ~1.5 h
        until manual restart).

        The actual fix for the reported bug lives **one layer up**:
        ``HomeWizardPowermeter`` / ``HomeAssistant`` now raise
        ``ValueError`` when their cached value is too old, at which
        point CT002 takes a different code path — that path is
        exercised by
        :meth:`test_powermeter_stale_error_is_handled_gracefully`.

        Keeping this test around as a regression marker: if the
        balancer ever gains the ability to recover *without* help
        from the powermeter layer, this assertion will start to fail
        and this test should be updated or deleted.
        """
        h = _Harness(
            load_a=94.0,
            min_efficient_power=50,
            efficiency_rotation_interval=9999,
        )
        await h.start()
        try:
            # Warm-up to steady state: one battery actively covering
            # demand, grid near zero, smoother converged to ~0.
            await h.step(200)

            before = h.battery_powers()
            assert max(abs(p) for p in before) > 40.0, (
                f"Warm-up failed. Powers: {before}"
            )
            assert abs(h.grid_total()) < 30.0, (
                f"Grid not settled. grid={h.grid_total():.1f}"
            )

            # Freeze the meter *right before* rotating.  This is
            # exactly the timing in the user's log: the WebSocket went
            # quiet while the balancer was in its quiet "both batteries
            # at their share, grid balanced" steady state.
            h.freeze_meter_at_current_reading()
            h.ct002.force_efficiency_rotation()

            # Let the probe run, commit, fade, and settle.
            for _ in range(200):
                await h.step()

            after = h.battery_powers()
            grid_after = h.grid_total()
            smoothed = h.ct002._smoother.value

            # The real grid is measurably off-balance because the
            # emulator drove the handoff blind.  Accept either sign:
            # the failure mode could be either over-discharge or
            # under-coverage depending on how the batteries behave.
            print(
                f"\n  after: powers={after} grid={grid_after:.1f} "
                f"smoothed_emulator={smoothed}"
            )
            assert abs(grid_after) > 40.0, (
                "Stale-meter reproduction failed to trigger the "
                f"lockup: grid={grid_after:.1f} W.  The test needs a "
                "stronger trigger or the emulator has gained recovery "
                "behaviour that invalidates this regression."
            )
        finally:
            await h.stop()

    async def test_powermeter_stale_error_is_handled_gracefully(
        self,
        caplog,
    ) -> None:
        """The fixed path: when the powermeter proactively raises
        ``ValueError`` on detected staleness (as the HomeWizard /
        HomeAssistant powermeters now do after the heartbeat + age
        check), the CT002 emulator must:

        1. Log a rate-limited warning on the first failure.
        2. Not spam the log with one warning per battery poll.
        3. Hold its last known state (batteries stay put).
        4. Log a recovery message when the powermeter returns.
        """
        import logging

        h = _Harness(
            load_a=94.0,
            min_efficient_power=50,
            efficiency_rotation_interval=9999,
        )
        await h.start()
        try:
            await h.step(200)
            before = h.battery_powers()

            # Flip the powermeter into raising-stale mode.  Equivalent
            # to a HomeWizard dongle that has detected its own
            # measurement stream has stalled.
            h.powermeter_raises_stale = True

            with caplog.at_level(logging.WARNING, logger="astrameter"):
                # Step through ~40 battery polls = 40 * 3s = 120 s of
                # simulated wall time.  The rate limit is 30 s of
                # simulated time per warning, so the correct cadence is
                # 1 + floor(120/30) = 5 warnings max (one on the first
                # failure, then at t=30/60/90/120).  Anything
                # significantly above 5 means the rate limit is broken
                # (i.e. per-tick logging has come back) and the
                # assertion will catch the regression.
                for _ in range(40):
                    await h.step()

            stale_warnings = [
                r for r in caplog.records if "before_send failed" in r.getMessage()
            ]
            # Tight bound: under the fake clock we expect exactly one
            # warning per 30 simulated seconds plus the first one.  A
            # regression that removed the rate limit entirely would
            # emit ~80 warnings (one per CT002 poll over 40 steps,
            # both consumers).
            assert 3 <= len(stale_warnings) <= 6, (
                f"Expected 3-6 rate-limited stale warnings, got "
                f"{len(stale_warnings)}: "
                f"{[r.getMessage() for r in stale_warnings]}"
            )

            # Batteries held their state — they did NOT get commanded
            # off-axis by the balancer acting on bad data.  Tolerance
            # is generous because the balancer can still emit small
            # corrections from its existing smoothed value.
            after_stale = h.battery_powers()
            active_idx = 0 if abs(before[0]) > abs(before[1]) else 1
            assert abs(after_stale[active_idx] - before[active_idx]) < 20.0, (
                f"Active battery moved significantly despite stale meter: "
                f"before={before} after_stale={after_stale}"
            )

            # Now recover: powermeter starts returning fresh values
            # again.  We should see a recovery log line and the
            # balancer should pick up again.
            h.powermeter_raises_stale = False
            with caplog.at_level(logging.INFO, logger="astrameter"):
                caplog.clear()
                await h.step(10)

            recovery_logs = [
                r for r in caplog.records if "before_send recovered" in r.getMessage()
            ]
            assert len(recovery_logs) == 1, (
                f"Expected exactly one recovery log, got {len(recovery_logs)}"
            )
        finally:
            await h.stop()

    async def test_harness_start_unwinds_on_partial_failure(self) -> None:
        """If the second service (CT002) fails to start after the first
        (the powermeter) already came up, :meth:`_Harness.start` must
        tear the powermeter back down before re-raising so the test
        run doesn't leak a listening HTTP server.
        """
        h = _Harness(
            load_a=94.0,
            min_efficient_power=50,
            efficiency_rotation_interval=9999,
        )

        # Sabotage CT002 so it will raise after the powermeter has
        # already come up.  Using ``RuntimeError`` so it can't be
        # confused with a genuine asyncio / network error.
        original_ct002_start = h.ct002.start

        async def _fail_ct002_start() -> None:
            raise RuntimeError("simulated CT002 start failure")

        h.ct002.start = _fail_ct002_start  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="simulated CT002 start failure"):
            await h.start()

        # After the failed start the powermeter must no longer be
        # listening: its runner should have been cleaned up by the
        # unwind path.  We detect this by verifying the HTTP port
        # is free (we can rebind to it) — if the unwind skipped the
        # stop, the port would still be held.
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", h.powermeter.port))
            except OSError as exc:
                raise AssertionError(
                    f"Powermeter HTTP port {h.powermeter.port} is still "
                    f"bound after the failed start — unwind did not "
                    f"call powermeter.stop(): {exc}"
                ) from exc
            finally:
                probe.close()
        finally:
            # Restore and drain any remaining state.  ``h.stop()`` must
            # be idempotent here — the powermeter was stopped by the
            # unwind and CT002 was never started, so this should be a
            # no-op with only reservation-socket close fallthrough.
            h.ct002.start = original_ct002_start  # type: ignore[method-assign]
            await h.stop()
