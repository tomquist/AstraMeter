import logging
from unittest.mock import MagicMock

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


def test_ct002_relay_reports_battery_count_per_phase():
    """Relay mode forwards the real per-phase battery count as *_chrg_nb.

    Each battery divides the forwarded aggregate by this count to take its
    1/N share, so the count must be the actual number of batteries on the
    phase, not a flat 1.
    """
    device = CT002(active_control=False)
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "-100"]

    # Two batteries on phase A, one on B, none on C.
    _set_instruction(device, "consumer-a1", phase="A", instructed=-180)
    _set_instruction(device, "consumer-a2", phase="A", instructed=-120)
    _set_instruction(device, "consumer-b", phase="B", instructed=-240)

    response = device._build_response_fields(
        request_fields=request_fields,
        values=[10, 20, 30],
    )

    assert response[8] == "2"  # A_chrg_nb: two batteries on phase A
    assert response[9] == "1"  # B_chrg_nb: one battery on phase B
    assert response[10] == "0"  # C_chrg_nb: none


def test_ct002_active_control_reports_count_one_per_phase():
    """Active control distributes a per-consumer target, so *_chrg_nb stays 1."""
    device = CT002(active_control=True)
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "-100"]

    _set_instruction(device, "consumer-a1", phase="A", instructed=-180)
    _set_instruction(device, "consumer-a2", phase="A", instructed=-120)

    # Only phase A carries power, so B/C stay inactive.
    response = device._build_response_fields(
        request_fields=request_fields,
        values=[10, 0, 0],
    )

    assert response[8] == "1"  # A_chrg_nb: flat 1 in active control (2 batteries)
    assert response[10] == "0"  # C_chrg_nb: inactive phase


def test_ct002_excludes_non_participating_from_aggregation():
    """A consumer that sent participate=0 is left out of the relay aggregates."""
    device = CT002(active_control=False)
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "-100"]

    _set_instruction(device, "consumer-a1", phase="A", instructed=-180)
    _set_instruction(device, "consumer-a2", phase="A", instructed=-120)
    # consumer-a2 opts out.
    device._consumers["consumer-a2"].participates = False

    response = device._build_response_fields(
        request_fields=request_fields,
        values=[10, 0, 0],
    )

    # Only the participating battery's -180 is forwarded, and the count is 1.
    assert response[8] == "1"  # A_chrg_nb: one participating battery
    assert response[15] == "-180"  # A_chrg_power excludes the opted-out -120


async def test_ct002_handle_request_respects_participate_field():
    """The optional 7th request field marks a consumer non-participating."""
    transport = MagicMock()

    async def before_send(_addr, _fields, _consumer_id):
        return [0, 0, 0]

    # 7th field == "0" → opted out → treated as inactive by active control.
    optout = CT002(ct_mac="112233445566", active_control=True)
    optout.before_send = before_send
    req = build_payload(
        ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "-100", "0"]
    )
    await optout._handle_request(req, ("1.1.1.1", 12345), transport)
    consumer = next(iter(optout._consumers.values()))
    assert consumer.participates is False
    assert optout._consumer_mode(consumer.consumer_id).mode == "inactive"

    # No 7th field → defaults to participating.
    default = CT002(ct_mac="112233445566", active_control=False)
    default.before_send = before_send
    req2 = build_payload(
        ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "-100"]
    )
    await default._handle_request(req2, ("1.1.1.1", 12345), transport)
    assert next(iter(default._consumers.values())).participates is True


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
    """Regression for #376: positive *report* but negative *net instruction*
    must not populate *_dchrg_power (otherwise other batteries idle)."""
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "C", "-100"]

    # Venus D reports +500 (PV passthrough) but its net instructed target is
    # -500 (we expect it to charge, even though firmware will keep
    # passing PV through).
    device._update_consumer_report("venus-d", phase="A", power=500)
    device._consumers["venus-d"].last_instructed_power = -500.0

    response = device._build_response_fields(
        request_fields=request_fields,
        values=[0, 0, -500],
    )

    assert response[20] == "0"  # A_dchrg_power must be 0
    assert response[15] == "-500"  # A_chrg_power reflects the net instruction


