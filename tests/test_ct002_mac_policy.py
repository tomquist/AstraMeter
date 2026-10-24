from unittest.mock import MagicMock

from astrameter.ct002.ct002 import CT002, build_payload, parse_request


def make_request(ct_mac):
    fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", ct_mac, "B", "0"]
    return build_payload(fields)


async def test_ct002_accepts_any_when_no_mac() -> None:
    device = CT002(ct_mac="")
    transport = MagicMock()
    await device._handle_request(
        make_request("DEADBEEF0001"), ("1.1.1.1", 12345), transport
    )
    transport.sendto.assert_called_once()


async def test_ct002_configured_mac_rejects_mismatch() -> None:
    device = CT002(ct_mac="AABBCCDDEEFF")
    transport = MagicMock()
    await device._handle_request(
        make_request("DEADBEEF0001"), ("1.1.1.1", 12345), transport
    )
    transport.sendto.assert_not_called()


async def test_ct002_configured_mac_accepts_match() -> None:
    device = CT002(ct_mac="AABBCCDDEEFF")
    transport = MagicMock()
    await device._handle_request(
        make_request("AABBCCDDEEFF"), ("1.1.1.1", 12345), transport
    )
    transport.sendto.assert_called_once()


def reply_fields(transport: MagicMock) -> list[str]:
    transport.sendto.assert_called_once()
    fields, error = parse_request(transport.sendto.call_args[0][0])
    assert fields is not None, error
    return fields


async def test_ct002_answers_zero_mac_probe_with_configured_mac() -> None:
    # An unbound battery probes with CT MAC 000000000000; a real CT answers it
    # with its own MAC, which is how the battery learns which CT to pair with.
    device = CT002(ct_mac="02b250aabbcc")
    transport = MagicMock()
    await device._handle_request(
        make_request("000000000000"), ("1.1.1.1", 12345), transport
    )
    assert reply_fields(transport)[1] == "02b250aabbcc"


async def test_ct002_configured_mac_ignores_empty_ct_mac() -> None:
    # Only 000000000000 is a wildcard: a real CT drops an empty CT MAC field.
    device = CT002(ct_mac="02b250aabbcc")
    transport = MagicMock()
    await device._handle_request(make_request(""), ("1.1.1.1", 12345), transport)
    transport.sendto.assert_not_called()


async def test_ct002_replies_with_normalized_configured_mac() -> None:
    # The battery compares our reply's CT MAC byte for byte with the one it
    # stores (lowercase, no separators), so CT_MAC is normalized to that form.
    device = CT002(ct_mac="AA:BB:CC:DD:EE:FF")
    transport = MagicMock()
    await device._handle_request(
        make_request("aabbccddeeff"), ("1.1.1.1", 12345), transport
    )
    assert reply_fields(transport)[1] == "aabbccddeeff"
