from __future__ import annotations

import math

from astrameter.shelly.wire import dumps


def test_floats_always_carry_two_decimals() -> None:
    """The one property a battery's parser depends on.

    At least one battery firmware mis-reads a shorter literal, taking ``180``
    for something other than 180 W, so this is not cosmetic.
    """
    assert dumps(180.0) == "180.00"
    assert dumps(0.0) == "0.00"
    assert dumps(-120.5) == "-120.50"
    assert dumps(1.3043478260869565) == "1.30"


def test_integers_stay_integers() -> None:
    """Counts must not acquire decimals: they are not measurements."""
    assert dumps(3600) == "3600"
    assert dumps({"uptime": 3600, "gen": 2, "rssi": -50}) == (
        '{"uptime":3600,"gen":2,"rssi":-50}'
    )


def test_booleans_are_not_rendered_as_integers() -> None:
    """``True`` is an ``int`` subclass, so the order of checks matters."""
    assert dumps(True) == "true"
    assert dumps(False) == "false"
    assert dumps({"is_valid": True}) == '{"is_valid":true}'


def test_non_finite_measurements_render_as_zero() -> None:
    """A misconfigured multiplier must not produce invalid JSON.

    ``NaN`` is not JSON, and dropping the field instead would remove a key a
    consumer requires.
    """
    assert dumps(math.nan) == "0.00"
    assert dumps(math.inf) == "0.00"
    assert dumps(-math.inf) == "0.00"


def test_key_order_is_preserved() -> None:
    """Consumers match on field order, so it is part of the contract."""
    assert dumps({"b": 1, "a": 2}) == '{"b":1,"a":2}'


def test_nesting_and_empty_containers() -> None:
    assert dumps({"reverse": {}, "user_calibrated_phase": []}) == (
        '{"reverse":{},"user_calibrated_phase":[]}'
    )
    assert dumps({"a": {"b": [1, 2.5]}}) == '{"a":{"b":[1,2.50]}}'


def test_none_is_null() -> None:
    assert dumps({"n_current": None}) == '{"n_current":null}'


def test_strings_are_escaped() -> None:
    assert dumps('a"b\\c') == '"a\\"b\\\\c"'
    assert dumps("line\nbreak\ttab") == '"line\\nbreak\\ttab"'
    assert dumps("\x01") == '"\\u0001"'


def test_separators_carry_no_whitespace() -> None:
    """A byte-stable body is what lets the golden tests pin it exactly."""
    assert dumps({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'
