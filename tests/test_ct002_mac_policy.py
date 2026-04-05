from unittest.mock import MagicMock

from astrameter.ct002.ct002 import CT002, build_payload


def make_request(ct_mac):
    fields = ["HMG-50", "AABBCCDDEEFF", "HME-4", ct_mac, "B", "0"]
    return build_payload(fields)


async def test_ct002_accepts_any_when_no_mac():
    device = CT002(ct_mac="")
    transport = MagicMock()
    await device._handle_request(
        make_request("DEADBEEF0001"), ("1.1.1.1", 12345), transport
    )
    transport.sendto.assert_called_once()


async def test_ct002_configured_mac_rejects_mismatch():
    device = CT002(ct_mac="AABBCCDDEEFF")
    transport = MagicMock()
    await device._handle_request(
        make_request("DEADBEEF0001"), ("1.1.1.1", 12345), transport
    )
    transport.sendto.assert_not_called()


async def test_ct002_configured_mac_accepts_match():
    device = CT002(ct_mac="AABBCCDDEEFF")
    transport = MagicMock()
    await device._handle_request(
        make_request("AABBCCDDEEFF"), ("1.1.1.1", 12345), transport
    )
    transport.sendto.assert_called_once()
