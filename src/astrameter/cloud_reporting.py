"""Opt-in Marstek HTTP cloud reporting (``hamedata.com``).

A real CT002 (``HME-4``) / CT003 (``HME-3``) reports to the Marstek cloud over
**plain HTTP GET** (no TLS, no token/signature — the device is identified only
by the cleartext ``id``/``uid`` query params). Only the newest firmware does it
(``HME-4`` v124, ``HME-3`` v122; the older images have no such code). Read from
those images, every cycle of its reporting state machine:

1. runs the **handshake** ``getDateInfoeu.php`` (``uid``/``fcv``/``aid``/``sv``
   = device id, Wi-Fi module version, model, firmware version), whose
   response body is the server's wall-clock time
   (``_YYYY_MM_DD_HH_MM_SS_``). On success it rolls its day/hour energy
   registers forward against that server time, then
2. sends ``setCtReporting``, retrying until the cloud answers, with ``timeNo``
   = the 10-minute slot of the server's day and ``date`` = the server's date,
3. waits 300 s and starts over at the handshake.

The two URL templates here match what each model sends on the wire (see
``docs/marstek-mqtt-http.md``). The field **set is model-dependent**: ``HME-4``
(a clamp) also sends instantaneous voltage / current (``va/vb/vc``, ``ia/ib/ic``)
and 32-bit energy; ``HME-3`` (a smart-meter reader) sends 64-bit energy and
**no** V/I, and has an on-wire quirk — a missing ``&`` between ``slv`` and
``udp`` (``…&slv=<n>udp=<n>…``) — which we reproduce so the request matches a
real device.

Two deliberate departures, because the Marstek app's device list shows each
report as grid = ``ap + bp + cp + dp``: ``dp`` (the total a real CT sends) goes
out as ``0`` so the list is not doubled, and ``HME-4`` powers keep their sign
(a real one sends magnitudes) so an export does not show as an import.

This is **opt-in** and best-effort: reporting failures are logged and never
disturb the UDP control loop. AstraMeter has no energy register, so it
integrates the grid power it reads into one; V/I on ``HME-4`` default to ``0``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

logger = logging.getLogger(__name__)

DEFAULT_HOST = "eu.hamedata.com"
# Default build stamp sent as ``fcv`` in the handshake (matches DEFAULT_FC4_V):
# a real CT sends the same Wi-Fi module version it reports as ``fc4_v``.
DEFAULT_FCV = "202409090159"
# A real CT sends its own firmware version as ``sv``. Only these versions report
# to the cloud at all, so they are the ones a reporting CT claims.
REPORTING_FIRMWARE_VERSIONS = {"HME-4": 124, "HME-3": 122}
# Both images wait 300 s (their seconds tick) after a report the cloud accepted
# before starting the next handshake.
DEFAULT_INTERVAL_SECONDS = 300.0
# A real CT retries a failed handshake or report on its next main-loop pass,
# bounded only by the Wi-Fi module's 60 s HTTP timeout. Wait that long instead
# of hammering a cloud that keeps rejecting us.
RETRY_SECONDS = 60.0
# How often the grid power is sampled into the energy register between reports.
DEFAULT_SAMPLE_SECONDS = 10.0
# ``eled``/``elet`` are in 0.1 Wh: HME-3 scales its meter's Wh reading by 10,
# HME-4 counts kWh in units of 10 000. One watt for one second is 1/360 of that.
_WATT_SECONDS_PER_UNIT = 360.0

# The handshake response, as both images scan it: ``_%hu_%hhu_%hhu_%hhu_%hhu_%hhu_``.
_SERVER_TIME_RE = re.compile(r"_(\d+)_(\d+)_(\d+)_(\d+)_(\d+)_(\d+)_")


def reporting_firmware_version(ct_type: str) -> int:
    """The firmware version a cloud-reporting CT of *ct_type* sends as ``sv``."""
    return REPORTING_FIRMWARE_VERSIONS.get(
        ct_type, REPORTING_FIRMWARE_VERSIONS["HME-4"]
    )


@dataclass(frozen=True)
class ServerTime:
    """Wall-clock time as the handshake response gives it (the server's zone)."""

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int = 0

    @property
    def time_no(self) -> int:
        """The ``timeNo`` a CT reports: the 10-minute slot of the day (0-143)."""
        return (self.hour * 60 + self.minute) // 10

    @property
    def date(self) -> datetime.date:
        return datetime.date(self.year, self.month, self.day)

    @classmethod
    def from_datetime(cls, dt: datetime.datetime) -> ServerTime:
        return cls(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def parse_server_time(body: str) -> ServerTime | None:
    """Parse the handshake response body, or ``None`` if it carries no time."""
    match = _SERVER_TIME_RE.search(body)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    try:
        datetime.datetime(year, month, day, hour, minute, second)
    except (ValueError, OverflowError):
        return None
    return ServerTime(year, month, day, hour, minute, second)


class EnergyIntegrator:
    """Integrates sampled grid power (W) into an energy register (0.1 Wh).

    Each sample's power is held until the next one (a left Riemann sum), the
    way a meter's pulse counter would have seen it.
    """

    def __init__(self) -> None:
        self._total = 0.0
        self._last: tuple[float, float] | None = None

    def sample(self, now_s: float, power_w: float) -> None:
        if self._last is not None:
            last_s, last_w = self._last
            if now_s > last_s:
                self._total += last_w * (now_s - last_s) / _WATT_SECONDS_PER_UNIT
        self._last = (now_s, power_w)

    @property
    def total(self) -> int:
        return int(self._total)


class EnergyCounters:
    """The ``eled``/``elet`` registers: energy since the start of the day/hour.

    Mirrors the handshake step of both images: add what the energy register
    gained since the last handshake to both, then clear ``elet`` when the
    server's hour changed and ``eled`` when its day did.
    """

    def __init__(self) -> None:
        self.eled = 0
        self.elet = 0
        self._last_total = 0
        self._hour: int | None = None
        self._day: int | None = None

    def update(self, total: int, now: ServerTime) -> None:
        delta = total - self._last_total
        self.eled += delta
        self.elet += delta
        self._last_total = total
        if now.hour != self._hour:
            self.elet = 0
        if now.day != self._day:
            self.eled = 0
        self._hour = now.hour
        self._day = now.day


@dataclass(frozen=True)
class CtMeasurement:
    """The live values a CT puts in a ``setCtReporting`` GET.

    Powers are watts, signed (import positive); ``dp`` is the total, which only
    the energy register uses (the URL builder sends ``0``). ``c*`` and ``d*`` are the bucket fields in the CT's
    ``x``/``A``/``B``/``C``/``ABC`` order (``z``↔``x`` unassigned, ``d``↔``ABC``
    combined): ``c*`` carries the UDP response's ``*_dchrg_power`` and ``d*`` its
    ``*_chrg_power``, which is where both images read them from. ``va/vb/vc``
    (volts) and ``ia/ib/ic`` (amps) are ``HME-4`` only; ``eled``/``elet`` are
    the day/hour energy registers in 0.1 Wh (``HME-3`` carries them as 64-bit).
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

    Both images fill it from the same values as their MQTT runtime frame:
    ``uid`` the device id, ``fcv`` the Wi-Fi module version (``fc4_v``), ``aid``
    the model and ``sv`` the firmware version (``ver_v``). The device record's
    ``version`` does not follow ``sv``; see ``docs/marstek-mqtt-http.md`` §6.3.
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

    ``dp`` always goes out as ``0`` and powers keep their sign: the device list
    shows grid = ``ap + bp + cp + dp``, so the total a real CT puts in ``dp``
    would double it, and a real ``HME-4``'s magnitudes would show an export as
    an import.
    """
    base = f"http://{host}/prod/api/v1/setCtReporting"
    did = quote(device_id, safe="")
    hme3 = ct_type == "HME-3"
    head = (
        f"?id={did}&eled={int(m.eled)}&elet={int(m.elet)}"
        f"&ap={int(m.ap)}&bp={int(m.bp)}&cp={int(m.cp)}&dp=0"
        f"&rssi={int(m.rssi)}"
    )
    # Quirk: HME-3 omits the '&' between slv and udp; HME-4 includes it.
    if hme3:
        link = f"&slv={int(m.slv)}udp={int(m.udp)}&mqtt={int(m.mqtt)}"
    else:
        link = f"&slv={int(m.slv)}&udp={int(m.udp)}&mqtt={int(m.mqtt)}"
    when = f"&timeNo={int(time_no)}&date={date.year}-{date.month:02d}-{date.day:02d}"
    # HME-4 (clamp) reports instantaneous voltage/current; HME-3 does not.
    vi = (
        f"&va={int(m.va)}&vb={int(m.vb)}&vc={int(m.vc)}"
        f"&ia={m.ia:.2f}&ib={m.ib:.2f}&ic={m.ic:.2f}"
        if not hme3
        else ""
    )
    buckets = (
        f"&cz={int(m.cz)}&ca={int(m.ca)}&cb={int(m.cb)}&cc={int(m.cc)}&cd={int(m.cd)}"
        f"&dz={int(m.dz)}&da={int(m.da)}&db={int(m.db)}&dc={int(m.dc)}&dd={int(m.dd)}"
    )
    return base + head + link + when + vi + buckets


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str


# An injectable async GET: returns the response (or ``None`` on error). Kept as a
# seam so the reporter is testable without a real network / aiohttp.
HttpGet = Callable[[str], Awaitable[HttpResponse | None]]

# Both images read the response into a 512-byte buffer.
_MAX_BODY_BYTES = 512


async def aiohttp_get(url: str, *, timeout: float = 10.0) -> HttpResponse | None:
    """Default :data:`HttpGet`: a plain HTTP GET via aiohttp."""
    import aiohttp

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp,
        ):
            body = await resp.read()
            return HttpResponse(
                resp.status, body[:_MAX_BODY_BYTES].decode("utf-8", errors="replace")
            )
    except Exception as exc:
        logger.debug("cloud reporting GET failed: %s", exc)
        return None


def _ok(resp: HttpResponse | None) -> bool:
    # Both images wait for the module's ``+QHTTPGET: 0,200``: only 200 counts.
    return resp is not None and resp.status == 200


@dataclass
class CloudReporterConfig:
    """Operator-supplied settings for opt-in cloud reporting."""

    ct_type: str
    device_id: str
    host: str = DEFAULT_HOST
    fcv: str = DEFAULT_FCV
    # ``None`` sends the firmware version a reporting CT of ``ct_type`` runs.
    sv: int | None = None
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    retry_seconds: float = RETRY_SECONDS
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS


@dataclass(frozen=True, slots=True)
class CloudReporterSnapshot:
    """Immutable view of one cloud reporter for the status API.

    Carries the ``host`` only, never a built URL: both templates embed the
    device id in the query string (``uid``/``id``), so a URL here would leak
    identity into every status response.  ``report_count`` counts *attempts*
    that reached the HTTP seam and ``fail_count`` the subset of those that did
    not come back ``200``; ``last_report_at`` and ``last_http_status`` are the
    wall-clock time and outcome of the same, most recent attempt.
    """

    device_id: str
    ct_type: str
    host: str
    interval: float
    last_report_at: datetime.datetime | None
    last_http_status: int | None
    report_count: int
    fail_count: int


class CloudReporter:
    """Runs the handshake → report → wait cycle a real CT does."""

    def __init__(
        self,
        config: CloudReporterConfig,
        gather: Callable[[], Awaitable[CtMeasurement]],
        *,
        http_get: HttpGet | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._cfg = config
        self._gather = gather
        self._http_get = http_get or aiohttp_get
        self._clock = clock or (lambda: datetime.datetime.now())
        self._monotonic = monotonic or time.monotonic
        self._energy = EnergyIntegrator()
        self._counters = EnergyCounters()
        # The server time from the last handshake; every report until the next
        # handshake (retries included) carries it, as on a real CT.
        self._server_time: ServerTime | None = None
        # Read-only status surface (see status_snapshot).  Reporting is
        # best-effort and failures are only logged at debug, so these counters
        # are the operator's only evidence that the cloud is rejecting us.
        self._last_status: int | None = None
        self._last_report_at: datetime.datetime | None = None
        self._report_count = 0
        self._fail_count = 0

    @property
    def _sv(self) -> int:
        if self._cfg.sv is not None:
            return self._cfg.sv
        return reporting_firmware_version(self._cfg.ct_type)

    def status_snapshot(self) -> CloudReporterSnapshot:
        """Immutable view of this reporter for the status API.

        MUST stay a plain ``def``: the reporter task and the HTTP handlers
        share one asyncio loop, so an await-free builder can never observe a
        half-updated report.
        """
        return CloudReporterSnapshot(
            device_id=self._cfg.device_id,
            ct_type=self._cfg.ct_type,
            host=self._cfg.host,
            interval=self._cfg.interval_seconds,
            last_report_at=self._last_report_at,
            last_http_status=self._last_status,
            report_count=self._report_count,
            fail_count=self._fail_count,
        )

    def _integrate(self, m: CtMeasurement) -> None:
        # HME-4 nets its per-phase import and export registers; HME-3 reads the
        # meter's import register (OBIS 1.8.0), which only counts import.
        power = m.dp if self._cfg.ct_type != "HME-3" else max(m.dp, 0)
        self._energy.sample(self._monotonic(), power)

    async def _sample_energy(self) -> None:
        try:
            self._integrate(await self._gather())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("cloud energy sample failed", exc_info=True)

    async def _handshake(self) -> bool:
        """Run ``getDateInfo``; on success adopt its server time and roll the
        energy registers forward. Returns whether the cloud answered."""
        await self._sample_energy()
        url = build_get_date_info_url(
            self._cfg.host,
            uid=self._cfg.device_id,
            fcv=self._cfg.fcv,
            aid=self._cfg.ct_type,
            sv=self._sv,
        )
        resp = await self._http_get(url)
        logger.debug("cloud handshake getDateInfo -> %s", resp.status if resp else None)
        if resp is None or not _ok(resp):
            return False
        server_time = parse_server_time(resp.body)
        if server_time is None:
            logger.debug("cloud handshake carried no server time; using local time")
            server_time = ServerTime.from_datetime(self._clock())
        self._server_time = server_time
        self._counters.update(self._energy.total, server_time)
        return True

    async def _report_once(self) -> bool:
        """Send one ``setCtReporting``. Returns whether the cloud accepted it."""
        m = await self._gather()
        self._integrate(m)
        server_time = self._server_time or ServerTime.from_datetime(self._clock())
        url = build_set_ct_reporting_url(
            self._cfg.host,
            self._cfg.ct_type,
            device_id=self._cfg.device_id,
            time_no=server_time.time_no,
            date=server_time.date,
            m=dataclasses.replace(
                m, eled=self._counters.eled, elet=self._counters.elet
            ),
        )
        resp = await self._http_get(url)
        self._last_status = resp.status if resp is not None else None
        self._last_report_at = self._clock()
        self._report_count += 1
        # `None` is the seam's "the request never completed"; anything but
        # 200 is the cloud rejecting the report. Both count as a failure.
        ok = _ok(resp)
        if not ok:
            self._fail_count += 1
        logger.debug("cloud setCtReporting -> %s", self._last_status)
        return ok

    async def _idle(self, seconds: float) -> None:
        """Wait *seconds*, sampling the grid into the energy register meanwhile."""
        deadline = self._monotonic() + seconds
        while (remaining := deadline - self._monotonic()) > 0:
            await asyncio.sleep(min(self._cfg.sample_seconds, remaining))
            await self._sample_energy()

    async def _attempt(self, step: Callable[[], Awaitable[bool]], name: str) -> bool:
        try:
            return await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("cloud %s failed", name, exc_info=True)
            return False

    async def run(self) -> None:
        """Handshake, report, wait ``interval_seconds``; repeat until cancelled.

        A failed handshake or report is retried after ``retry_seconds``, the
        report without a new handshake, as a real CT does. Failures are only
        logged (at debug) so a flaky cloud never stalls the emulator.
        """
        logger.info(
            "Cloud reporting enabled for %s as id=%s -> %s (every %.0fs)",
            self._cfg.ct_type,
            self._cfg.device_id,
            self._cfg.host,
            self._cfg.interval_seconds,
        )
        while True:
            if not await self._attempt(self._handshake, "handshake"):
                await self._idle(self._cfg.retry_seconds)
                continue
            while not await self._attempt(self._report_once, "report"):
                await self._idle(self._cfg.retry_seconds)
            await self._idle(self._cfg.interval_seconds)