def test_ct002_discharging_battery_with_small_correction_keeps_dchrg_signal():
    """Net-power semantics: a battery discharging at +500 W that we just
    corrected down by 100 must still register as discharging (+400 W),
    not flip into the charge bucket on the strength of the delta alone."""
    device = CT002()
    request_fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "B", "0"]

    # Venus on phase A is discharging at 500 W; we just sent it a -100 W
    # correction (charge a little) → net target 400 W (still discharging).
    device._update_consumer_report("venus-a", phase="A", power=500)
    device._consumers["venus-a"].last_instructed_power = 400.0

    response = device._build_response_fields(
        request_fields=request_fields,
        values=[0, 0, 0],
    )

    assert response[20] == "400"  # A_dchrg_power = net 400 W discharge
    assert response[15] == "0"  # A_chrg_power stays 0


async def _drive_request(
    device: CT002,
    battery_mac: str,
    phase: str,
    reported_power: int,
    delta_values: list[float],
) -> None:
    """Send one UDP request through ``_handle_request`` with a pinned
    ``before_send`` that injects *delta_values* as the per-phase deltas
    AstraMeter will return.  Drives the production path that populates
    ``last_instructed_power``."""
    transport = MagicMock()

    async def before_send(_addr, _fields, _consumer_id):
        return list(delta_values)

    device.before_send = before_send
    request = build_payload(
        ["HMG-50", battery_mac, "HME-4", "112233445566", phase, str(reported_power)]
    )
    await device._handle_request(request, ("1.1.1.1", 12345), transport)


async def test_handle_request_records_net_instructed_power_not_delta():
    """Regression for the delta-vs-net mistake.

    Venus discharging at +500 W, we correct down by 100 W.  The simulator
    interprets the response as ``new_target = current_power + grid_reading``,
    so the *net* target is +400 W.  ``last_instructed_power`` must record
    400, not the raw -100 delta."""
    device = CT002(ct_mac="112233445566", active_control=False)
    await _drive_request(
        device,
        battery_mac="AABBCCDDEEFF",
        phase="A",
        reported_power=500,
        delta_values=[-100, 0, 0],
    )
    consumer = device._consumers["aabbccddeeff"]
    assert consumer.last_instructed_power == 400.0, (
        f"Expected net target 500 + (-100) = 400, got {consumer.last_instructed_power}"
    )


async def test_handle_request_pv_passthrough_records_zero_net_target():
    """Venus D scenario from issue #376: reports +500 (passthrough), we
    send a -500 charge delta → net target 0, A_dchrg_power must be 0."""
    device = CT002(ct_mac="112233445566", active_control=False)
    await _drive_request(
        device,
        battery_mac="AABBCCDDEEFF",
        phase="A",
        reported_power=500,
        delta_values=[-500, 0, 0],
    )
    consumer = device._consumers["aabbccddeeff"]
    assert consumer.last_instructed_power == 0.0
    by_phase = device._collect_reports_by_phase()
    assert by_phase["A"]["dchrg_power"] == 0
    assert by_phase["A"]["chrg_power"] == 0


async def test_handle_request_skips_instruction_update_in_inspection_mode():
    """No instruction is being given in inspection mode — we send raw
    meter readings as information so the battery can identify its phase,
    and the battery runs its phase-discovery routine rather than our
    integral controller.  ``last_instructed_power`` would mix unrelated
    quantities (the battery's probe + a meter reading we don't expect
    it to apply) and would be attributed to phase A since the battery
    hasn't declared its real phase, so it must stay untouched."""
    device = CT002(ct_mac="112233445566", active_control=False)
    await _drive_request(
        device,
        battery_mac="AABBCCDDEEFF",
        phase="0",
        reported_power=100,
        delta_values=[500, 0, 0],
    )
    consumer = device._consumers["aabbccddeeff"]
    assert consumer.last_instructed_power == 0.0


