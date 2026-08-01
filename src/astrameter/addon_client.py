"""Minimal async client for the Home Assistant Supervisor add-on API.

Only the ``/addons/self/*`` endpoints the dashboard needs, so an add-on install
can read and write its own options without the browser ever seeing
``SUPERVISOR_TOKEN``: the token stays here, is sent as a bearer header, and is
never logged nor put into an exception message.

``hassio_role: homeassistant`` is enough for all of these — Supervisor's
security middleware bypasses role checks for single-segment ``/addons/self/<x>``
paths.  Two-segment paths (``/addons/self/options/validate``) are not bypassed
and are deliberately not used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

SUPERVISOR_BASE_URL = "http://supervisor"

_TIMEOUT = aiohttp.ClientTimeout(total=10)
# A restart tears down the container serving us, so waiting out the full
# timeout only delays the "restarting…" UI — see :meth:`SupervisorClient.restart`.
_RESTART_TIMEOUT = aiohttp.ClientTimeout(total=5)


class SupervisorError(Exception):
    """A Supervisor call failed.

    ``message`` is Supervisor's own ``message`` field where it sent one — for a
    rejected options write that is its ``humanize_error`` text, which is
    rendered to the user verbatim, so it must not be reworded or wrapped.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class SupervisorClient:
    """Talks to the Supervisor on behalf of the add-on it runs inside."""

    def __init__(
        self,
        *,
        base_url: str = SUPERVISOR_BASE_URL,
        token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")

    def available(self) -> bool:
        """Whether we run as a Supervisor add-on and may call the API."""
        return bool(self._token)

    async def get_info(self) -> dict[str, Any]:
        """``GET /addons/self/info`` → the ``data`` object.

        ``data.options`` is the merged view (``config.yaml`` defaults overlaid
        with the persisted user overrides) and returns ``!secret foo`` values
        raw, which is what editing needs.  Never read options from
        ``/addons/self/options/config`` instead: that one resolves secrets and
        a round-trip would write the plaintext back.
        """
        data = await self._request("GET", "/addons/self/info", timeout=_TIMEOUT)
        if not data:
            # Callers validate writes against ``data.schema``; an empty info
            # would make every key look unknown and silently drop the write.
            raise SupervisorError("Supervisor returned no add-on info")
        return data

    async def set_options(self, options: Mapping[str, Any]) -> None:
        """``POST /addons/self/options`` — write the persisted option overlay.

        This is a **full replace**, not a patch: the protocol is get → merge →
        post the complete desired dict.  Supervisor silently **drops keys it
        does not know**, so a typo loses the value with no error — callers must
        reject any key absent from the ``data.schema`` of a freshly fetched
        :meth:`get_info` before calling this.  ``null`` is a hard error and
        never means "clear": to clear an optional value omit the key (allowed
        for ``type?`` entries only) or send ``""`` for ``str?``/``password?``.
        """
        await self._request(
            "POST",
            "/addons/self/options",
            payload={"options": dict(options)},
            timeout=_TIMEOUT,
        )

    async def set_ingress_panel(self, enabled: bool) -> None:
        """Show or hide the add-on's sidebar panel, effective immediately.

        Same endpoint as :meth:`set_options` but a sibling key, not part of the
        options overlay, so this leaves the persisted options untouched.
        """
        await self._request(
            "POST",
            "/addons/self/options",
            payload={"ingress_panel": enabled},
            timeout=_TIMEOUT,
        )

    async def restart(self) -> None:
        """``POST /addons/self/restart`` — request our own restart.

        The container serving this request is torn down by the restart, so the
        response often never arrives: a 200, a connection reset and a timeout
        all mean "restart initiated" and none of them is an error.  Only a
        refusal Supervisor managed to answer with raises.  Callers confirm the
        restart by polling ``health`` until it responds again.
        """
        try:
            await self._request(
                "POST", "/addons/self/restart", timeout=_RESTART_TIMEOUT
            )
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            logger.debug("restart response never arrived (%s); assuming success", exc)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout,
    ) -> dict[str, Any]:
        if not self._token:
            raise SupervisorError("Not running as a Home Assistant add-on")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        async with (
            aiohttp.ClientSession() as session,
            session.request(
                method,
                f"{self._base_url}{path}",
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp,
        ):
            body = await resp.text()
            status = resp.status
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {}
        message = parsed.get("message") if isinstance(parsed, dict) else None
        if status >= 400:
            # Supervisor's own wording is what the user is shown; only fall
            # back to our own when it did not send any.  Never include the
            # request headers here — that is where the token lives.
            raise SupervisorError(
                str(message) if message else f"Supervisor returned HTTP {status}",
                status=status,
            )
        data = parsed.get("data") if isinstance(parsed, dict) else None
        return data if isinstance(data, dict) else {}
