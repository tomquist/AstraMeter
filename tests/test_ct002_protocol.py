import logging

from astrameter.ct002 import CT002, ReportingConsumerRow
from astrameter.ct002.protocol import (
    ETX,
    RESPONSE_LABELS,
    SOH,
    STX,
    build_payload,
    calculate_checksum,
    parse_request,
)


def test_parse_request_roundtrip():
    fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "5", "7"]
    payload = build_payload(fields)
    parsed, error = parse_request(payload)
    assert error is None
    assert parsed == fields


def test_parse_request_checksum_error():
    fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "0", "0"]
    payload = bytearray(build_payload(fields))
    payload[-1] = ord("0") if payload[-1] != ord("0") else ord("1")
    parsed, error = parse_request(payload)
    assert parsed is None
    assert "Checksum" in error


def test_parse_request_checksum_space_tolerance():
    fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "0", "0"]
    payload = bytearray(build_payload(fields))
    payload[-2] = ord(" ")
    parsed, error = parse_request(payload)
    assert error is None
    assert parsed == fields


def test_build_payload_length_and_checksum():
    fields = ["HMG-50", "AABBCCDDEEFF", "HME-3", "112233445566", "0", "0"]
    payload = build_payload(fields)
    assert payload[0] == SOH
    assert payload[1] == STX
    assert payload[-3] == ETX
    sep_index = payload.find(b"|", 2)
    length = int(payload[2:sep_index].decode("ascii"))
    assert length == len(payload)
    xor = 0
    for b in payload[: length - 2]:
        xor ^= b
    expected = f"{xor:02x}".encode("ascii")
    assert payload[-2:] == expected


def test_checksum_matches_helper():
    payload = bytearray([SOH, STX, 0x30, 0x30, ETX])
    checksum = calculate_checksum(payload)
    assert isinstance(checksum, int)
    expected = SOH ^ STX ^ 0x30 ^ 0x30 ^ ETX
    assert checksum == expected


def test_ct002_response_field_count_stable():
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "0", "0"]

    response_fields = device._build_response_fields(
        request_fields=request_fields,
        values=[500, 0, 0],
    )

    assert len(response_fields) == len(RESPONSE_LABELS)


def test_reporting_consumer_count() -> None:
    device = CT002()
    assert device.reporting_consumer_count() == 0
    device._update_consumer_report("a", "A", 1)
    device._update_consumer_report("b", "B", -2)
    assert device.reporting_consumer_count() == 2


def test_reporting_consumer_rows_order_and_shape() -> None:
    device = CT002()
    assert device.reporting_consumer_rows() == ()

    device._update_consumer_report("z-mac", "C", 1, "HMA-2", source_ip="192.168.1.51")
    device._update_consumer_report("a-mac", "A", 2, "HME-4", source_ip="192.168.1.50")
    rows = device.reporting_consumer_rows()
    assert rows == (
        ReportingConsumerRow("HME-4", "a-mac", "192.168.1.50", "a"),
        ReportingConsumerRow("HMA-2", "z-mac", "192.168.1.51", "c"),
    )


def _set_instruction(
    device: CT002, consumer_id: str, phase: str, instructed: float
) -> None:
    """Record an instruction value for *consumer_id* on *phase*.

    The cross-talk *_chrg_power / *_dchrg_power fields aggregate the
    *instructions* AstraMeter sends to each battery, not the powers they
    report.  Tests that want to assert on the aggregate must populate the
    instruction state, not just the report.
    """
    device._update_consumer_report(consumer_id, phase=phase, power=0)
    device._consumers[consumer_id].last_instructed_power = float(instructed)


