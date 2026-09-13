"""The exact bytes every Shelly method answers with.

Consumers match on field names, field order, which keys are present and which
values are ``null``, so these are whole-body assertions rather than spot
checks: a reordered key or a dropped ``null`` is a pairing failure on a device
nobody can debug from here.

Every value is a function of the two injected time inputs and the injected
address, so the bodies are identical on any machine in any timezone.
"""

from __future__ import annotations

import pytest

from astrameter.shelly import rpc, wire
from astrameter.shelly.identity import ShellyIdentity
from astrameter.shelly.rpc import ShellyRpcError

#: The model device every body below is asserted against.
IDENTITY = ShellyIdentity(
    mac="B827EB364242",
    instance="ShellyPro3EM-B827EB364242",
    hostname="ShellyPro3EM-B827EB364242",
    shelly_id="shellypro3em-b827eb364242",
)
NOW = 1789999999.0
STARTED_AT = NOW - 3600
READING = [300.0, 0.0, -120.0]


@pytest.fixture
def ctx() -> rpc.RequestContext:
    energy = rpc.EnergyCounters(
        seed={"a_total_act_energy": 12.40, "c_total_act_ret_energy": 4.90},
        now=lambda: NOW,
    )
    return rpc.RequestContext(
        identity=IDENTITY,
        profile=rpc.PROFILES["shellypro3em"],
        udp_port=1010,
        announced_ip="192.168.1.50",
        settings=rpc.SettingsStore(),
        energy=energy,
        now=NOW,
        started_at=STARTED_AT,
    )


def body(ctx: rpc.RequestContext, method: str, **params: object) -> str:
    return wire.dumps(rpc.METHODS[method.lower()](ctx, dict(params), READING))


def test_shelly_get_device_info(ctx: rpc.RequestContext) -> None:
    assert body(ctx, "Shelly.GetDeviceInfo") == (
        '{"id":"shellypro3em-b827eb364242","mac":"B827EB364242","slot":1,'
        '"model":"SPEM-003CEBEU","gen":2,'
        '"fw_id":"20250924-062729/1.7.1-gd336f31","ver":"1.7.1","app":"Pro3EM",'
        '"auth_en":false,"auth_domain":null,"profile":"triphase"}'
    )


def test_em_get_status(ctx: rpc.RequestContext) -> None:
    """The body a battery steers on.

    ``n_current`` is an explicit ``null`` rather than an absent key, and
    ``user_calibrated_phase`` an empty list, because that is what real firmware
    sends on a device with no neutral clamp.
    """
    assert body(ctx, "EM.GetStatus", id=0) == (
        '{"id":0,"a_current":1.30,"a_voltage":230.00,"a_act_power":300.00,'
        '"a_aprt_power":300.00,"a_pf":1.00,"a_freq":50.00,"b_current":0.00,'
        '"b_voltage":230.00,"b_act_power":0.00,"b_aprt_power":0.00,"b_pf":1.00,'
        '"b_freq":50.00,"c_current":-0.52,"c_voltage":230.00,'
        '"c_act_power":-120.00,"c_aprt_power":-120.00,"c_pf":1.00,'
        '"c_freq":50.00,"n_current":null,"total_current":0.78,'
        '"total_act_power":180.00,"total_aprt_power":180.00,'
        '"user_calibrated_phase":[]}'
    )


def test_em_get_config(ctx: rpc.RequestContext) -> None:
    assert body(ctx, "EM.GetConfig", id=0) == (
        '{"id":0,"name":null,"blink_mode_selector":"active_energy",'
        '"phase_selector":"a","monitor_phase_sequence":true,"reverse":{},'
        '"ct_type":"120A"}'
    )


def test_em1_get_status_reports_the_grid_total(ctx: rpc.RequestContext) -> None:
    """Single-channel status is the *total*, not phase A.

    This is the quantity the UDP surface has always reported for this method.
    Answering one method two different ways depending on transport would be
    worse than either choice on its own.
    """
    assert body(ctx, "EM1.GetStatus", id=0) == (
        '{"id":0,"current":0.78,"voltage":230.00,"act_power":180.00,'
        '"aprt_power":180.00,"pf":1.00,"freq":50.00}'
    )


