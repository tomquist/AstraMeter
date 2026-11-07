"""End-to-end against real battery firmware: the CT002 emulator steers emulated
Marstek devices running their unmodified firmware images.

``marsemu`` runs the firmware (CPU, Wi-Fi module, power stage and battery
models).  The device's CT002 polls are handed to ``CT002._handle_request``
in-process and its reply goes straight back to the firmware, all in the
device's emulated time, so the loop is deterministic and runs faster than
real time.  The house sees what the device actually delivers: the grid
reading AstraMeter answers with is the load minus the battery's output.

Opt-in: skipped unless ``marsemu`` is importable and the firmware images are
present (they are not redistributable).  To run::

    uv run --with-editable /path/to/marsemu pytest tests/test_marsemu_e2e.py

with ``MARSEMU_FIRMWARE`` pointing at images laid out as
``<device type>/<version>/*.bin`` (e.g. ``HMA-1/V232/...bin``) and, for the
Venus E 3.0, ``MARSEMU_ARCHIVE`` at a firmware archive containing
``VNSE3-0/Control/151`` and ``VNSE3-0/BMS/115``.  Each test takes one to
three minutes.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
from typing import Any

import pytest

from astrameter.ct002.ct002 import CT002

marsemu = pytest.importorskip("marsemu")

FIRMWARE = os.environ.get("MARSEMU_FIRMWARE", "")
ARCHIVE = os.environ.get("MARSEMU_ARCHIVE", "")
CT_MAC = "112233445566"
#: virtual epoch the CT002 clock starts at (device time 0)
T0 = 1_700_000_000.0


def _image(root: str, pattern: str) -> str:
    if not root:
        pytest.skip("firmware location not set (MARSEMU_FIRMWARE / MARSEMU_ARCHIVE)")
    files = sorted(glob.glob(os.path.join(root, pattern), recursive=True))
    if not files:
        pytest.skip(f"firmware not available: {pattern}")
    return files[0]


class _Sink:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, addr: Any) -> None:
        self.sent.append(bytes(data))


class _Harness:
    """One emulated device in a house, its CT002 polls answered by ``CT002``."""

    def __init__(self, device: Any, house: Any) -> None:
        self.device = device
        self.house = house
        self.ct002 = CT002(
            ct_mac=CT_MAC, active_control=True, clock=lambda: T0 + device.time
        )
        self.ct002.before_send = self._meter
        self._loop = asyncio.new_event_loop()
        self.phases: list[str] = []
        self.meter = marsemu.connect_house(device, house, meter=self._answer)
        marsemu.configure_self_consumption(device, ct_mac=CT_MAC)
        # emulated time since the reading last left the tolerance band
        self._tol = 0.0
        self._last_out = 0.0
        device.sim.tickers.append(self._watch)

    async def _meter(self, addr: Any, request: Any, consumer_id: str) -> list[float]:
        g = self.house.grid()
        return [g["A"], g["B"], g["C"]]

    def _answer(self, data: bytes, addr: tuple[str, int]) -> bytes | None:
        sink = _Sink()
        self._loop.run_until_complete(self.ct002._handle_request(data, addr, sink))
        return sink.sent[-1] if sink.sent else None

    def total(self) -> float:
        return float(sum(self.house.grid().values()))

    def _watch(self, now: float, dt: float) -> None:
        if abs(self.total()) > self._tol:
            self._last_out = now

    def settle(self, tolerance: float, hold: float, budget: float) -> bool:
        """Run until the total grid power has stayed within ``tolerance`` for
        ``hold`` seconds (emulated), at most ``budget`` seconds."""
        self._tol = tolerance
        self._last_out = self.device.time
        self.device.run(budget, until=lambda: self.device.time - self._last_out >= hold)
        return self.device.time - self._last_out >= hold

    def consumer(self) -> Any:
        consumers = self.ct002.status_snapshot().consumers
        assert len(consumers) == 1, consumers
        return consumers[0]

    def close(self) -> None:
        self.device.close()
        self._loop.close()


@pytest.mark.timeout(900)
@pytest.mark.parametrize("build", ["HMA-1/V232", "HMJ-2/V118"])
def test_b2500_nulls_the_grid_and_follows_a_load_step(build: str) -> None:
    """B2500 (DC-output steering): covers the house load from its battery and
    PV, and follows a load step."""
    image = _image(FIRMWARE, f"{build}/*.bin")
    device = marsemu.B2500(image, pv=[300, 300])
    h = _Harness(device, marsemu.House({"A": 200.0}))
    try:
        assert h.settle(tolerance=15, hold=20, budget=180), (
            f"grid not nulled: {h.house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert device.ac_power > 150
        h.house.loads["A"] = 450.0
        assert h.settle(tolerance=15, hold=20, budget=90), (
            f"load step not followed: {h.house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert device.ac_power > 400
        assert h.meter.polls > 50
        assert h.consumer().phase == "A"
    finally:
        h.close()


@pytest.mark.timeout(1200)
def test_venus_e3_detects_its_phase_and_nulls_the_total() -> None:
    """Venus E 3.0: the firmware's phase detection (inspection polls) runs
    against the CT002 and finds the phase it is on (B), then it nulls the
    total over all phases and follows a load step."""
    control = _image(ARCHIVE, "**/VNSE3-0/Control/151/*.bin")
    bms = _image(ARCHIVE, "**/VNSE3-0/BMS/115/*.bin")
    device = marsemu.VenusE3(control, bms)
    house = marsemu.House({"A": 100.0, "B": 600.0, "C": 50.0}, battery_phase="B")
    h = _Harness(device, house)
    try:
        assert h.settle(tolerance=25, hold=20, budget=240), (
            f"total not nulled: {house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert h.consumer().phase == "B"
        assert device.ac_power > 600
        house.loads["B"] = 300.0
        assert h.settle(tolerance=25, hold=20, budget=120), (
            f"load step not followed: {house.grid()}, battery {device.ac_power:.0f} W"
        )
        assert 350 < device.ac_power < 500
    finally:
        h.close()


def _identity(n: int) -> dict[str, Any]:
    """A distinct MCU UID, module MAC and IP for the n-th emulated battery, so
    two units built from the same image are two consumers to the CT002."""
    from marsemu.fc41d.module import Fc41dConfig

    return {
        "uid": bytes([0x40 + n] * 12),
        "fc41d": Fc41dConfig(
            wifi_mac=f"c8:47:8c:00:{n:02x}:01",
            ble_mac=f"c8:47:8c:00:{n:02x}:02",
            ip=f"192.168.1.{50 + n}",
        ),
    }


class _Fleet:
    """Several emulated batteries in one house behind one ``CT002``, on one
    emulated timeline (``marsemu.run_together``)."""

    def __init__(
        self, devices: dict[str, tuple[Any, str]], house: Any, **ct002: Any
    ) -> None:
        if not hasattr(marsemu, "run_together"):
            pytest.skip("marsemu without multi-device support (run_together)")
        self.devices = {name: d for name, (d, _) in devices.items()}
        self.house = house
        first = next(iter(self.devices.values()))
        self.ct002 = CT002(
            ct_mac=CT_MAC,
            active_control=True,
            clock=lambda: T0 + first.time,
            **ct002,
        )
        self.ct002.before_send = self._meter
        self._loop = asyncio.new_event_loop()
        self._topics: dict[str, str] = {}
        for k, (name, (device, phase)) in enumerate(devices.items()):
            marsemu.connect_house(
                device, house, meter=self._answer, phase=phase, name=name
            )
            self._configure(name, device, at=30.0 + 40.0 * k)

    def _configure(self, name: str, device: Any, at: float) -> None:
        """Self-consumption with our CT, as the app would, one battery after
        the other: batteries that start their phase probes together cannot
        tell their phases apart."""
        emu = device.control
        emu.on("mqtt_subscribe", lambda t: self._topics.setdefault(name, t))

        def setup() -> None:
            topic = self._topics.get(name)
            if topic is None:
                device.sim.after(1.0, setup)
                return
            emu.mqtt_inject(topic, "cd=2,md=0")
            device.sim.after(
                2.0, lambda: emu.mqtt_inject(topic, f"cd=18,meter=3,mac={CT_MAC}")
            )

        device.sim.at(at, setup)

    def send(self, name: str, payload: str, delay: float = 0.0) -> None:
        """An MQTT command to battery *name* once it is configured."""
        device = self.devices[name]

        def go() -> None:
            topic = self._topics.get(name)
            if topic is None:
                device.sim.after(1.0, go)
                return
            device.control.mqtt_inject(topic, payload)

        device.sim.after(delay, go)

    async def _meter(self, addr: Any, request: Any, consumer_id: str) -> list[float]:
        g = self.house.grid()
        return [g["A"], g["B"], g["C"]]

    def _answer(self, data: bytes, addr: tuple[str, int]) -> bytes | None:
        sink = _Sink()
        self._loop.run_until_complete(self.ct002._handle_request(data, addr, sink))
        return sink.sent[-1] if sink.sent else None

    @property
    def time(self) -> float:
        return float(next(iter(self.devices.values())).time)

    def run(self, seconds: float) -> None:
        marsemu.run_together(list(self.devices.values()), seconds)

    def every(self, period: float, fn: Any) -> None:
        next(iter(self.devices.values())).sim.every(period, fn)

    def close(self) -> None:
        """Close every device and the loop, then raise the first failure."""
        errors: list[Exception] = []
        for device in self.devices.values():
            try:
                device.close()
            except Exception as exc:  # re-raised below
                errors.append(exc)
        self._loop.close()
        if errors:
            raise errors[0]


@pytest.mark.timeout(2400)
def test_full_venus_d_passing_pv_through_does_not_stop_the_others() -> None:
    """Issue #763: a full Venus D feeds its PV surplus into the grid (phase A)
    while an AC-only Venus D (B) and a Venus E 3.0 (C) charge from the export.
    Its passthrough must never reach the others as cross-talk discharge: a
    charging Venus drops to 0 W on any *_dchrg_power, and the pair would
    switch on and off for as long as the PV unit is full."""
    d_control = _image(ARCHIVE, "**/VNSD-0/Control/151/*.bin")
    d_bms = _image(ARCHIVE, "**/VNSD-0/BMS/118/*.bin")
    e_control = _image(ARCHIVE, "**/VNSE3-0/Control/151/*.bin")
    e_bms = _image(ARCHIVE, "**/VNSE3-0/BMS/115/*.bin")
    from marsemu.plant.battery import LfpPack

    pv_unit = marsemu.VenusD(
        d_control,
        d_bms,
        pv=[750, 750, 0, 0],
        pack=LfpPack(soc=1.0, capacity_ah=100.0),
        control_kwargs=_identity(1),
    )
    ac_unit = marsemu.VenusD(
        d_control,
        d_bms,
        pack=LfpPack(soc=0.5, capacity_ah=100.0),
        control_kwargs=_identity(2),
    )
    venus_e = marsemu.VenusE3(
        e_control, e_bms, pack=LfpPack(soc=0.5), control_kwargs=_identity(3)
    )
    house = marsemu.House({"A": -1000.0, "B": -800.0, "C": -800.0})
    fleet = _Fleet(
        {"A": (pv_unit, "A"), "B": (ac_unit, "B"), "C": (venus_e, "C")}, house
    )
    fleet.send("A", "cd=43,full_d=1", delay=40.0)  # surplus feed-in when full
    samples: list[tuple[float, float, float, float]] = []
    fleet.every(
        1.0,
        lambda now: samples.append(
            (now, pv_unit.ac_power, ac_unit.ac_power, venus_e.ac_power)
        ),
    )
    try:
        fleet.run(500)
        phases = {c.last_ip: c.phase for c in fleet.ct002.status_snapshot().consumers}
        assert phases == {
            "192.168.1.51": "A",
            "192.168.1.52": "B",
            "192.168.1.53": "C",
        }, phases
        # From once all three are steering and the PV unit exports
        settled = [s for s in samples if s[0] >= 260]
        assert max(s[1] for s in settled) > 200, "the full Venus D never exported"
        for _, a, b, c in settled:
            assert b < -100 and c < -100, (
                f"a charging battery stopped while the full one exported {a:.0f} W: "
                f"B {b:.0f} W, C {c:.0f} W"
            )
    finally:
        fleet.close()


@pytest.mark.timeout(1800)
def test_venus_e3_below_its_backup_soc_is_never_rotated_to(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #703: a Venus E 3.0 below its discharge cut-off (depth of
    discharge 30, so 70 %, at 66 %) never runs its phase detection and keeps
    polling as phase 0, even under an export it could charge from.  With
    efficiency rotation on, the CT002 must leave it alone and let the healthy
    battery next to it take the whole surplus, instead of rotating to it and
    probing it over and over."""
    from marsemu.plant.battery import LfpPack

    from astrameter.ct002.balancer import BalancerConfig

    control = _image(ARCHIVE, "**/VNSE3-0/Control/151/*.bin")
    bms = _image(ARCHIVE, "**/VNSE3-0/BMS/115/*.bin")
    low = marsemu.VenusE3(
        control, bms, pack=LfpPack(soc=0.66), control_kwargs=_identity(3)
    )
    ok = marsemu.VenusE3(
        control, bms, pack=LfpPack(soc=0.50), control_kwargs=_identity(4)
    )
    house = marsemu.House({"A": -1200.0})
    fleet = _Fleet(
        {"low": (low, "A"), "ok": (ok, "A")},
        house,
        balancer=BalancerConfig(
            min_efficient_power=1500, efficiency_rotation_interval=60
        ),
    )
    fleet.send("low", "cd=56,dod=30", delay=25.0)
    try:
        with caplog.at_level(logging.INFO, logger="astrameter"):
            fleet.run(360)
        consumers = {c.last_ip: c for c in fleet.ct002.status_snapshot().consumers}
        assert consumers["192.168.1.53"].phase == "0", "the low battery found a phase"
        # Not even briefly: AstraMeter 2.3.0 ends in the same state, having
        # probed the low battery and fallen back to the healthy one each time.
        low_id = consumers["192.168.1.53"].consumer_id
        rotations = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("Efficiency:") and low_id in r.getMessage()
        ]
        assert not rotations, rotations
        assert consumers["192.168.1.54"].phase == "A"
        assert abs(low.ac_power) < 20
        assert abs(sum(house.grid().values())) < 50, (
            f"surplus not taken by the healthy battery: {house.grid()}, "
            f"ok {ok.ac_power:.0f} W"
        )
    finally:
        fleet.close()
