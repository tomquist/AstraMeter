"""Every power source AstraMeter can read.

A process runs one or two backends out of the twenty-odd here, and several
bring a heavy third-party stack with them (aioesphomeapi, pymodbus, pyserial,
...). So the backends are imported on first access rather than with the
package: ``from astrameter.powermeter import Envoy`` still works, it just loads
only the Envoy module. The base class and the signal wrappers are cheap and
needed by every configuration, so they load eagerly.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .base import Powermeter
from .wrappers import (
    DeadbandPowermeter,
    HampelPowermeter,
    PidPowermeter,
    PowermeterWrapper,
    SmoothedPowermeter,
    ThrottledPowermeter,
    TransformedPowermeter,
)

if TYPE_CHECKING:
    from .amisreader import AmisReader
    from .emlog import Emlog
    from .envoy import Envoy
    from .esphome import ESPHome
    from .esphome_native import ESPHomeNative
    from .fritz import FritzSmartEnergy
    from .fronius import Fronius
    from .homeassistant import HomeAssistant
    from .homewizard import HomeWizardPowermeter
    from .iobroker import IoBroker
    from .json_http import JsonHttpPowermeter
    from .modbus import ModbusPowermeter
    from .mqtt import MqttPowermeter
    from .refoss import Refoss
    from .script import Script
    from .shelly import Shelly, Shelly1PM, Shelly3EMPro, ShellyEM, ShellyPlus1PM
    from .shrdzm import Shrdzm
    from .sma_energy_meter import SmaEnergyMeter
    from .sml import Sml, parse_sml_obis_config
    from .tasmota import Tasmota
    from .tibber_pulse import TibberPulse
    from .tq_em import TQEnergyManager
    from .vzlogger import VZLogger

    #: ``TYPE = 3EM`` builds a :class:`ShellyEM`; the old name stays importable.
    Shelly3EM = ShellyEM

#: Public name -> (submodule, attribute) for every lazily loaded backend.
_LAZY: dict[str, tuple[str, str]] = {
    "AmisReader": (".amisreader", "AmisReader"),
    "Emlog": (".emlog", "Emlog"),
    "Envoy": (".envoy", "Envoy"),
    "ESPHome": (".esphome", "ESPHome"),
    "ESPHomeNative": (".esphome_native", "ESPHomeNative"),
    "FritzSmartEnergy": (".fritz", "FritzSmartEnergy"),
    "Fronius": (".fronius", "Fronius"),
    "HomeAssistant": (".homeassistant", "HomeAssistant"),
    "HomeWizardPowermeter": (".homewizard", "HomeWizardPowermeter"),
    "IoBroker": (".iobroker", "IoBroker"),
    "JsonHttpPowermeter": (".json_http", "JsonHttpPowermeter"),
    "ModbusPowermeter": (".modbus", "ModbusPowermeter"),
    "MqttPowermeter": (".mqtt", "MqttPowermeter"),
    "Refoss": (".refoss", "Refoss"),
    "Script": (".script", "Script"),
    "Shelly": (".shelly", "Shelly"),
    "Shelly1PM": (".shelly", "Shelly1PM"),
    "Shelly3EM": (".shelly", "ShellyEM"),
    "Shelly3EMPro": (".shelly", "Shelly3EMPro"),
    "ShellyEM": (".shelly", "ShellyEM"),
    "ShellyPlus1PM": (".shelly", "ShellyPlus1PM"),
    "Shrdzm": (".shrdzm", "Shrdzm"),
    "SmaEnergyMeter": (".sma_energy_meter", "SmaEnergyMeter"),
    "Sml": (".sml", "Sml"),
    "parse_sml_obis_config": (".sml", "parse_sml_obis_config"),
    "Tasmota": (".tasmota", "Tasmota"),
    "TibberPulse": (".tibber_pulse", "TibberPulse"),
    "TQEnergyManager": (".tq_em", "TQEnergyManager"),
    "VZLogger": (".vzlogger", "VZLogger"),
}


def __getattr__(name: str) -> Any:
    try:
        module, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module, __name__), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})


__all__ = [
    "AmisReader",
    "DeadbandPowermeter",
    "ESPHome",
    "ESPHomeNative",
    "Emlog",
    "Envoy",
    "FritzSmartEnergy",
    "Fronius",
    "HampelPowermeter",
    "HomeAssistant",
    "HomeWizardPowermeter",
    "IoBroker",
    "JsonHttpPowermeter",
    "ModbusPowermeter",
    "MqttPowermeter",
    "PidPowermeter",
    "Powermeter",
    "PowermeterWrapper",
    "Refoss",
    "Script",
    "Shelly",
    "Shelly1PM",
    "Shelly3EM",
    "Shelly3EMPro",
    "ShellyEM",
    "ShellyPlus1PM",
    "Shrdzm",
    "SmaEnergyMeter",
    "Sml",
    "SmoothedPowermeter",
    "TQEnergyManager",
    "Tasmota",
    "ThrottledPowermeter",
    "TibberPulse",
    "TransformedPowermeter",
    "VZLogger",
    "parse_sml_obis_config",
]
