"""Moving between add-on options and a hand-written ``config.ini``.

In add-on options mode there is no configuration file at all — the settings
come straight from the Supervisor. Switching to file mode therefore has to
*write* one that reproduces what is running, so the user starts from their
own configuration rather than a blank page.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from astrameter.config.addon import ADDON_CONFIG_DIR
from astrameter.config.ini_config import render_ini
from astrameter.config.logger import logger

if TYPE_CHECKING:
    from astrameter.config.settings import AppConfig

__all__ = ["ADDON_CONFIG_DIR", "materialize_config", "target_path"]

_BANNER = """\
# Written by AstraMeter from the configuration that was running.
#
# The add-on's `custom_config` option now points at this file, so the options
# in the add-on UI are no longer used — edit this file instead.
# Delete the `custom_config` option to go back to the guided setup.
"""


def target_path(filename: str, config_dir: str = ADDON_CONFIG_DIR) -> str:
    """Absolute path for a ``custom_config`` filename, rejecting traversal."""
    safe = os.path.basename(filename.strip())
    if not safe or safe.startswith("."):
        raise ValueError(f"Invalid config filename: {filename!r}")
    return os.path.join(config_dir, safe)


def materialize_config(
    config: AppConfig,
    filename: str,
    config_dir: str = ADDON_CONFIG_DIR,
) -> str:
    """Write the running configuration to *filename* in the add-on config dir.

    Returns the written path. An existing file is never overwritten — the user
    may already have a config there worth keeping, and silently clobbering it
    would be unrecoverable.

    A configuration that already has a file behind it is copied verbatim,
    comments and all; there is nothing to gain from re-rendering it and a
    formatting round trip to lose. Everything else is rendered from its
    settings, which ``ini_config_test.py`` pins as a round trip.
    """
    destination = target_path(filename, config_dir)
    if os.path.exists(destination):
        logger.info("Config file %s already exists; leaving it as-is", destination)
        return destination

    if config.path and os.path.exists(config.path):
        with open(config.path, encoding="utf-8") as handle:
            body = handle.read()
    else:
        body = render_ini(config) + config.render_powermeters_ini()

    os.makedirs(config_dir, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(_BANNER + "\n" + body)
    logger.info("Wrote the running configuration to %s", destination)
    return destination
