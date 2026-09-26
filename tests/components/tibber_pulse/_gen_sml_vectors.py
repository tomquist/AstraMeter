"""Codegen: SML telegrams and what the Python decoder makes of them, as a C++
header for the host gtest of the firmware's decoder (host_sml_power_test.cpp).

Every expected value here comes from running
`astrameter.powermeter.sml.parse_sml_powers` on the same bytes, so the gtest
holds the C++ decoder to the Python one rather than to a second hand-written
expectation. Run via test_tibber_pulse_gtests.py, which invokes this before cmake; or
manually:

    uv run python tests/components/tibber_pulse/_gen_sml_vectors.py
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from smllib.crc.x25 import get_crc

from astrameter.powermeter.sml import (
    _OBIS_POWER_CURRENT,
    _OBIS_POWER_L1,
    _OBIS_POWER_L2,
    _OBIS_POWER_L3,
    parse_sml_powers,
)

HERE = Path(__file__).parent
DST = HERE / "host_sml_vectors.h"

UNIT_W = 0x1B
UNIT_WH = 0x1E
# 1-0:1.8.0*255, the import energy counter: present in every real telegram and
# never a power register, so it must be skipped.
OBIS_ENERGY = "0100010800ff"
# 1-0:96.1.0*255, the meter's serial number: an octet string.
OBIS_SERIAL = "0100600100ff"

# The one place the firmware departs from Python on purpose: a well-formed
# telegram with none of the power registers is a single 0 W phase in Python and
# "no reading" on the ESP (see decode_sml_power() in sml_power.h).
DIVERGENT_NO_POWER = {"no_power_registers"}


@dataclass
class Entry:
    obis: str
    value: int
    unit: int = UNIT_W
    scaler: int | None = 0
    # "int" (TL 0x55, signed) or "uint" (TL 0x65).
    kind: str = "int"
    # A 20-byte octet-string value instead of an integer (a serial number).
    octet: bytes | None = None
    # val_time as an SML_Time choice (a nested list) instead of "not set".
    with_time: bool = False


@dataclass
class Vector:
    name: str
    telegram: bytes
    obis: tuple[str, str, str, str] = (
        _OBIS_POWER_CURRENT,
        _OBIS_POWER_L1,
        _OBIS_POWER_L2,
        _OBIS_POWER_L3,
    )
    # The C++ status for a telegram Python rejects (None or an exception).
    reject: str = ""
    entries: list[Entry] = field(default_factory=list)


def _entry(e: Entry) -> bytes:
    out = b"\x77\x07" + bytes.fromhex(e.obis)
    out += b"\x01"  # status
    if e.with_time:
        # SML_Time: choice list [secIndex (1), uint32]
        out += b"\x72\x62\x01\x65" + struct.pack(">I", 12345678)
    else:
        out += b"\x01"
    out += b"\x62" + bytes([e.unit])
    out += b"\x01" if e.scaler is None else b"\x52" + struct.pack(">b", e.scaler)
    if e.octet is not None:
        size = len(e.octet) + 2  # two TL bytes
        out += bytes([0x80 | (size >> 4), size & 0x0F]) + e.octet
    elif e.kind == "uint":
        out += b"\x65" + struct.pack(">I", e.value)
    else:
        out += b"\x55" + struct.pack(">i", e.value)
    out += b"\x01"  # value signature
    return out


def _message(tag: int, body: bytes) -> bytes:
    return (
        b"\x76\x05\x00\x00\x00\x01\x62\x00\x62\x00\x72"
        + b"\x63"
        + struct.pack(">H", tag)
        + body
        + b"\x63\x00\x00\x00"
    )


def telegram(entries: list[Entry]) -> bytes:
    """A complete SML 1.04 transport frame, escaped and CRC'd."""
    open_resp = (
        b"\x76\x01\x01\x05\x00\x00\x00\x01"
        b"\x0b\x0a\x01ISK\x00\x05\x00\x9f\x5c\xe5\x01\x01"
    )
    body = b"\x77\x01\x01\x01" + bytes([0x70 | len(entries)])
    for e in entries:
        body += _entry(e)
    body += b"\x01\x01"
    payload = (
        _message(0x0101, open_resp)
        + _message(0x0701, body)
        + _message(0x0201, b"\x71\x01")
    )
    # Byte stuffing: an escape sequence in the payload is sent twice.
    payload = payload.replace(b"\x1b" * 4, b"\x1b" * 8)
    start = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"
    end = b"\x1b\x1b\x1b\x1b\x1a"
    padding = (4 - ((len(start) + len(payload) + len(end) + 3) % 4)) % 4
    frame = start + payload + b"\x00" * padding + end + bytes([padding])
    return frame + struct.pack(">H", get_crc(frame))


