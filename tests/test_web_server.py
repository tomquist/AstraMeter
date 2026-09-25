"""Unit tests for the embedded web server (web_server.py)."""

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from astrameter.web_server import WebServer, _health_json_bytes

# -- _health_json_bytes ----------------------------------------------------


def test_health_json_bytes_basic():
    with patch("astrameter.web_server.get_git_commit_sha", return_value=""):
        body = _health_json_bytes()
    data = json.loads(body)
    assert data["status"] == "healthy"
    assert data["service"] == "astrameter"
    assert "git_commit" not in data


def test_health_json_bytes_with_sha():
    with patch("astrameter.web_server.get_git_commit_sha", return_value="abc123"):
        body = _health_json_bytes()
    data = json.loads(body)
    assert data["git_commit"] == "abc123"


# -- WebServer via aiohttp test client -------------------------------------


@pytest.fixture
async def web_client(tmp_path):
    """Create a test client for the WebServer with config editor enabled."""
    config_path = tmp_path / "config.ini"
    config_path.write_text("[POWERMETER]\ntype = json_http\n")
    ws = WebServer(
        port=0,
        config_path=str(config_path),
        enable_web_config=True,
    )

    app = web.Application()
    for path in ("/health", "/health/", "/api", "/api/"):
        app.router.add_get(path, ws._handle_health)
    app.router.add_get("/config", ws._handle_config_ui)
    app.router.add_get("/config/", ws._handle_config_ui)
    app.router.add_get("/api/config", ws._handle_api_config_get)
    app.router.add_get("/api/config/", ws._handle_api_config_get)
    app.router.add_get("/api/key-types", ws._handle_api_key_types)
    app.router.add_get("/api/key-types/", ws._handle_api_key_types)
    app.router.add_post("/api/config", ws._handle_api_config_post)
    app.router.add_post("/api/config/", ws._handle_api_config_post)
    app.router.add_post("/api/restart", ws._handle_api_restart)
    app.router.add_post("/api/restart/", ws._handle_api_restart)
    app.router.add_route("*", "/{path:.*}", ws._handle_not_found)

    async with TestClient(TestServer(app)) as client:
        yield client


async def test_health_endpoint(web_client):
    resp = await web_client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "astrameter"


async def test_health_trailing_slash(web_client):
    resp = await web_client.get("/health/")
    assert resp.status == 200


async def test_api_health(web_client):
    resp = await web_client.get("/api")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "healthy"


async def test_config_ui_html(web_client):
    resp = await web_client.get("/config")
    assert resp.status == 200
    text = await resp.text()
    assert (
        "<html" in text.lower() or "<!doctype" in text.lower() or "<div" in text.lower()
    )


async def test_api_key_types(web_client):
    resp = await web_client.get("/api/key-types")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, (dict, list))


async def test_api_config_get(web_client):
    resp = await web_client.get("/api/config")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, (dict, list))


async def test_not_found(web_client):
    resp = await web_client.get("/nonexistent")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"] == "Not Found"


async def test_api_config_post_invalid_json(web_client):
    resp = await web_client.post(
        "/api/config",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_api_config_post_non_object(web_client):
    resp = await web_client.post("/api/config", json=[1, 2, 3])
    assert resp.status == 400


async def test_api_config_post_success(web_client):
    resp = await web_client.post(
        "/api/config",
        json={
            "sections": {"POWERMETER": {"type": "json_http"}},
            "order": ["POWERMETER"],
        },
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["success"] is True


# -- WebServer without config path -----------------------------------------


@pytest.fixture
async def web_client_no_config():
    ws = WebServer(port=0, config_path=None, enable_web_config=True)
    app = web.Application()
    app.router.add_get("/api/config", ws._handle_api_config_get)
    app.router.add_post("/api/config", ws._handle_api_config_post)
    app.router.add_route("*", "/{path:.*}", ws._handle_not_found)
    async with TestClient(TestServer(app)) as client:
        yield client


async def test_api_config_get_no_path(web_client_no_config):
    resp = await web_client_no_config.get("/api/config")
    assert resp.status == 500
    data = await resp.json()
    assert "error" in data


async def test_api_config_post_no_path(web_client_no_config):
    resp = await web_client_no_config.post("/api/config", json={"sections": {}})
    assert resp.status == 500


# -- WebServer.start / stop / is_running -----------------------------------


async def test_webserver_start_stop():
    ws = WebServer(port=0)
    assert ws.is_running() is False
    result = await ws.start()
    assert result is True
    assert ws.is_running() is True
    await ws.stop()
    assert ws.is_running() is False


async def test_webserver_stop_when_not_started():
    ws = WebServer(port=0)
    await ws.stop()
    assert ws.is_running() is False
