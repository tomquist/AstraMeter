"""Every body the Shelly HTTP surface serves, and nothing else.

This module is pure: it computes response payloads from a reading and a request
context and performs no I/O of its own. Everything that varies — the clock, the
advertised address, the live meter reading — is handed in, which is what lets
the golden tests pin whole bodies byte for byte on any machine in any timezone.

The shapes here follow real Shelly firmware: field names, field *order*, which
keys are present, and which values are ``null``. Consumers match on all four, so
none of it is cosmetic. Where a value cannot be sourced from the vendor's
documentation it is marked ``chosen`` in a comment, so a later reader can tell
the difference between a fact and a decision.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from astrameter.shelly.identity import ShellyIdentity

#: Electrical constants the emulator reports. A real meter measures these; we
#: have only active power, so the rest are fixed at nominal European values.
#: chosen — nothing in the protocol carries them and no consumer reads them.
DEFAULT_VOLTAGE = 230.0
DEFAULT_FREQUENCY = 50.0
DEFAULT_POWER_FACTOR = 1.0

#: chosen — a real device reports the SSID it joined; we have not joined one.
WIFI_SSID = "astrameter"
#: chosen — reads as "good signal" to a consumer that shows a bar graph.
WIFI_RSSI = -50
#: chosen — a real device increments this on every status change. A constant is
#: the honest answer for an emulator that has no such counter.
GEN1_SERIAL = 1

#: How long between checks that the advertised address still matches the host.
#: Well inside the 120 s TTL of the address records, and free when unchanged.
#: Owned by the emulator rather than the mDNS advertiser: it has to keep
#: running with mDNS switched off, or the HTTP surface freezes on a stale
#: address after a DHCP change.
ANNOUNCE_WATCHDOG_S = 30

#: A clock jump must not inject a spurious integral into the energy counters.
ENERGY_DT_CLAMP_S = (0.0, 60.0)

#: The one error a consumer sees in normal operation, verbatim as the
#: established emulation reports it, so a client matching on the text still
#: matches.
NO_POWER_DATA = (
    "power data is currently not available (no data received from the input device)"
)

#: Error code to HTTP status. Codes are the vendor's documented ones (-103
#: INVALID ARGUMENT, -105 NOT FOUND) plus the literal 404 real firmware puts in
#: ``error.code`` for a missing handler, and 1 for "no power data". The statuses
#: are chosen: no source states what a real device pairs with an error body.
DEFAULT_STATUS = {1: 503, -103: 400, -105: 404, 404: 404}


class ShellyRpcError(Exception):
    """An error the RPC surface reports to the client as an error object.

    *status* overrides :data:`DEFAULT_STATUS` for the one case the code alone
    cannot express: an oversize request body carries ``-103`` but has to answer
    413 rather than 400.
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        retry_after: int | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.status = status

    def http_status(self) -> int:
        """The status to answer with."""
        return self.status or DEFAULT_STATUS.get(self.code, 500)

    def body(self) -> dict[str, Any]:
        """The bare error object, as a real device spells it."""
        return {"error": {"code": self.code, "message": self.message}}


@dataclasses.dataclass(frozen=True, slots=True)
class ShellyProfile:
    """What one emulated device type claims to be."""

    device_type_key: str
    settings_type: str
    model: str
    app: str
    mdns_app: str
    profile: str
    gen: int
    fw_id: str
    ver: str
    arch: str
    num_meters: int


_PRO_3EM = ShellyProfile(
    device_type_key="shellypro3em",
    settings_type="SPEM-003CEBEU120",
    model="SPEM-003CEBEU",
    app="Pro3EM",
    mdns_app="shellypro3em",
    profile="triphase",
    gen=2,
    fw_id="20250924-062729/1.7.1-gd336f31",
    ver="1.7.1",
    arch="esp32",
    num_meters=3,
)

#: The device types with a TCP surface. All three ``shellypro3em`` spellings
#: describe one device — the pair differs only in which UDP port it answers.
PROFILES: dict[str, ShellyProfile] = {
    "shellypro3em": _PRO_3EM,
    "shellypro3em_old": _PRO_3EM,
    "shellypro3em_new": _PRO_3EM,
}

