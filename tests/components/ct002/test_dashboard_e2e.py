"""End-to-end tests for the ESPHome dashboard's document and write path.

Run against the **compiled host binary**, over the test-control channel, so
these cover the real code an ESP32 runs: the status document built from live
consumer/balancer state, and the control writes that come back the other way.

What they cannot cover is the HTTP layer around it. ESPHome has no web server
for the ``host`` platform — ``web_server`` is declared ESP32/ESP8266/BK72XX/
LN882X/RP2040/RTL87XX only, and ``web_server_base.h`` falls back to
``<ESPAsyncWebServer.h>`` off-ESP — so ``dashboard.cpp`` cannot be built here at
all. It is covered by the ESP32 compile matrix; everything behind it is covered
here, and the wire format itself by ``host_status_json_test.cpp``.
"""

from __future__ import annotations

import pytest
from test_shared_e2e import _running_esphome_backend

from astrameter.web_server import _coerce_control_value

pytestmark = pytest.mark.esphome_e2e

MAC = "AABBCCDDEEFF"
CONSUMER_ID = MAC.lower()


@pytest.fixture
def esphome():
    """The host binary, with one battery reporting on phase A into a 300 W import."""
    with _running_esphome_backend() as backend:
        backend.set_clock(1000)
        backend.set_grid(300)
        assert backend.poll(MAC, "A", 0) is not None
        yield backend


def _device(backend) -> dict:
    return backend.status()["devices"][0]


def _consumer(backend) -> dict:
    return _device(backend)["consumers"][0]


def test_document_carries_the_live_battery(esphome) -> None:
    device = _device(esphome)
    assert device["kind"] == "ct002"
    consumer = device["consumers"][0]
    assert consumer["consumer_id"] == CONSUMER_ID
    assert consumer["device_type"] == "HMG-50"
    assert consumer["phase"] == "A"
    assert consumer["bucket"] == "A"
    assert consumer["mode"] == "auto"
    # Read off the balancer, not defaulted: the battery was told to discharge
    # into the 300 W import.
    assert consumer["balancer"]["last_target_w"] > 0


def test_document_carries_the_grid_and_its_buckets(esphome) -> None:
    device = _device(esphome)
    assert device["grid"]["l1_w"] == 300
    assert device["grid"]["grid_total_w"] == 300
    assert device["grid"]["meter_failed"] is False
    bucket = device["buckets"]["A"]
    assert bucket["count"] == 1
    assert bucket["active"] is True
    # Every bucket name the schema defines is present, so the page never has
    # to guess which ones this backend knows about.
    assert set(device["buckets"]) == {"x", "A", "B", "C", "ABC"}


def test_document_carries_the_power_source(esphome) -> None:
    meter = esphome.status()["powermeters"][0]
    assert meter["kind"] == "SensorBackedPowermeter"
    assert meter["online"] is True
    assert meter["last_total_w"] == 300
    # This binary configures no cross-phase filters, so the chain is empty and
    # the key is absent rather than an empty list — the same "absence is not a
    # value" rule the rest of the document follows.
    assert "pipeline" not in meter


def test_document_carries_the_balancer_internals(esphome) -> None:
    balancer = _device(esphome)["balancer"]
    assert balancer["predictor"]["grid_estimate_w"] == 300
    assert "trust" in balancer["predictor"]
    assert "dwell_target" in balancer["import_trim"]
    # No probe is running, so the object is absent rather than empty.
    assert "probe" not in balancer


def test_document_carries_the_control_quality_verdict(esphome) -> None:
    quality = _device(esphome)["balancer"]["control_quality"]
    # One poll in: the loop is being judged but has nothing to say yet.
    assert quality["verdict"] in {"idle", "warmup", "stable", "off_target", "limited"}
    # The settling band is configuration, so it is always meaningful...
    assert quality["band_w"] > 0
    # ...while the measurements only appear once the window holds a sample,
    # so an unmeasured window cannot read as a perfectly held grid.
    if quality["samples"] == 0:
        assert "error_w" not in quality
        assert "in_band_fraction" not in quality
        assert "crossings_per_min" not in quality


def test_document_omits_what_the_firmware_cannot_produce(esphome) -> None:
    document = esphome.status()
    # No SNTP on this binary: ages are reported, dates are not invented.
    assert "generated_at" not in document
    assert "started_at" not in document["service"]
    consumer = document["devices"][0]["consumers"][0]
    assert "last_seen_at" not in consumer
    assert consumer["last_seen_age_s"] >= 0
    # And no configuration surface at all, which is what hides the page's
    # Configuration tab.
    assert "config_mode" not in document["capabilities"]
    assert document["capabilities"]["config_writable"] is False
    assert document["service"]["runtime"] == "esphome"


