"""Guard: the modules the native HA integration imports must stay light.

The HACS integration ships a copy of this package and lists only light
third-party deps in its ``manifest.json`` ``requirements``. It must therefore
import its entrypoints without dragging in the heavy/optional backends
(``pymodbus``, ``smllib``, ``pyserial-asyncio-fast``, ``aiomqtt``). Run in a
fresh interpreter so the check is unaffected by modules other tests imported.
"""

from __future__ import annotations

import subprocess
import sys

# Entrypoints the native integration imports (the "clean" subset). The package
# __init__s are included too: their lazy __getattr__ must keep a bare
# ``import astrameter.config`` / ``import astrameter.powermeter`` light.
_INTEGRATION_IMPORTS = [
    "astrameter.config",
    "astrameter.powermeter",
    "astrameter.powermeter.base",
    "astrameter.powermeter.wrappers.apply",
    "astrameter.config.client_filter",
    "astrameter.config.logger",
    "astrameter.ct002.ct002",
    "astrameter.shelly.shelly",
]

# Heavy/optional backend deps that must NOT be pulled in.
_HEAVY_MODULES = ["pymodbus", "smllib", "serial_asyncio_fast", "aiomqtt"]


def test_integration_entrypoints_do_not_pull_heavy_deps() -> None:
    code = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in _INTEGRATION_IMPORTS)
        + f"heavy = [m for m in {_HEAVY_MODULES!r} if m in sys.modules]\n"
        "assert not heavy, 'unexpected heavy imports: ' + repr(heavy)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_lazy_powermeter_access_still_works() -> None:
    code = (
        "from astrameter.powermeter import Powermeter, JsonHttpPowermeter, "
        "ModbusPowermeter\n"
        "assert Powermeter.__name__ == 'Powermeter'\n"
        "assert JsonHttpPowermeter.__name__ == 'JsonHttpPowermeter'\n"
        "assert ModbusPowermeter.__name__ == 'ModbusPowermeter'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
