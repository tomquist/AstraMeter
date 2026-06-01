"""Python parity test against the shared golden-vector fixture.

The C++ port in `esphome/components/ct002/protocol.{h,cpp}` runs the same
vectors through its own build_payload/parse_request implementation (see
`tests/components/ct002/host_protocol_test.cpp`). Both implementations are
canonical against the same JSON: any drift in either side is caught here
in Python or in the host-gcc gtest in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrameter.ct002.protocol import build_payload, parse_request

FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "tests"
    / "components"
    / "ct002"
    / "fixtures"
    / "protocol_golden_vectors.json"
)


def _load_vectors():
    data = json.loads(FIXTURE_PATH.read_text())
    return data["vectors"]


@pytest.mark.parametrize(
    "vec",
    _load_vectors(),
    ids=[v["description"][:60] for v in _load_vectors()],
)
def test_build_payload_matches_canonical_wire(vec):
    expected = bytes.fromhex(vec["wire_hex"])
    actual = bytes(build_payload(vec["fields"]))
    assert actual == expected, (
        f"build_payload diverged from canonical: "
        f"expected {expected.hex()}, got {actual.hex()}"
    )


@pytest.mark.parametrize(
    "vec",
    _load_vectors(),
    ids=[v["description"][:60] for v in _load_vectors()],
)
def test_parse_request_round_trips(vec):
    wire = bytes.fromhex(vec["wire_hex"])
    fields, error = parse_request(wire)
    assert error is None, f"parse_request rejected canonical bytes: {error}"
    assert fields == vec["fields"]


def test_parse_request_tolerates_checksum_space_high_nibble():
    # Find the vector that exercises this decode-only mutation.
    vectors = [v for v in _load_vectors() if "decode_only_mutations" in v]
    assert vectors, "Expected at least one vector flagged for space-tolerance"
    for vec in vectors:
        wire = bytearray.fromhex(vec["wire_hex"])
        wire[-2] = ord(" ")
        fields, error = parse_request(bytes(wire))
        assert error is None
        assert fields == vec["fields"]
