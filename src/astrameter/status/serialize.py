"""The single naming / units / absence layer between runtime state and the wire.

The runtime snapshot dataclasses (``CT002Snapshot``, ``BalancerSnapshot``,
``PowermeterHealth``, ...) are shaped for the code that produces them.  This
module is the one place that renames them to the documented wire schema,
stamps units into the key (``_w``, ``_s``, ``_pct``, ``_at``, ``_age_s``) and
decides what absence means.

Two rules the dashboard depends on:

* **Absence is not zero.**  ``compact()`` drops ``None`` so a field the
  backend could not produce is *missing*, letting the UI omit the card
  instead of rendering a real-looking ``0``.
* **Monotonic marks never cross the wire.**  A ``time.monotonic()`` value is
  meaningless to a browser; it is emitted as an age in seconds.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any


def compact(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so absence and zero stay distinguishable."""
    return {k: v for k, v in mapping.items() if v is not None}


def iso(epoch: float | None) -> str | None:
    """Wall-clock epoch seconds as ISO-8601 UTC, or ``None``."""
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def round_or_none(value: float | None, digits: int = 1) -> float | None:
    """Round a float for the wire, preserving ``None``.

    Keeps the payload small and stops a 17-digit repr from implying
    precision the control loop does not have.
    """
    if value is None:
        return None
    return round(float(value), digits)


def _phase_triple(values, suffix: str = "_w") -> dict[str, Any] | None:
    """Three per-phase numbers as ``l1``/``l2``/``l3``.

    The wire keeps ``l1/l2/l3`` because that is exactly what the existing
    MQTT payload uses; the UI relabels to "Phase A/B/C".
    """
    if values is None:
        return None
    padded = ([*list(values), 0.0, 0.0, 0.0])[:3]
    return {f"l{i + 1}{suffix}": round_or_none(float(v)) for i, v in enumerate(padded)}


def powermeter_to_wire(health) -> dict[str, Any]:
    """A :class:`PowermeterHealth` as its wire object."""
    return compact(
        {
            "name": health.name or None,
            "kind": health.kind,
            "pipeline": list(health.pipeline) or None,
            # Tri-state on purpose: None ("pull meter, unknowable without
            # I/O") must not collapse into False ("known down").
            "online": health.online,
            "last_read_age_s": round_or_none(health.last_read_age),
            "last_read_ok": health.last_read_ok,
            "last_values_w": [round_or_none(v) for v in health.last_values]
            if health.last_values is not None
            else None,
            "last_total_w": round_or_none(health.last_total),
        }
    )


def _balancer_consumer_to_wire(state) -> dict[str, Any] | None:
    if state is None:
        return None
    return compact(
        {
            "last_target_w": round_or_none(state.last_target),
            "last_intent_w": round_or_none(state.last_intent),
            "last_intent_reading_w": round_or_none(state.last_intent_reading),
            "saturation": round_or_none(state.saturation, 3),
            "saturation_grace_remaining_s": round_or_none(
                state.saturation_grace_remaining
            ),
            "fade_weight": round_or_none(state.fade_weight, 3),
            "deprioritized": state.deprioritized,
            "pace": compact(
                {
                    "cap_w": round_or_none(state.pace_cap),
                    "sign": state.pace_sign,
                }
            )
            or None,
            "oscillation": compact(
                {
                    "score": round_or_none(state.osc_score, 3),
                    "last_sign": state.osc_last_sign,
                }
            )
            or None,
        }
    )