def test_em1_get_config_uses_a_boolean_reverse(ctx: rpc.RequestContext) -> None:
    """``reverse`` is a boolean here, unlike the three-phase component's object.

    A single channel has one direction to invert, so copying the three-phase
    shape would be the natural mistake and the wrong one.
    """
    assert body(ctx, "EM1.GetConfig", id=0) == ('{"id":0,"name":null,"reverse":false}')


def test_emdata_get_status(ctx: rpc.RequestContext) -> None:
    assert body(ctx, "EMData.GetStatus", id=0) == (
        '{"id":0,"a_total_act_energy":12.40,"a_total_act_ret_energy":0.00,'
        '"b_total_act_energy":0.00,"b_total_act_ret_energy":0.00,'
        '"c_total_act_energy":0.00,"c_total_act_ret_energy":4.90,'
        '"total_act":12.40,"total_act_ret":4.90}'
    )


def test_sys_get_status_is_timezone_independent(ctx: rpc.RequestContext) -> None:
    """``time`` is UTC and ``utc_offset`` is zero, agreeing with a null zone.

    Reading the host timezone would make this body depend on the machine
    serving it, and claiming an offset while reporting no timezone would be
    self-contradictory.
    """
    assert body(ctx, "Sys.GetStatus") == (
        '{"mac":"B827EB364242","restart_required":false,"time":"14:13",'
        '"unixtime":1789999999,"last_sync_ts":1789999999,"uptime":3600,'
        '"ram_size":259176,"ram_free":87268,"ram_min_free":74044,'
        '"fs_size":524288,"fs_free":196608,"cfg_rev":9,"kvs_rev":0,'
        '"schedule_rev":0,"webhook_rev":0,"btrelay_rev":0,'
        '"available_updates":{},"reset_reason":1,"utc_offset":0}'
    )


def test_sys_get_config_reports_no_location(ctx: rpc.RequestContext) -> None:
    assert body(ctx, "Sys.GetConfig") == (
        '{"device":{"name":"ShellyPro3EM-B827EB364242","mac":"B827EB364242",'
        '"fw_id":"20250924-062729/1.7.1-gd336f31","eco_mode":false,'
        '"profile":"","discoverable":false},'
        '"location":{"tz":null,"lat":null,"lon":null},'
        '"debug":{"mqtt":{"enable":false},"websocket":{"enable":false},'
        '"udp":{"addr":null}},"ui_data":{},'
        '"rpc_udp":{"dst_addr":null,"listen_port":1010},'
        '"sntp":{"server":"pool.ntp.org"},"cfg_rev":10}'
    )


def test_cloud_get_status_reports_both_keys(ctx: rpc.RequestContext) -> None:
    """Deliberately a superset.

    At least one battery family refuses to run unless it can see the meter is
    not cloud-managed, and implementations disagree about which key carries
    that. Reporting both, both false, means no consumer can read a missing key
    as a live cloud connection.
    """
    assert body(ctx, "Cloud.GetStatus") == '{"enabled":false,"connected":false}'


def test_wifi_get_status_reports_the_advertised_address(
    ctx: rpc.RequestContext,
) -> None:
    assert body(ctx, "WiFi.GetStatus") == (
        '{"sta_ip":"192.168.1.50","status":"got ip","ssid":"astrameter",'
        '"bssid":"B8:27:EB:36:42:42","rssi":-50,"sta_ip6":[]}'
    )


def test_temperature_bodies(ctx: rpc.RequestContext) -> None:
    assert body(ctx, "Temperature.GetStatus", id=0) == (
        '{"id":0,"tC":39.10,"tF":102.40}'
    )
    assert body(ctx, "Temperature.GetConfig", id=0) == (
        '{"id":0,"name":null,"report_thr_C":5.00,"offset_C":0.00}'
    )


