"""Tests for the opt-in Marstek HTTP cloud reporter."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime

import pytest

from astrameter.cloud_reporting import (
    CloudReporter,
    CloudReporterConfig,
    CtMeasurement,
    EnergyCounters,
    EnergyIntegrator,
    HttpResponse,
    ServerTime,
    build_get_date_info_url,
    build_set_ct_reporting_url,
    parse_server_time,
)

_DATE = datetime.date(2026, 6, 18)
# What getDateInfoeu.php answers with: the server's wall-clock time.
_TIME_BODY = "_2026_06_18_13_47_05_"


def _measurement(**overrides: int) -> CtMeasurement:
    m = CtMeasurement(
        ap=10,
        bp=20,
        cp=30,
        dp=60,
        rssi=-55,
        slv=2,
        udp=1,
        mqtt=1,
        eled=111,
        elet=222,
        cz=5,
        ca=1,
        cb=2,
        cc=3,
        cd=4,
        dz=-5,
        da=-1,
        db=-2,
        dc=-3,
        dd=-4,
        va=230,
        vb=231,
        vc=232,
        ia=1.5,
        ib=2.25,
        ic=0.0,
    )
    return dataclasses.replace(m, **overrides)


def test_get_date_info_url_format() -> None:
    url = build_get_date_info_url(
        "eu.hamedata.com", uid="aabbccddeeff", fcv="202409090159", aid="acct1", sv=3
    )
    assert url == (
        "http://eu.hamedata.com/app/neng/getDateInfoeu.php"
        "?uid=aabbccddeeff&fcv=202409090159&aid=acct1&sv=3"
    )


def test_set_ct_reporting_hme4_includes_voltage_current() -> None:
    url = build_set_ct_reporting_url(
        "eu.hamedata.com",
        "HME-4",
        device_id="aabbccddeeff",
        time_no=82,
        date=_DATE,
        m=_measurement(),
    )
    # HME-4 keeps the '&' before udp and carries va/vb/vc + ia/ib/ic.
    assert "&slv=2&udp=1&mqtt=1&timeNo=82&date=2026-06-18" in url
    assert "&va=230&vb=231&vc=232&ia=1.50&ib=2.25&ic=0.00" in url
    assert url.endswith("&cz=5&ca=1&cb=2&cc=3&cd=4&dz=-5&da=-1&db=-2&dc=-3&dd=-4")
    assert "eled=111&elet=222" in url


def test_set_ct_reporting_hme4_sends_power_magnitudes() -> None:
    url = build_set_ct_reporting_url(
        "eu.hamedata.com",
        "HME-4",
        device_id="m",
        time_no=0,
        date=_DATE,
        m=_measurement(ap=-150, bp=20, cp=-70000, dp=-200),
    )
    # The unsigned 16-bit cast drops the sign and wraps past 65535.
    assert "&ap=150&bp=20&cp=4464&dp=200&" in url


def test_set_ct_reporting_hme3_omits_vi_and_keeps_missing_amp_quirk() -> None:
    url = build_set_ct_reporting_url(
        "eu.hamedata.com",
        "HME-3",
        device_id="aabbccddeeff",
        time_no=82,
        date=_DATE,
        m=_measurement(ap=-150, dp=-90),
    )
    # Reproduce the on-wire quirk: no '&' between slv and udp.
    assert "&slv=2udp=1&mqtt=1" in url
    # HME-3 sends no instantaneous voltage/current, and signed powers.
    assert "va=" not in url and "ia=" not in url
    assert "&ap=-150&bp=20&cp=30&dp=-90&" in url
    assert url.endswith("&cz=5&ca=1&cb=2&cc=3&cd=4&dz=-5&da=-1&db=-2&dc=-3&dd=-4")


def test_host_is_configurable() -> None:
    url = build_get_date_info_url("cn.hamedata.com", uid="m", fcv="f", aid="a", sv=0)
    assert url.startswith("http://cn.hamedata.com/")


def test_parse_server_time() -> None:
    t = parse_server_time(_TIME_BODY)
    assert t == ServerTime(2026, 6, 18, 13, 47, 5)
    assert t is not None and t.date == _DATE
    assert parse_server_time("") is None
    assert parse_server_time("<html>error</html>") is None
    assert parse_server_time("_2026_13_40_25_61_00_") is None
    assert parse_server_time("_99999999999999999999_1_1_0_0_0_") is None


@pytest.mark.parametrize(
    ("hour", "minute", "slot"),
    [(0, 0, 0), (0, 9, 0), (0, 10, 1), (13, 47, 82), (23, 59, 143)],
)
def test_time_no_is_ten_minute_slot_of_day(hour: int, minute: int, slot: int) -> None:
    assert ServerTime(2026, 6, 18, hour, minute).time_no == slot


def test_energy_integrator_holds_each_sample_until_the_next() -> None:
    e = EnergyIntegrator()
    e.sample(0.0, 3600.0)
    assert e.total == 0
    # 3600 W for 10 s = 10 Wh = 100 units of 0.1 Wh.
    e.sample(10.0, -1800.0)
    assert e.total == 100
    # -1800 W for 10 s = -5 Wh.
    e.sample(20.0, 0.0)
    assert e.total == 50


def test_energy_counters_reset_on_new_hour_and_day() -> None:
    c = EnergyCounters()
    c.update(1000, ServerTime(2026, 6, 18, 13, 0))
    # The first handshake has no previous hour or day, so both start at 0.
    assert (c.eled, c.elet) == (0, 0)
    c.update(1500, ServerTime(2026, 6, 18, 13, 5))
    assert (c.eled, c.elet) == (500, 500)
    # A new hour clears elet after adding the gain, as on the device.
    c.update(1700, ServerTime(2026, 6, 18, 14, 0))
    assert (c.eled, c.elet) == (700, 0)
    c.update(1800, ServerTime(2026, 6, 18, 14, 5))
    assert (c.eled, c.elet) == (800, 100)
    c.update(1900, ServerTime(2026, 6, 19, 0, 0))
    assert (c.eled, c.elet) == (0, 0)


class _FakeCloud:
    def __init__(self, responses: list[HttpResponse | None] | None = None) -> None:
        self.seen: list[str] = []
        self._responses = responses

    async def get(self, url: str) -> HttpResponse | None:
        self.seen.append(url)
        if self._responses is not None:
            return self._responses.pop(0)
        if "getDateInfoeu.php" in url:
            return HttpResponse(200, _TIME_BODY)
        return HttpResponse(200, "")


def _reporter(
    cloud: _FakeCloud,
    *,
    ct_type: str = "HME-4",
    measurement: CtMeasurement | None = None,
    monotonic: list[float] | None = None,
) -> CloudReporter:
    async def gather() -> CtMeasurement:
        return measurement or _measurement()

    clock = monotonic if monotonic is not None else [0.0]
    return CloudReporter(
        CloudReporterConfig(ct_type=ct_type, device_id="aabbccddeeff"),
        gather=gather,
        http_get=cloud.get,
        clock=lambda: datetime.datetime(2026, 6, 18, 12, 0, 0),
        monotonic=lambda: clock[0],
    )


@pytest.mark.parametrize(("ct_type", "sv"), [("HME-4", 124), ("HME-3", 122)])
async def test_reporter_handshakes_then_reports(ct_type: str, sv: int) -> None:
    cloud = _FakeCloud()
    reporter = _reporter(cloud, ct_type=ct_type)

    assert await reporter._handshake()
    assert await reporter._report_once()

    assert "getDateInfoeu.php" in cloud.seen[0]
    # The handshake re-asserts the device record: aid=model, sv=the firmware
    # version a cloud-reporting CT of that model runs.
    assert f"aid={ct_type}&sv={sv}" in cloud.seen[0]
    assert "setCtReporting" in cloud.seen[1]
    assert "id=aabbccddeeff" in cloud.seen[1]
    # timeNo and date come from the handshake's server time, not the local clock.
    assert "&timeNo=82&date=2026-06-18" in cloud.seen[1]


async def test_reporter_falls_back_to_local_time_without_server_time() -> None:
    cloud = _FakeCloud([HttpResponse(200, "ok"), HttpResponse(200, "")])
    reporter = _reporter(cloud)
    assert await reporter._handshake()
    await reporter._report_once()
    # The local clock says 12:00 -> slot 72.
    assert "&timeNo=72&date=2026-06-18" in cloud.seen[1]


async def test_failed_handshake_keeps_counters() -> None:
    cloud = _FakeCloud([HttpResponse(503, _TIME_BODY), None])
    reporter = _reporter(cloud)
    assert not await reporter._handshake()
    assert not await reporter._handshake()
    assert reporter._server_time is None


async def test_reported_energy_is_integrated_grid_power() -> None:
    cloud = _FakeCloud(
        [
            HttpResponse(200, "_2026_06_18_13_00_00_"),
            HttpResponse(200, ""),
            HttpResponse(200, "_2026_06_18_13_05_00_"),
            HttpResponse(200, ""),
        ]
    )
    now = [0.0]
    reporter = _reporter(cloud, measurement=_measurement(dp=1200), monotonic=now)
    await reporter._handshake()
    await reporter._report_once()
    assert "eled=0&elet=0" in cloud.seen[1]

    # 1200 W for 300 s = 100 Wh = 1000 units of 0.1 Wh.
    now[0] = 300.0
    await reporter._handshake()
    await reporter._report_once()
    assert "eled=1000&elet=1000" in cloud.seen[3]


async def test_hme3_energy_counts_import_only() -> None:
    cloud = _FakeCloud(
        [
            HttpResponse(200, "_2026_06_18_13_00_00_"),
            HttpResponse(200, "_2026_06_18_13_05_00_"),
            HttpResponse(200, ""),
        ]
    )
    now = [0.0]
    reporter = _reporter(
        cloud, ct_type="HME-3", measurement=_measurement(dp=-1200), monotonic=now
    )
    await reporter._handshake()
    now[0] = 300.0
    await reporter._handshake()
    await reporter._report_once()
    assert "eled=0&elet=0" in cloud.seen[2]


async def test_run_retries_report_then_waits_interval() -> None:
    cloud = _FakeCloud(
        [
            None,  # handshake fails -> retry
            HttpResponse(200, _TIME_BODY),
            HttpResponse(500, ""),  # report rejected -> retry the report only
            HttpResponse(200, ""),
            HttpResponse(200, _TIME_BODY),  # next cycle starts with a handshake
            HttpResponse(200, ""),
        ]
    )
    now = [0.0]
    sleeps: list[float] = []

    async def gather() -> CtMeasurement:
        return _measurement()

    reporter = CloudReporter(
        CloudReporterConfig(
            ct_type="HME-4",
            device_id="m",
            interval_seconds=300,
            retry_seconds=60,
            sample_seconds=10,
        ),
        gather=gather,
        http_get=cloud.get,
        monotonic=lambda: now[0],
    )

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        if len(cloud.seen) >= 6:
            raise asyncio.CancelledError
        await real_sleep(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await reporter.run()

    kinds = ["hs" if "getDateInfo" in u else "rep" for u in cloud.seen]
    assert kinds == ["hs", "hs", "rep", "rep", "hs", "rep"]
    # Retry waits are 60 s, the post-report wait 300 s, sampled every 10 s.
    assert sum(sleeps[:6]) == 60
    assert sum(sleeps[6:12]) == 60
    assert sum(sleeps[12:42]) == 300
    assert set(sleeps) == {10}


async def test_report_counters() -> None:
    reporter = _reporter(
        _FakeCloud([HttpResponse(200, ""), None, HttpResponse(500, "")])
    )
    assert reporter.status_snapshot().last_report_at is None
    assert reporter.status_snapshot().report_count == 0

    assert await reporter._report_once()
    snap = reporter.status_snapshot()
    assert (snap.report_count, snap.fail_count, snap.last_http_status) == (1, 0, 200)
    assert snap.last_report_at == datetime.datetime(2026, 6, 18, 12, 0, 0)

    # Transport failure: the seam reports it as `None`.
    assert not await reporter._report_once()
    snap = reporter.status_snapshot()
    assert (snap.report_count, snap.fail_count, snap.last_http_status) == (2, 1, None)

    # A completed request the cloud rejected is a failure too.
    assert not await reporter._report_once()
    snap = reporter.status_snapshot()
    assert (snap.report_count, snap.fail_count, snap.last_http_status) == (3, 2, 500)


async def test_snapshot_exposes_host_not_url() -> None:
    reporter = _reporter(_FakeCloud())
    await reporter._report_once()
    snap = reporter.status_snapshot()

    assert snap.host == "eu.hamedata.com"
    assert (snap.device_id, snap.ct_type, snap.interval) == (
        "aabbccddeeff",
        "HME-4",
        300,
    )
    # The report URL embeds the device id (and could grow more identity params),
    # so no snapshot field may carry a URL.
    assert not any(
        isinstance(value, str) and ("://" in value or "?" in value)
        for value in dataclasses.asdict(snap).values()
    )


async def test_reporter_swallows_gather_errors() -> None:
    calls = {"n": 0}

    async def boom() -> CtMeasurement:
        calls["n"] += 1
        raise RuntimeError("powermeter offline")

    cloud = _FakeCloud()
    reporter = CloudReporter(
        CloudReporterConfig(ct_type="HME-4", device_id="m"),
        gather=boom,
        http_get=cloud.get,
    )
    # A failed energy sample does not stop the handshake ...
    assert await reporter._handshake()
    # ... but _report_once must raise (so run() can log + retry).
    with pytest.raises(RuntimeError):
        await reporter._report_once()
    assert calls["n"] == 2
