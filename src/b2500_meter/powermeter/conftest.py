from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_aiohttp_session():
    """Create a mock aiohttp.ClientSession that returns configurable JSON."""
    json_data = {}

    mock_resp = MagicMock()
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)
    session.close = AsyncMock()

    def set_json(data):
        mock_resp.json.return_value = data

    session.set_json = set_json
    return session
