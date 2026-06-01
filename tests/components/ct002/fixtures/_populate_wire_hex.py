"""One-shot helper to populate the canonical `wire_hex` for each vector in
protocol_golden_vectors.json using the Python implementation as the source of
truth. Run from the repo root after editing the vectors:

    uv run python tests/components/ct002/fixtures/_populate_wire_hex.py

The C++ port's host_protocol_test verifies its build_payload output matches
these bytes; the Python parity test verifies its own build_payload output
matches too, guarding against silent regressions in either implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

from astrameter.ct002.protocol import build_payload


def main() -> None:
    fixture_path = Path(__file__).parent / "protocol_golden_vectors.json"
    data = json.loads(fixture_path.read_text())
    for vec in data["vectors"]:
        wire = build_payload(vec["fields"])
        vec["wire_hex"] = bytes(wire).hex()
    fixture_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Updated {len(data['vectors'])} vectors in {fixture_path}")


if __name__ == "__main__":
    main()
