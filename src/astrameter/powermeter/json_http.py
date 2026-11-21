import functools
import json
import logging
from typing import Any

import aiohttp
from aiohttp import BasicAuth, DigestAuthMiddleware
from jsonpath_ng.ext import parse

from .base import as_list
from .http_client import HttpPowermeter

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")


@functools.lru_cache(maxsize=64)
def _compile(path: str) -> Any:
    """*path* parsed once. jsonpath-ng builds a fresh PLY parser per ``parse``
    call (~10 ms and ~0.5 MiB of short-lived objects), far too much to repeat
    for every reading of every phase."""
    return parse(path)


def extract_json_value(data: Any, path: str) -> float:
    match = _compile(path).find(data)
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


AUTH_METHODS = ("basic", "digest")


class JsonHttpPowermeter(HttpPowermeter):
    def __init__(
        self,
        url: str,
        json_path: str | list[str],
        username: str | None = None,
        password: str | None = None,
        headers: dict[str, str] | None = None,
        auth: str = "basic",
    ) -> None:
        method = auth.strip().lower()
        if method not in AUTH_METHODS:
            raise ValueError(
                f"Unknown JSON HTTP AUTH {auth!r}; expected one of "
                f"{', '.join(AUTH_METHODS)}"
            )
        self.url = url
        self.json_paths = as_list(json_path)
        self.username = username or ""
        self.password = password or ""
        self.auth_method = method
        self.headers = headers or {}

    def _session_options(self) -> dict[str, Any]:
        options = {**super()._session_options(), "headers": self.headers}
        if not (self.username or self.password):
            options["auth"] = None
        elif self.auth_method == "digest":
            # Answers the server's challenge, which Shelly Gen2+ RPC requires.
            options["middlewares"] = [
                DigestAuthMiddleware(self.username, self.password)
            ]
        else:
            options["auth"] = BasicAuth(self.username, self.password)
        return options

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
