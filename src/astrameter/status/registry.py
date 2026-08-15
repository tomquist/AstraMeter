"""Process-wide live-state registry backing the dashboard status API.

Populated incrementally as components come up, so ``/health`` and
``/api/status`` answer during the powermeter self-test window — which can run
for ~95 s before the first device exists, and which the Home Assistant
watchdog and the Docker ``HEALTHCHECK`` both probe through.

``snapshot()`` and every ``*.status_snapshot()`` it calls MUST be plain
``def``.  The whole device path runs on one asyncio loop, so an await-free
builder is atomic against every UDP handler; adding an ``await`` here
silently produces snapshots torn across two polls.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import datetime, timezone
from typing import Any

from astrameter.status.serialize import (
    compact,
    ct002_to_wire,
    iso,
    powermeter_to_wire,
    round_or_none,
    shelly_to_wire,
)

SCHEMA_VERSION = 1

# Coarse time bucket mixed into the ETag.  `rev` alone would let a client hold
# a 304 forever while every age/uptime field silently froze.
_ETAG_BUCKET_SECONDS = 5


@dataclasses.dataclass
class DeviceEntry:
    """One emulated device, registered only after it starts successfully."""

    device_id: str
    device_type: str
    device: Any


@dataclasses.dataclass
class StatusRegistry:
    """Mutable handle onto everything the dashboard can report.

    Built as the first statement of the process and handed to the web server,
    so routes exist before any device does.  Everything here is written from
    the single asyncio loop.
    """

    #: File backing the configuration, or ``None`` in add-on options mode
    #: where the settings come straight from the Supervisor.
    config_path: str | None
    log_level: str
    version: str
    git_commit: str
    #: The configuration backend currently running, so the mode switch can
    #: write out what it holds. Replaced (not mutated) on a config restart.
    app_config: Any = None
    config_mode: str = "standalone"
    addon_slug: str | None = None
    web_port: int = 52500
    dashboard_enabled: bool = False
    allow_write: bool = False
    direct_access: bool = False
    #: Whether a configuration surface is served at all. Set from the web
    #: server's route table, since ``WEB_CONFIG_ENABLED = False`` turns the
    #: editor off while the rest of the dashboard stays.
    config_editor: bool = True
    started_monotonic: float = dataclasses.field(default_factory=time.monotonic)
    started_at: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    powermeters: list = dataclasses.field(default_factory=list)
    devices: dict[str, DeviceEntry] = dataclasses.field(default_factory=dict)
    insights: Any = None
    cloud_reporters: dict[str, Any] = dataclasses.field(default_factory=dict)
    managed_marstek: dict[str, tuple[str, int]] = dataclasses.field(
        default_factory=dict
    )
    restart_pending: bool = False

    _seq: int = 0
    _rev: int = 0

    # -- lifecycle -----------------------------------------------------

    def bump(self) -> None:
        """Mark state dirty so pollers know to re-render."""
        self._rev += 1

    def register_device(self, device_id: str, device_type: str, device: Any) -> None:
        """Add a device that has already started successfully.

        Mirrors the existing rule that a device which failed to start is not
        wired into MQTT command routing either.
        """
        self.devices[device_id] = DeviceEntry(device_id, device_type, device)
        self.bump()

    def unregister_device(self, device_id: str) -> None:
        self.devices.pop(device_id, None)
        self.cloud_reporters.pop(device_id, None)
        self.bump()

    def reset_cycle(self) -> None:
        """Forget per-run state before a restart re-runs the device cycle."""
        self.devices.clear()
        self.cloud_reporters.clear()
        self.powermeters = []
        self.insights = None
        self.bump()

    # -- reads ---------------------------------------------------------

    def revision(self) -> int:
        """Registry revision plus every device's own, so a device-local
        mutation (a new battery report) invalidates the ETag too."""
        rev = self._rev
        for entry in self.devices.values():
            rev += getattr(entry.device, "_rev", 0)
        return rev

    def etag(self) -> str:
        bucket = int(time.monotonic() // _ETAG_BUCKET_SECONDS)
        return f'W/"{self.revision()}-{bucket}"'

    def under_supervisor(self) -> bool:
        """Whether Home Assistant is in front of us, i.e. ingress exists."""
        return self.config_mode.startswith("ha_")

    def serves_direct(self) -> bool:
        """Whether a request that did not arrive through ingress is served.

        Under the add-on the sidebar is the normal way in and the LAN port is
        extra exposure, so it stays an explicit opt-in.  Outside it there is
        no ingress peer and never will be, so the plain port is the *only*
        way in: requiring a second flag there would make ``DASHBOARD_ENABLED``
        do nothing on its own.
        """
        return self.direct_access or not self.under_supervisor()

    def capabilities(self, *, ingress: bool) -> dict[str, Any]:
        """What this deployment can do, so the UI never branches on identity."""
        supervisor = self.under_supervisor()
        writable = self.allow_write and (ingress or self.serves_direct())
        # No config_mode means "this backend has nothing to configure", which
        # is what hides the Configuration tab. An ESPHome device says it by
        # having its settings compiled in; here it is WEB_CONFIG_ENABLED
        # turned off, and the tab has to go the same way — the routes behind
        # it are not registered.
        editable = self.config_editor
        return compact(
            {
                "backend": "python",
                # Reserved: there is no push transport today, and the field
                # exists so a future one is a capability flip, not a redesign.
                "stream": False,
                "poll_interval_ms": 2000,
                "config_mode": self.config_mode if editable else None,
                "config_writable": editable
                and writable
                and self.config_mode != "ha_simple",
                "ha_options": editable and supervisor and writable,
                "controls": writable,
                "restart_process": writable,
                "restart_supervisor": supervisor and writable,
                "balancer_internals": True,
                "ingress": ingress,
            }
        )

    def snapshot(self, *, ingress: bool) -> dict[str, Any]:
        """Build the whole status document.  Pure, synchronous, no awaits."""
        self._seq += 1
        now = time.monotonic()
        devices: list[dict[str, Any]] = []
        for entry in self.devices.values():
            snapshot_fn = getattr(entry.device, "status_snapshot", None)
            if snapshot_fn is None:
                continue
            snap = snapshot_fn()
            wire = (
                ct002_to_wire(snap)
                if getattr(snap, "consumers", None) is not None
                else shelly_to_wire(snap)
            )
            devices.append(wire)

        return compact(
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "capabilities": self.capabilities(ingress=ingress),
                "seq": self._seq,
                "rev": self.revision(),
                "uptime_s": round_or_none(now - self.started_monotonic),
                "service": compact(
                    {
                        "version": self.version,
                        "git_commit": self.git_commit or None,
                        "log_level": self.log_level,
                        "config_path": self.config_path,
                        "config_mtime_at": iso(_mtime(self.config_path)),
                        "runtime": "ha_addon"
                        if self.config_mode.startswith("ha_")
                        else "docker",
                        "addon_slug": self.addon_slug,
                        "restart_pending": self.restart_pending,
                        "started_at": self.started_at.isoformat(),
                        "web": {"port": self.web_port, "ingress": ingress},
                    }
                ),
                "powermeters": [
                    powermeter_to_wire(pm.status_snapshot())
                    for pm in self.powermeters
                    if hasattr(pm, "status_snapshot")
                ]
                or None,
                "devices": devices or None,
                "integrations": self._integrations() or None,
            }
        )

    def _integrations(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.insights is not None and hasattr(self.insights, "status_snapshot"):
            out["mqtt_insights"] = _as_wire_dict(self.insights.status_snapshot())
        reporters = [
            _as_wire_dict(r.status_snapshot())
            for r in self.cloud_reporters.values()
            if hasattr(r, "status_snapshot")
        ]
        if reporters:
            out["cloud_reporting"] = reporters
        if self.managed_marstek:
            out["marstek_account"] = {
                "enabled": True,
                "managed": [
                    {"device_type": dt, "mac": mac, "ver_v": ver}
                    for dt, (mac, ver) in sorted(self.managed_marstek.items())
                ],
            }
        return out


def _mtime(path: str | None) -> float | None:
    import os

    if path is None:
        return None
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _as_wire_dict(snapshot: Any) -> dict[str, Any]:
    """Shallow dataclass → wire dict for the integration snapshots.

    Their field names are already the wire names, so unlike the device
    snapshots they need no rename layer — only ``None``-dropping and nested
    dataclass expansion.
    """
    if not dataclasses.is_dataclass(snapshot) or isinstance(snapshot, type):
        return {}
    out: dict[str, Any] = {}
    for field in dataclasses.fields(snapshot):
        value = getattr(snapshot, field.name)
        if value is None:
            continue
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            out[field.name] = _as_wire_dict(value)
        elif isinstance(value, (tuple, list)):
            out[field.name] = [
                _as_wire_dict(v)
                if dataclasses.is_dataclass(v) and not isinstance(v, type)
                else v
                for v in value
            ]
        else:
            out[field.name] = value
    return out