def consumer_to_wire(consumer) -> dict[str, Any]:
    """A :class:`ConsumerSnapshot` as its wire object."""
    return compact(
        {
            "consumer_id": consumer.consumer_id,
            "device_type": consumer.device_type or None,
            "capabilities": {
                "builtin_inverter": consumer.builtin_inverter,
                "ac_input": consumer.ac_input,
                "dc_input": consumer.dc_input,
            },
            "last_ip": consumer.last_ip or None,
            "phase": consumer.phase,
            "bucket": consumer.bucket,
            "participates": consumer.participates,
            "reported_power_w": round_or_none(consumer.reported_power),
            "last_instructed_power_w": round_or_none(consumer.last_instructed_power),
            "target_w": _phase_triple(consumer.target, suffix=""),
            "last_seen_at": iso(consumer.last_seen_at),
            "last_seen_age_s": round_or_none(consumer.last_seen_age),
            # Stated, not inferred from the absence of the two fields above.
            # A retained MQTT command creates a consumer to hold its setting
            # before any battery reports, and the UI must tell that apart from
            # a real battery; keying it on a missing timestamp would turn every
            # battery into a placeholder on a backend that serves a reduced
            # document.  Emitted only when true, so absence means "a battery".
            "never_reported": True if not consumer.last_seen_at else None,
            "poll_interval_s": round_or_none(consumer.poll_interval),
            # Only worth showing when it diverges from the poll interval, i.e.
            # when a dedupe window is actually suppressing replies.
            "answer_interval_s": round_or_none(consumer.answer_interval),
            "ttl_s": round_or_none(consumer.ttl),
            "expired": consumer.expired,
            "in_flight": consumer.in_flight,
            "mode": consumer.mode,
            "active": consumer.active,
            "manual_enabled": consumer.manual_enabled,
            "manual_target_w": round_or_none(consumer.manual_target),
            "distribution_weight": round_or_none(consumer.distribution_weight, 3),
            # As a percentage, matching the "Efficiency Window Weight" MQTT
            # entity's unit — the dashboard and the user's HA entity list must
            # not disagree about what the number means.
            "efficiency_window_weight_pct": round_or_none(
                consumer.efficiency_window_weight * 100
                if consumer.efficiency_window_weight is not None
                else None,
                1,
            ),
            "min_dc_output_w": round_or_none(consumer.min_dc_output),
            "min_dc_output_applicable": consumer.min_dc_output_applicable,
            "balancer": _balancer_consumer_to_wire(consumer.balancer),
        }
    )


def _balancer_to_wire(balancer) -> dict[str, Any]:
    probe = balancer.probe
    return compact(
        {
            "config": compact(
                {
                    f"{f.name}{_UNIT_SUFFIX.get(f.name, '')}": round_or_none(
                        getattr(balancer.config, f.name), 4
                    )
                    if isinstance(getattr(balancer.config, f.name), float)
                    else getattr(balancer.config, f.name)
                    for f in dataclasses.fields(balancer.config)
                }
            ),
            "efficiency_rotation_enabled": balancer.efficiency_rotation_enabled,
            "predictor": compact(
                {
                    "grid_estimate_w": round_or_none(balancer.predictor.grid_estimate),
                    "trust": round_or_none(balancer.predictor.trust, 3),
                    "innovation_sign": balancer.predictor.innovation_sign,
                    "pool_output_w": round_or_none(balancer.predictor.pool_output),
                }
            ),
            "import_trim": compact(
                {
                    "dwell": balancer.import_trim.dwell,
                    "dwell_target": balancer.import_trim.dwell_target,
                    "gate_w": round_or_none(balancer.import_trim.gate),
                    "engaged": balancer.import_trim.engaged,
                }
            ),
            "efficiency": compact(
                {
                    "demand_ema_w": round_or_none(balancer.efficiency.demand_ema),
                    "priority_order": list(balancer.efficiency.priority_order) or None,
                    "deprioritized": list(balancer.efficiency.deprioritized) or None,
                    "last_rotation_age_s": round_or_none(
                        balancer.efficiency.last_rotation_age
                    ),
                    "all_dc_under_surplus": balancer.efficiency.all_dc_under_surplus,
                }
            ),
            "probe": compact(
                {
                    "candidate_id": probe.candidate_id,
                    "active_ids": list(probe.active_ids) or None,
                    "backup_ids": list(probe.backup_ids) or None,
                    "proof_samples": probe.proof_samples,
                    "requested_power_w": round_or_none(probe.requested_power_abs),
                    "started_age_s": round_or_none(probe.started_age),
                    "deadline_in_s": round_or_none(probe.deadline_in),
                }
            )
            if probe is not None
            else None,
        }
    )