def test_the_small_constant_bodies(ctx: rpc.RequestContext) -> None:
    assert body(ctx, "EMData.GetConfig", id=0) == "{}"
    assert body(ctx, "Shelly.Reboot") == "{}"
    assert body(ctx, "BLE.GetStatus") == "{}"
    assert body(ctx, "BTHome.GetStatus") == "{}"
    assert body(ctx, "Modbus.GetStatus") == "{}"
    assert body(ctx, "Shelly.CheckForUpdate") == "{}"
    assert body(ctx, "Eth.GetStatus") == '{"ip":null,"ip6":null}'
    assert body(ctx, "Mqtt.GetStatus") == '{"connected":false}'
    assert body(ctx, "Ws.GetStatus") == '{"connected":false}'
    assert body(ctx, "Script.List") == '{"scripts":[]}'
    assert body(ctx, "Script.GetCode") == '{"data":"","left":0}'
    assert body(ctx, "Ws.GetConfig") == ('{"enable":false,"server":null,"ssl_ca":"*"}')
    assert body(ctx, "Cloud.GetConfig") == (
        '{"enable":false,"server":"shelly-143-eu.shelly.cloud:6022/jrpc"}'
    )


def test_settings_page(ctx: rpc.RequestContext) -> None:
    assert wire.dumps(rpc.settings_page(ctx, {}, READING)) == (
        '{"device":{"type":"SPEM-003CEBEU120","mac":"B827EB364242",'
        '"hostname":"ShellyPro3EM-B827EB364242","num_outputs":0,'
        '"num_meters":3},"login":{"enabled":false,"unprotected":false,'
        '"username":null},"fw":"20250924-062729/1.7.1-gd336f31",'
        '"discoverable":true}'
    )


def test_gen1_status(ctx: rpc.RequestContext) -> None:
    """The older whole-device page, which some batteries probe."""
    assert wire.dumps(rpc.gen1_status(ctx, {}, READING)) == (
        '{"wifi_sta":{"connected":true,"ssid":"astrameter",'
        '"ip":"192.168.1.50","rssi":-50},'
        '"cloud":{"enabled":false,"connected":false},'
        '"mqtt":{"connected":false},"time":"14:13","unixtime":1789999999,'
        '"serial":1,"has_update":false,"mac":"B827EB364242","relays":[],'
        '"emeters":[{"power":300.00,"reactive":0.00,"pf":1.00,"current":1.30,'
        '"voltage":230.00,"is_valid":true,"total":12.40,'
        '"total_returned":0.00},{"power":0.00,"reactive":0.00,"pf":1.00,'
        '"current":0.00,"voltage":230.00,"is_valid":true,"total":0.00,'
        '"total_returned":0.00},{"power":-120.00,"reactive":0.00,"pf":1.00,'
        '"current":-0.52,"voltage":230.00,"is_valid":true,"total":0.00,'
        '"total_returned":4.90}],"total_power":180.00,"fs_mounted":true,'
        '"update":{"status":"idle","has_update":false,"new_version":"",'
        '"old_version":"20250924-062729/1.7.1-gd336f31"},"ram_total":259176,'
        '"ram_free":87268,"fs_size":524288,"fs_free":196608,"uptime":3600}'
    )


def test_gen1_emeter_elements_match_the_status_page(
    ctx: rpc.RequestContext,
) -> None:
    """A per-phase page is the corresponding element of the status page.

    Serving a different number on the two would make one device disagree with
    itself about one phase.
    """
    page = rpc.gen1_status(ctx, {}, READING)
    elements = rpc.gen1_emeters(ctx, READING)
    assert elements == page["emeters"]


def test_shelly_get_status_carries_every_component(
    ctx: rpc.RequestContext,
) -> None:
    status = rpc.shelly_get_status(ctx, {}, READING)
    assert list(status) == [
        "ble",
        "bthome",
        "cloud",
        "em:0",
        "emdata:0",
        "eth",
        "modbus",
        "mqtt",
        "sys",
        "temperature:0",
        "wifi",
        "ws",
    ]
    # Each member is the same body the method of its own serves, so one device
    # cannot answer two different values for one component.
    assert status["em:0"] == rpc.em_get_status(ctx, {}, READING)
    assert status["sys"] == rpc.sys_get_status(ctx, {}, READING)
    assert status["wifi"] == rpc.wifi_get_status(ctx, {}, READING)