class _CaptureTransport:
    """Captures the bytes CT002 sends back so the response can be decoded."""

    def __init__(self) -> None:
        self.sent: bytes | None = None

    def sendto(self, data: bytes, _addr) -> None:
        self.sent = data


async def _drive_and_capture(
    device: CT002,
    battery_mac: str,
    phase: str,
    reported_power: int,
    *,
    before_send,
) -> list[str]:
    """Drive one request with *before_send* pinned and return the decoded
    response fields (so per-phase deltas can be asserted)."""
    transport = _CaptureTransport()
    device.before_send = before_send
    request = build_payload(
        ["HMG-50", battery_mac, "HME-4", "112233445566", phase, str(reported_power)]
    )
    await device._handle_request(request, ("1.1.1.1", 12345), transport)
    assert transport.sent is not None, "CT002 sent no response"
    fields, error = parse_request(transport.sent)
    assert error is None, error
    return fields


async def _seed_good_reading(device: CT002, battery_mac: str) -> None:
    """Establish a non-zero cached grid reading via a successful poll."""

    async def good(_addr, _fields, _consumer_id):
        return [500, 0, 0]

    await _drive_and_capture(device, battery_mac, "A", 0, before_send=good)


async def _raise_stale(_addr, _fields, _consumer_id):
    raise ValueError("powermeter unavailable (test)")


async def test_handle_request_holds_with_zero_delta_when_before_send_fails():
    """Issue #403: when the powermeter is unavailable (before_send raises),
    the response must be a zero adjustment so the battery holds — not a delta
    re-derived from the stale cached reading (which would wind it up)."""
    device = CT002(ct_mac="112233445566", active_control=True, fair_distribution=True)
    await _seed_good_reading(device, "AABBCCDDEEFF")
    assert device._consumers["aabbccddeeff"].values == [500, 0, 0]

    fields = await _drive_and_capture(
        device, "AABBCCDDEEFF", "A", 250, before_send=_raise_stale
    )

    # Per-phase deltas + total are all zero (hold).
    assert fields[4:8] == ["0", "0", "0", "0"], fields[4:8]
    # The cached reading is preserved, not overwritten by the failure.
    assert device._consumers["aabbccddeeff"].values == [500, 0, 0]
    # The battery is instructed to hold at its reported output (delta 0).
    assert device._consumers["aabbccddeeff"].last_instructed_power == 250.0


async def test_handle_request_inspection_mode_holds_when_before_send_fails():
    """A phase self-diagnosis poll (inspection marker) while the meter is down
    must not be fed the frozen per-phase reading — feeding stale data is what
    corrupts the Venus phase detection in #403."""
    device = CT002(ct_mac="112233445566", active_control=True)
    await _seed_good_reading(device, "AABBCCDDEEFF")

    fields = await _drive_and_capture(
        device, "AABBCCDDEEFF", "0", 0, before_send=_raise_stale
    )

    assert fields[4:7] == ["0", "0", "0"], fields[4:7]


async def test_handle_request_uses_cache_when_before_send_returns_none():
    """A before_send returning None (e.g. no powermeter matches this client)
    is NOT a failure: the cached reading is still served. Guards that the hold
    path keys on the raised exception, not on a None return."""
    device = CT002(ct_mac="112233445566", active_control=True)
    await _seed_good_reading(device, "AABBCCDDEEFF")

    async def returns_none(_addr, _fields, _consumer_id):
        return None

    # Inspection mode skips the balancer, so the served value is the cached
    # reading verbatim — proving None is treated as "use cache", not "hold".
    fields = await _drive_and_capture(
        device, "AABBCCDDEEFF", "0", 0, before_send=returns_none
    )

    assert fields[4] == "500", fields[4]


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
