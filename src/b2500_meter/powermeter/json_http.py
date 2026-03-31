import json

import aiohttp
from aiohttp import BasicAuth, ClientTimeout
from jsonpath_ng import parse

from b2500_meter.config.logger import logger

from .base import Powermeter


def extract_json_value(data, path):
    jsonpath_expr = parse(path)
    match = jsonpath_expr.find(data)
    if match:
        return float(match[0].value)
    else:
        raise ValueError("No match found for the JSON path")


class JsonHttpPowermeter(Powermeter):
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
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        self.session = aiohttp.ClientSession(
            auth=self.auth,
            headers=self.headers,
            timeout=ClientTimeout(total=10),
        )

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_json(self):
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        try:
            async with self.session.get(self.url) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON: {e}")
            raise ValueError(f"Invalid JSON response: {e}") from e
        except aiohttp.ClientError as e:
            logger.error(f"HTTP request error: {e}")
            raise ValueError(f"HTTP request error: {e}") from e

    async def get_powermeter_watts_async(self) -> list[float]:
        data = await self.get_json()
        values = []
        for path in self.json_paths:
            values.append(extract_json_value(data, path))
        return values
