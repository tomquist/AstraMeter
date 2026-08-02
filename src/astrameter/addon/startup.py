"""Add-on startup: work out the configuration before the service runs.

Replaces what ``ha_addon/run.sh`` used to do in bash.  ``run.sh`` is now a
launcher; everything decided here is decided in Python, where it can be
tested.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import urllib.error
import urllib.request

from astrameter.config.logger import logger

from .generate import generate_config
from .options import is_set

# The three paths below are fixed by the Supervisor at runtime.  Each takes an
# environment override so the whole add-on startup can be exercised outside a
# Home Assistant install — by a test, or by a developer running the container
# by hand.
#
#: What the Supervisor writes for the add-on, with `!secret` references
#: already RESOLVED.  This is what ``bashio::config`` read, and the only
#: correct source for the running configuration — ``/addons/self/info``
#: returns them raw, which would hand the broker a literal "!secret foo".
OPTIONS_PATH = os.environ.get("ASTRAMETER_ADDON_OPTIONS", "/data/options.json")

#: Where a `custom_config` filename is resolved from.
ADDON_CONFIG_DIR = os.environ.get("ASTRAMETER_ADDON_CONFIG_DIR", "/config")

#: Where the config generated from the add-on options is written.
GENERATED_CONFIG_PATH = os.environ.get(
    "ASTRAMETER_ADDON_GENERATED_CONFIG", "/app/config.ini"
)

SUPERVISOR_URL = "http://supervisor"
CORE_READY_URL = f"{SUPERVISOR_URL}/core/api/"

_SECRET_LINE = re.compile(
    r"^\s*(MAILBOX|USERNAME|PASSWORD|ACCESSTOKEN|TOKEN|SECRET|API_KEY)\s*=.*$",
    re.IGNORECASE,
)
_URI_USERINFO = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")

#: Options the generated config would have covered, which a custom config
#: file overrides entirely.  Warned about rather than silently dropped.
_IGNORED_WITH_CUSTOM_CONFIG = (
    "marstek_auto_register_ct_device",
    "marstek_mailbox",
    "marstek_password",
    "mqtt_uri",
)


@dataclasses.dataclass(frozen=True, slots=True)
class AddonConfig:
    """Where the running configuration came from."""

    #: Path the service should read.
    path: str
    #: ``"custom_config"`` (a file the user maintains) or ``"addon_options"``
    #: (generated from the add-on UI, and safe to regenerate).
    source: str


def read_options(path: str | None = None) -> dict:
    """The add-on's merged options, or ``{}`` when not running as an add-on."""
    path = path or OPTIONS_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.error("Could not read add-on options from %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _supervisor_get(path: str, timeout: float = 10.0):
    """A Supervisor API GET, returning ``data`` or None."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    request = urllib.request.Request(
        f"{SUPERVISOR_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.debug("Supervisor GET %s failed: %s", path, exc)
        return None
    return body.get("data") if isinstance(body, dict) else None


def mqtt_service() -> dict | None:
    """Connection details for Home Assistant's own MQTT broker, if offered."""
    data = _supervisor_get("/services/mqtt")
    if not data or not data.get("host"):
        return None
    return data


def addon_slug() -> str | None:
    """This add-on's slug, so MQTT discovery can link devices via_device."""
    data = _supervisor_get("/addons/self/info")
    return (data or {}).get("slug") or None


def wait_for_home_assistant(timeout: float = 300.0, interval: float = 5.0) -> bool:
    """Block until Home Assistant answers, or the timeout expires.

    A timeout is not fatal — the powermeter has its own retries — so this
    only logs and moves on, exactly as the shell did.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return True
    logger.info("Waiting for Home Assistant to be ready...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            CORE_READY_URL, headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status < 400:
                    logger.info("Home Assistant is ready")
                    return True
        except Exception:  # any failure just means "not yet"
            pass
        time.sleep(interval)
    logger.warning(
        "Home Assistant did not become ready within %.0fs; continuing anyway",
        timeout,
    )
    return False


def redact(text: str) -> str:
    """Config text with credentials removed, for logging.

    The add-on prints its effective configuration at startup and users paste
    that into bug reports, so this has to be reliable rather than tidy.
    """
    lines = []
    for line in text.split("\n"):
        if _SECRET_LINE.match(line):
            key = line.split("=", 1)[0].rstrip()
            lines.append(f"{key} = REDACTED")
        else:
            lines.append(_URI_USERINFO.sub(r"\1***:***@", line))
    return "\n".join(lines)


def resolve_config(
    options: dict,
    *,
    config_dir: str = ADDON_CONFIG_DIR,
    generated_path: str = GENERATED_CONFIG_PATH,
    supervisor: bool = True,
) -> AddonConfig:
    """Decide which config file to run from, writing it if it is generated.

    A ``custom_config`` naming a missing file falls back to the generated
    config — the same thing the shell did — but says so at error level,
    because otherwise the user just sees their settings being ignored.
    """
    custom = options.get("custom_config")
    if custom:
        path = os.path.join(config_dir, str(custom))
        if os.path.isfile(path):
            logger.info("Using custom config file: %s", path)
            for ignored in _IGNORED_WITH_CUSTOM_CONFIG:
                if is_set(options.get(ignored)) and options.get(ignored) is not False:
                    logger.warning(
                        "Add-on option '%s' is ignored while custom_config is set; "
                        "the config file controls it",
                        ignored,
                    )
            # Read the user's file where it lives. Copying it to
            # /app/config.ini meant every edit made to it — from the config
            # editor or a file editor — was discarded on the next restart.
            return AddonConfig(path, "custom_config")
        logger.error(
            "custom_config is set to '%s' but %s does not exist; "
            "falling back to the add-on options",
            custom,
            path,
        )

    text = generate_config(
        options,
        mqtt_service=mqtt_service() if supervisor else None,
        addon_slug=addon_slug() if supervisor else None,
    )
    with open(generated_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return AddonConfig(generated_path, "addon_options")


def bootstrap(options: dict) -> AddonConfig:
    """Prepare the add-on's configuration and report where it came from."""
    resolved = resolve_config(
        options,
        config_dir=ADDON_CONFIG_DIR,
        generated_path=GENERATED_CONFIG_PATH,
    )

    try:
        with open(resolved.path, encoding="utf-8") as handle:
            body = redact(handle.read())
    except OSError as exc:
        raise RuntimeError(f"Cannot read {resolved.path}: {exc}") from exc
    logger.info("Effective configuration (%s):\n%s", resolved.path, body)

    wait_for_home_assistant()
    return resolved