# Balancer config field names are already unit-free in the dataclass; stamp the
# unit into the wire key so the dashboard never has to guess.
_UNIT_SUFFIX = {
    "balance_deadband": "_w",
    "error_boost_threshold": "_w",
    "error_reduce_threshold": "_w",
    "max_correction_per_step": "_w",
    "max_target_step": "_w",
    "pace_base_step": "_w",
    "pace_max_step": "_w",
    "osc_damp_threshold": "_w",
    "concentrate_deadband": "_w",
    "import_trim_w": "",
    "min_efficient_power": "_w",
    "probe_min_power": "_w",
    "min_dc_output": "_w",
    "efficiency_rotation_interval": "_s",
}


def ct002_to_wire(device) -> dict[str, Any]:
    """A :class:`CT002Snapshot` as its wire object."""
    grid = _phase_triple(device.grid)
    if grid is not None:
        grid = compact(
            {
                **grid,
                "grid_total_w": round_or_none(device.grid_total),
                "sample_at": iso(device.grid_sample_at),
                "meter_failed": device.meter_failed,
                "consecutive_meter_failures": device.consecutive_meter_failures,
            }
        )
    return compact(
        {
            "kind": "ct002",
            "device_id": device.device_id or None,
            "ct_type": device.ct_type,
            "ct_mac": device.ct_mac or None,
            "udp_port": device.udp_port,
            "wifi_rssi_dbm": device.wifi_rssi,
            "running": device.running,
            "started_at": iso(device.started_at),
            "control": compact(
                {
                    "active_control": device.active_control,
                    "consumer_ttl_s": device.consumer_ttl,
                    "dedupe_window_s": round_or_none(device.dedupe_window),
                    "debug_status": device.debug_status,
                    "info_idx": device.info_idx,
                }
            ),
            "grid": grid,
            "buckets": {
                name: compact(
                    {
                        "chrg_w": bucket.get("chrg_power"),
                        "dchrg_w": bucket.get("dchrg_power"),
                        "count": bucket.get("count"),
                        "active": bucket.get("active"),
                    }
                )
                for name, bucket in device.buckets.items()
            },
            "balancer": _balancer_to_wire(device.balancer),
            "consumers": [consumer_to_wire(c) for c in device.consumers],
            "orphan_overrides": [
                compact(
                    {
                        "consumer_id": cid,
                        "manual_target_w": round_or_none(override.manual_target),
                        "manual_enabled": override.manual_enabled,
                        "active": override.active,
                        "distribution_weight": round_or_none(
                            override.distribution_weight, 3
                        ),
                        "efficiency_window_weight": round_or_none(
                            override.efficiency_window_weight, 3
                        ),
                        "min_dc_output_w": round_or_none(override.min_dc_output),
                    }
                )
                for cid, override in device.orphan_overrides
            ]
            or None,
        }
    )


def shelly_to_wire(device) -> dict[str, Any]:
    """A :class:`ShellySnapshot` as its wire object.

    A Shelly emulator has no balancer and no per-battery targets — batteries
    poll it and read the meter, nothing is steered — so its wire object is
    deliberately much smaller than :func:`ct002_to_wire`'s. It still goes
    through this layer rather than a generic dataclass dump, so timestamps are
    ISO and durations carry their unit like everywhere else.
    """
    return compact(
        {
            "kind": "shelly",
            "device_id": device.device_id or None,
            "device_type": device.device_type or None,
            "udp_port": device.udp_port,
            "running": device.running,
            "started_at": iso(device.started_at),
            "inactive_timeout_s": device.inactive_timeout,
            "batteries": [
                compact(
                    {
                        "ip": battery.ip,
                        "last_seen_at": iso(battery.last_seen_at),
                        "last_seen_age_s": round_or_none(battery.last_seen_age),
                        "poll_interval_s": round_or_none(battery.poll_interval),
                        "active": battery.active,
                    }
                )
                for battery in device.batteries
            ]
            or None,
        }
    )