def test_shelly_get_config_carries_every_component(
    ctx: rpc.RequestContext,
) -> None:
    config = rpc.shelly_get_config(ctx, {}, READING)
    assert list(config) == [
        "ble",
        "cloud",
        "em:0",
        "sys",
        "temperature:0",
        "wifi",
        "ws",
    ]
    assert config["em:0"] == rpc.em_get_config(ctx, {}, READING)


def test_ble_answers_one_value_everywhere(ctx: rpc.RequestContext) -> None:
    """One device, one answer for one setting.

    ``BLE.GetConfig``, ``Shelly.GetConfig.ble`` and the ``ble`` component of
    ``Shelly.GetComponents`` must agree; a device that reports ``enable`` two
    ways depending on which method was asked is the failure this pins.
    """
    from_method = rpc.ble_get_config(ctx, {}, READING)
    from_config = rpc.shelly_get_config(ctx, {}, READING)["ble"]
    components = rpc.shelly_get_components(ctx, {}, READING)["components"]
    from_components = components[0]["config"]
    assert from_method == from_config == from_components
    assert from_method == {"enable": False, "rpc": {"enable": True}}


def test_shelly_get_components(ctx: rpc.RequestContext) -> None:
    result = rpc.shelly_get_components(ctx, {}, READING)
    assert [component["key"] for component in result["components"]] == [
        "ble",
        "em:0",
        "emdata:0",
    ]
    assert result["cfg_rev"] == 1
    assert result["offset"] == 0
    # Counts the dynamic components rather than the returned ones.
    assert result["total"] == 2


def test_shelly_get_components_dynamic_only_is_empty(
    ctx: rpc.RequestContext,
) -> None:
    """This device has no components that come and go at runtime.

    The parameter arrives as a *string*, so a truthiness test on it would read
    ``"false"`` as true — which is how an earlier design got this wrong.
    """
    result = rpc.shelly_get_components(ctx, {"dynamic_only": "true"}, READING)
    assert result == {"components": [], "cfg_rev": 1, "offset": 0, "total": 0}
    assert rpc.shelly_get_components(ctx, {"dynamic_only": "false"}, READING)[
        "components"
    ]
    assert rpc.shelly_get_components(ctx, {"dynamic_only": "0"}, READING)["components"]


def test_list_methods_returns_canonical_spellings(
    ctx: rpc.RequestContext,
) -> None:
    """Dispatch is case-insensitive; a client testing membership is not."""
    methods = rpc.shelly_list_methods(ctx, {}, READING)["methods"]
    assert methods == sorted(rpc.METHOD_NAMES)
    assert "Shelly.GetComponents" in methods
    assert len(methods) == 33


def test_degraded_status_keeps_every_key(ctx: rpc.RequestContext) -> None:
    """With no reading, the measurements are ``null`` but the keys remain.

    A smart-home integration builds its sensors from this key set and indexes
    them directly, so dropping the unreadable fields would create no sensors
    at all — or raise — rather than showing them as unavailable.
    """
    healthy = rpc.em_get_status(ctx, {}, READING)
    degraded = rpc.em_get_status(ctx, {}, None)
    assert set(healthy) <= set(degraded)
    assert len(healthy) == 24
    assert len(degraded) == 25
    assert degraded["errors"] == ["power_meter_failure"]
    assert degraded["id"] == 0
    assert degraded["user_calibrated_phase"] == []
    nulled = [
        key
        for key, value in degraded.items()
        if key not in ("id", "user_calibrated_phase", "errors")
    ]
    assert len(nulled) == 22
    assert all(degraded[key] is None for key in nulled)