def _all(agg: int, l1: int, l2: int, l3: int, **kw) -> list[Entry]:
    return [
        Entry(_OBIS_POWER_CURRENT, agg, **kw),
        Entry(_OBIS_POWER_L1, l1, **kw),
        Entry(_OBIS_POWER_L2, l2, **kw),
        Entry(_OBIS_POWER_L3, l3, **kw),
    ]


def vectors() -> list[Vector]:
    energy = Entry(OBIS_ENERGY, 123456789, unit=UNIT_WH, scaler=-1)
    good = telegram([energy, *_all(1234, 400, 500, 334)])
    out = [
        Vector("per_phase_and_total", good),
        Vector("total_only", telegram([energy, Entry(_OBIS_POWER_CURRENT, 1500)])),
        Vector("phases_only", telegram(_all(0, 500, 600, 400)[1:])),
        Vector("feed_in_is_negative", telegram(_all(-2300, -800, -700, -800))),
        # #519: a meter reporting mW with scaler -3.
        Vector("scaler_minus_3", telegram(_all(170000, 170000, 0, 0, scaler=-3))),
        Vector(
            "scaler_plus_1",
            telegram([Entry(_OBIS_POWER_CURRENT, 123, scaler=1)]),
        ),
        Vector("scaler_absent", telegram(_all(250, 100, 50, 100, scaler=None))),
        Vector(
            "incomplete_phases_fall_back_to_total",
            telegram(_all(900, 300, 300, 0)[:3]),
        ),
        Vector(
            "unsigned_values",
            telegram(_all(3000, 1000, 1000, 1000, kind="uint")),
        ),
        # An energy counter whose bytes are an escape sequence: stuffed on the
        # wire, and must be unstuffed before the entries are read.
        Vector(
            "byte_stuffed_payload",
            telegram(
                [
                    Entry(OBIS_ENERGY, 0x1B1B1B1B, unit=UNIT_WH, kind="uint"),
                    Entry(_OBIS_POWER_CURRENT, 42),
                ]
            ),
        ),
        # A 20-byte serial number needs a two-byte type-length field.
        Vector(
            "long_octet_string",
            telegram(
                [
                    Entry(OBIS_SERIAL, 0, unit=0, octet=b"1EMH0012345678901234"),
                    Entry(_OBIS_POWER_CURRENT, 777),
                ]
            ),
        ),
        Vector(
            "nested_time_list",
            telegram(_all(1100, 400, 300, 400, with_time=True)),
        ),
        Vector("leading_bytes", b"\x00\x42junk" + good),
        Vector("trailing_bytes", good + b"\x1b\x1b\x1b\x1b\x01\x01"),
        Vector(
            "custom_obis",
            telegram(
                [
                    Entry("0100100701ff", 640),
                    Entry(_OBIS_POWER_CURRENT, 1),
                ]
            ),
            obis=("0100100701ff", _OBIS_POWER_L1, _OBIS_POWER_L2, _OBIS_POWER_L3),
        ),
        Vector("no_power_registers", telegram([energy]), reject="NO_POWER"),
        Vector(
            "crc_mismatch",
            good[:-1] + bytes([good[-1] ^ 0xFF]),
            reject="CRC_MISMATCH",
        ),
        Vector("truncated", good[:-5], reject="NO_FRAME"),
        Vector("not_sml", b"not an sml telegram", reject="NO_FRAME"),
        Vector("empty", b"", reject="NO_FRAME"),
        Vector(
            "wrong_unit",
            telegram([Entry(_OBIS_POWER_CURRENT, 1500, unit=UNIT_WH)]),
            reject="WRONG_UNIT",
        ),
        Vector(
            "wrong_unit_phase",
            telegram(
                [Entry(_OBIS_POWER_L1, 5, unit=UNIT_WH), Entry(_OBIS_POWER_CURRENT, 5)]
            ),
            reject="WRONG_UNIT",
        ),
    ]
    return out


