"""Serializing the Shelly HTTP surface, with every float at two decimals.

Real Shelly firmware reports measurements with two decimal places, and at least
one battery firmware mis-parses a shorter literal — a reading of ``180`` is read
as something other than 180 W. ``json.dumps`` cannot be made to do this: both
its C and pure-Python encoders call ``float.__repr__``, which subclassing a
``float`` does not change, so the rendering is done here instead.

Integers stay integers: ``id``, ``gen``, ``slot``, ``uptime``, ``ram_*``,
``cfg_rev``, ``rssi`` and ``unixtime`` are all counts, not measurements, and a
consumer that reads them as strings-of-digits would trip over ``3600.00``. That
makes this a statement about the *types* a builder hands in, which is why the
clock-derived values are wrapped in ``int()`` where they are computed rather
than relying on this module to notice.
"""

from __future__ import annotations

import math

#: What a non-finite measurement renders as. A reading can go non-finite
#: through a misconfigured multiplier; serving ``NaN`` would be invalid JSON
#: and serving nothing would drop a field a consumer requires.
_NON_FINITE = "0.00"


def _dump(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_dump_str(value))
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_NON_FINITE if not math.isfinite(value) else f"{value:.2f}")
    elif isinstance(value, dict):
        out.append("{")
        for index, (key, item) in enumerate(value.items()):
            if index:
                out.append(",")
            out.append(_dump_str(str(key)))
            out.append(":")
            _dump(item, out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _dump(item, out)
        out.append("]")
    else:
        raise TypeError(f"cannot serialize {type(value).__name__} onto the wire")


_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _dump_str(value: str) -> str:
    out = ['"']
    for char in value:
        escape = _ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
        elif char < " ":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def dumps(value: object) -> str:
    """*value* as compact JSON, every float at exactly two decimal places.

    Key order is preserved, separators carry no whitespace, and the output is
    byte-stable for a given input — which is what lets the golden tests pin
    whole response bodies character for character.
    """
    out: list[str] = []
    _dump(value, out)
    return "".join(out)
