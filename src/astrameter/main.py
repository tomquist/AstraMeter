import argparse
import asyncio
import contextlib
import os
import signal
from collections.abc import Sequence
from dataclasses import replace

from astrameter.cloud_reporting import (
    CloudReporter,
    CloudReporterConfig,
    CtMeasurement,
)
from astrameter.config import addon
from astrameter.config.config_loader import ClientFilter
from astrameter.config.ini_config import IniAppConfig
from astrameter.config.logger import logger, setLogLevel
from astrameter.config.settings import (
    AppConfig,
    CtSettings,
    GeneralSettings,
    MarstekSettings,
)
from astrameter.ct002 import CT002
from astrameter.marstek_api import (
    MarstekApiError,
    MarstekConfig,
    ensure_managed_fake_device,
)
from astrameter.mqtt_insights import (
    MarstekMqttBinding,
    MqttInsightsService,
    format_cd4_slave_csv,
    normalize_mac,
    ver_v_from_marstek_api_version,
)
from astrameter.powermeter import Powermeter
from astrameter.powermeter.wrappers.health import HealthTrackingPowermeter
from astrameter.shelly import Shelly
from astrameter.status import StatusRegistry, detect_config_mode
from astrameter.version_info import get_git_commit_sha, get_version
from astrameter.web_server import WebServer

# CT002/CT003 phase assignment is auto-managed by emulator runtime.


def _powermeter_log_name(powermeter: Powermeter) -> str:
    """Label for logs: the underlying meter class, seen through the outermost
    HealthTrackingPowermeter wrapper that now wraps every configured meter."""
    inner = (
        powermeter.wrapped_powermeter
        if isinstance(powermeter, HealthTrackingPowermeter)
        else powermeter
    )
    return type(inner).__name__


async def read_ct_powermeter(
    addr: tuple[str, int],
    powermeters: list[tuple[Powermeter, ClientFilter, bool]],
) -> list[float] | None:
    """Pick the powermeter matching *addr* and return up to three phase values.

    Optionally awaits a fresh push (with a 2 s cap) when the matched
    powermeter has ``WAIT_FOR_NEXT_MESSAGE`` enabled. A timeout there is
    swallowed so the cached value is still served — `update_readings`
    callers should never see a stale-meter `TimeoutError`.
    """
    powermeter = None
    wait_for_next = False
    for pm, client_filter, wait_flag in powermeters:
        if client_filter.matches(addr[0]):
            powermeter = pm
            wait_for_next = wait_flag
            break
    if powermeter is None:
        logger.debug(f"No powermeter found for client {addr[0]}")
        return None
    if wait_for_next:
        try:
            await powermeter.wait_for_next_message(timeout=2)
        except TimeoutError:
            logger.debug(
                "Powermeter %s produced no fresh message within 2s; "
                "serving last known value",
                _powermeter_log_name(powermeter),
            )
    values = await powermeter.get_powermeter_watts()
    value1 = values[0] if len(values) > 0 else 0
    value2 = values[1] if len(values) > 1 else 0
    value3 = values[2] if len(values) > 2 else 0
    return [value1, value2, value3]


