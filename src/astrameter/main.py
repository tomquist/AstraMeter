import argparse
import asyncio
import configparser
import os
import signal
from collections import OrderedDict

from astrameter.config.config_loader import (
    ClientFilter,
    read_all_powermeter_configs,
    read_mqtt_insights_config,
)
from astrameter.config.logger import logger, setLogLevel
from astrameter.ct002 import CT002, UDP_PORT
from astrameter.health_service import HealthCheckService
from astrameter.marstek_api import (
    MarstekApiError,
    MarstekConfig,
    ensure_managed_fake_device,
)
from astrameter.mqtt_insights import MqttInsightsService
from astrameter.powermeter import Powermeter
from astrameter.shelly import Shelly
from astrameter.version_info import get_git_commit_sha

# CT002/CT003 phase assignment is auto-managed by emulator runtime.


def get_ct_section(device_type: str, cfg: configparser.ConfigParser) -> str:
    section = "CT002"
    if device_type == "ct003" and cfg.has_section("CT003"):
        section = "CT003"
    return section


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
            powermeter_name = powermeter.__class__.__name__
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


async def run_device(
    device_type: str,
    cfg: configparser.ConfigParser,
    args: argparse.Namespace,
    powermeters: list[tuple[Powermeter, ClientFilter]],
    device_id: str | None = None,
    insights: MqttInsightsService | None = None,
):
    logger.debug(f"Starting device: {device_type}")

    device: CT002 | Shelly

    if device_type in ["ct002", "ct003"]:
        ct_section = get_ct_section(device_type, cfg)
        ct_type = "HME-4" if device_type == "ct002" else "HME-3"
        ct_mac = cfg.get(ct_section, "CT_MAC", fallback="")
        ct_udp_port = cfg.getint(ct_section, "UDP_PORT", fallback=UDP_PORT)
        wifi_rssi = cfg.getint(ct_section, "WIFI_RSSI", fallback=-50)
        dedupe_time_window = cfg.getint(ct_section, "DEDUPE_TIME_WINDOW", fallback=0)
        consumer_ttl = cfg.getint(ct_section, "CONSUMER_TTL", fallback=120)
        debug_status = cfg.getboolean(ct_section, "DEBUG_STATUS", fallback=False)
        if os.environ.get("DEBUG_STATUS", "").lower() in ("1", "true", "yes"):
            debug_status = True
        active_control = cfg.getboolean(ct_section, "ACTIVE_CONTROL", fallback=True)
        smooth_target_alpha = cfg.getfloat(
            ct_section, "SMOOTH_TARGET_ALPHA", fallback=0.9
        )
        max_smooth_step = cfg.getint(ct_section, "MAX_SMOOTH_STEP", fallback=0)
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
        balance_deadband = cfg.getint(ct_section, "BALANCE_DEADBAND", fallback=15)
        deadband = cfg.getint(ct_section, "DEADBAND", fallback=20)
        max_correction_per_step = cfg.getint(
            ct_section, "MAX_CORRECTION_PER_STEP", fallback=80
        )
        max_target_step = cfg.getint(ct_section, "MAX_TARGET_STEP", fallback=0)
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
        saturation_decay_factor = cfg.getfloat(
            ct_section, "SATURATION_DECAY_FACTOR", fallback=0.995
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
                "Active control enabled (alpha=%.2f): smooth target + load split%s",
                smooth_target_alpha,
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
            smooth_target_alpha=smooth_target_alpha,
            max_smooth_step=max_smooth_step,
            fair_distribution=fair_distribution,
            balance_gain=balance_gain,
            error_boost_threshold=error_boost_threshold,
            error_boost_max=error_boost_max,
            error_reduce_threshold=error_reduce_threshold,
            balance_deadband=balance_deadband,
            deadband=deadband,
            max_correction_per_step=max_correction_per_step,
            max_target_step=max_target_step,
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
            saturation_decay_factor=saturation_decay_factor,
            device_id=device_id or "",
        )

        async def update_readings(addr, _fields=None, _consumer_id=None):
            powermeter = None
            for pm, client_filter in powermeters:
                if client_filter.matches(addr[0]):
                    powermeter = pm
                    break
            if powermeter is None:
                logger.debug(f"No powermeter found for client {addr[0]}")
                return None
            values = await powermeter.get_powermeter_watts()
            value1 = values[0] if len(values) > 0 else 0
            value2 = values[1] if len(values) > 1 else 0
            value3 = values[2] if len(values) > 2 else 0

            return [value1, value2, value3]

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
        device = Shelly(powermeters=powermeters, device_id=device_id, udp_port=1010)

    elif device_type == "shellypro3em_new":
        logger.debug("Shelly Pro 3EM Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(powermeters=powermeters, device_id=device_id, udp_port=2220)

    elif device_type == "shellyemg3":
        logger.debug("Shelly EM Gen3 Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(powermeters=powermeters, device_id=device_id, udp_port=2222)

    elif device_type == "shellyproem50":
        logger.debug("Shelly Pro EM 50 Settings:")
        logger.debug(f"Device ID: {device_id}")
        device = Shelly(powermeters=powermeters, device_id=device_id, udp_port=2223)

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
        insights.register_rotation_handler(
            device_id or "", device.force_efficiency_rotation
        )

    try:
        await device.wait()
    finally:
        if insights and isinstance(device, CT002):
            insights.unregister_handlers(device_id or "")
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
):
    # Start health check server
    health = None
    if cfg.getboolean("GENERAL", "ENABLE_HEALTH_CHECK", fallback=True):
        logger.info("Starting health check service...")
        try:
            health = HealthCheckService()
            if await health.start():
                logger.info("Health check service started successfully")
            else:
                logger.error("Failed to start health check service")
                health = None
        except Exception:
            logger.exception("Health check service failed to initialize")
            if health:
                await health.stop()
            health = None

    powermeters: list[tuple[Powermeter, ClientFilter]] = []
    insights: MqttInsightsService | None = None

    try:
        # Create powermeters
        powermeters = read_all_powermeter_configs(cfg)

        # Start powermeter lifecycle
        for pm, _ in powermeters:
            await pm.start()

        if not skip_test:
            for powermeter, client_filter in powermeters:
                await test_powermeter(powermeter, client_filter)

        # MQTT Insights (optional)
        insights_cfg = read_mqtt_insights_config(cfg)
        if insights_cfg:
            insights = MqttInsightsService(insights_cfg)
            await insights.start()
            logger.info("MQTT Insights service started")

        if not device_types:
            logger.warning("No runnable device types configured after filtering.")
            return

        await asyncio.gather(
            *(
                run_device(device_type, cfg, args, powermeters, device_id, insights)
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
        for pm, _ in powermeters:
            try:
                await pm.stop()
            except Exception:
                logger.exception("Error stopping powermeter %s", pm)
        if health:
            logger.info("Stopping health check service...")
            try:
                await asyncio.wait_for(health.stop(), timeout=5.0)
            except TimeoutError:
                logger.warning("Health check service stop timed out")
            except Exception:
                logger.exception("Error stopping health check service")


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

    # Load general settings
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

    device_ids = args.device_ids if args.device_ids is not None else []
    # Load device IDs from config if not provided via CLI
    if not device_ids:
        cfg_device_ids = cfg.get("GENERAL", "DEVICE_IDS", fallback="").strip()
        if cfg_device_ids:
            device_ids = [
                did.strip() for did in cfg_device_ids.split(",") if did.strip()
            ]
    # Fill missing device IDs with default format
    while len(device_ids) < len(device_types):
        device_type = device_types[len(device_ids)]
        if device_type in ["shellypro3em", "shellyemg3", "shellyproem50"]:
            device_ids.append(f"{device_type}-ec4609c439c{len(device_ids) + 1}")
        else:
            device_ids.append(f"device-{len(device_ids) + 1}")

    # For backward compatibility, replace shellypro3em with shellypro3em_old and shellypro3em_new
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

    # Apply command line throttling override if specified
    if args.throttle_interval is not None:
        if not cfg.has_section("GENERAL"):
            cfg.add_section("GENERAL")
        cfg.set("GENERAL", "THROTTLE_INTERVAL", str(args.throttle_interval))

    # Optional Marstek cloud registration for managed fake CT devices (sync, before event loop)
    marstek_enabled = cfg.getboolean("MARSTEK", "ENABLE", fallback=False)
    if marstek_enabled:
        mailbox = cfg.get("MARSTEK", "MAILBOX", fallback="")
        password = cfg.get("MARSTEK", "PASSWORD", fallback="")
        base_url = cfg.get("MARSTEK", "BASE_URL", fallback="https://eu.hamedata.com")
        timezone_name = cfg.get("MARSTEK", "TIMEZONE", fallback="Europe/Berlin")

        if not mailbox or not password:
            logger.warning(
                "MARSTEK.ENABLE is true, but MAILBOX/PASSWORD missing; skipping fake-device auto-registration"
            )
        else:
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
                        ensure_managed_fake_device(marstek_cfg, dt)
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
                        (
                            " / ".join(ct_names)
                            if ct_names
                            else "the managed AstraMeter CT"
                        ),
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

    # Map SIGTERM to KeyboardInterrupt so asyncio.run cancels tasks and
    # runs finally-cleanup the same way it does for SIGINT (Ctrl+C).
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    try:
        asyncio.run(async_main(cfg, args, device_types, device_ids, skip_test))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        logger.error("%s", exc)
        exit(1)


# end main

if __name__ == "__main__":
    main()
