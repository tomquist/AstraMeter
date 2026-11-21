from unittest.mock import MagicMock, patch

import pytest
from aiohttp import BasicAuth, ClientTimeout, DigestAuthMiddleware

from astrameter.powermeter import JsonHttpPowermeter


async def test_single_phase(mock_aiohttp_session: MagicMock) -> None:
    mock_aiohttp_session.set_json({"power": 100})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = JsonHttpPowermeter("http://localhost", "$.power")
        await meter.start()
        assert await meter.get_powermeter_watts() == [100.0]
        await meter.stop()


async def test_three_phase(mock_aiohttp_session: MagicMock) -> None:
    mock_aiohttp_session.set_json({"p1": 100, "p2": 200, "p3": 300})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = JsonHttpPowermeter("http://localhost", ["$.p1", "$.p2", "$.p3"])
        await meter.start()
        assert await meter.get_powermeter_watts() == [100.0, 200.0, 300.0]
        await meter.stop()


async def test_strips_unit_suffix_via_jsonpath_ext(
    mock_aiohttp_session: MagicMock,
) -> None:
    mock_aiohttp_session.set_json({"state": "331.74 W"})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = JsonHttpPowermeter(
            "http://localhost", "$.state.`sub(/[^0-9.\\-]+$/, )`"
        )
        await meter.start()
        assert await meter.get_powermeter_watts() == [331.74]
        await meter.stop()


async def test_headers_and_auth(mock_aiohttp_session: MagicMock) -> None:
    mock_aiohttp_session.set_json({"power": 50})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session) as mock_cls:
        meter = JsonHttpPowermeter(
            "http://localhost",
            "$.power",
            username="user",
            password="pass",
            headers={"X-Test": "1"},
        )
        await meter.start()
        mock_cls.assert_called_once_with(
            auth=BasicAuth("user", "pass"),
            headers={"X-Test": "1"},
            timeout=ClientTimeout(total=2, connect=1),
        )
        result = await meter.get_powermeter_watts()
        assert result == [50.0]
        mock_aiohttp_session.get.assert_called_once_with("http://localhost")
        await meter.stop()


async def test_digest_auth_answers_the_challenge(
    mock_aiohttp_session: MagicMock,
) -> None:
    mock_aiohttp_session.set_json({"act_power": -42})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session) as mock_cls:
        meter = JsonHttpPowermeter(
            "http://shelly/rpc/EM1.GetStatus?id=0",
            "$.act_power",
            username="admin",
            password="secret",
            auth="Digest",
        )
        await meter.start()
        options = mock_cls.call_args.kwargs
        assert "auth" not in options
        [middleware] = options["middlewares"]
        assert isinstance(middleware, DigestAuthMiddleware)
        assert await meter.get_powermeter_watts() == [-42.0]
        await meter.stop()


async def test_digest_without_credentials_sends_no_auth(
    mock_aiohttp_session: MagicMock,
) -> None:
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session) as mock_cls:
        meter = JsonHttpPowermeter("http://localhost", "$.power", auth="digest")
        await meter.start()
        options = mock_cls.call_args.kwargs
        assert options["auth"] is None
        assert "middlewares" not in options
        await meter.stop()


def test_unknown_auth_method_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown JSON HTTP AUTH 'ntlm'"):
        JsonHttpPowermeter("http://localhost", "$.power", auth="ntlm")


async def test_json_path_parsed_once_across_readings(
    mock_aiohttp_session: MagicMock,
) -> None:
    """Parsing a path builds a whole PLY parser; every reading must reuse it."""
    from astrameter.powermeter import json_http

    json_http._compile.cache_clear()
    mock_aiohttp_session.set_json({"p1": 100, "p2": 200})
    with (
        patch("aiohttp.ClientSession", return_value=mock_aiohttp_session),
        patch.object(json_http, "parse", wraps=json_http.parse) as parse,
    ):
        meter = JsonHttpPowermeter("http://localhost", ["$.p1", "$.p2"])
        await meter.start()
        for _ in range(5):
            assert await meter.get_powermeter_watts() == [100.0, 200.0]
        await meter.stop()
    assert parse.call_count == 2
