from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_aiohttp_session():
    """Create a mock aiohttp.ClientSession that returns configurable JSON."""
    json_data = {}

    mock_resp = MagicMock()
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    post_json_data = {}

    mock_post_resp = MagicMock()
    mock_post_resp.json = AsyncMock(return_value=post_json_data)
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.status = 200
    mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
    mock_post_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)
    session.post = MagicMock(return_value=mock_post_resp)
    session.close = AsyncMock()

    def set_json(data):
        mock_resp.json.return_value = data

    def set_post_json(data):
        mock_post_resp.json.return_value = data

    session.set_json = set_json
    session.set_post_json = set_post_json
    return session
