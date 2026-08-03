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

import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import aiohttp

from astrameter.power_units import POWER_UNIT_SCALE, is_power_unit

logger = logging.getLogger(__name__)

# Overridable so the add-on flows can be exercised against a stand-in
# Supervisor; inside a real add-on the default is the only correct value.
SUPERVISOR_BASE_URL = os.environ.get(
    "ASTRAMETER_SUPERVISOR_URL", "http://supervisor"
).rstrip("/")

_TIMEOUT = aiohttp.ClientTimeout(total=10)
# A restart tears down the container serving us, so waiting out the full
# timeout only delays the "restarting…" UI — see :meth:`SupervisorClient.restart`.
_RESTART_TIMEOUT = aiohttp.ClientTimeout(total=5)


class SupervisorError(Exception):
    """A Supervisor call failed.

    ``message`` is Supervisor's own ``message`` field where it sent one — for a
    rejected options write that is its ``humanize_error`` text, which is
    rendered to the user verbatim, so it must not be reworded or wrapped.

    ``unreachable`` marks the case where the call never got an answer at all,
    which :meth:`SupervisorClient.restart` treats as success — it is exactly
    what tearing down our own container looks like.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.unreachable = unreachable


class SupervisorClient:
    """Talks to the Supervisor on behalf of the add-on it runs inside."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("ASTRAMETER_SUPERVISOR_URL")
            or SUPERVISOR_BASE_URL
        ).rstrip("/")
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
        except SupervisorError as exc:
            if not exc.unreachable:
                raise  # a refusal Supervisor did manage to answer with
            logger.debug("restart response never arrived (%s); assuming success", exc)

    async def list_power_entities(self) -> list[dict[str, Any]]:
        """Home Assistant entities that could plausibly be a grid-power sensor.

        Reached through the Supervisor's Core proxy (``/core/api/states``),
        which is what ``homeassistant_api: true`` grants — the same route the
        HOMEASSISTANT powermeter already uses.

        "Applicable" is deliberately generous: a `device_class: power` sensor
        is the obvious case, but plenty of real installs expose grid power as
        a plain sensor in a power unit with no device class, and excluding
        those would make the picker useless exactly for the people who need
        it. Unavailable entities are kept — a sensor can be briefly
        unavailable at add-on start and still be the right choice.

        The unit test is :func:`~astrameter.power_units.is_power_unit`, shared
        with the powermeter rather than a list kept here: when MW and mW were
        added there this one still said "W or kW", so the picker hid sensors
        that read perfectly and flagged a configured one as "not found in Home
        Assistant".

        A `device_class: power` entity whose unit is *not* readable is still
        offered — a template sensor mislabelled `power` is a common mistake,
        and hiding it only makes the entity the user is looking for vanish —
        but it is marked ``readable: False`` so the picker can say why rather
        than let it fail on every read once chosen.

        The domain is not part of the test.  Readings are fetched per entity
        from ``/api/states/<entity_id>``, which does not care what domain the
        id is in, so a `number.` or `input_number.` entity carrying watts is a
        working configuration — and filtering to `sensor.` told those users
        their entity was "not found in Home Assistant" while it was feeding
        the controller perfectly well.
        """
        states = await self._request_raw("GET", "/core/api/states")
        if not isinstance(states, list):
            return []
        out: list[dict[str, Any]] = []
        for state in states:
            if not isinstance(state, dict):
                continue
            entity_id = str(state.get("entity_id", ""))
            if "." not in entity_id:
                continue
            attrs = state.get("attributes") or {}
            device_class = attrs.get("device_class")
            unit = str(attrs.get("unit_of_measurement") or "")
            # Two different questions. Offering one needs a *declared* power
            # unit or a device class saying so — "no unit" is readable (it is
            # assumed to be watts) but it describes every numeric entity in
            # the house, so it is not something to suggest. Readability is
            # only about whether the powermeter would accept it once chosen.
            readable = is_power_unit(unit or None)
            if device_class != "power" and unit not in POWER_UNIT_SCALE:
                continue
            out.append(
                {
                    "entity_id": entity_id,
                    "name": attrs.get("friendly_name") or entity_id,
                    "unit": unit,
                    "device_class": device_class,
                    "state": state.get("state"),
                    "readable": readable,
                }
            )
        out.sort(key=lambda e: e["entity_id"])
        return out

    async def _request_raw(self, method: str, path: str) -> Any:
        """A Core-proxy call whose body is not the Supervisor result envelope."""
        if not self._token:
            raise SupervisorError("Not running as a Home Assistant add-on")
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.request(
                    method,
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=_TIMEOUT,
                ) as resp,
            ):
                body = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SupervisorError(
                f"Could not reach Home Assistant: {exc}", unreachable=True
            ) from exc
        if status >= 400:
            raise SupervisorError(
                f"Home Assistant returned HTTP {status}", status=status
            )
        try:
            return json.loads(body) if body else None
        except ValueError:
            return None

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
        try:
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
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Callers render SupervisorError verbatim; a raw aiohttp error
            # escaping here would surface as an unhandled 500 instead.
            raise SupervisorError(
                f"Could not reach Supervisor: {exc}", unreachable=True
            ) from exc
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