async def test_powermeter(powermeter: Powermeter, client_filter: ClientFilter):
    """Test powermeter configuration with minimal retry logic for edge cases."""
    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(max_retries + 1):
        try:
            logger.debug(
                f"Testing powermeter configuration... (attempt {attempt + 1}/{max_retries + 1})"
            )
            await powermeter.wait_for_message(timeout=30)
            value = await powermeter.get_powermeter_watts()
            value_with_units = " | ".join([f"{v}W" for v in value])
            powermeter_name = _powermeter_log_name(powermeter)
            filter_description = ", ".join([str(n) for n in client_filter.netmasks])
            logger.info(
                f"Successfully fetched {powermeter_name} powermeter value (filter {filter_description}): {value_with_units}"
            )
            return  # Success, exit the function
        except Exception as e:
            logger.debug(f"Error on attempt {attempt + 1}: {e}")

            if attempt < max_retries:
                logger.info(f"Retrying powermeter test in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                continue
            else:
                # Last attempt failed
                raise RuntimeError(
                    f"Failed to test powermeter after {max_retries + 1} attempts: {e}"
                ) from e


def _reset_all_powermeters(
    powermeters: Sequence[tuple[Powermeter, object, object]],
) -> None:
    for pm, *_ in powermeters:
        pm.reset()


def _build_ct002(
    ct: CtSettings,
    ct_type: str,
    device_id: str,
    debug_status: bool,
    reset_fn,
) -> CT002:
    """Create the emulator a :class:`CtSettings` describes."""
    return CT002(
        udp_port=ct.udp_port,
        ct_type=ct_type,
        ct_mac=ct.ct_mac,
        wifi_rssi=ct.wifi_rssi,
        dedupe_time_window=ct.dedupe_time_window,
        consumer_ttl=ct.consumer_ttl,
        debug_status=debug_status,
        active_control=ct.active_control,
        fair_distribution=ct.fair_distribution,
        balance_gain=ct.balance_gain,
        error_boost_threshold=ct.error_boost_threshold,
        error_boost_max=ct.error_boost_max,
        error_reduce_threshold=ct.error_reduce_threshold,
        balance_deadband=ct.balance_deadband,
        max_correction_per_step=ct.max_correction_per_step,
        max_target_step=ct.max_target_step,
        pace_base_step=ct.pace_base_step,
        pace_max_step=ct.pace_max_step,
        osc_damp_max=ct.osc_damp_max,
        osc_damp_alpha=ct.osc_damp_alpha,
        osc_damp_decay=ct.osc_damp_decay,
        osc_damp_threshold=ct.osc_damp_threshold,
        grid_predict_trust=ct.grid_predict_trust,
        concentrate_deadband=ct.concentrate_deadband,
        import_trim_w=ct.import_trim_w,
        saturation_detection=ct.saturation_detection,
        saturation_alpha=ct.saturation_alpha,
        min_target_for_saturation=ct.min_target_for_saturation,
        saturation_grace_seconds=ct.saturation_grace_seconds,
        saturation_stall_timeout_seconds=ct.saturation_stall_timeout_seconds,
        min_efficient_power=ct.min_efficient_power,
        probe_min_power=ct.probe_min_power,
        efficiency_rotation_interval=ct.efficiency_rotation_interval,
        efficiency_fade_alpha=ct.efficiency_fade_alpha,
        efficiency_saturation_threshold=ct.efficiency_saturation_threshold,
        efficiency_demand_alpha=ct.efficiency_demand_alpha,
        min_dc_output=ct.min_dc_output,
        saturation_decay_factor=ct.saturation_decay_factor,
        device_id=device_id,
        reset_fn=reset_fn,
    )


async def run_device(
    device_type: str,
    config: AppConfig,
    general: GeneralSettings,
    powermeters: list[tuple[Powermeter, ClientFilter, bool]],
    device_id: str | None = None,
    insights: MqttInsightsService | None = None,
    marstek_mac: str = "",
    marstek_ver_v: int | None = None,
    registry: StatusRegistry | None = None,
):
    logger.debug(f"Starting device: {device_type}")

    device: CT002 | Shelly
    cloud_reporting = False

    if device_type in ["ct002", "ct003"]:
        ct = config.ct(device_type)
        ct_type = "HME-4" if device_type == "ct002" else "HME-3"
        cloud_reporting = ct.cloud_reporting
        debug_status = ct.debug_status or os.environ.get(
            "DEBUG_STATUS", ""
        ).lower() in ("1", "true", "yes")
        if 0 < ct.min_dc_output < ct.min_target_for_saturation:
            logger.warning(
                "MIN_DC_OUTPUT (%gW) is below MIN_TARGET_FOR_SATURATION (%dW): a "
                "floored battery's target never clears the saturation gate, so an "
                "empty/full unit can't be detected. Consider MIN_DC_OUTPUT >= %d.",
                ct.min_dc_output,
                ct.min_target_for_saturation,
                ct.min_target_for_saturation,
            )

        logger.debug(f"{device_type.upper()} Settings for {device_id}: {ct}")
        logger.debug(f"CT Type: {ct_type}")
        logger.debug(
            "CT control model: %s",
            (
                "active control (emulator computes targets)"
                if ct.active_control
                else "relay (forward consumer aggregates)"
            ),
        )
        if ct.active_control:
            extras = []
            if ct.fair_distribution:
                extras.append("fair distribution")
            if ct.saturation_detection:
                extras.append("saturation detection")
            if ct.min_efficient_power > 0:
                extras.append(f"efficiency optimization ({ct.min_efficient_power}W)")
            logger.info(
                "Active control enabled: load split%s",
                " + " + " + ".join(extras) if extras else "",
            )

        device = _build_ct002(
            ct,
            ct_type,
            device_id or "",
            debug_status,
            lambda: _reset_all_powermeters(powermeters),
        )

        async def update_readings(addr, _fields=None, _consumer_id=None):
            return await read_ct_powermeter(addr, powermeters)

        device.before_send = update_readings

        if insights:

            def _ct002_event_listener(dev_id, consumer_id, data):
                # {"_removed": True} is a sentinel from _cleanup_consumers
                if data.get("_removed"):
                    insights.on_ct002_consumer_removed(dev_id, consumer_id)
                else:
                    insights.on_ct002_response(dev_id, consumer_id, data)

            device.event_listener = _ct002_event_listener

    elif device_type == "shellypro3em_old":
        logger.debug("Shelly Pro 3EM Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(
            powermeters=powermeters,
            device_id=device_id,
            device_type=device_type,
            udp_port=1010,
            dedupe_time_window=general.dedupe_time_window,
        )

    elif device_type == "shellypro3em_new":
        logger.debug("Shelly Pro 3EM Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(
            powermeters=powermeters,
            device_id=device_id,
            device_type=device_type,
            udp_port=2220,
            dedupe_time_window=general.dedupe_time_window,
        )

    elif device_type == "shellyemg3":
        logger.debug("Shelly EM Gen3 Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(
            powermeters=powermeters,
            device_id=device_id,
            device_type=device_type,
            udp_port=2222,
            dedupe_time_window=general.dedupe_time_window,
        )

    elif device_type == "shellyproem50":
        logger.debug("Shelly Pro EM 50 Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(
            powermeters=powermeters,
            device_id=device_id,
            device_type=device_type,
            udp_port=2223,
            dedupe_time_window=general.dedupe_time_window,
        )

    else:
        raise ValueError(f"Unsupported device type: {device_type}")

    # Wire Shelly event listener
    if insights and isinstance(device, Shelly):

        def _shelly_event_listener(dev_id, battery_ip, data):
            if data.get("_removed"):
                insights.on_shelly_battery_removed(dev_id, battery_ip)
            else:
                insights.on_shelly_response(dev_id, battery_ip, data)

        device.event_listener = _shelly_event_listener

    try:
        await device.start()
    except Exception:
        # Log but don't re-raise: a single device failing to start (e.g. port
        # conflict) should not take down other healthy devices in the gather.
        logger.exception("Device %s (%s) failed to start", device_type, device_id)
        try:
            await device.stop()
        except Exception:
            logger.exception(
                "Device %s (%s) cleanup also failed", device_type, device_id
            )
        return

    # Same rule as the MQTT handlers below: only a device that actually came
    # up is visible to the dashboard, so a port conflict shows as an absent
    # device rather than a device reporting zeros.
    if registry is not None:
        registry.register_device(device_id or "", device_type, device)

    # Register active handler only after successful start so MQTT commands
    # are never routed to a device that failed to come up.
    if insights and isinstance(device, CT002):
        insights.register_active_handler(device_id or "", device.set_consumer_active)
        insights.register_manual_target_handler(
            device_id or "", device.set_consumer_manual_target
        )
        insights.register_auto_target_handler(
            device_id or "", device.set_consumer_auto_target
        )
        insights.register_distribution_weight_handler(
            device_id or "", device.set_consumer_distribution_weight
        )
        insights.register_efficiency_window_weight_handler(
            device_id or "", device.set_consumer_efficiency_window_weight
        )
        insights.register_min_dc_output_handler(
            device_id or "", device.set_consumer_min_dc_output
        )
        insights.register_rotation_handler(
            device_id or "", device.force_efficiency_rotation
        )
        insights.register_active_control_handler(
            device_id or "", device.set_active_control
        )

    # Marstek MQTT responder — only wired up when Marstek credentials
    # yielded a managed MAC (so hame-relay can route the replies back to
    # the Marstek app) and the feature is enabled.
    if isinstance(device, CT002) and insights and insights.marstek_mqtt_enabled:
        if marstek_mac:

            async def _marstek_get_values(
                _pms: list[tuple[Powermeter, ClientFilter, bool]] = powermeters,
            ) -> list[float]:
                chosen: Powermeter | None = next(
                    (pm for pm, cf, _ in _pms if cf.matches("0.0.0.0")), None
                )
                if chosen is None and _pms:
                    chosen = _pms[0][0]
                if chosen is None:
                    return [0.0, 0.0, 0.0]
                # Bound the wait so a quiet/offline powermeter can't pin a
                # Marstek poll responder task; fall back to last-known values.
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(chosen.wait_for_next_message(), timeout=2.0)
                vs = await chosen.get_powermeter_watts_raw()
                return [float(vs[i]) if i < len(vs) else 0.0 for i in range(3)]

            def _marstek_connected_slave_count() -> int:
                return device.reporting_consumer_count()

            def _marstek_cd4_slave_csv() -> str:
                return format_cd4_slave_csv(device.reporting_consumer_rows())

            await insights.register_marstek(
                MarstekMqttBinding(
                    device_id=device_id or "",
                    ct_type=device.ct_type,
                    mac=marstek_mac,
                    get_values=_marstek_get_values,
                    get_connected_slave_count=_marstek_connected_slave_count,
                    get_cd4_slave_csv=_marstek_cd4_slave_csv,
                    wifi_rssi=device.wifi_rssi,
                    ver_v=marstek_ver_v
                    if marstek_ver_v is not None
                    else ver_v_from_marstek_api_version(None),
                )
            )
        else:
            logger.info(
                "Marstek MQTT responder not wired for %s: no managed MAC "
                "available. Enable [MARSTEK] with MAILBOX/PASSWORD to use "
                "this feature, or set MARSTEK_MQTT_ENABLED=false to silence "
                "this notice.",
                device_id,
            )

    # Opt-in HTTP cloud reporting (hamedata.com), mimicking what a real CT does:
    # a handshake then a periodic setCtReporting GET with live grid/bucket data.
    cloud_task: asyncio.Task[None] | None = None
    if isinstance(device, CT002) and cloud_reporting:
        # The reported id is the CT's MAC: the one AstraMeter registered in the
        # Marstek account (the id the cloud actually knows) when configured, else
        # the locally set CT_MAC.
        report_id = marstek_mac or ct.ct_mac
        if not report_id:
            logger.warning(
                "CLOUD_REPORTING enabled for %s but no device id is available; "
                "set CT_MAC, or enable the Marstek account so the registered "
                "device id is used. Cloud reporting disabled.",
                device_id,
            )
        else:
            ct_device: CT002 = device

            async def _cloud_gather(
                _pms: list[tuple[Powermeter, ClientFilter, bool]] = powermeters,
                _dev: CT002 = ct_device,
                _insights: MqttInsightsService | None = insights,
            ) -> CtMeasurement:
                chosen: Powermeter | None = next(
                    (pm for pm, cf, _ in _pms if cf.matches("0.0.0.0")), None
                )
                if chosen is None and _pms:
                    chosen = _pms[0][0]
                phases = [0.0, 0.0, 0.0]
                if chosen is not None:
                    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                        await asyncio.wait_for(
                            chosen.wait_for_next_message(), timeout=2.0
                        )
                    vs = await chosen.get_powermeter_watts_raw()
                    phases = [float(vs[i]) if i < len(vs) else 0.0 for i in range(3)]
                ap, bp, cp = (round(p) for p in phases)
                buckets = _dev.reporting_phase_buckets()

                def _chrg(b: str) -> int:
                    return int(buckets.get(b, {}).get("chrg_power", 0))

                def _dchrg(b: str) -> int:
                    return int(buckets.get(b, {}).get("dchrg_power", 0))

                return CtMeasurement(
                    ap=ap,
                    bp=bp,
                    cp=cp,
                    dp=ap + bp + cp,
                    rssi=_dev.wifi_rssi,
                    slv=_dev.reporting_consumer_count(),
                    udp=1,
                    mqtt=1 if _insights is not None else 0,
                    cz=_chrg("x"),
                    ca=_chrg("A"),
                    cb=_chrg("B"),
                    cc=_chrg("C"),
                    cd=_chrg("ABC"),
                    dz=_dchrg("x"),
                    da=_dchrg("A"),
                    db=_dchrg("B"),
                    dc=_dchrg("C"),
                    dd=_dchrg("ABC"),
                )

            reporter = CloudReporter(
                CloudReporterConfig(
                    ct_type=device.ct_type,
                    device_id=report_id,
                    host=ct.cloud_reporting_host,
                    interval_seconds=ct.cloud_reporting_interval,
                ),
                _cloud_gather,
            )
            if registry is not None:
                registry.cloud_reporters[device_id or ""] = reporter
            cloud_task = asyncio.create_task(reporter.run())

    try:
        await device.wait()
    finally:
        if registry is not None:
            registry.unregister_device(device_id or "")
        if cloud_task is not None:
            cloud_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cloud_task
        if insights and isinstance(device, CT002):
            insights.unregister_handlers(device_id or "")
            with contextlib.suppress(Exception):
                await insights.unregister_marstek(device_id or "")
        try:
            await device.stop()
        except Exception:
            logger.exception("Device %s (%s) failed to stop", device_type, device_id)


async def async_main(
    config: AppConfig,
    general: GeneralSettings,
    device_types: list[str],
    device_ids: list[str],
    skip_test: bool,
    managed_marstek: dict[str, tuple[str, int]] | None = None,
    registry: StatusRegistry | None = None,
):
    managed_marstek = managed_marstek or {}

    powermeters: list[tuple[Powermeter, ClientFilter, bool]] = []
    insights: MqttInsightsService | None = None

    try:
        # Create powermeters
        powermeters = config.powermeters(general)
        if registry is not None:
            registry.powermeters = [pm for pm, _, _ in powermeters]
            registry.bump()

        # Start powermeter lifecycle
        for pm, _, _ in powermeters:
            await pm.start()

        if not skip_test:
            for powermeter, client_filter, _ in powermeters:
                await test_powermeter(powermeter, client_filter)

        # MQTT Insights (optional)
        insights_cfg = config.mqtt_insights()
        if insights_cfg:
            insights = MqttInsightsService(
                insights_cfg, powermeters=[pm for pm, _, _ in powermeters]
            )
            await insights.start()
            logger.info("MQTT Insights service started")
        if registry is not None:
            registry.insights = insights
            registry.bump()

        if not device_types:
            logger.warning("No runnable device types configured after filtering.")
            return

        await asyncio.gather(
            *(
                run_device(
                    device_type,
                    config,
                    general,
                    powermeters,
                    device_id,
                    insights,
                    managed_marstek.get(device_type, ("", None))[0],
                    managed_marstek.get(device_type, ("", None))[1],
                    registry=registry,
                )
                for device_type, device_id in zip(
                    device_types, device_ids, strict=False
                )
            )
        )
    finally:
        # Best-effort shutdown: each resource gets a stop attempt even if
        # an earlier one fails.
        if insights:
            try:
                await insights.stop()
                logger.info("MQTT Insights service stopped")
            except Exception:
                logger.exception("Error stopping MQTT Insights service")
        for pm, _, _ in powermeters:
            try:
                await pm.stop()
            except Exception:
                logger.exception("Error stopping powermeter %s", pm)


def _build_managed_marstek(
    marstek: MarstekSettings, device_types: Sequence[str]
) -> dict[str, tuple[str, int]]:
    """Register managed fake CT devices with Marstek and return the MAC/ver map.

    Called both at startup and after a config-driven restart so the MAC/version
    wiring stays in sync with the (possibly reloaded) config and device_types.
    """
    managed_marstek: dict[str, tuple[str, int]] = {}
    if not marstek.enable:
        return managed_marstek

    if not marstek.mailbox or not marstek.password:
        logger.warning(
            "Marstek auto-registration is enabled, but the mailbox/password is missing; skipping it"
        )
        return managed_marstek

    marstek_cfg = MarstekConfig(
        base_url=marstek.base_url,
        mailbox=marstek.mailbox,
        password=marstek.password,
        timezone=marstek.timezone,
    )
    try:
        any_ct = False
        for dt in ("ct002", "ct003"):
            if dt in device_types:
                any_ct = True
                created = ensure_managed_fake_device(marstek_cfg, dt)
                if created is not None:
                    normalized = normalize_mac(str(created.get("mac", "")))
                    if normalized:
                        managed_marstek[dt] = (
                            normalized,
                            ver_v_from_marstek_api_version(created.get("version")),
                        )
        if any_ct:
            logger.info(
                "Managed fake CT registration completed. Fake CT devices appear as offline in the Marstek app CT list (this is expected)."
            )
            ct_names = []
            if "ct002" in device_types:
                ct_names.append("AstraMeter CT002")
            if "ct003" in device_types:
                ct_names.append("AstraMeter CT003")
            logger.info(
                "Pairing hint: refresh the CT device list (or log out/in if needed), select %s, switch battery mode to Automatic, and choose that CT."
                " The CT should be selectable as soon as it appears in the device list.",
                (" / ".join(ct_names) if ct_names else "the managed AstraMeter CT"),
            )
            logger.info(
                "Credentials are only needed for one-time registration. You can remove MARSTEK mailbox/password from config now."
            )
    except MarstekApiError as exc:
        logger.error("Marstek auto-registration failed: %s", exc, exc_info=True)
    except Exception as exc:
        logger.error(
            "Unexpected Marstek auto-registration error: %s", exc, exc_info=True
        )
    return managed_marstek


def _addon_slug(args: argparse.Namespace) -> str | None:
    """This add-on's slug, so the dashboard can build ingress-relative links."""
    if not args.addon:
        return None
    return addon.SupervisorClient().addon_slug() or None


def _apply_cli_overrides(
    general: GeneralSettings, args: argparse.Namespace
) -> GeneralSettings:
    """Re-apply CLI flags that override configured values."""
    if args.throttle_interval is None:
        return general
    # Also reaches the power sources: they are built from these settings.
    return replace(
        general,
        signal=replace(general.signal, throttle_interval=args.throttle_interval),
    )


def _resolve_device_config(
    config: AppConfig, general: GeneralSettings, args: argparse.Namespace
) -> tuple[list[str], list[str], bool]:
    """Derive device_types, device_ids and skip_test from the config and CLI."""
    device_types = (
        args.device_types
        if args.device_types is not None
        else list(general.device_types)
    )
    skip_test = (
        args.skip_powermeter_test
        if args.skip_powermeter_test is not None
        else general.skip_powermeter_test
    )

    device_ids: list[str] = list(args.device_ids) if args.device_ids is not None else []
    if not device_ids:
        device_ids = list(general.device_ids)
    shelly_id_prefixes = {
        "shellypro3em": "shellypro3em",
        "shellypro3em_old": "shellypro3em",
        "shellypro3em_new": "shellypro3em",
        "shellyemg3": "shellyemg3",
        "shellyproem50": "shellyproem50",
    }
    while len(device_ids) < len(device_types):
        device_type = device_types[len(device_ids)]
        prefix = shelly_id_prefixes.get(device_type)
        if prefix is not None:
            device_ids.append(f"{prefix}-ec4609c439c{len(device_ids) + 1}")
        else:
            device_ids.append(f"device-{len(device_ids) + 1}")

    if "shellypro3em" in device_types:
        shellypro3em_index = device_types.index("shellypro3em")
        device_types[shellypro3em_index] = "shellypro3em_old"
        device_types.append("shellypro3em_new")
        device_ids.append(device_ids[shellypro3em_index])

    ct_ports = [
        config.ct(device_type).udp_port
        for device_type in device_types
        if device_type in ("ct002", "ct003")
    ]
    if len(ct_ports) != len(set(ct_ports)):
        raise ValueError(
            "Multiple CT002/CT003 devices are configured with the same UDP port. "
            "Set UDP_PORT in [CT002]/[CT003] to avoid conflicts."
        )

    logger.info(f"Device Types: {device_types}")
    logger.info(f"Device IDs: {device_ids}")
    logger.info(f"Skip Test: {skip_test}")

    return device_types, device_ids, skip_test


def _load_config(
    args: argparse.Namespace, options: addon.Options | None = None
) -> AppConfig:
    """Pick the configuration backend the command line asks for.

    ``--addon`` takes the settings from the Home Assistant add-on options (and
    the Supervisor); otherwise they come from the ``--config`` file.

    Whatever the backend has to fetch remotely is resolved here, while we are
    still outside the event loop — this runs at startup *and* on a config
    restart, so neither path can leave a blocking lookup to the running loop.
    """
    if args.addon:
        config: AppConfig = addon.load_config(
            addon.load_options() if options is None else options
        )
    else:
        config = IniAppConfig.from_file(args.config)
    config.prefetch()
    return config


def main():
    parser = argparse.ArgumentParser(description="Power meter device emulator")
    parser.add_argument(
        "-c", "--config", default="config.ini", help="Path to the configuration file"
    )
    parser.add_argument(
        "--addon",
        action="store_true",
        help="Run as the Home Assistant add-on: take the configuration from the "
        "add-on options and the Supervisor API instead of a config file",
    )
    parser.add_argument(
        "-t", "--skip-powermeter-test", action="store_true", default=None
    )
    parser.add_argument(
        "-d",
        "--device-types",
        nargs="+",
        choices=[
            "ct002",
            "ct003",
            "shellypro3em",
            "shellyemg3",
            "shellyproem50",
            "shellypro3em_old",
            "shellypro3em_new",
        ],
        help="List of device types to emulate",
    )
    parser.add_argument("--device-ids", nargs="+", help="List of device IDs")
    parser.add_argument(
        "-log",
        "--loglevel",
        default=os.environ.get("LOG_LEVEL", "warning"),
        help="Provide logging level. Example --loglevel debug. Can also be set via LOG_LEVEL env var",
    )

    parser.add_argument(
        "--throttle-interval",
        type=float,
        help="Throttling interval in seconds to prevent control instability",
    )

    args = parser.parse_args()

    # In add-on mode the log level is an add-on option, so the options have to
    # be read before the logger is configured — everything the config backend
    # logs while talking to the Supervisor honours the user's level that way.
    addon_options = addon.load_options() if args.addon else {}
    log_level = str(addon.get_option(addon_options, "log_level", args.loglevel))
    setLogLevel(log_level)
    logger.info("started astrameter application")
    _sha = get_git_commit_sha()
    if _sha:
        logger.info("Git commit: %s", _sha)
    else:
        logger.debug(
            "Git commit not logged (set GIT_COMMIT_SHA at image build for CI images)"
        )

    config = _load_config(args, addon_options)

    if args.addon:
        # Home Assistant is the power source, so give it a chance to finish
        # booting before the first reading is attempted.
        addon.wait_for_home_assistant(addon.SupervisorClient())

    general = _apply_cli_overrides(config.general(), args)
    logger.info("Effective configuration: %s", general)

    registry = StatusRegistry(
        config_path=config.path,
        log_level=log_level,
        version=get_version(),
        git_commit=_sha,
        app_config=config,
        config_mode=detect_config_mode(addon=args.addon, config_path=config.path),
        addon_slug=_addon_slug(args),
        web_port=general.web_server_port,
        dashboard_enabled=general.dashboard,
        allow_write=general.dashboard_allow_write,
        direct_access=general.dashboard_direct_access,
    )

    # Map SIGTERM to KeyboardInterrupt so asyncio.run cancels tasks and
    # runs finally-cleanup the same way it does for SIGINT (Ctrl+C).
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    try:
        asyncio.run(_supervise(config, general, args, registry))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        logger.error("%s", exc)
        exit(1)


async def _supervise(
    config: AppConfig,
    general: GeneralSettings,
    args: argparse.Namespace,
    registry: StatusRegistry,
) -> None:
    """Own the one asyncio loop for the process lifetime.

    The web server is started here, *outside* the per-cycle ``async_main``,
    so the dashboard URL and the add-on watchdog endpoint survive a restart.
    An ``AppRunner`` is bound to the loop that created it, so a long-lived
    server is only possible with exactly one loop per process — which is why
    the restart is an :class:`asyncio.Event` rather than a re-``asyncio.run``.
    """
    loop = asyncio.get_running_loop()
    restart = asyncio.Event()

    def _on_sigusr1() -> None:
        registry.restart_pending = True
        registry.bump()
        restart.set()

    loop.add_signal_handler(signal.SIGUSR1, _on_sigusr1)

    web_server = None
    if general.enable_web_server:
        logger.info("Starting web server...")
        try:
            web_server = WebServer(
                port=general.web_server_port,
                config_path=config.path,
                enable_web_config=general.web_config_enabled,
                status=registry,
            )
            if not await web_server.start():
                logger.error("Failed to start web server")
                web_server = None
        except Exception:
            logger.exception("Web server failed to initialize")
            if web_server:
                await web_server.stop()
            web_server = None

    try:
        while True:
            restart.clear()
            registry.restart_pending = False
            device_types, device_ids, skip_test = _resolve_device_config(
                config, general, args
            )
            # Marstek registration is blocking HTTP with retries; off-loop so a
            # slow or unreachable cloud cannot stall /health for ~40 s.
            managed_marstek = await asyncio.to_thread(
                _build_managed_marstek, config.marstek(), device_types
            )
            registry.managed_marstek = managed_marstek
            registry.bump()

            cycle = asyncio.create_task(
                async_main(
                    config,
                    general,
                    device_types,
                    device_ids,
                    skip_test,
                    managed_marstek,
                    registry,
                )
            )
            waiter = asyncio.create_task(restart.wait())
            try:
                await asyncio.wait({cycle, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

            if cycle.done():
                # A finished cycle means the devices exited on their own; let
                # the exception (if any) propagate as before.
                await cycle
                if not restart.is_set():
                    break

            # Restart requested: unwind this cycle fully before starting the
            # next one, so no device task, powermeter or MQTT client leaks
            # into it.
            logger.info("Restarting service…")
            cycle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cycle
            registry.reset_cycle()

            # Re-read the configuration off-loop: a backend may have to ask
            # the Supervisor for part of it, and prefetch() is blocking.
            #
            # The dashboard is what writes the file this re-reads, so a bad
            # write must not be fatal: letting the error out would stop the
            # web server in the `finally` below and take away the only surface
            # that can repair the configuration. Keep running the last good one.
            try:
                new_config = await asyncio.to_thread(_load_config, args)
                new_general = _apply_cli_overrides(new_config.general(), args)
            except Exception:
                logger.exception(
                    "Could not reload the configuration; "
                    "continuing with the previous one"
                )
            else:
                config, general = new_config, new_general
            # The dashboard can add or remove `custom_config`, so the source
            # the next cycle runs from may not be the one this one used.
            registry.app_config = config
            registry.config_path = config.path
            registry.config_mode = detect_config_mode(
                addon=args.addon, config_path=config.path
            )
            registry.bump()
    finally:
        loop.remove_signal_handler(signal.SIGUSR1)
        if web_server:
            logger.info("Stopping web server...")
            try:
                await asyncio.wait_for(web_server.stop(), timeout=5.0)
            except TimeoutError:
                logger.warning("Web server stop timed out")
            except Exception:
                logger.exception("Error stopping web server")


# end main

if __name__ == "__main__":
    main()
