"""Home Assistant add-on support.

Everything the add-on needs before the service starts — reading the user's
options, turning them into a ``config.ini``, waiting for Home Assistant —
lives here rather than in ``ha_addon/run.sh``, so it can be unit-tested.

Nothing in this package is imported unless ``astrameter --addon`` is used, so
a standalone Docker or pip install never touches it.
"""

from .generate import generate_config
from .options import HANDLED_SEPARATELY, OPTION_MAP
from .startup import AddonConfig, bootstrap, read_options

__all__ = [
    "HANDLED_SEPARATELY",
    "OPTION_MAP",
    "AddonConfig",
    "bootstrap",
    "generate_config",
    "read_options",
]