#: Static memory and filesystem figures, reported as a real device does.
_RAM_SIZE = 259176
_RAM_FREE = 87268
_RAM_MIN_FREE = 74044
_FS_SIZE = 524288
_FS_FREE = 196608

#: chosen — a plausible fixed internal temperature. No consumer reads it.
_TEMP_C = 39.1
_TEMP_F = 102.4

_ENERGY_FIELDS = (
    "a_total_act_energy",
    "a_total_act_ret_energy",
    "b_total_act_energy",
    "b_total_act_ret_energy",
    "c_total_act_energy",
    "c_total_act_ret_energy",
)


class SettingsStore:
    """The five values the two setters can move, held only in memory.

    Not persisted and not restored: a restart legitimately starts from the
    defaults, which is the lifetime a real device's unsaved config has. It
    exists so a ``GetConfig`` echoes what a ``SetConfig`` was given, which is
    what a phone app expects after a write, and for nothing else — no other
    part of AstraMeter reads it.
    """

    __slots__ = ("cloud_enable", "cloud_server", "ws_enable", "ws_server", "ws_ssl_ca")

    def __init__(self) -> None:
        self.cloud_enable = False
        self.cloud_server = "shelly-143-eu.shelly.cloud:6022/jrpc"
        self.ws_enable = False
        self.ws_server: str | None = None
        self.ws_ssl_ca = "*"


class EnergyCounters:
    """Wh accumulated from the readings served, integrated over wall time.

    Import and export are counted separately per phase, as a real meter does.
    Not persisted: the counters are a convenience for a consumer that reads
    them, not a billing record.
    """

    __slots__ = ("_last", "_now", "_totals")

    def __init__(
        self, *, seed: dict[str, float] | None = None, now: Callable[[], float]
    ) -> None:
        self._now = now
        self._totals = {name: 0.0 for name in _ENERGY_FIELDS}
        if seed:
            for name, value in seed.items():
                if name in self._totals:
                    self._totals[name] = float(value)
        self._last: float | None = None

    def update(self, powers: tuple[float, float, float]) -> None:
        """Integrate *powers* over the time since the previous update."""
        now = self._now()
        previous, self._last = self._last, now
        if previous is None:
            return
        low, high = ENERGY_DT_CLAMP_S
        delta = now - previous
        if not low < delta <= high:
            # A clock jump, or the first reading after a long idle period.
            return
        hours = delta / 3600.0
        for phase, power in zip("abc", powers, strict=True):
            if power >= 0:
                self._totals[f"{phase}_total_act_energy"] += power * hours
            else:
                self._totals[f"{phase}_total_act_ret_energy"] += -power * hours

    def snapshot(self) -> dict[str, float]:
        """The eight energy values, per phase and summed."""
        totals = dict(self._totals)
        totals["total_act"] = sum(self._totals[f"{p}_total_act_energy"] for p in "abc")
        totals["total_act_ret"] = sum(
            self._totals[f"{p}_total_act_ret_energy"] for p in "abc"
        )
        return totals


@dataclasses.dataclass(frozen=True, slots=True)
class RequestContext:
    """Everything a builder may read, resolved once per request.

    Passing this rather than reaching for globals is what keeps the module
    pure, and what lets a test freeze the clock and the address.
    """

    identity: ShellyIdentity
    profile: ShellyProfile
    udp_port: int
    announced_ip: str
    settings: SettingsStore
    energy: EnergyCounters
    now: float
    started_at: float

    @property
    def uptime(self) -> int:
        """Seconds since this device started.

        ``int`` deliberately: ``now`` is a float, and the wire writer renders
        every float at two decimals, so a plain subtraction would serve
        ``3600.00`` where a consumer expects a count.
        """
        return int(self.now - self.started_at)

    @property
    def unixtime(self) -> int:
        """Wall-clock seconds, as an integer for the same reason as *uptime*."""
        return int(self.now)

    @property
    def clock(self) -> str:
        """``HH:MM`` in UTC, derivable from *unixtime* in the same body.

        UTC rather than local time so the value is a function of the injected
        clock alone; a host-timezone read would make it machine-dependent, and
        we report no timezone either way.
        """
        return datetime.fromtimestamp(self.now, timezone.utc).strftime("%H:%M")

    @property
    def bssid(self) -> str:
        """A BSSID derived from our own MAC rather than an invented one."""
        mac = self.identity.mac
        return ":".join(mac[i : i + 2] for i in range(0, 12, 2))