def test_ct002_relays_sum_of_charge_instructions_by_phase():
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "B", "-100"]

    # We *instructed* consumer-a to charge on A and consumer-b to charge on B.
    _set_instruction(device, "consumer-a", phase="A", instructed=-180)
    _set_instruction(device, "consumer-b", phase="B", instructed=-240)

    response_for_a = device._build_response_fields(
        request_fields=request_fields,
        values=[10, 20, 30],
    )

    # negative instructions are forwarded into *_chrg_power
    assert response_for_a[15] == "-180"  # A_chrg_power
    assert response_for_a[16] == "-240"  # B_chrg_power
    assert response_for_a[21] == "0"  # B_dchrg_power
    assert response_for_a[8] == "1"  # A_chrg_nb
    assert response_for_a[9] == "1"  # B_chrg_nb

    response_for_b = device._build_response_fields(
        request_fields=request_fields,
        values=[10, 20, 30],
    )

    assert response_for_b[15] == "-180"  # A_chrg_power
    assert response_for_b[16] == "-240"  # B_chrg_power


def test_ct002_splits_positive_instructions_into_dchrg_fields():
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "B", "100"]

    _set_instruction(device, "consumer-a", phase="A", instructed=500)
    _set_instruction(device, "consumer-b", phase="B", instructed=800)

    response = device._build_response_fields(
        request_fields=request_fields,
        values=[10, 20, 30],
    )

    # positive instructions are forwarded into *_dchrg_power
    assert response[15] == "0"  # A_chrg_power
    assert response[16] == "0"  # B_chrg_power
    assert response[20] == "500"  # A_dchrg_power
    assert response[21] == "800"  # B_dchrg_power
    assert response[8] == "1"  # A_chrg_nb flag still marks active phase contribution
    assert response[9] == "1"  # B_chrg_nb


def test_ct002_splits_mixed_sign_instructions_per_storage_before_aggregation():
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "0"]

    # Same phase, opposite instructions to different storages.
    _set_instruction(device, "consumer-a", phase="A", instructed=-300)
    _set_instruction(device, "consumer-b", phase="A", instructed=120)

    response = device._build_response_fields(
        request_fields=request_fields,
        values=[10, 20, 30],
    )

    # Split is done per storage instruction before phase aggregation.
    assert response[15] == "-300"  # A_chrg_power
    assert response[20] == "120"  # A_dchrg_power
    assert response[8] == "1"  # A_chrg_nb active flag


def test_ct002_pv_passthrough_does_not_appear_as_dchrg():
    """Regression for #376: positive *report* but negative *instruction* must
    not populate *_dchrg_power (otherwise other batteries idle)."""
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "C", "-100"]

    # Venus D reports +500 (PV passthrough) but we instructed -500 (charge).
    device._update_consumer_report("venus-d", phase="A", power=500)
    device._consumers["venus-d"].last_instructed_power = -500.0

    response = device._build_response_fields(
        request_fields=request_fields,
        values=[0, 0, -500],
    )

    assert response[20] == "0"  # A_dchrg_power must be 0
    assert response[15] == "-500"  # A_chrg_power reflects the instruction


def test_ct002_info_idx_increments_and_wraps():
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "0"]

    first = device._build_response_fields(request_fields, [1, 2, 3])
    second = device._build_response_fields(request_fields, [1, 2, 3])

    assert first[13] == "0"
    assert second[13] == "1"

    device._info_idx_counter = 255
    wrap = device._build_response_fields(request_fields, [1, 2, 3])
    after_wrap = device._build_response_fields(request_fields, [1, 2, 3])

    assert wrap[13] == "255"
    assert after_wrap[13] == "0"


def test_ct002_logs_phase_detection_and_change(caplog):
    device = CT002()

    with caplog.at_level(logging.INFO):
        device._update_consumer_report("consumer-a", phase="A", power=100)
        device._update_consumer_report("consumer-a", phase="A", power=80)
        device._update_consumer_report("consumer-a", phase="B", power=120)

    messages = [r.message for r in caplog.records if "CT002 consumer" in r.message]
    assert any("phase detected: A" in m for m in messages)
    assert any("phase changed: A -> B" in m for m in messages)
    assert len(messages) == 2
