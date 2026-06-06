import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrameter.powermeter.fritz import FritzSmartEnergy, compute_login_response

CHALLENGE_XML = (
    '<?xml version="1.0"?>'
    "<SessionInfo><SID>0000000000000000</SID>"
    "<Challenge>2$10000$5A1711$2000$5A1722</Challenge>"
    "<BlockTime>0</BlockTime></SessionInfo>"
)
SID_XML = (
    "<SessionInfo><SID>abcdef0123456789</SID><BlockTime>0</BlockTime></SessionInfo>"
)
LOGIN_FAILED_XML = (
    "<SessionInfo><SID>0000000000000000</SID>"
    "<Challenge>2$10000$5A1711$2000$5A1722</Challenge>"
    "<BlockTime>5</BlockTime></SessionInfo>"
)


def _device_list(power1: str = "-71000", power2: str = "120000") -> str:
    return f"""<?xml version="1.0"?>
<devicelist version="1">
  <device functionbitmask="1" identifier="12345 0123456" productname="FRITZ!Smart Energy 250">
    <present>1</present><name>Energy</name><battery>100</battery>
  </device>
  <device functionbitmask="8322" identifier="12345 0123456-1" productname="FRITZ!Smart Energy 250">
    <present>1</present><name>Strombezug</name>
    <powermeter><power>{power1}</power><energy>16933817</energy></powermeter>
  </device>
  <device functionbitmask="8322" identifier="12345 0123456-2" productname="FRITZ!Smart Energy 250">
    <present>1</present><name>Einspeisung</name>
    <powermeter><power>{power2}</power><energy>653065</energy></powermeter>
  </device>
</devicelist>"""


def _resp(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.text = AsyncMock(return_value=text)
    resp.raise_for_status = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _session(get_responses: list[tuple[str, int]]) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=[_resp(t, s) for t, s in get_responses])
    session.close = AsyncMock()
    return session


async def test_reads_import_branch_as_signed_net_power():
    session = _session(
        [(CHALLENGE_XML, 200), (SID_XML, 200), (_device_list(power1="-71000"), 200)]
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = FritzSmartEnergy("fritz.box", "user", "pass", "12345 0123456")
        await meter.start()
        assert await meter.get_powermeter_watts() == [-71.0]
        await meter.stop()


async def test_explicit_export_branch_suffix():
    session = _session(
        [(CHALLENGE_XML, 200), (SID_XML, 200), (_device_list(power2="120000"), 200)]
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = FritzSmartEnergy("fritz.box", "user", "pass", "123450123456-2")
        await meter.start()
        assert await meter.get_powermeter_watts() == [120.0]
        await meter.stop()


async def test_relogin_on_expired_sid():
    session = _session(
        [
            (CHALLENGE_XML, 200),
            (SID_XML, 200),
            ("", 403),  # SID expired -> 403
            (CHALLENGE_XML, 200),
            (SID_XML, 200),
            (_device_list(power1="50000"), 200),
        ]
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = FritzSmartEnergy("fritz.box", "user", "pass", "12345 0123456")
        await meter.start()
        assert await meter.get_powermeter_watts() == [50.0]
        await meter.stop()


async def test_login_failure_raises():
    session = _session([(CHALLENGE_XML, 200), (LOGIN_FAILED_XML, 200)])
    with patch("aiohttp.ClientSession", return_value=session):
        meter = FritzSmartEnergy("fritz.box", "user", "wrong", "12345 0123456")
        await meter.start()
        with pytest.raises(RuntimeError, match="login failed"):
            await meter.get_powermeter_watts()
        await meter.stop()


async def test_unknown_ain_raises():
    session = _session([(CHALLENGE_XML, 200), (SID_XML, 200), (_device_list(), 200)])
    with patch("aiohttp.ClientSession", return_value=session):
        meter = FritzSmartEnergy("fritz.box", "user", "pass", "99999 9999999")
        await meter.start()
        with pytest.raises(ValueError, match="not found"):
            await meter.get_powermeter_watts()
        await meter.stop()


async def test_get_before_start_raises():
    meter = FritzSmartEnergy("fritz.box", "user", "pass", "12345 0123456")
    with pytest.raises(RuntimeError, match="not started"):
        await meter.get_powermeter_watts()


def test_ain_suffix_appended_by_default():
    meter = FritzSmartEnergy("fritz.box", "user", "pass", "12345 0123456")
    assert meter._ain == "123450123456-1"


def test_ain_suffix_preserved():
    meter = FritzSmartEnergy("fritz.box", "user", "pass", "123450123456-2")
    assert meter._ain == "123450123456-2"


def test_empty_ain_raises():
    with pytest.raises(ValueError, match="requires an AIN"):
        FritzSmartEnergy("fritz.box", "user", "pass", "")


def test_base_url_defaults_to_http():
    meter = FritzSmartEnergy("192.168.1.1", "u", "p", "12345 0123456")
    assert meter._base_url == "http://192.168.1.1"


def test_base_url_https_when_tls():
    meter = FritzSmartEnergy("fritz.box", "u", "p", "12345 0123456", use_tls=True)
    assert meter._base_url == "https://fritz.box"


def test_base_url_explicit_scheme_preserved():
    meter = FritzSmartEnergy("https://fritz.box:443/", "u", "p", "12345 0123456")
    assert meter._base_url == "https://fritz.box:443"


def test_compute_login_response_pbkdf2():
    challenge = "2$10000$5A1711$2000$5A1722"
    password = "1example!"
    hash1 = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex("5A1711"), 10000
    )
    hash2 = hashlib.pbkdf2_hmac("sha256", hash1, bytes.fromhex("5A1722"), 2000)
    assert compute_login_response(challenge, password) == f"5A1722${hash2.hex()}"


def test_compute_login_response_md5_legacy():
    # AVM-documented legacy challenge/response example.
    assert (
        compute_login_response("1234567z", "äbc")
        == "1234567z-9e224a41eeefa284df7bb0f26c2913e2"
    )
