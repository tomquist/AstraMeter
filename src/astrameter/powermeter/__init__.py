"""AstraMeter powermeter package.

Lazily exposes every powermeter class so that importing the package (or a light
submodule such as ``astrameter.powermeter.base`` or
``astrameter.powermeter.wrappers``) does **not** eagerly import heavy/optional
backends (``pymodbus``, ``smllib``, ``pyserial-asyncio-fast``, ``aiomqtt``).
Each name resolves to its submodule only on first access via :pep:`562`
``__getattr__``. ``from astrameter.powermeter import X`` and
``astrameter.powermeter.X`` both work unchanged.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# Public name -> defining submodule (relative to this package).
_LAZY: dict[str, str] = {
    "AmisReader": "amisreader",
    "Powermeter": "base",
    "Emlog": "emlog",
    "Envoy": "envoy",
    "ESPHome": "esphome",
    "FritzSmartEnergy": "fritz",
    "HomeAssistant": "homeassistant",
    "HomeWizardPowermeter": "homewizard",
    "IoBroker": "iobroker",
    "JsonHttpPowermeter": "json_http",
    "ModbusPowermeter": "modbus",
    "MqttPowermeter": "mqtt",
    "Script": "script",
    "Shelly": "shelly",
    "Shelly1PM": "shelly",
    "Shelly3EM": "shelly",
    "Shelly3EMPro": "shelly",
    "ShellyEM": "shelly",
    "ShellyPlus1PM": "shelly",
    "Shrdzm": "shrdzm",
    "SmaEnergyMeter": "sma_energy_meter",
    "Sml": "sml",
    "parse_sml_obis_config": "sml",
    "Tasmota": "tasmota",
    "TQEnergyManager": "tq_em",
    "VZLogger": "vzlogger",
    "DeadbandPowermeter": "wrappers",
    "HampelPowermeter": "wrappers",
    "PidPowermeter": "wrappers",
    "PowermeterWrapper": "wrappers",
    "SmoothedPowermeter": "wrappers",
    "ThrottledPowermeter": "wrappers",
    "TransformedPowermeter": "wrappers",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    submodule = _LAZY.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{submodule}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache for subsequent lookups
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:
    from .amisreader import AmisReader as AmisReader
    from .base import Powermeter as Powermeter
    from .emlog import Emlog as Emlog
    from .envoy import Envoy as Envoy
    from .esphome import ESPHome as ESPHome
    from .fritz import FritzSmartEnergy as FritzSmartEnergy
    from .homeassistant import HomeAssistant as HomeAssistant
    from .homewizard import HomeWizardPowermeter as HomeWizardPowermeter
    from .iobroker import IoBroker as IoBroker
    from .json_http import JsonHttpPowermeter as JsonHttpPowermeter
    from .modbus import ModbusPowermeter as ModbusPowermeter
    from .mqtt import MqttPowermeter as MqttPowermeter
    from .script import Script as Script
    from .shelly import (
        Shelly as Shelly,
    )
    from .shelly import (
        Shelly1PM as Shelly1PM,
    )
    from .shelly import (
        Shelly3EM as Shelly3EM,
    )
    from .shelly import (
        Shelly3EMPro as Shelly3EMPro,
    )
    from .shelly import (
        ShellyEM as ShellyEM,
    )
    from .shelly import (
        ShellyPlus1PM as ShellyPlus1PM,
    )
    from .shrdzm import Shrdzm as Shrdzm
    from .sma_energy_meter import SmaEnergyMeter as SmaEnergyMeter
    from .sml import (
        Sml as Sml,
    )
    from .sml import (
        parse_sml_obis_config as parse_sml_obis_config,
    )
    from .tasmota import Tasmota as Tasmota
    from .tq_em import TQEnergyManager as TQEnergyManager
    from .vzlogger import VZLogger as VZLogger
    from .wrappers import (
        DeadbandPowermeter as DeadbandPowermeter,
    )
    from .wrappers import (
        HampelPowermeter as HampelPowermeter,
    )
    from .wrappers import (
        PidPowermeter as PidPowermeter,
    )
    from .wrappers import (
        PowermeterWrapper as PowermeterWrapper,
    )
    from .wrappers import (
        SmoothedPowermeter as SmoothedPowermeter,
    )
    from .wrappers import (
        ThrottledPowermeter as ThrottledPowermeter,
    )
    from .wrappers import (
        TransformedPowermeter as TransformedPowermeter,
    )
