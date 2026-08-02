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
from typing import TYPE_CHECKING

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

    ConfiguredPowermeter = tuple[Powermeter, ClientFilter, bool]


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
    web_config_enabled: bool = False
    web_server_port: int = 52500
    #: The live status dashboard. Off by default: outside Home Assistant the
    #: web port is unauthenticated, so serving it has to be a deliberate act.
    dashboard: bool = False
    #: Whether the dashboard may write — edit the configuration and steer
    #: batteries — rather than only display.
    dashboard_allow_write: bool = False
    #: Serve the dashboard on the plain web port as well as through Home
    #: Assistant ingress. Ingress carries the user's identity; the port does
    #: not, so reaching it directly is a separate opt-in.
    dashboard_direct_access: bool = False
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

        Only needed by a backend with no file of its own, so that the
        dashboard can hand its user a config file to take over from. Power
        sources are the one part of the configuration that never becomes
        settings — :meth:`powermeters` goes straight from the source to built
        objects — so unlike the rest they cannot be rendered generically.
        Deleting this hook is the natural follow-up to giving them a settings
        type of their own.
        """
        return ""
