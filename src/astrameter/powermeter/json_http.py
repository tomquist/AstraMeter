import json
import logging
from typing import Any

import aiohttp
from aiohttp import BasicAuth
from jsonpath_ng.ext import parse

from .http_client import HttpPowermeter

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")


def extract_json_value(data: Any, path: str) -> float:
    match = parse(path).find(data)
    if match:
        return float(match[0].value)
    raise ValueError("No match found for the JSON path")


class JsonHttpPowermeter(HttpPowermeter):
    def __init__(
        self,
        url: str,
        json_path: str | list[str],
        username: str | None = None,
        password: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.url = url
        self.json_paths = [json_path] if isinstance(json_path, str) else list(json_path)
        self.auth = (
            BasicAuth(username or "", password or "") if username or password else None
        )
        self.headers = headers or {}

    def _session_options(self) -> dict[str, Any]:
        return {
            **super()._session_options(),
            "auth": self.auth,
            "headers": self.headers,
        }

    async def get_powermeter_watts(self) -> list[float]:
        try:
            data = await self.get_json(self.url)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON: {e}")
            raise ValueError(f"Invalid JSON response: {e}") from e
        except aiohttp.ClientError as e:
            logger.error(f"HTTP request error: {e}")
            raise ValueError(f"HTTP request error: {e}") from e
        return [extract_json_value(data, path) for path in self.json_paths]
