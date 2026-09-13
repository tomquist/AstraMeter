"""The emulated device's own identity: one MAC, three names derived from it.

A real Shelly is known on the network by its MAC. Everything a consumer uses to
recognise it — the mDNS instance label, the host in the SRV and A records, and
the ``id`` it reports over HTTP — is that MAC in a particular spelling. This
module produces all three from a single value that has to stay the same across
restarts, because a battery that paired with one identity will not follow it to
another.

The emulator's ``device_id`` is deliberately *not* part of this. That string is
the MQTT-Insights entity identity and the UDP ``src``; rederiving it would
rename every Home Assistant entity an existing install already has and orphan
its retained discovery topics. The two identities stay separate.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import tempfile
from datetime import datetime, timezone

from astrameter.config.logger import logger
from astrameter.config.settings import ShellySettings
from astrameter.net_info import default_route_interface, interface_mac

_MAC_HEX_RE = re.compile(r"[0-9a-f]{12}")

#: Name of the persisted identity inside the add-on's data volume.
ADDON_STATE_PATH = (
    pathlib.Path(os.environ.get("ASTRAMETER_ADDON_DATA_DIR", "/data")) / "identity.json"
)

#: Name used beside a user's config file, and inside the fallback state dir.
STATE_FILENAME = "astrameter-identity.json"

#: Bumped only if the file's shape ever changes; an unparseable or
#: newer-versioned file is ignored rather than treated as an error.
_STATE_VERSION = 1


@dataclasses.dataclass(frozen=True, slots=True)
class ShellyIdentity:
    """The four strings every Shelly-facing surface needs.

    ``instance`` and ``hostname`` are computed independently of each other and
    of ``shelly_id``: pinning one must not move the others. ``shelly_id`` is
    always the lowercase form derived straight from the MAC.
    """

    mac: str
    """``"B827EB364242"`` — 12 uppercase hex digits, no separators."""

    instance: str
    """``"ShellyPro3EM-B827EB364242"`` — the mDNS service instance label."""

    hostname: str
    """``"ShellyPro3EM-B827EB364242"`` — the SRV target and A host, no dot."""

    shelly_id: str
    """``"shellypro3em-b827eb364242"`` — the ``id`` reported over HTTP."""


def normalize_mac(raw: str) -> str:
    """*raw* as 12 uppercase hex digits, or ``""`` when it is not a MAC.

    Accepts colons, dashes and bare hex, matching what the MQTT side already
    accepts so a user can paste the same spelling into either.
    """
    if not raw:
        return ""
    cleaned = raw.replace(":", "").replace("-", "").strip().lower()
    return cleaned.upper() if _MAC_HEX_RE.fullmatch(cleaned) else ""


def _mac_from_device_id(device_id: str | None) -> str:
    """The MAC a *user-written* device id ends in, or ``""``.

    Only ever called with an id a human configured: the generated default also
    ends in 12 hex characters, so matching on the shape alone would adopt it on
    every default install and defeat the derivation below.
    """
    if not device_id or "-" not in device_id:
        return ""
    return normalize_mac(device_id.rsplit("-", 1)[1])


def state_dir(config_path: str | None, *, addon: bool) -> pathlib.Path | None:
    """Where the identity file belongs, or ``None`` when nothing is writable.

    The add-on has a persistent volume; a Docker or direct install with a
    config file keeps the identity beside it; otherwise an explicit override or
    the XDG state directory is used.
    """
    if addon:
        return ADDON_STATE_PATH.parent

    override = os.environ.get("ASTRAMETER_STATE_DIR")
    if override:
        return pathlib.Path(override)

    if config_path:
        return pathlib.Path(config_path).resolve().parent

    xdg = os.environ.get("XDG_STATE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "state"
    return base / "astrameter"


def _state_file(config_path: str | None, *, addon: bool) -> pathlib.Path | None:
    directory = state_dir(config_path, addon=addon)
    if directory is None:
        return None
    return directory / ("identity.json" if addon else STATE_FILENAME)


def _read_state(path: pathlib.Path) -> str:
    """The MAC the file at *path* holds, or ``""``."""
    try:
        raw = path.read_text()
    except OSError:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring unreadable identity file %s", path)
        return ""
    if not isinstance(parsed, dict) or parsed.get("version") != _STATE_VERSION:
        logger.warning("Ignoring identity file %s: unexpected shape", path)
        return ""
    return normalize_mac(str(parsed.get("mac", "")))


def _write_state(path: pathlib.Path, mac: str) -> bool:
    """Persist *mac* at *path*. ``True`` when it landed.

    Written through a temporary file in the same directory so a crash cannot
    leave a half-written identity behind.
    """
    payload = {
        "version": _STATE_VERSION,
        "mac": mac,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(payload))
            temp_name = handle.name
        os.replace(temp_name, path)
    except OSError as exc:
        logger.warning(
            "Could not persist the emulated Shelly identity to %s (%s). The "
            "identity is still derived from this host, so it normally survives "
            "a restart anyway; set MAC in [EMULATOR_SHELLYPRO3EM] to pin it.",
            path,
            exc,
        )
        return False
    return True


def _derived_mac() -> str:
    """The MAC of the interface carrying the default route, or a random one.

    A host-networked container routes through the host's real NIC, so this is a
    burned-in address that does not move between restarts. The random fallback
    sets the local-administration bit so it cannot collide with real hardware.
    """
    interface = default_route_interface()
    if interface:
        address = interface_mac(interface)
        normalized = normalize_mac(address or "")
        if normalized:
            return normalized
        logger.debug("Interface %s has no usable hardware address", interface)

    octets = bytearray(os.urandom(6))
    octets[0] = (octets[0] | 0x02) & 0xFE
    return octets.hex().upper()


def resolve_identity(
    settings: ShellySettings,
    device_id_hint: str | None,
    *,
    addon: bool,
    config_path: str | None,
) -> ShellyIdentity:
    """The identity this device announces, resolved in priority order.

    An explicit ``MAC`` wins, then a user-written device id, then the persisted
    file, then this host's default-route interface, then a random value. The
    last two are written back, so the file answers every later start even if the
    interface changes.

    *device_id_hint* is ``None`` unless the id was configured by the user.
    """
    configured = normalize_mac(settings.mac)
    if configured:
        return _identity_for(configured, settings)

    from_device_id = _mac_from_device_id(device_id_hint)
    if from_device_id:
        return _identity_for(from_device_id, settings)

    path = _state_file(config_path, addon=addon)
    if path is not None:
        persisted = _read_state(path)
        if persisted:
            return _identity_for(persisted, settings)

    derived = _derived_mac()
    if path is not None:
        _write_state(path, derived)
    return _identity_for(derived, settings)


def _identity_for(mac: str, settings: ShellySettings) -> ShellyIdentity:
    """Build the three names from *mac*, honouring the two name overrides.

    The overrides move only their own name. ``shelly_id`` is never one of them:
    it is what a consumer matches the device on across mDNS and HTTP, so it
    stays the lowercase form of the MAC whatever the DNS names are set to.
    """
    default_name = f"ShellyPro3EM-{mac}"
    return ShellyIdentity(
        mac=mac,
        instance=settings.mdns_instance.strip() or default_name,
        hostname=settings.hostname.strip() or default_name,
        shelly_id=f"shellypro3em-{mac.lower()}",
    )
