"""Tests for the opt-in Marstek HTTP cloud reporter."""

from __future__ import annotations

import dataclasses
import datetime

import pytest

from astrameter.cloud_reporting import (
    CloudReporter,
    CloudReporterConfig,
    CtMeasurement,
    build_get_date_info_url,
    build_set_ct_reporting_url,
)

_DATE = datetime.date(2026, 6, 18)


def _measurement() -> CtMeasurement:
    return CtMeasurement(
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
        cz=-5,
        ca=-1,
        cb=-2,
        cc=-3,
        cd=-4,
        dz=5,
        da=1,
        db=2,
        dc=3,
        dd=4,
        va=230,
        vb=231,
        vc=232,
        ia=1.5,
        ib=2.25,
        ic=0.0,
    )


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
        time_no=1700000000,
        date=_DATE,
        m=_measurement(),
    )
    # HME-4 keeps the '&' before udp and carries va/vb/vc + ia/ib/ic.
    assert "&slv=2&udp=1&mqtt=1" in url
    assert "&va=230&vb=231&vc=232&ia=1.50&ib=2.25&ic=0.00" in url
    assert url.endswith("&cz=-5&ca=-1&cb=-2&cc=-3&cd=-4&dz=5&da=1&db=2&dc=3&dd=4")
    assert "date=2026-06-18" in url
    assert "eled=111&elet=222" in url


def test_set_ct_reporting_hme3_omits_vi_and_keeps_missing_amp_quirk() -> None:
    url = build_set_ct_reporting_url(
        "eu.hamedata.com",
        "HME-3",
        device_id="aabbccddeeff",
        time_no=1700000000,
        date=_DATE,
        m=_measurement(),
    )
    # Reproduce the on-wire quirk: no '&' between slv and udp.
    assert "&slv=2udp=1&mqtt=1" in url
    # HME-3 sends no instantaneous voltage/current.
    assert "va=" not in url and "ia=" not in url
    assert url.endswith("&cz=-5&ca=-1&cb=-2&cc=-3&cd=-4&dz=5&da=1&db=2&dc=3&dd=4")


def test_host_is_configurable() -> None:
    url = build_get_date_info_url("cn.hamedata.com", uid="m", fcv="f", aid="a", sv=0)
    assert url.startswith("http://cn.hamedata.com/")


async def test_reporter_handshakes_then_reports() -> None:
    seen: list[str] = []

    async def fake_get(url: str) -> int:
        seen.append(url)
        return 200

    async def gather() -> CtMeasurement:
        return _measurement()

    fixed = datetime.datetime(2026, 6, 18, 12, 0, 0)
    reporter = CloudReporter(
        CloudReporterConfig(
            ct_type="HME-4",
            device_id="aabbccddeeff",
            interval_seconds=0.01,
        ),
        gather=gather,
        http_get=fake_get,
        clock=lambda: fixed,
    )

    # One handshake + one report, then stop.
    await reporter._handshake()
    await reporter._report_once()

    assert "getDateInfoeu.php" in seen[0]
    # The handshake re-asserts the device record: aid=model, sv=managed version.
    assert "aid=HME-4" in seen[0]
    assert "sv=121" in seen[0]
    assert "setCtReporting" in seen[1]
    assert "id=aabbccddeeff" in seen[1]


def _counting_reporter(statuses: list[int | None]) -> CloudReporter:
    async def fake_get(url: str) -> int | None:
        return statuses.pop(0)

    async def gather() -> CtMeasurement:
        return _measurement()

    return CloudReporter(
        CloudReporterConfig(
            ct_type="HME-4", device_id="aabbccddeeff", interval_seconds=999
        ),
        gather=gather,
        http_get=fake_get,
        clock=lambda: datetime.datetime(2026, 6, 18, 12, 0, 0),
    )


async def test_report_counters() -> None:
    reporter = _counting_reporter([200, None, 500])
    assert reporter.status_snapshot().last_report_at is None
    assert reporter.status_snapshot().report_count == 0

    await reporter._report_once()
    snap = reporter.status_snapshot()
    assert (snap.report_count, snap.fail_count, snap.last_http_status) == (1, 0, 200)
    assert snap.last_report_at == datetime.datetime(2026, 6, 18, 12, 0, 0)

    # Transport failure: the seam reports it as a `None` status.
    await reporter._report_once()
    snap = reporter.status_snapshot()
    assert (snap.report_count, snap.fail_count, snap.last_http_status) == (2, 1, None)

    # A completed request the cloud rejected is a failure too.
    await reporter._report_once()
    snap = reporter.status_snapshot()
    assert (snap.report_count, snap.fail_count, snap.last_http_status) == (3, 2, 500)


async def test_snapshot_exposes_host_not_url() -> None:
    reporter = _counting_reporter([200])
    await reporter._report_once()
    snap = reporter.status_snapshot()

    assert snap.host == "eu.hamedata.com"
    assert (snap.device_id, snap.ct_type, snap.interval) == (
        "aabbccddeeff",
        "HME-4",
        999,
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

    async def fake_get(url: str) -> int:
        return 200

    reporter = CloudReporter(
        CloudReporterConfig(ct_type="HME-4", device_id="m", interval_seconds=999),
        gather=boom,
        http_get=fake_get,
    )
    # _report_once must raise (so run() can log+continue); run() must not crash.
    with pytest.raises(RuntimeError):
        await reporter._report_once()
    assert calls["n"] == 1
