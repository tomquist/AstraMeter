"""Codegen unit tests for the tibber_pulse ESPHome external component.

Exercise the sensor platform's validators directly, without the full ESPHome
pipeline. Skipped if ESPHome isn't installed in the venv; the YAML compile
matrix in CI is the integration-level guard.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

pytest.importorskip("esphome", reason="ESPHome not installed; skipping codegen tests")

import esphome.config_validation as cv

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "esphome" / "components"))

from tibber_pulse import sensor as tp  # noqa: E402

# The defaults of src/astrameter/powermeter/sml.py. Restated, not imported:
# the esphome package pins its own paho-mqtt, which the astrameter package
# cannot import, and CI installs it into the venv only to run these.
_OBIS_POWER_CURRENT = "0100100700ff"
_OBIS_POWER_L1 = "0100240700ff"
_OBIS_POWER_L2 = "0100380700ff"
_OBIS_POWER_L3 = "01004c0700ff"


@pytest.mark.parametrize(
    "raw",
    [_OBIS_POWER_CURRENT, _OBIS_POWER_L1, _OBIS_POWER_L2, _OBIS_POWER_L3],
)
def test_obis_accepts_the_python_sections_hex_form(raw: str) -> None:
    assert tp.obis_code(raw) == list(bytes.fromhex(raw))
    assert tp.obis_code(raw.upper()) == list(bytes.fromhex(raw))


def test_obis_text_form_defaults_the_f_group_to_255() -> None:
    assert tp.obis_code("1-0:16.7.0") == list(bytes.fromhex(_OBIS_POWER_CURRENT))
    assert tp.obis_code("1-0:36.7.0*255") == list(bytes.fromhex(_OBIS_POWER_L1))
    assert tp.obis_code("1-0:16.7.0*1") == [1, 0, 16, 7, 0, 1]


@pytest.mark.parametrize(
    "raw", ["", "0100100700", "0100100700ffff", "1-0:16.7", "1-0:256.7.0", "zz"]
)
def test_obis_rejects_malformed(raw: str) -> None:
    with pytest.raises(cv.Invalid):
        tp.obis_code(raw)


def test_node_id_takes_ints_and_rejects_url_syntax() -> None:
    assert tp.node_id(1) == "1"
    assert tp.node_id("12") == "12"
    for bad in ["", "1&x=2", "1/..", "1 2"]:
        with pytest.raises(cv.Invalid):
            tp.node_id(bad)


def test_authorization_header_is_http_basic() -> None:
    header = tp.authorization_header("admin", "AD56-54BA")
    assert header.startswith("Basic ")
    assert base64.b64decode(header[6:]) == b"admin:AD56-54BA"


def test_needs_at_least_one_sensor() -> None:
    with pytest.raises(cv.Invalid):
        tp._require_a_sensor({"host": "x", "password": "y"})
    assert tp._require_a_sensor({"power_l2": {}}) == {"power_l2": {}}
