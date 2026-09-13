"""Reading the host's network identity off a fixture tree, not the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrameter import net_info
from astrameter.net_info import (
    AnnouncedAddress,
    announced_ipv4,
    default_route_interface,
    interface_mac,
)

#: The shape of ``/proc/net/route``: a header, then one row per route with the
#: interface first and the destination, metric and mask in fixed columns.
_ROUTE_HEADER = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask"
    "\t\tMTU\tWindow\tIRTT"
)


def _write_route(tmp_path: Path, rows: list[str]) -> str:
    proc = tmp_path / "proc" / "net"
    proc.mkdir(parents=True)
    (proc / "route").write_text("\n".join([_ROUTE_HEADER, *rows]) + "\n")
    return str(tmp_path / "proc")


def test_the_default_route_interface_is_the_one_with_no_destination(
    tmp_path: Path,
) -> None:
    proc = _write_route(
        tmp_path,
        [
            "eth0\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0",
            "eth0\t0002A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0",
        ],
    )
    assert default_route_interface(proc) == "eth0"


def test_the_lowest_metric_wins(tmp_path: Path) -> None:
    """A host with two default routes uses the cheaper one."""
    proc = _write_route(
        tmp_path,
        [
            "wlan0\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0",
            "eth0\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0",
        ],
    )
    assert default_route_interface(proc) == "eth0"


def test_no_default_route_is_not_an_error(tmp_path: Path) -> None:
    proc = _write_route(
        tmp_path,
        ["eth0\t0002A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0"],
    )
    assert default_route_interface(proc) is None


def test_a_missing_route_table_is_not_an_error(tmp_path: Path) -> None:
    """A non-Linux host, or a stripped container, reads as "unknown"."""
    assert default_route_interface(str(tmp_path / "absent")) is None


def test_a_malformed_row_is_skipped(tmp_path: Path) -> None:
    proc = _write_route(
        tmp_path,
        [
            "truncated\trow",
            "eth0\t00000000\t0102A8C0\t0003\t0\t0\tnotanumber\t00000000\t0\t0\t0",
            "eth1\t00000000\t0102A8C0\t0003\t0\t0\t50\t00000000\t0\t0\t0",
        ],
    )
    assert default_route_interface(proc) == "eth1"


def test_interface_mac_reads_the_hardware_address(tmp_path: Path) -> None:
    address = tmp_path / "sys" / "class" / "net" / "eth0"
    address.mkdir(parents=True)
    (address / "address").write_text("b8:27:eb:36:42:42\n")
    assert interface_mac("eth0", str(tmp_path / "sys")) == "b8:27:eb:36:42:42"


def test_a_null_hardware_address_reads_as_absent(tmp_path: Path) -> None:
    """A virtual interface reports all zeroes, which is not an identity."""
    address = tmp_path / "sys" / "class" / "net" / "lo"
    address.mkdir(parents=True)
    (address / "address").write_text("00:00:00:00:00:00\n")
    assert interface_mac("lo", str(tmp_path / "sys")) is None


def test_a_missing_interface_reads_as_absent(tmp_path: Path) -> None:
    assert interface_mac("nope", str(tmp_path / "sys")) is None


def test_an_explicit_address_is_announced_verbatim() -> None:
    """A host whose reachable address is not the one it routes from."""
    assert announced_ipv4("10.1.2.3") == "10.1.2.3"


def test_an_interface_name_is_resolved_to_its_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        net_info,
        "ipv4_for_interface",
        lambda name: "10.9.9.9" if name == "br0" else None,
    )
    assert announced_ipv4("br0") == "10.9.9.9"


def test_an_unresolvable_name_falls_back_to_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not leave the device advertising nothing."""
    monkeypatch.setattr(net_info, "ipv4_for_interface", lambda name: None)
    monkeypatch.setattr(net_info, "local_ipv4", lambda: "10.0.0.5")
    assert announced_ipv4("typo0") == "10.0.0.5"


def test_no_address_at_all_falls_back_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers always have a string to serve, even on a host with no network."""
    monkeypatch.setattr(net_info, "local_ipv4", lambda: None)
    assert announced_ipv4("") == "127.0.0.1"


def test_announced_address_reports_only_real_changes() -> None:
    """The advertiser re-announces on change, so "no change" has to be cheap."""
    # What the resolver returns on each successive call.
    values = ["10.0.0.1", "10.0.0.2"]
    address = AnnouncedAddress("10.0.0.1", resolve=lambda: values.pop(0))
    assert address.value == "10.0.0.1"
    assert address.refresh() is None
    assert address.refresh() == "10.0.0.2"
    assert address.value == "10.0.0.2"


def test_announced_address_survives_a_failing_resolver() -> None:
    """A transient failure keeps the last known address rather than clearing it."""

    def boom() -> str:
        raise OSError("no route")

    address = AnnouncedAddress("10.0.0.1", resolve=boom)
    assert address.refresh() is None
    assert address.value == "10.0.0.1"
