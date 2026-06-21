"""Marstek MQTT responder — answer CT002/CT003 poll requests on the local broker.

Pure helpers (topic formatting, payload building, poll detection) plus a
binding dataclass that the :class:`MqttInsightsService` stores per device.

Wire format (UTF-8): the Marstek app typically parses payloads with
``replaceAll(' ', '')``, then ``split(',')``, then each token ``split('=')``
expecting **exactly one** ``=`` per token. Replies to ``cd=1`` / ``cd=4`` polls
omit a ``cd=`` echo; ``cd=4`` slave lists use flat ``slv_t/…/slv_p`` tokens only.
Aggregate replies include power, ``slv_n``, optional extras, and kWh placeholders.

This emulates — but does not byte-for-byte reproduce — what a real CT sends; a
real CT's ``cd=1``/``cd=4`` layout differs by model and key order. The app's
tolerant, order-independent parser accepts our superset. See
``docs/ct002-ct003-protocol.md`` ("MQTT runtime-info frame") for the reference
layouts.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from astrameter.ct002 import ReportingConsumerRow

APP_TOPIC_TEMPLATES = (
    "hame_energy/{ct_type}/App/{mac}/ctrl",
    "marstek_energy/{ct_type}/App/{mac}/ctrl",
)
DEVICE_TOPIC_TEMPLATES = (
    "hame_energy/{ct_type}/device/{mac}/ctrl",
    "marstek_energy/{ct_type}/device/{mac}/ctrl",
)

# Matches observed real-device values; included so hm2mqtt-style parsers
# recognise the message as a well-formed runtime-info frame.
DEFAULT_VER_V = 148
# Observed-style firmware/build stamp for ``fc4_v`` (string-ish numeric).
DEFAULT_FC4_V = "202409090159"
# Placeholder kWh fields (two decimal places; app may parse as float or centi-units).
DEFAULT_CD1_KWH = (0.0, 0.0, 0.0, 0.0)

_APP_TOPIC_RE = re.compile(
    r"^(?:hame|marstek)_energy/(?P<ct_type>[^/]+)/App/(?P<mac>[^/]+)/ctrl$"
)
_MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")


def ver_v_from_marstek_api_version(value: Any) -> int:
    """Turn EMS/device-list ``version`` into MQTT ``ver_v`` (integer firmware-style field).

    The cloud list sometimes omits or changes shape; keep the wire value aligned
    when present so app-side parsers see a consistent ``ver_v`` with the API record.
    """
    if isinstance(value, bool):
        return DEFAULT_VER_V
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return DEFAULT_VER_V
        try:
            return int(s)
        except ValueError:
            return DEFAULT_VER_V
    return DEFAULT_VER_V


@dataclass(frozen=True)
class MarstekMqttBinding:
    """Per-device registration used by the MQTT Insights service."""

    device_id: str
    ct_type: str
    mac: str
    get_values: Callable[[], Awaitable[list[float]]]
    wifi_rssi: int
    ver_v: int = DEFAULT_VER_V
    get_connected_slave_count: Callable[[], int] | None = None
    get_cd4_slave_csv: Callable[[], str] | None = None
    ble_s: int = 0
    fc4_v: str = DEFAULT_FC4_V


def normalize_mac(raw: str) -> str:
    """Lowercase, strip ``:``/``-``; return ``""`` if not 12 hex chars."""
    if not raw:
        return ""
    cleaned = raw.replace(":", "").replace("-", "").strip().lower()
    return cleaned if _MAC_HEX_RE.fullmatch(cleaned) else ""


def _parse_ctrl_kv(body: bytes) -> dict[str, str] | None:
    """Decode *body* as UTF-8 CSV ``k=v`` pairs; keys lowercased. ``None`` if invalid.

    Matches a naive split like the app's outer pass (after stripping spaces). Any
    value that must contain ``,`` or ``=`` cannot round-trip here; ``cd=4`` slave
    lists are emitted as **flat** repeated ``slv_*`` tokens instead.
    """
    if not body:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    out: dict[str, str] = {}
    for chunk in text.split(","):
        key, sep, value = chunk.partition("=")
        if not sep:
            continue
        k = key.strip().lower()
        if k:
            out[k] = value.strip()
    return out


@dataclass(frozen=True)
class MarstekPollContext:
    """How to answer an App/ctrl runtime-info request (aggregate vs slave list)."""

    echo_cd: int
    slave_id: int | None = None


def parse_marstek_poll_payload(body: bytes) -> MarstekPollContext | None:
    """Recognise Marstek poll requests: ``cd=1`` (aggregate) or ``cd=4`` + ``p1`` (slave list).

    ``cd=4`` without ``p1`` is ignored so we do not invent a selector the app
    did not send.
    """
    kv = _parse_ctrl_kv(body)
    if kv is None or "cd" not in kv:
        return None
    try:
        cd = int(kv["cd"], 10)
    except ValueError:
        return None
    if cd == 1:
        return MarstekPollContext(echo_cd=1, slave_id=None)
    if cd == 4:
        if "p1" not in kv:
            return None
        try:
            slave_id = int(kv["p1"], 10)
        except ValueError:
            return None
        return MarstekPollContext(echo_cd=4, slave_id=slave_id)
    return None


def is_poll_payload(body: bytes) -> bool:
    """Return True iff *body* requests runtime info (``cd=1`` or ``cd=4`` with ``p1``)."""
    return parse_marstek_poll_payload(body) is not None


def parse_app_topic(topic: str) -> tuple[str, str] | None:
    """Return ``(ct_type, mac)`` for a Marstek App topic, else ``None``."""
    match = _APP_TOPIC_RE.match(topic)
    if not match:
        return None
    return match.group("ct_type"), match.group("mac").lower()


def app_topics_for(binding: MarstekMqttBinding) -> tuple[str, str]:
    old, new = APP_TOPIC_TEMPLATES
    return (
        old.format(ct_type=binding.ct_type, mac=binding.mac),
        new.format(ct_type=binding.ct_type, mac=binding.mac),
    )


def device_topics_for(binding: MarstekMqttBinding) -> tuple[str, str]:
    old, new = DEVICE_TOPIC_TEMPLATES
    return (
        old.format(ct_type=binding.ct_type, mac=binding.mac),
        new.format(ct_type=binding.ct_type, mac=binding.mac),
    )


def _fmt_kwh(x: float) -> str:
    return f"{x:.2f}"


def _cd4_escape_field(value: str) -> str:
    # Marstek-style outer parse splits on commas; each token must have a single "=".
    return value.replace(",", "_").replace(";", "_").replace("=", "_")


def format_cd4_slave_csv(rows: Sequence[ReportingConsumerRow]) -> str:
    """CSV body for a ``cd=4`` reply: repeated ``slv_t/slv_id/slv_ip/slv_p`` tokens.

    *rows* come from :meth:`astrameter.ct002.ct002.CT002.reporting_consumer_rows`.
    """
    if not rows:
        return ""
    parts: list[str] = []
    for row in rows:
        host = row.last_ip.strip() or "0.0.0.0"
        parts.append(
            f"slv_t={_cd4_escape_field(row.device_type)},slv_id={_cd4_escape_field(row.consumer_id)},"
            f"slv_ip={_cd4_escape_field(host)},slv_p={row.phase}"
        )
    return ",".join(parts)


def build_cd4_response(slave_kv_tail: str) -> bytes:
    """Slave list reply (no ``cd=`` echo): flat ``slv_t/…/slv_p`` tokens only."""
    return slave_kv_tail.encode()


def build_response(
    binding: MarstekMqttBinding,
    watts: list[float],
    *,
    poll: MarstekPollContext | None = None,
    connected_slave_count: int = 0,
    kwh_fields: tuple[float, float, float, float] | None = None,
) -> bytes:
    """Build the CSV ``k=v`` body for aggregate power/status (or legacy core only).

    When *poll* has ``echo_cd == 1``, emit the extended runtime frame **without** a
    ``cd=`` key (the app already knows the poll kind). Order: phase powers,
    ``wif_s`` before RSSI/version, ``slv_n``, ``cur_d=0``, ``ble_s``, ``fc4_v``, kWh placeholders.
    """
    vs = list(watts) + [0.0] * (3 - len(watts))
    a, b, c = (round(v) for v in vs[:3])
    total = a + b + c
    core = (
        f"pwr_a={a},pwr_b={b},pwr_c={c},pwr_t={total},"
        f"wif_r={binding.wifi_rssi},ver_v={binding.ver_v},wif_s=2"
    )
    k0, k1, k2, k3 = kwh_fields if kwh_fields is not None else DEFAULT_CD1_KWH
    cd1_tail = (
        f"ble_s={binding.ble_s},fc4_v={binding.fc4_v},"
        f"kwh={_fmt_kwh(k0)},n_kwh={_fmt_kwh(k1)},used_kwh={_fmt_kwh(k2)},fed_kwh={_fmt_kwh(k3)}"
    )
    if poll is not None and poll.echo_cd == 1:
        payload = (
            f"pwr_a={a},pwr_b={b},pwr_c={c},pwr_t={total},wif_s=2,"
            f"wif_r={binding.wifi_rssi},ver_v={binding.ver_v},slv_n={connected_slave_count},cur_d=0,"
            f"{cd1_tail}"
        )
    else:
        payload = core
    return payload.encode()
