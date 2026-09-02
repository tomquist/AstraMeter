"""The configuration the app runs on, independent of where it came from.

:class:`AppConfig` is the boundary between the app and its configuration
source. The app asks for typed settings — it never asks for a section or a
key — and each backend answers from whatever shape its source has:

* :class:`astrameter.config.ini_config.IniAppConfig` parses a ``config.ini``.
* :class:`astrameter.config.addon.AddonAppConfig` reads the Home Assistant
  add-on options as they are, straight out of the Supervisor's JSON.

The defaults live here, on the dataclass fields, so both backends agree on
what an unset value means.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

DEFAULT_CT_UDP_PORT = 12345
"""Port a CT emulator listens on.

Mirrors ``astrameter.ct002.UDP_PORT``, which cannot be imported here: the
emulator imports this package, so the config layer must not import it back
(``settings_test.py`` keeps the two in step).
"""

if TYPE_CHECKING:
    from astrameter.config.config_loader import ClientFilter
    from astrameter.mqtt_insights import MqttInsightsConfig
    from astrameter.powermeter import Powermeter


class ConfiguredPowermeter(NamedTuple):
    """A built power source, the clients it answers, and how it is read.

    A tuple rather than a plain dataclass because the emulators still take the
    whole list and unpack it positionally.
    """

    powermeter: Powermeter
    client_filter: ClientFilter
    wait_for_next_message: bool


@dataclass(frozen=True)
class DeviceType:
    """One emulated device type.

    *ct_type* is the CT protocol type a CT002/CT003 emulator announces, and is
    empty for the Shelly emulations; *udp_port* is the port a Shelly emulation
    listens on, which the battery firmware picks by device type; *id_prefix*
    starts the device id generated when the user did not name one.
    """

    ct_type: str = ""
    udp_port: int | None = None
    id_prefix: str = ""


#: Every ``DEVICE_TYPE`` the app accepts, in the order the command line offers
#: them. ``shellypro3em`` has no port of its own: it is expanded into the
#: ``_old``/``_new`` pair before a device is built (see ``_resolve_device_config``).
DEVICE_TYPES: dict[str, DeviceType] = {
    "ct002": DeviceType(ct_type="HME-4"),
    "ct003": DeviceType(ct_type="HME-3"),
    "shellypro3em": DeviceType(id_prefix="shellypro3em"),
    "shellyemg3": DeviceType(udp_port=2222, id_prefix="shellyemg3"),
    "shellyproem50": DeviceType(udp_port=2223, id_prefix="shellyproem50"),
    "shellypro3em_old": DeviceType(udp_port=1010, id_prefix="shellypro3em"),
    "shellypro3em_new": DeviceType(udp_port=2220, id_prefix="shellypro3em"),
}

#: The CT emulator types, in ``DEVICE_TYPES`` order.
CT_DEVICE_TYPES = tuple(name for name, spec in DEVICE_TYPES.items() if spec.ct_type)


def is_ct(device_type: str) -> bool:
    """Whether *device_type* is a CT emulator rather than a Shelly one."""
    return device_type in CT_DEVICE_TYPES


@dataclass(frozen=True)
class SignalSettings:
    """Conditioning applied on top of a raw power source's readings."""

    # Scaling/correction of the raw reading; ``None`` = untouched.
    offsets: list[float] | None = None
    multipliers: list[float] | None = None
    throttle_interval: float = 0.0
    wait_for_next_message: bool = True
    # Hampel outlier filter; a window of 0 disables it.
    hampel_window: int = 0
    hampel_n_sigma: float = 3.0
    hampel_min_threshold: float = 0.0
    # EMA smoothing; an alpha of 0 disables it.
    smooth_alpha: float = 0.0
    max_smooth_step: float = 0.0
    deadband: float = 0.0
    # PID controller; a Kp of 0 disables it.
    pid_kp: float = 0.0
    pid_ki: float = 0.0
    pid_kd: float = 0.0
    pid_output_max: float = 800.0
    pid_mode: str = "bias"