def test_the_composites_degrade_rather_than_fail(ctx: rpc.RequestContext) -> None:
    status = rpc.shelly_get_status(ctx, {}, None)
    assert status["em:0"]["errors"] == ["power_meter_failure"]
    # The energy counters do not come from the meter, so they are unaffected.
    assert status["emdata:0"]["a_total_act_energy"] == 12.40
    components = rpc.shelly_get_components(ctx, {}, None)["components"]
    assert components[1]["status"]["errors"] == ["power_meter_failure"]
    assert components[2]["status"]["a_total_act_energy"] == 12.40


def test_a_single_phase_reading_goes_to_phase_a(ctx: rpc.RequestContext) -> None:
    """The padding rule the UDP path already uses."""
    single = rpc.em_get_status(ctx, {}, [250.0])
    assert single["a_act_power"] == 250.0
    assert single["b_act_power"] == 0.0
    assert single["c_act_power"] == 0.0
    assert single["total_act_power"] == 250.0


@pytest.mark.parametrize("method", ["EM.GetStatus", "EM.GetConfig", "EM1.GetStatus"])
def test_an_absent_id_means_zero(ctx: rpc.RequestContext, method: str) -> None:
    """Every consumer relies on this: a Pro 3EM has exactly one of each."""
    assert rpc.METHODS[method.lower()](ctx, {}, READING)["id"] == 0


@pytest.mark.parametrize(
    "method",
    [
        "EM.GetStatus",
        "EM.GetConfig",
        "EM1.GetStatus",
        "EM1.GetConfig",
        "EMData.GetStatus",
        "EMData.GetConfig",
        "Temperature.GetStatus",
        "Temperature.GetConfig",
    ],
)
def test_an_unknown_instance_is_not_found(ctx: rpc.RequestContext, method: str) -> None:
    """Naming a component this device does not have is the "not found" case."""
    with pytest.raises(ShellyRpcError) as raised:
        rpc.METHODS[method.lower()](ctx, {"id": 3}, READING)
    assert raised.value.code == -105
    assert raised.value.message == "Bad id=3"
    assert raised.value.http_status() == 404


def test_an_unparseable_id_is_an_invalid_argument(
    ctx: rpc.RequestContext,
) -> None:
    with pytest.raises(ShellyRpcError) as raised:
        rpc.em_get_status(ctx, {"id": "abc"}, READING)
    assert raised.value.code == -103
    assert raised.value.http_status() == 400


def test_setters_merge_rather_than_replace(ctx: rpc.RequestContext) -> None:
    """A partial write must not blank the keys it does not mention.

    Otherwise setting ``enable`` alone would silently drop the server address,
    and a later read would come back missing a key it had before.
    """
    assert rpc.cloud_set_config(ctx, {"config": {"enable": True}}, None) == {
        "restart_required": False
    }
    assert rpc.cloud_get_config(ctx, {}, None) == {
        "enable": True,
        "server": "shelly-143-eu.shelly.cloud:6022/jrpc",
    }
    rpc.ws_set_config(ctx, {"config": {"enable": True}}, None)
    assert rpc.ws_get_config(ctx, {}, None) == {
        "enable": True,
        "server": None,
        "ssl_ca": "*",
    }


def test_setters_accept_every_config_spelling(ctx: rpc.RequestContext) -> None:
    """Three encodings are in the wild; all three are honoured.

    A client written against the vendor's documentation sends dotted scalars or
    a flat object; one written against an existing emulation nests it.
    """
    rpc.cloud_set_config(ctx, {"config.enable": "true"}, None)
    assert rpc.cloud_get_config(ctx, {}, None)["enable"] is True

    rpc.cloud_set_config(ctx, {"config": '{"enable": false}'}, None)
    assert rpc.cloud_get_config(ctx, {}, None)["enable"] is False

    rpc.cloud_set_config(ctx, {"config": {"config": {"enable": True}}}, None)
    assert rpc.cloud_get_config(ctx, {}, None)["enable"] is True

    rpc.cloud_set_config(ctx, {"enable": False}, None)
    assert rpc.cloud_get_config(ctx, {}, None)["enable"] is False