def test_a_manual_target_write_lands_in_the_document(esphome) -> None:
    assert esphome.control("manual_target", -250, CONSUMER_ID).startswith("ok")
    consumer = _consumer(esphome)
    assert consumer["manual_target_w"] == -250
    # Storing a target does NOT enter manual mode on either stack — the two are
    # independent persisted settings, so a retained replay of one cannot drag
    # the battery out of automatic control (see CT002.set_consumer_manual_target).
    assert consumer["mode"] == "auto"
    assert consumer["manual_enabled"] is False


def test_auto_target_off_is_what_enters_manual_mode(esphome) -> None:
    assert esphome.control("manual_target", -250, CONSUMER_ID).startswith("ok")
    assert esphome.control("auto_target", "false", CONSUMER_ID).startswith("ok")
    consumer = _consumer(esphome)
    assert consumer["mode"] == "manual"
    assert consumer["manual_enabled"] is True
    # ...and back again.
    assert esphome.control("auto_target", "true", CONSUMER_ID).startswith("ok")
    assert _consumer(esphome)["mode"] == "auto"


def test_deactivating_a_battery_shows_up_as_inactive(esphome) -> None:
    assert esphome.control("active", "false", CONSUMER_ID).startswith("ok")
    consumer = _consumer(esphome)
    assert consumer["active"] is False
    assert consumer["mode"] == "inactive"


def test_efficiency_window_weight_round_trips_as_a_percentage(esphome) -> None:
    # The wire unit is a percentage (matching the MQTT entity of that name)
    # and the setter's is a fraction; the scale has to cancel out exactly.
    assert esphome.control("efficiency_window_weight", 50, CONSUMER_ID).startswith("ok")
    assert _consumer(esphome)["efficiency_window_weight_pct"] == 50


def test_a_device_wide_write_lands_too(esphome) -> None:
    assert esphome.control("active_control", "false").startswith("ok")
    assert _device(esphome)["control"]["active_control"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("manual_target", "99999"),
        ("distribution_weight", "-1"),
        ("efficiency_window_weight", "101"),
        ("min_dc_output", "5000"),
    ],
)
def test_out_of_range_is_refused_with_python_s_own_message(
    esphome, field, value
) -> None:
    """The firmware must refuse exactly what the Python dashboard refuses.

    The CT002 setters do not bound their inputs — the ranges live in the MQTT
    command handlers — so a value one stack accepts and the other rejects would
    be settable here and then silently reverted by the next retained-command
    replay.
    """
    reply = esphome.control(field, value, CONSUMER_ID)
    assert reply.startswith("err ")
    with pytest.raises(ValueError) as exc_info:
        _coerce_control_value(field, float(value))
    assert str(exc_info.value) in reply


def test_a_boolean_field_refuses_a_number(esphome) -> None:
    reply = esphome.control("active", 3, CONSUMER_ID)
    assert "must be true or false" in reply


def test_an_unknown_field_is_refused(esphome) -> None:
    assert "Unknown" in esphome.control("nonsense", 1, CONSUMER_ID)


def test_a_write_to_an_unknown_battery_is_refused(esphome) -> None:
    """A stale page — or anyone poking the endpoint — must not mint batteries.

    Every consumer setter creates the entry when it is missing, which is what
    lets a retained MQTT command hold a setting for a battery that has not
    reported yet. Reached from an unauthenticated HTTP endpoint, that would
    grow the consumer map until the ESP32 ran out of heap, and each entry
    would show up on the page as a battery that never existed.
    """
    before = len(_device(esphome)["consumers"])
    assert esphome.control("manual_target", -100, "ffffffffffff").startswith("err")
    assert len(_device(esphome)["consumers"]) == before


def test_a_saved_setting_for_an_absent_battery_stays_writable(esphome) -> None:
    """...but a battery the document DOES show must stay settable.

    A consumer that has been evicted still has its saved override, and the
    page draws a card for it (`never_reported`), so the guard above has to
    admit it rather than turning that card read-only.
    """
    assert esphome.control("manual_target", -250, CONSUMER_ID).startswith("ok")
    esphome.advance_clock(10_000)
    esphome._cmd("evict")
    assert esphome.control("manual_target", -300, CONSUMER_ID).startswith("ok")