def python_result(v: Vector) -> list[float] | None:
    """What the Python source makes of the telegram: watts, or None if it fails."""
    try:
        powers = parse_sml_powers(v.telegram, *v.obis)
    except ValueError:
        return None
    return None if powers is None else [float(p) for p in powers]


def _total(v: Vector, powers: list[float]) -> float:
    """The firmware's `power`: the aggregate register, else the phase sum."""
    if len(powers) == 1:
        return powers[0]
    # Ask Python for the aggregate alone by naming it as all three phases: a
    # usable one comes back three times; a missing one falls through to the
    # single-0 quirk, and one not in watts raises.
    agg = v.obis[0]
    try:
        alone = parse_sml_powers(v.telegram, agg, agg, agg, agg)
    except ValueError:
        alone = None
    return float(alone[0]) if alone and len(alone) == 3 else float(sum(powers))


def _obis_cpp(hex_code: str) -> str:
    return "{" + ", ".join(f"0x{b:02x}" for b in bytes.fromhex(hex_code)) + "}"


def _bytes_cpp(data: bytes) -> str:
    if not data:
        return "{}"
    rows = [
        "      " + ", ".join(f"0x{b:02x}" for b in data[i : i + 12]) + ","
        for i in range(0, len(data), 12)
    ]
    return "{\n" + "\n".join(rows) + "\n    }"


def render() -> str:
    lines = [
        "// Generated by _gen_sml_vectors.py from the Python decoder — do not edit by hand.",
        "// Regenerate via: uv run python tests/components/tibber_pulse/_gen_sml_vectors.py",
        "#pragma once",
        "",
        "#include <cstdint>",
        "#include <vector>",
        "",
        '#include "esphome/components/tibber_pulse/sml_power.h"',
        "",
        "namespace tibber_pulse_test {",
        "",
        "using esphome::tibber_pulse::DecodeStatus;",
        "using esphome::tibber_pulse::ObisSelection;",
        "",
        "struct SmlVector {",
        "  const char *name;",
        "  std::vector<uint8_t> telegram;",
        "  ObisSelection obis;",
        "  DecodeStatus status;",
        "  // Python's reading: three phases or one aggregate (status OK only).",
        "  std::vector<double> python_powers;",
        "  // The firmware's `power` sensor value (status OK only).",
        "  double total;",
        "};",
        "",
        "inline const std::vector<SmlVector> &sml_vectors() {",
        "  static const std::vector<SmlVector> vectors = {",
    ]
    for v in vectors():
        powers = python_result(v)
        if v.name in DIVERGENT_NO_POWER:
            assert powers == [0.0], f"{v.name}: Python quirk changed: {powers}"
            powers = None
        if v.reject:
            assert powers is None, f"{v.name}: Python accepted it: {powers}"
            status, total, py = v.reject, 0.0, "{}"
        else:
            assert powers is not None, f"{v.name}: Python rejected it"
            status = "OK"
            total = _total(v, powers)
            py = "{" + ", ".join(repr(p) for p in powers) + "}"
        obis = ", ".join(_obis_cpp(o) for o in v.obis)
        lines += [
            "    {",
            f'    "{v.name}",',
            f"    {_bytes_cpp(v.telegram)},",
            f"    ObisSelection{{{obis}}},",
            f"    DecodeStatus::{status},",
            f"    {py},",
            f"    {total!r},",
            "    },",
        ]
    lines += [
        "  };",
        "  return vectors;",
        "}",
        "",
        "}  // namespace tibber_pulse_test",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    DST.write_text(render())


if __name__ == "__main__":
    main()
