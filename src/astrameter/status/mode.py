"""Which configuration surface the running service is driven by."""

from __future__ import annotations

STANDALONE = "standalone"
HA_SIMPLE = "ha_simple"
HA_ADVANCED = "ha_advanced"


def detect_config_mode(*, addon: bool, config_path: str | None) -> str:
    """Classify the configuration backend the app actually loaded.

    Taken from the loaded backend, never probed from the filesystem: the
    add-on falls back to its own options when ``custom_config`` names a file
    that is missing or resolves outside the config mount, so probing would
    report the mode the user *intended* rather than the one running.

    A backend with no file behind it can only be the add-on options, which is
    what the guided form edits.
    """
    if not addon:
        return STANDALONE
    return HA_SIMPLE if config_path is None else HA_ADVANCED