@dataclass(frozen=True)
class GeneralSettings:
    """What to emulate, how to serve the UI, and the power-source defaults."""

    device_types: list[str] = field(default_factory=lambda: ["shellypro3em"])
    device_ids: list[str] = field(default_factory=list)
    skip_powermeter_test: bool = False
    dedupe_time_window: float = 0.0
    enable_web_server: bool = True
    #: The ``config.ini`` editor. Tri-state: ``None`` leaves it to the dashboard's
    #: Configuration tab, while an explicit ``True``/``False`` from an earlier
    #: release keeps meaning what it said (``WebServer.serve_config_editor``).
    web_config_enabled: bool | None = None
    web_server_port: int = 52500
    #: The live status dashboard; ``False`` serves nothing.
    dashboard: bool = True
    #: Whether the dashboard may edit the configuration and steer batteries.
    #: Nothing authenticates the web port outside Home Assistant, so ``False``
    #: is the read-only option.
    dashboard_allow_write: bool = True
    #: Serve the dashboard on the plain web port, not just through ingress.
    #: Ingress carries the user's identity; the port does not, hence the opt-in.
    dashboard_direct_access: bool = False
    #: Extra host names the web port answers under, comma-separated. A DNS name
    #: is the one thing an outside site can point at this port, so unlike IPs,
    #: ``localhost`` and ``.local`` it must be listed (``is_allowed_host``).
    dashboard_allowed_hosts: str = ""
    #: Conditioning every power source starts from; a source may override it.
    signal: SignalSettings = SignalSettings()


@dataclass(frozen=True)
class CtSettings:
    """A CT002/CT003 emulator and its active-control behaviour."""

    ct_mac: str = ""
    udp_port: int = DEFAULT_CT_UDP_PORT
    wifi_rssi: int = -50
    dedupe_time_window: float = 0.0
    #: ``None`` = adaptive eviction (~2 missed poll cycles, like the real CT).
    consumer_ttl: int | None = None
    debug_status: bool = False
    # Opt-in HTTP cloud reporting (hamedata.com).
    cloud_reporting: bool = False
    cloud_reporting_host: str = "eu.hamedata.com"
    cloud_reporting_interval: float = 60.0
    # Active control / balancing.
    active_control: bool = True
    fair_distribution: bool = True
    balance_gain: float = 0.2
    balance_deadband: int = 25
    max_correction_per_step: int = 80
    error_boost_threshold: int = 150
    error_boost_max: float = 0.5
    error_reduce_threshold: int = 20
    max_target_step: int = 0
    pace_base_step: int = 30
    pace_max_step: int = 100
    osc_damp_max: float = 0.95
    osc_damp_alpha: float = 0.3
    osc_damp_decay: float = 0.05
    osc_damp_threshold: float = 300.0
    grid_predict_trust: float = 0.5
    concentrate_deadband: float = 60.0
    import_trim_w: float = 15.0
    # Saturation detection (a full/empty battery handing load over).
    saturation_detection: bool = True
    saturation_alpha: float = 0.15
    min_target_for_saturation: int = 20
    saturation_grace_seconds: float = 90.0
    saturation_stall_timeout_seconds: float = 60.0
    saturation_decay_factor: float = 0.995
    # Efficiency optimization.
    min_efficient_power: int = 0
    probe_min_power: int = 80
    efficiency_rotation_interval: int = 900
    efficiency_fade_alpha: float = 0.15
    efficiency_saturation_threshold: float = 0.4
    efficiency_demand_alpha: float = 0.1
    min_dc_output: float = 0.0


@dataclass(frozen=True)
class MarstekSettings:
    """Marstek account used once to auto-register the managed fake CT device."""

    enable: bool = False
    mailbox: str = ""
    password: str = ""
    base_url: str = "https://eu.hamedata.com"
    timezone: str = "Europe/Berlin"


class AppConfig(ABC):
    """A source of configuration for the app.

    Implementations decide where the values come from; callers only see the
    settings above.
    """

    #: File backing this configuration, when there is one — the web UI offers
    #: its editor only for a real file.
    path: str | None = None

    def prefetch(self) -> None:  # noqa: B027 - an opt-in hook, not abstract
        """Do any slow lookups now, before the event loop starts.

        A backend that has to ask a remote service for part of its
        configuration resolves it here, so it never blocks the running loop
        later. Sources that only read local files need not override this.
        """

    @abstractmethod
    def general(self) -> GeneralSettings:
        """Emulation, web UI and power-source defaults."""

    @abstractmethod
    def ct(self, device_type: str) -> CtSettings:
        """Settings for the ``ct002`` / ``ct003`` emulator."""

    @abstractmethod
    def marstek(self) -> MarstekSettings:
        """Marstek cloud account settings."""

    @abstractmethod
    def mqtt_insights(self) -> MqttInsightsConfig | None:
        """MQTT insights configuration, or ``None`` when MQTT is off."""

    @abstractmethod
    def powermeters(self, general: GeneralSettings) -> list[ConfiguredPowermeter]:
        """Build the configured power sources, conditioning included.

        *general* is passed in (rather than read again) so command-line
        overrides applied to it also reach the power sources.
        """

    def render_powermeters_ini(self) -> str:
        """The power-source sections of an equivalent ``config.ini``.

        Power sources are built objects rather than settings, so a backend with
        no file of its own renders them here for the dashboard's hand-over file.
        """
        return ""