def test_a_missing_or_unparseable_config_is_rejected(
    ctx: rpc.RequestContext,
) -> None:
    with pytest.raises(ShellyRpcError) as raised:
        rpc.cloud_set_config(ctx, {}, None)
    assert raised.value.message == "Missing required 'config'"
    with pytest.raises(ShellyRpcError) as raised:
        rpc.cloud_set_config(ctx, {"config": "{nope"}, None)
    assert raised.value.message == "Invalid 'config'"
    with pytest.raises(ShellyRpcError) as raised:
        rpc.cloud_set_config(ctx, {"config": ""}, None)
    assert raised.value.message == "Invalid 'config'"


def test_uptime_and_unixtime_are_integers(ctx: rpc.RequestContext) -> None:
    """The one place the "integers stay integers" rule is not automatic.

    Both are computed from a float clock, so without an explicit ``int`` they
    would render as ``3600.00``.
    """
    status = rpc.sys_get_status(ctx, {}, READING)
    assert isinstance(status["uptime"], int)
    assert isinstance(status["unixtime"], int)
    assert isinstance(status["last_sync_ts"], int)


def test_every_method_has_a_reading_rule() -> None:
    """The dispatch table and the reading-dependency table cannot drift.

    An unroutable key would be a ``KeyError`` at request time, so this is what
    keeps that unreachable.
    """
    assert set(rpc.METHODS) <= set(rpc.NEEDS_READING)
    assert set(rpc.NEEDS_READING) - set(rpc.METHODS) == {
        "/status",
        "/emeter",
        "/shelly",
        "/settings",
        "/reset_data",
    }


def test_the_reading_dependent_surfaces_are_the_meter_ones() -> None:
    assert {key for key, needs in rpc.NEEDS_READING.items() if needs} == {
        "em.getstatus",
        "em1.getstatus",
        "emdata.getstatus",
        "/status",
        "/emeter",
    }


def test_battery_polls_are_a_strict_subset_of_reading_dependent() -> None:
    """Liveness is narrower than "needs a reading", deliberately.

    The two composite methods need a reading but must not mark their caller as
    a battery, or a smart-home integration polling every 60 s would appear on
    the dashboard as one.
    """
    assert set(rpc.NEEDS_READING) > rpc.COUNTS_AS_BATTERY_POLL
    assert rpc.TRIES_READING.isdisjoint(rpc.COUNTS_AS_BATTERY_POLL)
    assert all(not rpc.NEEDS_READING[key] for key in rpc.TRIES_READING)


def test_the_method_table_is_complete_in_both_directions() -> None:
    """Every name has a builder and every builder has a name."""
    assert len(rpc.METHOD_NAMES) == 33
    assert len(set(rpc.METHOD_NAMES)) == 33
    assert len(rpc.METHODS) == 33
    assert set(rpc.METHODS) == {name.lower() for name in rpc.METHOD_NAMES}


def test_energy_counters_integrate_import_and_export_separately() -> None:
    clock = [0.0]
    counters = rpc.EnergyCounters(now=lambda: clock[0])
    counters.update((3600.0, 0.0, -3600.0))
    clock[0] = 1.0
    counters.update((3600.0, 0.0, -3600.0))
    totals = counters.snapshot()
    assert totals["a_total_act_energy"] == pytest.approx(1.0)
    assert totals["c_total_act_ret_energy"] == pytest.approx(1.0)
    assert totals["a_total_act_ret_energy"] == 0.0
    assert totals["total_act"] == pytest.approx(1.0)
    assert totals["total_act_ret"] == pytest.approx(1.0)


def test_energy_counters_ignore_a_clock_jump() -> None:
    """A jump must not inject a spurious integral.

    A container resumed after an hour would otherwise book an hour of the last
    reading as though it had been flowing the whole time.
    """
    clock = [0.0]
    counters = rpc.EnergyCounters(now=lambda: clock[0])
    counters.update((3600.0, 0.0, 0.0))
    clock[0] = 100000.0
    counters.update((3600.0, 0.0, 0.0))
    assert counters.snapshot()["a_total_act_energy"] == 0.0
