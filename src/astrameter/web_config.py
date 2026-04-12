"""
Web-based configuration editor for AstraMeter.

Provides helpers and an HTML page for reading and editing config.ini via a browser.
"""

import configparser
import contextlib
import errno
import importlib.resources
import json
import os
import shutil
import tempfile
import threading
from collections import OrderedDict

from configupdater import ConfigUpdater


def _load_config_editor_html() -> str:
    """Load the config editor HTML from the bundled static file."""
    return (
        importlib.resources.files("astrameter")
        .joinpath("static/config_editor.html")
        .read_text("utf-8")
    )


CONFIG_EDITOR_HTML: str = _load_config_editor_html()


def read_config_as_dict(config_path: str) -> tuple[dict, list]:
    """
    Read config.ini and return (sections_dict, ordered_section_list).

    The sections_dict maps section names to dicts of key->value.
    Case of keys is preserved.
    """
    cfg = configparser.RawConfigParser(dict_type=OrderedDict)
    cfg.optionxform = str  # type: ignore[assignment]  # preserve key case
    if os.path.exists(config_path):
        cfg.read(config_path)
    sections: dict[str, dict[str, str]] = {}
    order = []
    for section in cfg.sections():
        sections[section] = dict(cfg.items(section))
        order.append(section)
    return sections, order


_CONFIG_WRITE_LOCK = threading.Lock()


