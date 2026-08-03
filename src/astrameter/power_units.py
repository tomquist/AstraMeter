"""What counts as a power reading, and what it takes to get watts from it.

A leaf module on purpose. The powermeter that converts readings and the
dashboard that offers sensors to configure have to agree on this exactly —
when ``MW`` and ``mW`` were added to the converter, the dashboard's own copy
of the list still said "W or kW", so it hid sensors that read perfectly and
flagged a configured one as missing. Neither side keeps its own list now.

It lives outside ``astrameter.powermeter`` because importing anything from
that package runs its ``__init__``, which pulls in every meter and cycles back
through the config loader.
"""

from __future__ import annotations

#: Multiplier from a declared unit to watts. Case matters: "mW" is
#: milliwatts, "MW" megawatts. Any *other* declared unit (°C, %, kWh, ...) is
#: not a power reading — see issues #39 / #572, where kW values were silently
#: read as watts and left batteries idle.
POWER_UNIT_SCALE = {
    "W": 1.0,
    "kW": 1000.0,
    "MW": 1_000_000.0,
    "mW": 0.001,
}

#: What error messages name, in the order a reader expects.
POWER_UNITS = tuple(POWER_UNIT_SCALE)


def is_power_unit(unit: str | None) -> bool:
    """Whether a ``unit_of_measurement`` is one AstraMeter reads as power.

    ``None`` (no unit attribute at all) counts: those are assumed to be watts,
    which is what installs relied on before units were read, and staying
    compatible with them is why the assumption exists.
    """
    return unit is None or unit in POWER_UNIT_SCALE
