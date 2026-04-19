import ssl
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from astrameter.powermeter import envoy as envoy_module
from astrameter.powermeter.envoy import Envoy

SAMPLE_LINES_RESPONSE = {
    "consumption": [
        {
            "measurementType": "total-consumption",
            "wNow": 1200.5,
            "lines": [{"wNow": 400.0}, {"wNow": 350.0}, {"wNow": 450.5}],
        },
        {
            "measurementType": "net-consumption",
            "wNow": -300.0,
            "lines": [{"wNow": -100.0}, {"wNow": -80.0}, {"wNow": -120.0}],
        },
    ],
}


def _mock_response(json_data: dict | None = None, *, raise_status: int | None = None):
    response = AsyncMock()
    response.json = AsyncMock(return_value=json_data or {})
    if raise_status is not None:
        response.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=raise_status,
                message="error",
            )
        )
    else:
        response.raise_for_status = MagicMock()
    return response


def _ctx(response) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _mock_session(json_data: dict) -> MagicMock:
    session = MagicMock()
    session.get.return_value = _ctx(_mock_response(json_data))
    session.close = AsyncMock()
    return session


def _mock_session_sequence(responses: list[MagicMock]) -> MagicMock:
    session = MagicMock()
    session.get.side_effect = [_ctx(r) for r in responses]
    session.close = AsyncMock()
    return session


def _make_envoy(**overrides: object) -> Envoy:
    defaults: dict[str, object] = {"host": "192.168.1.200", "token": "test-token"}
    defaults.update(overrides)
    return Envoy(**defaults)  # type: ignore[arg-type]


# 1. Single-phase response (no `lines`) -> [wNow_as_float]; assert element type is float.
async def test_single_phase_no_lines() -> None:
    envoy = _make_envoy()
    envoy._session = _mock_session(
        {"consumption": [{"measurementType": "net-consumption", "wNow": 1234.5}]}
    )
    result = await envoy.get_powermeter_watts()
    assert result == [1234.5]
    assert all(isinstance(v, float) for v in result)


# 2. Single-phase fallback when lines: [] -> uses aggregate wNow.
async def test_single_phase_empty_lines() -> None:
    envoy = _make_envoy()
    envoy._session = _mock_session(
        {
            "consumption": [
                {"measurementType": "net-consumption", "wNow": 999.0, "lines": []}
            ]
        }
    )
    assert await envoy.get_powermeter_watts() == [999.0]


# 3. Three-phase with three lines -> [float, float, float].
async def test_three_phase() -> None:
    envoy = _make_envoy()
    envoy._session = _mock_session(SAMPLE_LINES_RESPONSE)
    result = await envoy.get_powermeter_watts()
    assert result == [-100.0, -80.0, -120.0]
    assert all(isinstance(v, float) for v in result)


# 4. Missing net-consumption entry -> ValueError mentioning net-consumption / CTs.
async def test_missing_net_consumption_raises() -> None:
    envoy = _make_envoy()
    envoy._session = _mock_session(
        {"consumption": [{"measurementType": "total-consumption", "wNow": 800.0}]}
    )
    with pytest.raises(ValueError, match="net-consumption"):
        await envoy.get_powermeter_watts()


# 5. Missing consumption key entirely -> ValueError.
async def test_missing_consumption_key_raises() -> None:
    envoy = _make_envoy()
    envoy._session = _mock_session({"production": []})
    with pytest.raises(ValueError, match="consumption"):
        await envoy.get_powermeter_watts()


# 6. Static-token path: Authorization: Bearer <static> header passed through.
async def test_static_token_header() -> None:
    envoy = _make_envoy(token="my-static-token")
    envoy._session = _mock_session(SAMPLE_LINES_RESPONSE)
    await envoy.get_powermeter_watts()
    headers = envoy._session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer my-static-token"


# 7. Auto-obtain when no static token: monkeypatch _obtain_token.
async def test_auto_obtain_when_no_token(monkeypatch) -> None:
    obtain = AsyncMock(return_value="fresh-jwt")
    monkeypatch.setattr(envoy_module, "_obtain_token", obtain)
    envoy = _make_envoy(token="", username="u@example.com", password="pw", serial="123")
    envoy._session = _mock_session(SAMPLE_LINES_RESPONSE)
    envoy._cloud_session = MagicMock()
    await envoy.get_powermeter_watts()
    obtain.assert_awaited_once_with(envoy._cloud_session, "u@example.com", "pw", "123")
    assert envoy._token == "fresh-jwt"
    headers = envoy._session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer fresh-jwt"


# 8. 401 refresh: first .get() yields 401, second yields data; obtain awaited once.
async def test_refreshes_on_401(monkeypatch) -> None:
    obtain = AsyncMock(return_value="new-jwt")
    monkeypatch.setattr(envoy_module, "_obtain_token", obtain)
    envoy = _make_envoy(
        token="expired", username="u@example.com", password="pw", serial="123"
    )
    envoy._session = _mock_session_sequence(
        [
            _mock_response(raise_status=401),
            _mock_response(SAMPLE_LINES_RESPONSE),
        ]
    )
    envoy._cloud_session = MagicMock()
    result = await envoy.get_powermeter_watts()
    assert result == [-100.0, -80.0, -120.0]
    obtain.assert_awaited_once()
    assert envoy._token == "new-jwt"
    # Second call must use the refreshed token.
    second_call_headers = envoy._session.get.call_args_list[1].kwargs["headers"]
    assert second_call_headers["Authorization"] == "Bearer new-jwt"


# 9. 401 with no credentials configured (static token only) -> propagates.
async def test_401_without_credentials_propagates(monkeypatch) -> None:
    obtain = AsyncMock()
    monkeypatch.setattr(envoy_module, "_obtain_token", obtain)
    envoy = _make_envoy(token="static-only")
    envoy._session = _mock_session_sequence([_mock_response(raise_status=401)])
    with pytest.raises(aiohttp.ClientResponseError):
        await envoy.get_powermeter_watts()
    obtain.assert_not_awaited()


# 10. __init__ without any auth config raises ValueError.
def test_init_without_auth_raises() -> None:
    with pytest.raises(ValueError, match="TOKEN or USERNAME"):
        Envoy(host="192.168.1.200")


def test_init_without_host_raises() -> None:
    with pytest.raises(ValueError, match="HOST"):
        Envoy(host="", token="t")


# 11. Cloud session ignores VERIFY_SSL: spy on TCPConnector to confirm only the
# local session gets the no-verify SSLContext; the cloud session uses defaults.
async def test_cloud_session_ignores_verify_ssl(monkeypatch) -> None:
    captured: list[dict] = []
    real_connector = aiohttp.TCPConnector

    def spy(**kwargs):
        captured.append(kwargs)
        return real_connector(**kwargs)

    monkeypatch.setattr(envoy_module, "TCPConnector", spy)

    envoy = _make_envoy(verify_ssl=False)
    await envoy.start()
    try:
        assert len(captured) == 1, (
            "Only the local Envoy session should construct a custom TCPConnector; "
            "the cloud session must use aiohttp's default secure connector."
        )
        local_ssl = captured[0]["ssl"]
        assert isinstance(local_ssl, ssl.SSLContext)
        assert local_ssl.verify_mode == ssl.CERT_NONE
    finally:
        await envoy.stop()


def test_build_ssl_context_verify_true() -> None:
    ctx = envoy_module._build_ssl_context(verify_ssl=True)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_build_ssl_context_verify_false() -> None:
    ctx = envoy_module._build_ssl_context(verify_ssl=False)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
