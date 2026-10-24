"""Starting AstraMeter must not load what the configuration doesn't use.

Every power-meter backend, MQTT and the config writer each bring their own
third-party stack. Loaded eagerly they cost tens of MiB of resident memory in
every process, whichever single backend it actually runs, so they load on
first use. A new top-level import of one of them quietly undoes that; these
tests catch it.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

#: Loaded only by the feature that needs them.
OPTIONAL_STACKS = (
    "aioesphomeapi",  # [ESPHOMENATIVE]
    "aiomqtt",  # [MQTT], MQTT Insights
    "configupdater",  # saving the config from the dashboard
    "jsonpath_ng",  # [JSON_HTTP], [MQTT]
    "paho",  # via aiomqtt
    "pymodbus",  # [MODBUS]
    "serial_asyncio_fast",  # [SML]
    "smllib",  # [SML], [TIBBER_PULSE]
    "zeroconf",  # via aioesphomeapi
)


def _modules_after(code: str) -> set[str]:
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import json, sys\n{code}\nprint(json.dumps(sorted(sys.modules)))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {name.split(".")[0] for name in json.loads(out.splitlines()[-1])}


def test_starting_the_app_loads_no_optional_stack() -> None:
    loaded = _modules_after("import astrameter.main")
    assert loaded.isdisjoint(OPTIONAL_STACKS), sorted(
        loaded.intersection(OPTIONAL_STACKS)
    )
    # Not needed at all any more: the Supervisor client uses urllib.
    assert "requests" not in loaded


def test_reading_a_config_loads_only_its_backend() -> None:
    loaded = _modules_after(
        "from astrameter.config.config_loader import read_all_powermeter_configs\n"
        "from astrameter.config.config_loader import new_config_parser\n"
        "c = new_config_parser()\n"
        "c.read_string('[JSON_HTTP]\\nURL = http://x/\\nJSON_PATHS = $.p\\n')\n"
        "read_all_powermeter_configs(c)\n"
    )
    assert "jsonpath_ng" in loaded
    assert loaded.isdisjoint(set(OPTIONAL_STACKS) - {"jsonpath_ng"})


@pytest.mark.parametrize(
    "name",
    ["ESPHomeNative", "ModbusPowermeter", "Shelly3EM", "Sml", "parse_sml_obis_config"],
)
def test_backends_stay_importable_from_the_package(name: str) -> None:
    import astrameter.powermeter as powermeter

    assert getattr(powermeter, name) is not None
    assert name in dir(powermeter)


def test_the_old_shelly_3em_name_still_means_shelly_em() -> None:
    from astrameter.powermeter import Shelly3EM, ShellyEM

    assert Shelly3EM is ShellyEM


def test_an_unknown_backend_name_is_an_attribute_error() -> None:
    import astrameter.powermeter as powermeter

    with pytest.raises(AttributeError):
        powermeter.NoSuchMeter  # noqa: B018
