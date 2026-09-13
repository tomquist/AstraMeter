import json
import logging
from typing import Any

import aiohttp
from aiohttp import BasicAuth
from jsonpath_ng.ext import parse

from .base import as_list
from .http_client import HttpPowermeter

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")


def extract_json_value(data: Any, path: str) -> float:
    match = parse(path).find(data)
    if not match:
        raise ValueError("No match found for the JSON path")
    value = match[0].value
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        # A meter that publishes ``null`` (or an object) for a reading it has
        # no answer for right now matches the path but yields nothing to
        # convert. That is a bad reading, not a programming error, so raise
        # what every caller already handles.
        raise ValueError(
            f"JSON path {path!r} matched a non-numeric value: {value!r}"
        ) from exc


class JsonHttpPowermeter(HttpPowermeter):
    def __init__(
        self,
        url: str,
        json_path: str | list[str],
        username: str | None = None,
        password: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.json_paths = as_list(json_path)
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
            logger.error("JSON HTTP: failed to decode response: %s", e)
            raise ValueError(f"Invalid JSON response: {e}") from e
        except aiohttp.ClientError as e:
            logger.error("JSON HTTP: request failed: %s", e)
            raise ValueError(f"HTTP request error: {e}") from e
        return [extract_json_value(data, path) for path in self.json_paths]
