"""Codegen: turn protocol_golden_vectors.json into a C++ header for the
host-gcc protocol parity gtest. Keeps the gtest binary free of any JSON
dependency while still sharing the canonical fixture with the Python side.

Run via the test_host_protocol.py pytest wrapper, which invokes this before
cmake; or manually:

    uv run python tests/components/ct002/_gen_protocol_test_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path


def _cpp_string_literal(s: str) -> str:
    # Emit as a sequence of escaped chars; safe for ASCII content (the fixture
    # has only printable ASCII or known multi-byte description text we handle).
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ord(ch) < 0x20 or ord(ch) >= 0x7F:
            # Escape non-ASCII so the generated header stays portable.
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def main() -> None:
    here = Path(__file__).parent
    src = here / "fixtures" / "protocol_golden_vectors.json"
    dst = here / "host_protocol_test_vectors.h"
    data = json.loads(src.read_text())

    lines = [
        "// Generated from fixtures/protocol_golden_vectors.json — do not edit by hand.",
        "// Regenerate via: uv run python tests/components/ct002/_gen_protocol_test_vectors.py",
        "#pragma once",
        "",
        "#include <cstdint>",
        "#include <string>",
        "#include <vector>",
        "",
        "namespace ct002_test {",
        "",
        "struct GoldenVector {",
        "  const char *description;",
        "  std::vector<std::string> fields;",
        "  std::vector<uint8_t> wire;",
        "  bool exercise_space_tolerance;",
        "};",
        "",
        "inline std::vector<GoldenVector> load_golden_vectors() {",
        "  std::vector<GoldenVector> out;",
    ]

    for vec in data["vectors"]:
        lines.append("  {")
        lines.append("    GoldenVector v;")
        lines.append(f"    v.description = {_cpp_string_literal(vec['description'])};")
        lines.append("    v.fields = {")
        for f in vec["fields"]:
            lines.append(f"      {_cpp_string_literal(f)},")
        lines.append("    };")
        wire_bytes = bytes.fromhex(vec["wire_hex"])
        wire_inits = ", ".join(f"0x{b:02x}" for b in wire_bytes)
        lines.append(f"    v.wire = {{ {wire_inits} }};")
        space_tol = "decode_only_mutations" in vec
        lines.append(
            f"    v.exercise_space_tolerance = {'true' if space_tol else 'false'};"
        )
        lines.append("    out.push_back(std::move(v));")
        lines.append("  }")

    lines.append("  return out;")
    lines.append("}")
    lines.append("")
    lines.append("}  // namespace ct002_test")
    lines.append("")

    dst.write_text("\n".join(lines))
    print(f"Generated {dst} with {len(data['vectors'])} vectors")


if __name__ == "__main__":
    main()