def three_phases(powers: list[float] | None) -> tuple[float, float, float]:
    """*powers* as three floats, padding the way the UDP path already does.

    A single reading is phase A with the other two at zero; anything that is
    neither one nor three values reads as zero across the board.
    """
    if not powers:
        return 0.0, 0.0, 0.0
    if len(powers) == 1:
        return float(powers[0]), 0.0, 0.0
    if len(powers) >= 3:
        return float(powers[0]), float(powers[1]), float(powers[2])
    return 0.0, 0.0, 0.0


def _phase_block(prefix: str, power: float) -> dict[str, Any]:
    """One phase's six measurements, derived from its active power.

    Current and apparent power keep the sign of the active power: a consumer
    that steers on this needs to know which way the energy is flowing.
    """
    return {
        f"{prefix}_current": power / DEFAULT_VOLTAGE,
        f"{prefix}_voltage": DEFAULT_VOLTAGE,
        f"{prefix}_act_power": power,
        f"{prefix}_aprt_power": power,
        f"{prefix}_pf": DEFAULT_POWER_FACTOR,
        f"{prefix}_freq": DEFAULT_FREQUENCY,
    }


#: The 22 measurement keys of an ``em:0`` status, i.e. every key except ``id``
#: and ``user_calibrated_phase``. The vendor documents each as "number or
#: null", which is what makes the degraded body below legal.
_EM_MEASUREMENTS = (
    *(
        key
        for prefix in "abc"
        for key in (
            f"{prefix}_current",
            f"{prefix}_voltage",
            f"{prefix}_act_power",
            f"{prefix}_aprt_power",
            f"{prefix}_pf",
            f"{prefix}_freq",
        )
    ),
    "n_current",
    "total_current",
    "total_act_power",
    "total_aprt_power",
)


def em_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The three-phase meter status — the body a battery steers on."""
    _require_id_zero(params)
    if reading is None:
        return _degraded_em_status()
    a, b, c = three_phases(reading)
    body: dict[str, Any] = {"id": 0}
    body.update(_phase_block("a", a))
    body.update(_phase_block("b", b))
    body.update(_phase_block("c", c))
    total = a + b + c
    body["n_current"] = None
    body["total_current"] = total / DEFAULT_VOLTAGE
    body["total_act_power"] = total
    body["total_aprt_power"] = total
    body["user_calibrated_phase"] = []
    return body


def _degraded_em_status() -> dict[str, Any]:
    """``em:0`` with every measurement ``null`` and its own error reported.

    Keeping all 24 keys is what makes this safe for a consumer that indexes
    them directly — Home Assistant creates its sensors from the key set, so a
    body that *drops* the unreadable fields would create no sensors at all, or
    raise. The vendor documents every measurement as nullable and documents
    ``power_meter_failure`` as an ``errors`` member, so this is the shape a real
    device uses when its own meter is unavailable.
    """
    body: dict[str, Any] = {"id": 0}
    for key in _EM_MEASUREMENTS:
        body[key] = None
    body["user_calibrated_phase"] = []
    body["errors"] = ["power_meter_failure"]
    return body


def em_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The three-phase meter's configuration.

    ``reverse`` is an *object* here because each phase of a three-phase
    component can be inverted independently; it is empty because we invert
    none of them.
    """
    _require_id_zero(params)
    return {
        "id": 0,
        "name": None,
        "blink_mode_selector": "active_energy",
        "phase_selector": "a",
        "monitor_phase_sequence": True,
        "reverse": {},
        "ct_type": "120A",
    }


def em1_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The single-channel meter status, reporting the grid *total*.

    Deliberately the total rather than one phase: this is the quantity the UDP
    surface has always reported for this method, and answering one method two
    different ways depending on transport would be worse than either choice. A
    single-phase consumer steering on a three-phase feed wants the net import
    anyway, not one leg of it.
    """
    _require_id_zero(params)
    a, b, c = three_phases(reading)
    total = a + b + c
    return {
        "id": 0,
        "current": total / DEFAULT_VOLTAGE,
        "voltage": DEFAULT_VOLTAGE,
        "act_power": total,
        "aprt_power": total,
        "pf": DEFAULT_POWER_FACTOR,
        "freq": DEFAULT_FREQUENCY,
    }


def em1_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The single-channel meter's configuration.

    ``reverse`` is a plain boolean here, unlike the three-phase component's
    object: there is one channel to invert. ``ct_type`` is deliberately absent
    — the vendor documents it, but any value we put there would be invented,
    and no consumer reads it.
    """
    _require_id_zero(params)
    return {"id": 0, "name": None, "reverse": False}


