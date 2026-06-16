import argparse
import asyncio
import configparser
import contextlib
import os
import signal
from collections import OrderedDict
from collections.abc import Sequence

from astrameter.config.config_loader import (
    ClientFilter,
    read_all_powermeter_configs,
    read_mqtt_insights_config,
)
from astrameter.config.logger import logger, setLogLevel
from astrameter.ct002 import CT002, UDP_PORT
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
from astrameter.version_info import get_git_commit_sha
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


def get_ct_section(device_type: str, cfg: configparser.ConfigParser) -> str:
    section = "CT002"
    if device_type == "ct003" and cfg.has_section("CT003"):
        section = "CT003"
    return section


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


async def run_device(
    device_type: str,
    cfg: configparser.ConfigParser,
    args: argparse.Namespace,
    powermeters: list[tuple[Powermeter, ClientFilter, bool]],
    device_id: str | None = None,
    insights: MqttInsightsService | None = None,
    marstek_mac: str = "",
    marstek_ver_v: int | None = None,
):
    logger.debug(f"Starting device: {device_type}")

    device: CT002 | Shelly

    global_dedupe_time_window = cfg.getfloat(
        "GENERAL", "DEDUPE_TIME_WINDOW", fallback=0.0
    )

    if device_type in ["ct002", "ct003"]:
        ct_section = get_ct_section(device_type, cfg)
        ct_type = "HME-4" if device_type == "ct002" else "HME-3"
        ct_mac = cfg.get(ct_section, "CT_MAC", fallback="")
        ct_udp_port = cfg.getint(ct_section, "UDP_PORT", fallback=UDP_PORT)
        wifi_rssi = cfg.getint(ct_section, "WIFI_RSSI", fallback=-50)
        dedupe_time_window = cfg.getfloat(
            ct_section, "DEDUPE_TIME_WINDOW", fallback=global_dedupe_time_window
        )
        # Unset (default) → adaptive eviction (~2 missed poll cycles, like the
        # real CT); a number → fixed TTL in seconds.
        consumer_ttl = cfg.getint(ct_section, "CONSUMER_TTL", fallback=None)
        debug_status = cfg.getboolean(ct_section, "DEBUG_STATUS", fallback=False)
        if os.environ.get("DEBUG_STATUS", "").lower() in ("1", "true", "yes"):
            debug_status = True
        active_control = cfg.getboolean(ct_section, "ACTIVE_CONTROL", fallback=True)
        fair_distribution = cfg.getboolean(
            ct_section, "FAIR_DISTRIBUTION", fallback=True
        )
        balance_gain = cfg.getfloat(ct_section, "BALANCE_GAIN", fallback=0.2)
        error_boost_threshold = cfg.getint(
            ct_section, "ERROR_BOOST_THRESHOLD", fallback=150
        )
        error_boost_max = cfg.getfloat(ct_section, "ERROR_BOOST_MAX", fallback=0.5)
        error_reduce_threshold = cfg.getint(
            ct_section, "ERROR_REDUCE_THRESHOLD", fallback=20
        )
        balance_deadband = cfg.getint(ct_section, "BALANCE_DEADBAND", fallback=25)
        max_correction_per_step = cfg.getint(
            ct_section, "MAX_CORRECTION_PER_STEP", fallback=80
        )
        max_target_step = cfg.getint(ct_section, "MAX_TARGET_STEP", fallback=0)
        pace_base_step = cfg.getint(ct_section, "PACE_BASE_STEP", fallback=30)
        pace_max_step = cfg.getint(ct_section, "PACE_MAX_STEP", fallback=100)
        osc_damp_max = cfg.getfloat(ct_section, "OSC_DAMP_MAX", fallback=0.95)
        osc_damp_alpha = cfg.getfloat(ct_section, "OSC_DAMP_ALPHA", fallback=0.3)
        osc_damp_decay = cfg.getfloat(ct_section, "OSC_DAMP_DECAY", fallback=0.05)
        osc_damp_threshold = cfg.getfloat(
            ct_section, "OSC_DAMP_THRESHOLD", fallback=300
        )
        grid_predict_trust = cfg.getfloat(
            ct_section, "GRID_PREDICT_TRUST", fallback=0.5
        )
        concentrate_deadband = cfg.getfloat(
            ct_section, "CONCENTRATE_DEADBAND", fallback=60.0
        )
        import_trim_w = cfg.getfloat(ct_section, "IMPORT_TRIM_W", fallback=15.0)
        saturation_detection = cfg.getboolean(
            ct_section, "SATURATION_DETECTION", fallback=True
        )
        saturation_alpha = cfg.getfloat(ct_section, "SATURATION_ALPHA", fallback=0.15)
        min_target_for_saturation = cfg.getint(
            ct_section, "MIN_TARGET_FOR_SATURATION", fallback=20
        )
        saturation_grace_seconds = cfg.getfloat(
            ct_section, "SATURATION_GRACE_SECONDS", fallback=90
        )
        saturation_stall_timeout_seconds = cfg.getfloat(
            ct_section, "SATURATION_STALL_TIMEOUT_SECONDS", fallback=60
        )
        min_efficient_power = cfg.getint(ct_section, "MIN_EFFICIENT_POWER", fallback=0)
        probe_min_power = cfg.getint(ct_section, "PROBE_MIN_POWER", fallback=80)
        efficiency_rotation_interval = cfg.getint(
            ct_section, "EFFICIENCY_ROTATION_INTERVAL", fallback=900
        )
        efficiency_fade_alpha = cfg.getfloat(
            ct_section, "EFFICIENCY_FADE_ALPHA", fallback=0.15
        )
        efficiency_saturation_threshold = cfg.getfloat(
            ct_section, "EFFICIENCY_SATURATION_THRESHOLD", fallback=0.4
        )
        efficiency_demand_alpha = cfg.getfloat(
            ct_section, "EFFICIENCY_DEMAND_ALPHA", fallback=0.1
        )
        saturation_decay_factor = cfg.getfloat(
            ct_section, "SATURATION_DECAY_FACTOR", fallback=0.995
        )
        min_dc_output = cfg.getfloat(ct_section, "MIN_DC_OUTPUT", fallback=0.0)
        if 0 < min_dc_output < min_target_for_saturation:
            logger.warning(
                "MIN_DC_OUTPUT (%gW) is below MIN_TARGET_FOR_SATURATION (%dW): a "
                "floored battery's target never clears the saturation gate, so an "
                "empty/full unit can't be detected. Consider MIN_DC_OUTPUT >= %d.",
                min_dc_output,
                min_target_for_saturation,
                min_target_for_saturation,
            )

        logger.debug(f"{device_type.upper()} Settings for {device_id}:")
        logger.debug(f"CT Type: {ct_type}")
        logger.debug(f"CT MAC: {ct_mac}")
        logger.debug(f"CT UDP Port: {ct_udp_port}")
        logger.debug(f"WiFi RSSI: {wifi_rssi}")
        logger.debug(
            "CT control model: %s",
            (
                "active control (emulator computes targets)"
                if active_control
                else "relay (forward consumer aggregates)"
            ),
        )
        if active_control:
            extras = []
            if fair_distribution:
                extras.append("fair distribution")
            if saturation_detection:
                extras.append("saturation detection")
            if min_efficient_power > 0:
                extras.append(f"efficiency optimization ({min_efficient_power}W)")
            logger.info(
                "Active control enabled: load split%s",
                " + " + " + ".join(extras) if extras else "",
            )

        device = CT002(
            udp_port=ct_udp_port,
            ct_type=ct_type,
            ct_mac=ct_mac,
            wifi_rssi=wifi_rssi,
            dedupe_time_window=dedupe_time_window,
            consumer_ttl=consumer_ttl,
            debug_status=debug_status,
            active_control=active_control,
            fair_distribution=fair_distribution,
            balance_gain=balance_gain,
            error_boost_threshold=error_boost_threshold,
            error_boost_max=error_boost_max,
            error_reduce_threshold=error_reduce_threshold,
            balance_deadband=balance_deadband,
            max_correction_per_step=max_correction_per_step,
            max_target_step=max_target_step,
            pace_base_step=pace_base_step,
            pace_max_step=pace_max_step,
            osc_damp_max=osc_damp_max,
            osc_damp_alpha=osc_damp_alpha,
            osc_damp_decay=osc_damp_decay,
            osc_damp_threshold=osc_damp_threshold,
            grid_predict_trust=grid_predict_trust,
            concentrate_deadband=concentrate_deadband,
            import_trim_w=import_trim_w,
            saturation_detection=saturation_detection,
            saturation_alpha=saturation_alpha,
            min_target_for_saturation=min_target_for_saturation,
            saturation_grace_seconds=saturation_grace_seconds,
            saturation_stall_timeout_seconds=saturation_stall_timeout_seconds,
            min_efficient_power=min_efficient_power,
            probe_min_power=probe_min_power,
            efficiency_rotation_interval=efficiency_rotation_interval,
            efficiency_fade_alpha=efficiency_fade_alpha,
            efficiency_saturation_threshold=efficiency_saturation_threshold,
            efficiency_demand_alpha=efficiency_demand_alpha,
            min_dc_output=min_dc_output,
            saturation_decay_factor=saturation_decay_factor,
            device_id=device_id or "",
            reset_fn=lambda: _reset_all_powermeters(powermeters),
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
            udp_port=1010,
            dedupe_time_window=global_dedupe_time_window,
        )

    elif device_type == "shellypro3em_new":
        logger.debug("Shelly Pro 3EM Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(
            powermeters=powermeters,
            device_id=device_id,
            udp_port=2220,
            dedupe_time_window=global_dedupe_time_window,
        )

    elif device_type == "shellyemg3":
        logger.debug("Shelly EM Gen3 Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(
            powermeters=powermeters,
            device_id=device_id,
            udp_port=2222,
            dedupe_time_window=global_dedupe_time_window,
        )

    elif device_type == "shellyproem50":
        logger.debug("Shelly Pro EM 50 Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(
            powermeters=powermeters,
            device_id=device_id,
            udp_port=2223,
            dedupe_time_window=global_dedupe_time_window,
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

    try:
        await device.wait()
    finally:
        if insights and isinstance(device, CT002):
            insights.unregister_handlers(device_id or "")
            with contextlib.suppress(Exception):
                await insights.unregister_marstek(device_id or "")
        try:
            await device.stop()
        except Exception:
            logger.exception("Device %s (%s) failed to stop", device_type, device_id)


async def async_main(
    cfg: configparser.ConfigParser,
    args: argparse.Namespace,
    device_types: list[str],
    device_ids: list[str],
    skip_test: bool,
    managed_marstek: dict[str, tuple[str, int]] | None = None,
):
    managed_marstek = managed_marstek or {}
    web_server = None
    if cfg.getboolean("GENERAL", "ENABLE_WEB_SERVER", fallback=True):
        logger.info("Starting web server...")
        try:
            enable_web_config = cfg.getboolean(
                "GENERAL", "WEB_CONFIG_ENABLED", fallback=False
            )
            port = cfg.getint("GENERAL", "WEB_SERVER_PORT", fallback=52500)
            web_server = WebServer(
                port=port,
                config_path=args.config,
                enable_web_config=enable_web_config,
            )
            if await web_server.start():
                logger.info("Web server started successfully")
            else:
                logger.error("Failed to start web server")
                web_server = None
        except Exception:
            logger.exception("Web server failed to initialize")
            if web_server:
                await web_server.stop()
            web_server = None

    powermeters: list[tuple[Powermeter, ClientFilter, bool]] = []
    insights: MqttInsightsService | None = None

    try:
        # Create powermeters
        powermeters = read_all_powermeter_configs(cfg)

        # Start powermeter lifecycle
        for pm, _, _ in powermeters:
            await pm.start()

        if not skip_test:
            for powermeter, client_filter, _ in powermeters:
                await test_powermeter(powermeter, client_filter)

        # MQTT Insights (optional)
        insights_cfg = read_mqtt_insights_config(cfg)
        if insights_cfg:
            insights = MqttInsightsService(
                insights_cfg, powermeters=[pm for pm, _, _ in powermeters]
            )
            await insights.start()
            logger.info("MQTT Insights service started")

        if not device_types:
            logger.warning("No runnable device types configured after filtering.")
            return

        await asyncio.gather(
            *(
                run_device(
                    device_type,
                    cfg,
                    args,
                    powermeters,
                    device_id,
                    insights,
                    *managed_marstek.get(device_type, ("", None)),
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
        if web_server:
            logger.info("Stopping web server...")
            try:
                await asyncio.wait_for(web_server.stop(), timeout=5.0)
            except TimeoutError:
                logger.warning("Web server stop timed out")
            except Exception:
                logger.exception("Error stopping web server")


def _build_managed_marstek(
    cfg: configparser.ConfigParser, device_types: Sequence[str]
) -> dict[str, tuple[str, int]]:
    """Register managed fake CT devices with Marstek and return the MAC/ver map.

    Called both at startup and after a config-driven restart so the MAC/version
    wiring stays in sync with the (possibly reloaded) config and device_types.
    """
    managed_marstek: dict[str, tuple[str, int]] = {}
    if not cfg.getboolean("MARSTEK", "ENABLE", fallback=False):
        return managed_marstek

    mailbox = cfg.get("MARSTEK", "MAILBOX", fallback="")
    password = cfg.get("MARSTEK", "PASSWORD", fallback="")
    base_url = cfg.get("MARSTEK", "BASE_URL", fallback="https://eu.hamedata.com")
    timezone_name = cfg.get("MARSTEK", "TIMEZONE", fallback="Europe/Berlin")

    if not mailbox or not password:
        logger.warning(
            "MARSTEK.ENABLE is true, but MAILBOX/PASSWORD missing; skipping fake-device auto-registration"
        )
        return managed_marstek

    marstek_cfg = MarstekConfig(
        base_url=base_url,
        mailbox=mailbox,
        password=password,
        timezone=timezone_name,
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


def _apply_cli_overrides(
    cfg: configparser.ConfigParser, args: argparse.Namespace
) -> None:
    """Re-apply CLI flags that override config-file values."""
    if args.throttle_interval is not None:
        if not cfg.has_section("GENERAL"):
            cfg.add_section("GENERAL")
        cfg.set("GENERAL", "THROTTLE_INTERVAL", str(args.throttle_interval))


def _resolve_device_config(
    cfg: configparser.ConfigParser, args: argparse.Namespace
) -> tuple[list[str], list[str], bool]:
    """Derive device_types, device_ids and skip_test from *cfg* and CLI *args*."""
    device_types = (
        args.device_types
        if args.device_types is not None
        else [
            dt.strip()
            for dt in cfg.get("GENERAL", "DEVICE_TYPE", fallback="shellypro3em").split(
                ","
            )
            if dt.strip()
        ]
    )
    skip_test = (
        args.skip_powermeter_test
        if args.skip_powermeter_test is not None
        else cfg.getboolean("GENERAL", "SKIP_POWERMETER_TEST", fallback=False)
    )

    device_ids: list[str] = list(args.device_ids) if args.device_ids is not None else []
    if not device_ids:
        cfg_device_ids = cfg.get("GENERAL", "DEVICE_IDS", fallback="").strip()
        if cfg_device_ids:
            device_ids = [
                did.strip() for did in cfg_device_ids.split(",") if did.strip()
            ]
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

    ct_ports = []
    for device_type in device_types:
        if device_type in ["ct002", "ct003"]:
            section = get_ct_section(device_type, cfg)
            ct_ports.append(cfg.getint(section, "UDP_PORT", fallback=UDP_PORT))
    if len(ct_ports) != len(set(ct_ports)):
        raise ValueError(
            "Multiple CT002/CT003 devices are configured with the same UDP port. "
            "Set UDP_PORT in [CT002]/[CT003] to avoid conflicts."
        )

    logger.info(f"Device Types: {device_types}")
    logger.info(f"Device IDs: {device_ids}")
    logger.info(f"Skip Test: {skip_test}")

    return device_types, device_ids, skip_test


def main():
    parser = argparse.ArgumentParser(description="Power meter device emulator")
    parser.add_argument(
        "-c", "--config", default="config.ini", help="Path to the configuration file"
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
    # Disable interpolation so literal '%' in credentials (e.g. MARSTEK.PASSWORD)
    # is read as-is from config.ini.
    cfg = configparser.ConfigParser(dict_type=OrderedDict, interpolation=None)
    cfg.read(args.config)

    # configure logger
    setLogLevel(args.loglevel)
    logger.info("started astrameter application")
    _sha = get_git_commit_sha()
    if _sha:
        logger.info("Git commit: %s", _sha)
    else:
        logger.debug(
            "Git commit not logged (set GIT_COMMIT_SHA at image build for CI images)"
        )

    device_types, device_ids, skip_test = _resolve_device_config(cfg, args)

    _apply_cli_overrides(cfg, args)

    # Optional Marstek cloud registration for managed fake CT devices (sync, before event loop).
    # When registration succeeds, the returned MAC is captured per device
    # type so the Marstek MQTT responder in MQTT Insights uses the same
    # MAC that hame-relay will route back to the Marstek app.
    managed_marstek = _build_managed_marstek(cfg, device_types)

    # Map SIGTERM to KeyboardInterrupt so asyncio.run cancels tasks and
    # runs finally-cleanup the same way it does for SIGINT (Ctrl+C).
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    # SIGUSR1 is used by the web UI restart button.  We set a flag *before*
    # raising KeyboardInterrupt so the outer loop knows to re-run instead of
    # exiting.
    restart_requested = False

    def _restart_handler(signum, frame):
        nonlocal restart_requested
        restart_requested = True
        signal.default_int_handler(signum, frame)

    signal.signal(signal.SIGUSR1, _restart_handler)

    while True:
        restart_requested = False
        try:
            asyncio.run(
                async_main(
                    cfg, args, device_types, device_ids, skip_test, managed_marstek
                )
            )
            break  # clean exit
        except KeyboardInterrupt:
            if not restart_requested:
                break
            logger.info("Restarting service…")
            cfg = configparser.ConfigParser(dict_type=OrderedDict, interpolation=None)
            cfg.read(args.config)
            _apply_cli_overrides(cfg, args)
            device_types, device_ids, skip_test = _resolve_device_config(cfg, args)
            managed_marstek = _build_managed_marstek(cfg, device_types)
        except RuntimeError as exc:
            logger.error("%s", exc)
            exit(1)


# end main

if __name__ == "__main__":
    main()
