"""AstraMeter config package.

Kept import-light: importing this package (for ``logger`` / ``setLogLevel`` /
``ClientFilter``) must NOT pull in ``config_loader`` and the full powermeter
backend chain (pymodbus, smllib, aiomqtt, …). ``read_all_powermeter_configs``
is therefore exposed lazily via ``__getattr__``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .client_filter import ClientFilter
from .logger import logger, setLogLevel

__all__ = [
    "ClientFilter",
    "logger",
    "read_all_powermeter_configs",
    "setLogLevel",
]

if TYPE_CHECKING:
    from .config_loader import read_all_powermeter_configs


def __getattr__(name: str) -> Any:
    if name == "read_all_powermeter_configs":
        from .config_loader import read_all_powermeter_configs

        return read_all_powermeter_configs
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
