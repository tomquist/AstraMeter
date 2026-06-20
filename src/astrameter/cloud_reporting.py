"""Opt-in Marstek HTTP cloud reporting (``hamedata.com``).

A real CT002 (``HME-4``) / CT003 (``HME-3``) periodically reports to the Marstek
cloud over **plain HTTP GET** (no TLS, no token/signature — the device is
identified only by the cleartext ``id``/``aid`` query params). It:

1. runs a one-shot **handshake** ``getDateInfoeu.php`` (``uid``/``fcv``/``aid``/
   ``sv``) — really a device upsert that writes ``aid``→the record's ``type``
   and ``sv``→its ``version``, so we send the CT model and firmware version,
   then
2. sends a **timer-driven** ``setCtReporting`` GET carrying the live grid power,
   the per-bucket charge/discharge split, link state and an incrementing
   ``timeNo``.

The two URL templates here match what each model sends on the wire (see
``docs/marstek-mqtt-http.md``). The field **set is model-dependent**: ``HME-4``
(a clamp) also sends instantaneous voltage / current (``va/vb/vc``, ``ia/ib/ic``)
and 32-bit energy; ``HME-3`` (a smart-meter reader) sends 64-bit energy and
**no** V/I, and has an on-wire quirk — a missing ``&`` between ``slv`` and
``udp`` (``…&slv=<n>udp=<n>…``) — which we reproduce so the request matches a
real device.

This is **opt-in** and best-effort: reporting failures are logged and never
disturb the UDP control loop. Several values a real device measures are not
available to AstraMeter (cumulative energy, and V/I on ``HME-4``); those default
to ``0`` unless supplied. The exact real-world report **cadence** and the server
**response contract** are not known — see the docs.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

logger = logging.getLogger(__name__)

DEFAULT_HOST = "eu.hamedata.com"
# Default build stamp sent as ``fcv`` in the handshake (matches DEFAULT_FC4_V).
DEFAULT_FCV = "202409090159"
# Firmware version sent as ``sv`` in the handshake. ``getDateInfoeu.php`` is a
# device check-in that **writes** its ``sv`` into the cloud record's ``version``
# field (and its ``aid`` into ``type``), so we send the same version AstraMeter
# registers managed devices with (``marstek_api._add_device``'s ``version=121``)
# to *re-assert* the record rather than clobber it with a bogus value.
DEFAULT_REPORTING_VERSION = 121
# No real-world cadence is documented; default to a conservative 60 s and let the
# operator tune it (a capture of a real device is the ground truth — see docs).
DEFAULT_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class CtMeasurement:
    """The live values a CT puts in a ``setCtReporting`` GET.

    Powers are watts. ``c*`` are the **charge** buckets and ``d*`` the
    **discharge** buckets, in the CT's ``x``/``A``/``B``/``C``/``ABC`` order
    (``z``↔``x`` unassigned, ``d``↔``ABC`` combined) — identical to the UDP
    response's ``*_chrg_power`` / ``*_dchrg_power`` split. ``va/vb/vc`` (volts)
    and ``ia/ib/ic`` (amps) are ``HME-4`` only; ``eled``/``elet`` are cumulative
    energy registers (``HME-3`` carries them as 64-bit).
    """

    ap: int = 0
    bp: int = 0
    cp: int = 0
    dp: int = 0
    rssi: int = 0
    slv: int = 0
    udp: int = 0
    mqtt: int = 0
    eled: int = 0
    elet: int = 0
    cz: int = 0
    ca: int = 0
    cb: int = 0
    cc: int = 0
    cd: int = 0
    dz: int = 0
    da: int = 0
    db: int = 0
    dc: int = 0
    dd: int = 0
    va: int = 0
    vb: int = 0
    vc: int = 0
    ia: float = 0.0
    ib: float = 0.0
    ic: float = 0.0


def build_get_date_info_url(host: str, *, uid: str, fcv: str, aid: str, sv: int) -> str:
    """Build the handshake/check-in GET (``getDateInfoeu.php``).

    Matches ``…/app/neng/getDateInfoeu.php?uid=%s&fcv=%s&aid=%s&sv=%d`` on both
    models. Identity params are percent-encoded for safety (a real device sends
    plain hex MACs, which are unaffected).

    Despite the param names, this endpoint is a **device upsert**: empirically
    the server stores ``aid``→the device record's ``type`` and ``sv``→its
    ``version`` (the response body is just server time). Callers therefore pass
    the CT model as ``aid`` and the firmware version as ``sv`` so the record is
    re-asserted, not corrupted — see ``CloudReporter._handshake``.
    """
    return (
        f"http://{host}/app/neng/getDateInfoeu.php"
        f"?uid={quote(uid, safe='')}&fcv={quote(fcv, safe='')}"
        f"&aid={quote(aid, safe='')}&sv={int(sv)}"
    )


def build_set_ct_reporting_url(
    host: str,
    ct_type: str,
    *,
    device_id: str,
    time_no: int,
    date: datetime.date,
    m: CtMeasurement,
) -> str:
    """Build the ``setCtReporting`` GET for *ct_type* (``HME-4`` or ``HME-3``).

    Builds the model-specific template field-for-field, including the model
    differences (``HME-4`` adds ``va/vb/vc``/``ia/ib/ic``; ``HME-3`` uses 64-bit
    energy, omits V/I, and keeps the missing-``&`` ``slv``/``udp`` quirk).
    """
    base = f"http://{host}/prod/api/v1/setCtReporting"
    did = quote(device_id, safe="")
    head = (
        f"?id={did}&eled={int(m.eled)}&elet={int(m.elet)}"
        f"&ap={int(m.ap)}&bp={int(m.bp)}&cp={int(m.cp)}&dp={int(m.dp)}&rssi={int(m.rssi)}"
    )
    # Quirk: HME-3 omits the '&' between slv and udp; HME-4 includes it.
    if ct_type == "HME-3":
        link = f"&slv={int(m.slv)}udp={int(m.udp)}&mqtt={int(m.mqtt)}"
    else:
        link = f"&slv={int(m.slv)}&udp={int(m.udp)}&mqtt={int(m.mqtt)}"
    when = f"&timeNo={int(time_no)}&date={date.year}-{date.month:02d}-{date.day:02d}"
    # HME-4 (clamp) reports instantaneous voltage/current; HME-3 does not.
    vi = (
        f"&va={int(m.va)}&vb={int(m.vb)}&vc={int(m.vc)}"
        f"&ia={m.ia:.2f}&ib={m.ib:.2f}&ic={m.ic:.2f}"
        if ct_type != "HME-3"
        else ""
    )
    buckets = (
        f"&cz={int(m.cz)}&ca={int(m.ca)}&cb={int(m.cb)}&cc={int(m.cc)}&cd={int(m.cd)}"
        f"&dz={int(m.dz)}&da={int(m.da)}&db={int(m.db)}&dc={int(m.dc)}&dd={int(m.dd)}"
    )
    return base + head + link + when + vi + buckets


# An injectable async GET: returns the HTTP status (or ``None`` on error). Kept as
# a seam so the reporter is testable without a real network / aiohttp.
HttpGet = Callable[[str], Awaitable[int | None]]


async def aiohttp_get(url: str, *, timeout: float = 10.0) -> int | None:
    """Default :data:`HttpGet`: a plain HTTP GET via aiohttp, returning the status."""
    import aiohttp

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp,
        ):
            await resp.read()
            return resp.status
    except Exception as exc:
        logger.debug("cloud reporting GET failed: %s", exc)
        return None


@dataclass
class CloudReporterConfig:
    """Operator-supplied settings for opt-in cloud reporting."""

    ct_type: str
    device_id: str
    host: str = DEFAULT_HOST
    fcv: str = DEFAULT_FCV
    sv: int = DEFAULT_REPORTING_VERSION
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS


class CloudReporter:
    """Runs the handshake + periodic ``setCtReporting`` push a real CT does."""

    def __init__(
        self,
        config: CloudReporterConfig,
        gather: Callable[[], Awaitable[CtMeasurement]],
        *,
        http_get: HttpGet | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._cfg = config
        self._gather = gather
        self._http_get = http_get or aiohttp_get
        self._clock = clock or (lambda: datetime.datetime.now())

    async def _handshake(self) -> None:
        # The handshake's `aid`/`sv` params are not an account id / settings
        # version (the names mislead): the server writes `aid`→the device's
        # `type` and `sv`→its `version`. So we send the CT model and the managed
        # firmware version to keep the cloud record correct.
        url = build_get_date_info_url(
            self._cfg.host,
            uid=self._cfg.device_id,
            fcv=self._cfg.fcv,
            aid=self._cfg.ct_type,
            sv=self._cfg.sv,
        )
        status = await self._http_get(url)
        logger.debug("cloud handshake getDateInfo -> %s", status)

    async def _report_once(self) -> None:
        m = await self._gather()
        # `timeNo` is the device's "time number"; epoch seconds is a monotonic
        # stand-in (its exact meaning isn't known).
        now = self._clock()
        url = build_set_ct_reporting_url(
            self._cfg.host,
            self._cfg.ct_type,
            device_id=self._cfg.device_id,
            time_no=int(now.timestamp()),
            date=now.date(),
            m=m,
        )
        status = await self._http_get(url)
        logger.debug("cloud setCtReporting -> %s", status)

    async def run(self) -> None:
        """Handshake once, then push ``setCtReporting`` every ``interval_seconds``.

        Runs until cancelled. Individual report failures are swallowed (logged at
        debug) so a flaky cloud never stalls the emulator.
        """
        logger.info(
            "Cloud reporting enabled for %s as id=%s -> %s (every %.0fs)",
            self._cfg.ct_type,
            self._cfg.device_id,
            self._cfg.host,
            self._cfg.interval_seconds,
        )
        try:
            await self._handshake()
        except Exception:
            logger.debug("cloud handshake failed", exc_info=True)
        while True:
            try:
                await self._report_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("cloud report failed", exc_info=True)
            await asyncio.sleep(self._cfg.interval_seconds)