def _atomic_write_lines(config_path: str, lines: list) -> None:
    """Write *lines* to *config_path* atomically via a temp-file + os.replace.

    Container environments (Docker bind-mounts, overlayfs) can block rename(2)
    with EBUSY/EACCES even when the file is otherwise writable.  Two fallbacks
    are tried in order:

    1. ``shutil.copyfile`` — overwrites the destination in-place.  Handles the
       common Docker bind-mount case where rename is blocked but the file is
       open-for-write accessible.
    2. ``os.unlink`` + ``os.replace`` — removes the destination first (creating
       an overlayfs whiteout), then renames the temp file into place.  Handles
       overlayfs setups where the file lives only in the read-only lower layer.

    If all strategies fail a :exc:`PermissionError` is raised with an
    actionable message.
    """
    dir_name = os.path.dirname(config_path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as tmp:
        tmp.writelines(lines)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    # errno values that indicate a filesystem/mount restriction rather than a
    # genuine logic error.  EBUSY = mount point, EPERM/EACCES = permission
    # denied (different kernels/filesystems use different codes for the same
    # bind-mount restriction).
    _RETRYABLE = (errno.EBUSY, errno.EPERM, errno.EACCES)

    try:
        os.replace(tmp_path, config_path)
        return
    except OSError as exc:
        if exc.errno not in _RETRYABLE:
            raise

    # rename(2) was blocked — common on Docker bind-mounts and overlayfs.
    # Try strategies in order, stopping as soon as one succeeds.
    transferred = False
    # temp_consumed tracks whether os.replace() has moved the temp file into
    # place (consuming it).  Strategy 1 (copyfile) leaves the temp file on disk
    # and lets the finally block remove it; Strategy 2 (unlink+replace) renames
    # the temp file, so the finally block must skip the unlink.
    temp_consumed = False
    try:
        # Strategy 1: overwrite in-place (open destination for writing).
        try:
            shutil.copyfile(tmp_path, config_path)
            transferred = True
        except OSError as exc2:
            if exc2.errno not in _RETRYABLE:
                raise
        # Strategy 2: unlink the bind-mounted file then rename the temp file.
        # Works on overlayfs where the destination cannot be opened for writing
        # but can be removed (a whiteout is created in the upper layer).
        if not transferred:
            try:
                os.unlink(config_path)
                os.replace(tmp_path, config_path)
                temp_consumed = True
                transferred = True
            except OSError as exc3:
                if exc3.errno not in _RETRYABLE:
                    raise
        if not transferred:
            raise PermissionError(
                f"Cannot write to {config_path!r}: the file is not writable. "
                "Check that the add-on has write access to the config file "
                "(e.g. the mapped volume is not read-only)."
            )
    finally:
        if not temp_consumed:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def _validate_config_payload(sections: dict, order: list) -> None:
    """Raise ValueError if any section name, key, or value would corrupt the INI."""
    if not isinstance(order, list) or any(not isinstance(s, str) for s in order):
        raise ValueError("'order' must be a list of section names")
    if len(order) != len(set(order)):
        raise ValueError("'order' contains duplicate section names")
    for section, pairs in sections.items():
        if (
            not isinstance(section, str)
            or not section
            or any(ch in section for ch in "\r\n]")
        ):
            raise ValueError(f"Invalid section name: {section!r}")
        if not isinstance(pairs, dict):
            raise ValueError(f"Section {section!r} must map to an object")
        for key, value in pairs.items():
            if not isinstance(key, str) or not key or any(ch in key for ch in "\r\n"):
                raise ValueError(f"Invalid key in section {section!r}: {key!r}")
            if not isinstance(value, str) or any(ch in value for ch in "\r\n"):
                raise ValueError(f"Invalid value for {section!r}.{key!r}")


def write_config_from_dict(config_path: str, sections: dict, order: list) -> None:
    """
    Write config.ini from the provided sections dict, preserving existing comments.

    If *config_path* already exists, comment lines (``#`` / ``;``) and blank
    lines are kept in their original positions while key values are updated
    in-place.  Keys absent from *sections* are removed; keys that are new are
    appended at the end of their section.  Sections absent from *sections* are
    dropped.  If the file does not yet exist it is written from scratch.

    ``sections`` maps section names to dicts of key->value.
    ``order`` controls the section order; sections not listed are appended last.
    """
    _validate_config_payload(sections, order)
    write_order = list(order) + [s for s in sections if s not in order]

    with _CONFIG_WRITE_LOCK:
        updater = ConfigUpdater()
        updater.optionxform = str  # type: ignore[assignment]  # preserve key case

        if os.path.exists(config_path):
            updater.read(config_path)

        # Update existing sections and add new keys / remove stale keys.
        for section_name, new_pairs in sections.items():
            if updater.has_section(section_name):
                for key in set(updater.options(section_name)) - new_pairs.keys():
                    updater.remove_option(section_name, key)
            else:
                updater.add_section(section_name)
            for key, value in new_pairs.items():
                updater.set(section_name, key, value)

        # Remove sections not present in the incoming payload.
        for section_name in list(updater.sections()):
            if section_name not in sections:
                updater.remove_section(section_name)

        # Re-order sections to match *write_order* by rebuilding from
        # detached copies.  Only needed when the order actually differs.
        current_order = updater.sections()
        desired = [s for s in write_order if s in sections]
        if current_order != desired:
            detached = {
                name: updater[name].detach() for name in list(updater.sections())
            }
            for name in desired:
                updater.add_section(detached[name])

        _atomic_write_lines(config_path, [str(updater)])


def validate_config(config_path: str) -> None:
    """Trial-load the config file the same way the main service does.

    Raises on any parse or semantic error (bad section, missing required
    key, invalid value, etc.) so the caller can roll back before the
    service tries to restart with a broken config.
    """
    import configparser as _cp
    from collections import OrderedDict

    from astrameter.config.config_loader import read_all_powermeter_configs

    cfg = _cp.ConfigParser(dict_type=OrderedDict, interpolation=None)
    if not cfg.read(config_path):
        raise ValueError(f"Cannot read config file: {config_path}")
    read_all_powermeter_configs(cfg)


def config_to_json(config_path: str) -> str:
    """Return the config as a JSON string suitable for the web UI."""
    sections, order = read_config_as_dict(config_path)
    return json.dumps({"sections": sections, "order": order})


# -- Key-type metadata served to the config editor --------------------------

_PM_COMMON: dict[str, dict[str, object]] = {
    "THROTTLE_INTERVAL": {"type": "float"},
    "POWER_OFFSET": {"type": "float"},
    "POWER_MULTIPLIER": {"type": "float"},
    "NETMASK": {},
    "PID_KP": {"type": "float"},
    "PID_KI": {"type": "float"},
    "PID_KD": {"type": "float"},
    "PID_OUTPUT_MAX": {"type": "float"},
    "PID_MODE": {"type": "select", "options": ["bias", "replace"]},
}


def _pm(**extras: dict[str, object]) -> dict[str, dict[str, object]]:
    """Merge powermeter-common keys with section-specific extras."""
    return {**_PM_COMMON, **extras}


SECTION_KEY_TYPES: dict[str, dict[str, dict[str, object]]] = {
    "GENERAL": {
        "DEVICE_TYPE": {
            "type": "select",
            "options": [
                "ct002",
                "ct003",
                "shellypro3em",
                "shellyemg3",
                "shellyproem50",
            ],
        },
        "SKIP_POWERMETER_TEST": {"type": "boolean"},
        "WEB_CONFIG_ENABLED": {"type": "boolean"},
        "ENABLE_WEB_SERVER": {"type": "boolean"},
        "WEB_SERVER_PORT": {"type": "integer"},
        "DISABLE_SUM_PHASES": {"type": "boolean"},
        "DISABLE_ABSOLUTE_VALUES": {"type": "boolean"},
        "THROTTLE_INTERVAL": {"type": "float"},
        "PID_KP": {"type": "float"},
        "PID_KI": {"type": "float"},
        "PID_KD": {"type": "float"},
        "PID_OUTPUT_MAX": {"type": "float"},
        "PID_MODE": {"type": "select", "options": ["bias", "replace"]},
    },
    "CT002": {
        "UDP_PORT": {"type": "integer"},
        "WIFI_RSSI": {"type": "integer"},
        "DEDUPE_TIME_WINDOW": {"type": "integer"},
        "CONSUMER_TTL": {"type": "integer"},
        "DEBUG_STATUS": {"type": "boolean"},
        "ACTIVE_CONTROL": {"type": "boolean"},
        "SMOOTH_TARGET_ALPHA": {"type": "float", "min": 0, "max": 1},
        "DEADBAND": {"type": "integer"},
        "MAX_SMOOTH_STEP": {"type": "integer"},
        "FAIR_DISTRIBUTION": {"type": "boolean"},
        "BALANCE_GAIN": {"type": "float"},
        "BALANCE_DEADBAND": {"type": "integer"},
        "MAX_CORRECTION_PER_STEP": {"type": "integer"},
        "ERROR_BOOST_THRESHOLD": {"type": "integer"},
        "ERROR_BOOST_MAX": {"type": "float"},
        "ERROR_REDUCE_THRESHOLD": {"type": "integer"},
        "MAX_TARGET_STEP": {"type": "integer"},
        "SATURATION_DETECTION": {"type": "boolean"},
        "SATURATION_ALPHA": {"type": "float", "min": 0, "max": 1},
        "MIN_TARGET_FOR_SATURATION": {"type": "integer"},
        "MIN_EFFICIENT_POWER": {"type": "integer"},
        "EFFICIENCY_ROTATION_INTERVAL": {"type": "integer"},
        "PROBE_MIN_POWER": {"type": "integer"},
        "EFFICIENCY_FADE_ALPHA": {"type": "float", "min": 0, "max": 1},
        "EFFICIENCY_SATURATION_THRESHOLD": {"type": "float", "min": 0, "max": 1},
        "SATURATION_DECAY_FACTOR": {"type": "float", "min": 0, "max": 1},
        "SATURATION_GRACE_SECONDS": {"type": "float"},
        "SATURATION_STALL_TIMEOUT_SECONDS": {"type": "float"},
    },
    "MARSTEK": {
        "ENABLE": {"type": "boolean"},
        "PASSWORD": {"type": "password"},
    },
    "SHELLY": _pm(
        TYPE={"type": "select", "options": ["1PM", "PLUS1PM", "EM", "3EM", "3EMPro"]},
        PASS={"type": "password"},
    ),
    "TASMOTA": _pm(
        PASS={"type": "password"},
        JSON_POWER_CALCULATE={"type": "boolean"},
    ),
    "SHRDZM": _pm(PASS={"type": "password"}),
    "EMLOG": _pm(
        METER_INDEX={"type": "integer"},
        JSON_POWER_CALCULATE={"type": "boolean"},
    ),
    "IOBROKER": _pm(
        PORT={"type": "integer"},
        POWER_CALCULATE={"type": "boolean"},
    ),
    "HOMEASSISTANT": _pm(
        PORT={"type": "integer"},
        HTTPS={"type": "boolean"},
        ACCESSTOKEN={"type": "password"},
        POWER_CALCULATE={"type": "boolean"},
    ),
    "VZLOGGER": _pm(PORT={"type": "integer"}),
    "ESPHOME": _pm(PORT={"type": "integer"}),
    "AMIS_READER": _pm(),
    "MODBUS": _pm(
        PORT={"type": "integer"},
        UNIT_ID={"type": "integer"},
        ADDRESS={"type": "integer"},
        COUNT={"type": "integer"},
        DATA_TYPE={
            "type": "select",
            "options": ["UINT16", "INT16", "UINT32", "INT32", "FLOAT32", "FLOAT64"],
        },
        BYTE_ORDER={"type": "select", "options": ["BIG", "LITTLE"]},
        WORD_ORDER={"type": "select", "options": ["BIG", "LITTLE"]},
        REGISTER_TYPE={"type": "select", "options": ["HOLDING", "INPUT"]},
    ),
    "MQTT": _pm(
        PORT={"type": "integer"},
        TLS={"type": "boolean"},
        PASSWORD={"type": "password"},
    ),
    "JSON_HTTP": _pm(PASSWORD={"type": "password"}),
    "TQ_EM": _pm(
        PASSWORD={"type": "password"},
        TIMEOUT={"type": "float"},
    ),
    "HOMEWIZARD": _pm(
        TOKEN={"type": "password"},
        VERIFY_SSL={"type": "boolean"},
    ),
    "SMA_ENERGY_METER": _pm(
        PORT={"type": "integer"},
        SERIAL_NUMBER={"type": "integer"},
    ),
    "SCRIPT": _pm(),
    "SML": _pm(),
    "MQTT_INSIGHTS": {
        "PORT": {"type": "integer"},
        "TLS": {"type": "boolean"},
        "PASSWORD": {"type": "password"},
        "HA_DISCOVERY": {"type": "boolean"},
    },
}
# Resolve aliases
SECTION_KEY_TYPES["CT003"] = SECTION_KEY_TYPES["CT002"]


def section_key_types_json() -> str:
    """Return SECTION_KEY_TYPES as a JSON string for the config editor."""
    return json.dumps(SECTION_KEY_TYPES)