def emdata_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Accumulated energy, per phase and summed."""
    _require_id_zero(params)
    totals = ctx.energy.snapshot()
    return {
        "id": 0,
        "a_total_act_energy": totals["a_total_act_energy"],
        "a_total_act_ret_energy": totals["a_total_act_ret_energy"],
        "b_total_act_energy": totals["b_total_act_energy"],
        "b_total_act_ret_energy": totals["b_total_act_ret_energy"],
        "c_total_act_energy": totals["c_total_act_energy"],
        "c_total_act_ret_energy": totals["c_total_act_ret_energy"],
        "total_act": totals["total_act"],
        "total_act_ret": totals["total_act_ret"],
    }


def emdata_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The energy component's configuration: nothing to configure."""
    _require_id_zero(params)
    return {}


def shelly_get_device_info(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Who this device is — the first thing most consumers ask for.

    ``id`` is the lowercase MAC-derived form, matching what the mDNS TXT record
    and the UDP surface report, so a consumer correlating the two sees one
    device rather than two.
    """
    return {
        "id": ctx.identity.shelly_id,
        "mac": ctx.identity.mac,
        "slot": 1,
        "model": ctx.profile.model,
        "gen": ctx.profile.gen,
        "fw_id": ctx.profile.fw_id,
        "ver": ctx.profile.ver,
        "app": ctx.profile.app,
        "auth_en": False,
        "auth_domain": None,
        "profile": ctx.profile.profile,
    }


def sys_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """System configuration.

    All three ``location`` fields are ``null``: we genuinely do not know where
    the device is, real firmware reports ``null`` when it is unset, and reading
    the host's timezone would make the response depend on the machine serving
    it.
    """
    return {
        "device": {
            "name": ctx.identity.hostname,
            "mac": ctx.identity.mac,
            "fw_id": ctx.profile.fw_id,
            "eco_mode": False,
            "profile": "",
            "discoverable": False,
        },
        "location": {"tz": None, "lat": None, "lon": None},
        "debug": {
            "mqtt": {"enable": False},
            "websocket": {"enable": False},
            "udp": {"addr": None},
        },
        "ui_data": {},
        "rpc_udp": {"dst_addr": None, "listen_port": ctx.udp_port},
        "sntp": {"server": "pool.ntp.org"},
        "cfg_rev": 10,
    }


def sys_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """System status.

    ``utc_offset`` is the constant ``0`` and ``time`` is UTC, so both agree
    with the ``null`` timezone above: claiming an offset while claiming no
    timezone would be self-contradictory.
    """
    return {
        "mac": ctx.identity.mac,
        "restart_required": False,
        "time": ctx.clock,
        "unixtime": ctx.unixtime,
        "last_sync_ts": ctx.unixtime,
        "uptime": ctx.uptime,
        "ram_size": _RAM_SIZE,
        "ram_free": _RAM_FREE,
        "ram_min_free": _RAM_MIN_FREE,
        "fs_size": _FS_SIZE,
        "fs_free": _FS_FREE,
        "cfg_rev": 9,
        "kvs_rev": 0,
        "schedule_rev": 0,
        "webhook_rev": 0,
        "btrelay_rev": 0,
        "available_updates": {},
        "reset_reason": 1,
        "utc_offset": 0,
    }


def cloud_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The cloud connection's configuration, as last set."""
    return {"enable": ctx.settings.cloud_enable, "server": ctx.settings.cloud_server}


def cloud_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Whether the cloud is connected — deliberately reporting *both* keys.

    At least one consumer family refuses to operate unless it can see that the
    device is not cloud-managed, and implementations disagree about which of
    these two keys carries that fact. Reporting both, both false, means neither
    a consumer reading ``enabled`` nor one reading ``connected`` can mistake a
    missing key for a live cloud connection.
    """
    return {"enabled": False, "connected": False}


def cloud_set_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Store a partial cloud configuration and report no restart needed.

    A key the request omits is left unchanged: ``SetConfig`` takes the
    configuration it is given, not a whole replacement, so a caller setting one
    field cannot silently blank another.
    """
    config = _config_param(params)
    if "enable" in config:
        ctx.settings.cloud_enable = bool(config["enable"])
    if "server" in config:
        ctx.settings.cloud_server = str(config["server"])
    return {"restart_required": False}


def ws_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The outbound websocket's configuration, as last set."""
    return {
        "enable": ctx.settings.ws_enable,
        "server": ctx.settings.ws_server,
        "ssl_ca": ctx.settings.ws_ssl_ca,
    }


def ws_set_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Store a partial websocket configuration, merging as *cloud_set_config*."""
    config = _config_param(params)
    if "enable" in config:
        ctx.settings.ws_enable = bool(config["enable"])
    if "server" in config:
        value = config["server"]
        ctx.settings.ws_server = None if value is None else str(value)
    if "ssl_ca" in config:
        ctx.settings.ws_ssl_ca = str(config["ssl_ca"])
    return {"restart_required": False}


def wifi_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The wireless configuration a real device would report."""
    return {
        "ap": {
            "ssid": ctx.identity.hostname,
            "is_open": True,
            "enable": False,
            "range_extender": {"enable": False},
        },
        "sta": {
            "ssid": WIFI_SSID,
            "is_open": False,
            "enable": True,
            "ipv4mode": "dhcp",
            "ip": None,
            "netmask": None,
            "gw": None,
            "nameserver": None,
        },
        "sta1": {
            "ssid": None,
            "is_open": True,
            "enable": False,
            "ipv4mode": "dhcp",
            "ip": None,
            "netmask": None,
            "gw": None,
            "nameserver": None,
        },
        "roam": {"rssi_thr": -80, "interval": 60},
    }


def wifi_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The wireless status, reporting the address we actually advertise."""
    return {
        "sta_ip": ctx.announced_ip,
        "status": "got ip",
        "ssid": WIFI_SSID,
        "bssid": ctx.bssid,
        "rssi": WIFI_RSSI,
        "sta_ip6": [],
    }


def temperature_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The device's internal temperature."""
    _require_id_zero(params)
    return {"id": 0, "tC": _TEMP_C, "tF": _TEMP_F}


def temperature_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The temperature component's configuration."""
    _require_id_zero(params)
    return {"id": 0, "name": None, "report_thr_C": 5.0, "offset_C": 0.0}


def script_list(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """No scripts: this is an emulation, and it runs none."""
    return {"scripts": []}


def script_get_code(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """No script code, for the same reason."""
    return {"data": "", "left": 0}


def shelly_reboot(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Acknowledged, and deliberately not acted on.

    A consumer occasionally reboots a meter to clear a fault. Restarting
    AstraMeter would take every *other* emulated device down with it, so the
    call is accepted and ignored — the same choice the established emulation
    makes.
    """
    return {}


def _ble_config() -> dict[str, Any]:
    """The Bluetooth component's configuration, in one place.

    One body, used by ``BLE.GetConfig``, ``Shelly.GetConfig`` and
    ``Shelly.GetComponents`` alike: one device must not answer two different
    values for the same setting depending on which method was asked.
    """
    return {"enable": False, "rpc": {"enable": True}}


def ble_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The Bluetooth component's configuration."""
    return _ble_config()


def ble_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Bluetooth has no status to report; we serve none."""
    return {}


def eth_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """No wired interface."""
    return {"ip": None, "ip6": None}


def mqtt_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The device's own MQTT client, which we do not run."""
    return {"connected": False}


def ws_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The outbound websocket, which we do not open."""
    return {"connected": False}


def shelly_check_for_update(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """No firmware updates: consistent with reporting none available."""
    return {}


def bthome_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """No BTHome devices."""
    return {}


def modbus_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Modbus is not served."""
    return {}


def shelly_list_methods(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Every method this device answers, in canonical spelling.

    Sorted, and in the capitalisation a client will compare against: dispatch
    is case-insensitive but a consumer testing membership is not.
    """
    return {"methods": sorted(METHOD_NAMES)}


def shelly_get_config(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Every component's configuration in one body."""
    return {
        "ble": _ble_config(),
        "cloud": cloud_get_config(ctx, {}, reading),
        "em:0": em_get_config(ctx, {}, reading),
        "sys": sys_get_config(ctx, {}, reading),
        "temperature:0": temperature_get_config(ctx, {}, reading),
        "wifi": wifi_get_config(ctx, {}, reading),
        "ws": ws_get_config(ctx, {}, reading),
    }


def shelly_get_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """Every component's status in one body.

    Answers 200 even with no meter reading, with ``em:0`` degraded: this is the
    body a smart-home integration fetches while *adding* the device, so failing
    it would stop the device being added at all rather than merely leaving one
    value stale.
    """
    return {
        "ble": {},
        "bthome": {},
        "cloud": cloud_get_status(ctx, {}, reading),
        "em:0": em_get_status(ctx, {}, reading),
        "emdata:0": emdata_get_status(ctx, {}, reading),
        "eth": eth_get_status(ctx, {}, reading),
        "modbus": {},
        "mqtt": mqtt_get_status(ctx, {}, reading),
        "sys": sys_get_status(ctx, {}, reading),
        "temperature:0": temperature_get_status(ctx, {}, reading),
        "wifi": wifi_get_status(ctx, {}, reading),
        "ws": ws_get_status(ctx, {}, reading),
    }


def shelly_get_components(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The component inventory, with each component's status and config.

    ``dynamic_only`` asks for components that can appear and disappear at
    runtime; this device has none, so the answer is an empty list. ``total``
    counts the dynamic components rather than the returned ones, which is why
    it stays at 2 beside three entries.
    """
    dynamic_only = _bool_param(params, "dynamic_only")
    offset = _int_param(params, "offset", default=0)
    if dynamic_only:
        return {"components": [], "cfg_rev": 1, "offset": offset, "total": 0}
    components = [
        {"key": "ble", "status": {}, "config": _ble_config()},
        {
            "key": "em:0",
            "status": em_get_status(ctx, {}, reading),
            "config": em_get_config(ctx, {}, reading),
        },
        {
            "key": "emdata:0",
            "status": emdata_get_status(ctx, {}, reading),
            "config": {},
        },
    ]
    return {
        "components": components,
        "cfg_rev": 1,
        "offset": offset,
        "total": 2,
    }


def settings_page(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The older settings page, which some consumers read to identify a device.

    ``device.hostname`` is the DNS name, which differs from the ``id`` above
    only in capitalisation.
    """
    return {
        "device": {
            "type": ctx.profile.settings_type,
            "mac": ctx.identity.mac,
            "hostname": ctx.identity.hostname,
            "num_outputs": 0,
            "num_meters": ctx.profile.num_meters,
        },
        "login": {"enabled": False, "unprotected": False, "username": None},
        "fw": ctx.profile.fw_id,
        "discoverable": True,
    }


def _emeter_element(
    power: float, total: float, total_returned: float
) -> dict[str, Any]:
    """One phase as the older per-phase meter shape.

    ``reactive`` is always zero: we measure active power only.
    """
    return {
        "power": power,
        "reactive": 0.0,
        "pf": DEFAULT_POWER_FACTOR,
        "current": power / DEFAULT_VOLTAGE,
        "voltage": DEFAULT_VOLTAGE,
        "is_valid": True,
        "total": total,
        "total_returned": total_returned,
    }


def gen1_emeters(
    ctx: RequestContext, reading: list[float] | None
) -> list[dict[str, Any]]:
    """The three per-phase elements of the older status page."""
    a, b, c = three_phases(reading)
    totals = ctx.energy.snapshot()
    return [
        _emeter_element(
            a, totals["a_total_act_energy"], totals["a_total_act_ret_energy"]
        ),
        _emeter_element(
            b, totals["b_total_act_energy"], totals["b_total_act_ret_energy"]
        ),
        _emeter_element(
            c, totals["c_total_act_energy"], totals["c_total_act_ret_energy"]
        ),
    ]


def gen1_status(
    ctx: RequestContext, params: dict[str, Any], reading: list[float] | None
) -> dict[str, Any]:
    """The older whole-device status page.

    Served because at least one consumer family probes it to confirm what it is
    talking to, and a client that takes this path is better served completely
    than half-served.
    """
    a, b, c = three_phases(reading)
    return {
        "wifi_sta": {
            "connected": True,
            "ssid": WIFI_SSID,
            "ip": ctx.announced_ip,
            "rssi": WIFI_RSSI,
        },
        "cloud": {"enabled": False, "connected": False},
        "mqtt": {"connected": False},
        "time": ctx.clock,
        "unixtime": ctx.unixtime,
        "serial": GEN1_SERIAL,
        "has_update": False,
        "mac": ctx.identity.mac,
        "relays": [],
        "emeters": gen1_emeters(ctx, reading),
        "total_power": a + b + c,
        "fs_mounted": True,
        "update": {
            "status": "idle",
            "has_update": False,
            "new_version": "",
            "old_version": ctx.profile.fw_id,
        },
        "ram_total": _RAM_SIZE,
        "ram_free": _RAM_FREE,
        "fs_size": _FS_SIZE,
        "fs_free": _FS_FREE,
        "uptime": ctx.uptime,
    }


def _require_id_zero(params: dict[str, Any]) -> None:
    """Accept an absent or zero ``id``; refuse any other component instance.

    An omitted ``id`` means zero, which is what every consumer relies on. This
    device has exactly one of each component, so naming another instance is the
    vendor's "not found" case rather than a bad argument.
    """
    raw = params.get("id")
    if raw is None or raw == "":
        return
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ShellyRpcError(-103, "Invalid 'id'") from None
    if value != 0:
        raise ShellyRpcError(-105, f"Bad id={value}")


def _bool_param(params: dict[str, Any], name: str) -> bool:
    """A boolean query parameter, accepting the JSON and query spellings.

    A query parameter arrives as a string, so ``not value`` is the wrong test:
    the string ``"false"`` is truthy.
    """
    raw = params.get(name)
    if raw is None or raw == "":
        return False
    if isinstance(raw, bool):
        return raw
    lowered = str(raw).strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    raise ShellyRpcError(-103, f"Invalid '{name}'")


def _int_param(params: dict[str, Any], name: str, *, default: int) -> int:
    """An integer query parameter."""
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ShellyRpcError(-103, f"Invalid '{name}'") from None


def _config_param(params: dict[str, Any]) -> dict[str, Any]:
    """The configuration object a setter was given, in any accepted spelling.

    Three forms reach here and all three are honoured, because a client may be
    written against any of them: dotted scalars in the query
    (``?config.enable=false``), a ``config`` member holding an object or a JSON
    string, or the parameters themselves with the envelope keys ignored.
    """
    import json

    dotted: dict[str, Any] = {}
    for key, value in params.items():
        if not key.startswith("config."):
            continue
        path = key[len("config.") :].split(".")
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            raise ShellyRpcError(-103, "Invalid 'config'") from None
        cursor = dotted
        for segment in path[:-1]:
            cursor = cursor.setdefault(segment, {})
        cursor[path[-1]] = parsed
    if dotted:
        return dotted

    if "config" in params:
        raw = params["config"]
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raise ShellyRpcError(-103, "Invalid 'config'") from None
        if isinstance(raw, dict):
            # A client may nest one level further — `params.config.config` —
            # which some implementations require and others reject. Unwrap it
            # rather than storing a key called "config".
            inner = raw.get("config")
            return inner if isinstance(inner, dict) else raw
        raise ShellyRpcError(-103, "Invalid 'config'")

    remainder = {
        key: value
        for key, value in params.items()
        if key not in ("id", "method", "src", "dst")
    }
    if not remainder:
        raise ShellyRpcError(-103, "Missing required 'config'")
    return remainder


#: Canonical spellings, in table order. The only place a method name is written
#: with capitals, and what ``Shelly.ListMethods`` reports.
METHOD_NAMES: tuple[str, ...] = (
    "EM.GetStatus",
    "EM.GetConfig",
    "EM1.GetStatus",
    "EM1.GetConfig",
    "EMData.GetStatus",
    "EMData.GetConfig",
    "Shelly.GetDeviceInfo",
    "Shelly.GetConfig",
    "Shelly.GetComponents",
    "Shelly.GetStatus",
    "Shelly.Reboot",
    "Sys.GetConfig",
    "Sys.GetStatus",
    "Cloud.GetConfig",
    "Cloud.GetStatus",
    "Cloud.SetConfig",
    "Ws.GetConfig",
    "Ws.SetConfig",
    "WiFi.GetConfig",
    "WiFi.GetStatus",
    "Temperature.GetConfig",
    "Temperature.GetStatus",
    "Script.List",
    "Script.GetCode",
    "Shelly.ListMethods",
    "BLE.GetStatus",
    "BLE.GetConfig",
    "Eth.GetStatus",
    "Mqtt.GetStatus",
    "Ws.GetStatus",
    "Shelly.CheckForUpdate",
    "BTHome.GetStatus",
    "Modbus.GetStatus",
)

Builder = Callable[[RequestContext, dict[str, Any], "list[float] | None"], Any]

_BUILDERS: dict[str, Builder] = {
    "EM.GetStatus": em_get_status,
    "EM.GetConfig": em_get_config,
    "EM1.GetStatus": em1_get_status,
    "EM1.GetConfig": em1_get_config,
    "EMData.GetStatus": emdata_get_status,
    "EMData.GetConfig": emdata_get_config,
    "Shelly.GetDeviceInfo": shelly_get_device_info,
    "Shelly.GetConfig": shelly_get_config,
    "Shelly.GetComponents": shelly_get_components,
    "Shelly.GetStatus": shelly_get_status,
    "Shelly.Reboot": shelly_reboot,
    "Sys.GetConfig": sys_get_config,
    "Sys.GetStatus": sys_get_status,
    "Cloud.GetConfig": cloud_get_config,
    "Cloud.GetStatus": cloud_get_status,
    "Cloud.SetConfig": cloud_set_config,
    "Ws.GetConfig": ws_get_config,
    "Ws.SetConfig": ws_set_config,
    "WiFi.GetConfig": wifi_get_config,
    "WiFi.GetStatus": wifi_get_status,
    "Temperature.GetConfig": temperature_get_config,
    "Temperature.GetStatus": temperature_get_status,
    "Script.List": script_list,
    "Script.GetCode": script_get_code,
    "Shelly.ListMethods": shelly_list_methods,
    "BLE.GetStatus": ble_get_status,
    "BLE.GetConfig": ble_get_config,
    "Eth.GetStatus": eth_get_status,
    "Mqtt.GetStatus": mqtt_get_status,
    "Ws.GetStatus": ws_get_status,
    "Shelly.CheckForUpdate": shelly_check_for_update,
    "BTHome.GetStatus": bthome_get_status,
    "Modbus.GetStatus": modbus_get_status,
}

#: Dispatch is case-folded, and built from the canonical names so the two
#: cannot drift apart.
METHODS: dict[str, Builder] = {name.lower(): _BUILDERS[name] for name in METHOD_NAMES}

#: The non-RPC pages, keyed the same way as the methods above.
PATH_STATUS = "/status"
PATH_EMETER = "/emeter"
PATH_SHELLY = "/shelly"
PATH_SETTINGS = "/settings"
PATH_RESET_DATA = "/reset_data"

#: Whether a call needs a live meter reading. Keyed over the 33 case-folded
#: method names plus the five non-RPC paths. A plain mapping of booleans, not a
#: predicate: a parameter-dependent rule was tried and got ``dynamic_only``
#: wrong, because a query parameter is a string and ``not "false"`` is False.
NEEDS_READING: dict[str, bool] = {name.lower(): False for name in METHOD_NAMES}
NEEDS_READING.update(
    {
        "em.getstatus": True,
        "em1.getstatus": True,
        "emdata.getstatus": True,
        PATH_STATUS: True,
        PATH_EMETER: True,
        PATH_SHELLY: False,
        PATH_SETTINGS: False,
        PATH_RESET_DATA: False,
    }
)

#: The two composites: they attempt a read and degrade when it fails, rather
#: than reporting an error. Their ``NEEDS_READING`` value is False.
TRIES_READING: frozenset[str] = frozenset({"shelly.getstatus", "shelly.getcomponents"})

#: The surfaces that mean "a battery is polling me". Liveness tracking and the
#: MQTT event are gated on this, so a smart-home integration polling the
#: composite methods does not appear as a battery on the dashboard.
COUNTS_AS_BATTERY_POLL: frozenset[str] = frozenset(
    {"em.getstatus", "em1.getstatus", "emdata.getstatus", PATH_STATUS, PATH_EMETER}
)
