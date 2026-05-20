//! AstraMeter ESP32-S3 entry point.
//!
//! On non-espidf targets this binary is a stub; build the real firmware
//! with:
//!
//! ```text
//! cargo +esp build --release -p astrameter-esp32 \
//!     --target xtensa-esp32s3-espidf
//! ```
//!
//! The boot sequence (espidf target):
//!   1. `esp_idf_svc::sys::link_patches()` + log bridge.
//!   2. Open NVS for config + Wi-Fi credentials. (Custom-partition path
//!      for a separate SPIFFS / LittleFS image needs partitions.csv to
//!      actually reach the IDF build — esp-idf-sys doesn't auto-copy
//!      it — so we keep config in NVS instead, which is always present
//!      in the default ESP-IDF partition layout.)
//!   3. Bring up Wi-Fi STA from `wifi.{ssid,password}` NVS keys. Boot
//!      continues even on failure so the user sees the next log line
//!      and can fix the credentials over serial or via OTA.
//!   4. Start SNTP and wait until at least one timestamp arrives.
//!   5. Build the `Platform`, instantiate the `PowermeterRegistry`, and
//!      drive the same emulator / insights / Marstek wiring the host
//!      binary uses (just without axum — there's no HTTP server on this
//!      build yet).

#[cfg(not(target_os = "espidf"))]
fn main() {
    eprintln!(
        "astrameter-esp32 {} is a stub on host targets. Build the firmware with:\n\
         \n\
         \x20   cargo +esp build -p astrameter-esp32 --target xtensa-esp32s3-espidf --release\n\
         \n\
         (Requires `espup install` for the Xtensa toolchain.)",
        astrameter_core::VERSION
    );
}

#[cfg(target_os = "espidf")]
fn main() -> anyhow::Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();
    // Bridge tracing events to the `log` crate so EspLogger captures
    // them. Crates like `insights-mqtt` use `tracing::info!` / `warn!`
    // for their runtime diagnostics, and without a Subscriber installed
    // those events vanish — leaving the firmware silent about MQTT
    // connect failures, event-handling errors, etc.
    let _ = tracing::subscriber::set_global_default(TracingToLog);
    log::info!("AstraMeter ESP32 {} booting", astrameter_core::VERSION);
    log_task_handle("main (app_main)");

    log_heap("boot, before worker spawn");

    // The ESP-IDF "main" task only gets ~3.5 KB of stack by default, so
    // we run the tokio runtime on a worker pthread we control. 64 KB
    // covers the deepest call chain we've observed now that the heavy
    // mbedTLS init has been moved off the worker into a PSRAM-stack
    // FreeRTOS task (see `mqtt_impl.rs`). IDF v5.2.3's pthread layer
    // allocates pthread stacks from internal SRAM regardless of
    // `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY` (that option only
    // affects `xTaskCreateWithCaps`, not pthread_create), so every KB
    // here comes out of the ~280 KiB internal heap that also has to
    // host Wi-Fi/lwIP, mbedTLS, esp_http_server, and tokio's
    // blocking-pool pthread stacks.
    let worker = std::thread::Builder::new()
        .name("astrameter".into())
        .stack_size(64 * 1024)
        .spawn(|| -> anyhow::Result<()> {
            log_task_handle("worker (astrameter)");
            log_heap("worker entered");
            log::info!("step: build tokio runtime");
            // Tokio's IO driver (mio → epoll) doesn't initialise on
            // ESP-IDF — `enable_io()` returns
            // `Permission denied (os error 13)`. Build the runtime with
            // `enable_time()` only. Network sockets go through
            // `platform-espidf::net_impl`'s blocking-`std::net` +
            // `spawn_blocking` path so the lack of an IO driver doesn't
            // break the emulators.
            // `thread_stack_size` sets the stack size for tokio's
            // blocking-pool pthreads. Without this, they inherit
            // CONFIG_PTHREAD_TASK_STACK_DEFAULT (3 KB by IDF default),
            // which is too tight for std::net::* syscalls via Rust's
            // newlib stubs.
            //
            // `max_blocking_threads` caps the pool so we can't exhaust
            // internal RAM by spawning unbounded pthreads. The default
            // is 512, which on a chip with ~280 KiB of internal RAM is
            // a footgun — `pthread_create` fails with ENOMEM the
            // moment several concurrent `spawn_blocking` calls land
            // (e.g. CT002 UDP recv parked + Marstek HTTP request +
            // UDP send). 4 slots × 12 KiB = 48 KiB ceiling covers the
            // long-lived UDP recv loops plus a couple of transient
            // HTTP/UART calls.
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_time()
                .thread_stack_size(12 * 1024)
                .max_blocking_threads(4)
                .build()
                .map_err(|e| anyhow::anyhow!("tokio runtime build: {e}"))?;
            log::info!("step: enter async_main");
            let result = runtime.block_on(async_main());
            if let Err(e) = &result {
                log::error!("async_main exited: {e:?}");
            }
            result
        })
        .map_err(|e| anyhow::anyhow!("spawn worker thread: {e}"))?;
    // `join()` blocks the main task forever; that's intentional —
    // returning from `app_main` would cause ESP-IDF to consider the
    // app finished.
    worker
        .join()
        .map_err(|_| anyhow::anyhow!("worker thread panicked"))?
}

#[cfg(target_os = "espidf")]
async fn async_main() -> anyhow::Result<()> {
    use std::sync::Arc;
    use std::time::Duration;

    use astrameter_config::Config;
    use astrameter_emulator_ct002::server::{BoundMeter as Ct002BoundMeter, Ct002Emulator};
    use astrameter_emulator_shelly::{BoundMeter as ShellyBoundMeter, ShellyEmulator};
    use astrameter_powermeters::{read_all_powermeter_configs, register_all, PowermeterRegistry};
    use esp_idf_svc::eventloop::EspSystemEventLoop;
    use esp_idf_svc::nvs::EspDefaultNvsPartition;

    log::info!("step: take system event loop");
    let sysloop =
        EspSystemEventLoop::take().map_err(|e| anyhow::anyhow!("EspSystemEventLoop::take: {e}"))?;

    log::info!("step: take NVS partition");
    let nvs_part = EspDefaultNvsPartition::take()
        .map_err(|e| anyhow::anyhow!("EspDefaultNvsPartition::take: {e}"))?;

    log::info!("step: load config from NVS");
    let cfg_raw = load_config_from_nvs(nvs_part.clone())?;
    let config = Config::parse(&cfg_raw).map_err(|e| anyhow::anyhow!("parse config: {e}"))?;
    log::info!("step: config loaded ({} bytes)", cfg_raw.len());

    log::info!("step: bring up Wi-Fi / lwIP");
    // `bring_up_wifi` always initialises the lwIP TCP/IP stack and
    // brings the Wi-Fi driver up in IDLE state, even when no SSID is
    // provisioned. That's required: the CT002 / Shelly emulators bind
    // UDP sockets which would otherwise hit
    // `assert failed: tcpip_send_msg_wait_sem ... (Invalid mbox)`.
    // Association is best-effort — `Ok(true)` means we associated,
    // `Ok(false)` means stack-up-but-offline.
    let online = match bring_up_wifi(&sysloop, nvs_part.clone()) {
        Ok(b) => b,
        Err(e) => {
            log::error!("Wi-Fi / lwIP bring-up failed: {e}. Continuing without network.");
            false
        }
    };
    if online {
        log::info!("step: Wi-Fi up; starting SNTP");
        if let Err(e) = start_sntp_and_wait_for_sync().await {
            log::warn!("SNTP sync skipped: {e}");
        }
    }

    log::info!("step: build platform");
    let platform = Arc::new(astrameter_platform_espidf::build_platform());

    // If the user configured an [SML] section, install the ESP-IDF
    // UART driver they asked for and register it in `serial_impl`'s
    // registry. The SML powermeter looks up the registry by name
    // (`SERIAL` key, e.g. "UART1"). Pin numbers come from the same
    // section so users don't have to recompile to change hardware.
    if let Some(sml) = config.section("SML") {
        let uart_index = sml.get_int("SML_UART_INDEX", 1).unwrap_or(1) as u8;
        let baud = sml.get_int("BAUD_RATE", 9600).unwrap_or(9600) as u32;
        let rx_gpio = sml.get_int("SML_RX_GPIO", -1).unwrap_or(-1) as i32;
        let tx_gpio = sml.get_int("SML_TX_GPIO", -1).unwrap_or(-1) as i32;
        if rx_gpio < 0 {
            log::warn!(
                "[SML] SML_RX_GPIO not set — SML UART not initialised. \
                 Add `SML_RX_GPIO=<pin>` (and optionally `SML_TX_GPIO`, \
                 `SML_UART_INDEX`, `BAUD_RATE`) to enable."
            );
        } else {
            match astrameter_platform_espidf::build_uart_driver(uart_index, baud, rx_gpio, tx_gpio)
            {
                Ok(uart) => {
                    let name = format!("UART{uart_index}");
                    astrameter_platform_espidf::register_uart(&name, uart);
                    let configured = sml.get_string("SERIAL", "");
                    if configured.is_empty() {
                        log::warn!(
                            "[SML] SERIAL is empty — set `SERIAL={name}` to match the \
                             UART we just initialised"
                        );
                    } else if !configured.eq_ignore_ascii_case(&name) {
                        log::warn!(
                            "[SML] SERIAL={configured:?} but the UART we initialised \
                             is `{name}` — SML open will fail unless these match"
                        );
                    }
                }
                Err(e) => log::error!("[SML] UART init failed: {e}"),
            }
        }
    }

    log::info!("step: register powermeters");
    let mut reg = PowermeterRegistry::new();
    register_all(&mut reg);
    let registry = Arc::new(reg);

    log::info!("step: bind powermeters from config");
    let bound = read_all_powermeter_configs(&config, &registry, platform.clone())
        .map_err(|e| anyhow::anyhow!("bind powermeters: {e}"))?;
    // NOTE: meter `start()` is deferred until after MQTT Insights has
    // been initialised — see the comment above `start_mqtt_insights`
    // for why.

    let device_type = config
        .section("GENERAL")
        .and_then(|s| s.get_opt_string("DEVICE_TYPE"))
        .unwrap_or_else(|| "ct002".to_string())
        .to_lowercase();
    let global_dedupe = config
        .section("GENERAL")
        .map(|s| s.get_float("DEDUPE_TIME_WINDOW", 0.0))
        .unwrap_or(Ok(0.0))
        .unwrap_or(0.0);

    log_heap("before emulator");
    log::info!("step: start emulator (DEVICE_TYPE={device_type})");
    let mut ct002_for_handlers: Option<Arc<Ct002Emulator>> = None;
    let mut shelly_for_listeners: Vec<Arc<ShellyEmulator>> = Vec::new();
    let _emu = match device_type.as_str() {
        "ct002" | "ct003" => {
            let section_name = if device_type == "ct003" && config.section("CT003").is_some() {
                "CT003"
            } else {
                "CT002"
            };
            match config.section(section_name) {
                Some(section) => {
                    let udp_port = section.get_int("UDP_PORT", 12345)? as u16;
                    let ct_mac = section.get_string("CT_MAC", "");
                    // Resolve device_id the same way the host supervisor
                    // does: honour the per-section `DEVICE_ID` override,
                    // else the first entry of `[GENERAL].DEVICE_IDS`,
                    // else `device-1` (Python parity — the HA Discovery
                    // node_id is derived from this, so an empty value
                    // produces a broken topic like
                    // `homeassistant/device/astrameter_ct002_/config`).
                    let device_id = section
                        .get_opt_string("DEVICE_ID")
                        .or_else(|| {
                            config
                                .section("GENERAL")
                                .and_then(|s| s.get_opt_string("DEVICE_IDS"))
                                .and_then(|v| {
                                    v.split(',')
                                        .map(|s| s.trim().to_string())
                                        .find(|s| !s.is_empty())
                                })
                        })
                        .unwrap_or_else(|| "device-1".to_string());
                    let meters: Vec<Ct002BoundMeter> = bound
                        .iter()
                        .map(|bp| Ct002BoundMeter {
                            meter: bp.meter.clone(),
                            filter: bp.client_filter.clone(),
                            wait_for_next: bp.wait_for_next_message,
                        })
                        .collect();
                    let settings = astrameter_emulator_ct002::server::Ct002Settings {
                        ct_mac,
                        ct_type: if device_type == "ct003" {
                            "HME-3".to_string()
                        } else {
                            "HME-4".to_string()
                        },
                        ..Default::default()
                    };
                    let emu = Arc::new(Ct002Emulator::with_settings(
                        udp_port,
                        device_id,
                        settings,
                        astrameter_emulator_ct002::balancer::BalancerConfig::default(),
                        meters,
                        platform.clone(),
                    ));
                    emu.start().await?;
                    ct002_for_handlers = Some(emu.clone());
                    Some(EsplEmu::Ct002(emu))
                }
                None => {
                    log::warn!("[{section_name}] missing — emulator not started");
                    None
                }
            }
        }
        s if s.starts_with("shelly") => {
            let port = match s {
                "shellypro3em_old" => 1010,
                "shellypro3em" | "shellypro3em_new" => 2220,
                "shellyemg3" => 2222,
                "shellyproem50" => 2223,
                _ => 2220,
            };
            let meters: Vec<ShellyBoundMeter> = bound
                .iter()
                .map(|bp| ShellyBoundMeter {
                    meter: bp.meter.clone(),
                    filter: bp.client_filter.clone(),
                    wait_for_next: bp.wait_for_next_message,
                })
                .collect();
            let emu = Arc::new(ShellyEmulator::new(
                port,
                s.to_string(),
                meters,
                Duration::from_secs_f64(global_dedupe.max(0.0)),
                platform.clone(),
            ));
            emu.start().await?;
            shelly_for_listeners.push(emu.clone());
            Some(EsplEmu::Shelly(emu))
        }
        other => {
            log::warn!("DEVICE_TYPE={other:?} not recognised; no emulator started");
            None
        }
    };

    // MQTT Insights runs BEFORE we kick off powermeter `start()`s
    // because `EspAsyncMqttClient::new` needs a 128 KB sacrificial
    // pthread for its mbedTLS context init (see `mqtt_impl.rs`), and
    // once the HomeAssistant powermeter's WebSocket task is up,
    // internal SRAM no longer has a contiguous 128 KB block free.
    // ESP-IDF v5.2.3 has no API to put pthread stacks in PSRAM, so
    // we order the startup instead.
    let _insights = match start_mqtt_insights(
        &config,
        &device_type,
        &bound,
        ct002_for_handlers.clone(),
        &shelly_for_listeners,
        platform.clone(),
    )
    .await
    {
        Ok(svc) => svc,
        Err(e) => {
            log::warn!("MQTT Insights not started: {e}");
            None
        }
    };

    // Marstek cloud auto-register (matches host main.rs:572-607). Spawn
    // as a detached task so a slow/failing HTTPS round-trip doesn't
    // block the supervisor.
    spawn_marstek_registration(&config, &device_type, platform.clone());

    // Now that MQTT init is done (and its 128 KB temp pthread is
    // joined + reclaimed), bring up the powermeters. Push-based
    // meters like HomeAssistant/HomeWizard each open their own
    // WebSocket task here; doing that before MQTT init would
    // fragment internal SRAM past the point where the 128 KB
    // sacrificial stack fits.
    log_heap("before powermeter start");
    log::info!("step: start powermeters");
    for bp in &bound {
        if let Err(e) = bp.meter.start().await {
            log::warn!("powermeter [{}] start: {e}", bp.section);
        }
    }

    // Web config editor — serves the config.ini editor + Wi-Fi reset at
    // http://<sta-ip>/ on the AP's STA-side address (e.g.
    // http://192.168.178.90/ in the user's network). Uses raw TCP for
    // the same reason as the captive portal (esp-idf-svc's httpd has a
    // 512-byte header limit that real browsers blow past instantly).
    if online {
        let cfg_nvs = nvs_part.clone();
        std::thread::Builder::new()
            .name("config-web".into())
            .stack_size(16 * 1024)
            .spawn(move || run_config_web_server(cfg_nvs))
            .map_err(|e| anyhow::anyhow!("spawn config web thread: {e}"))?;
        log::info!("astrameter-esp32: config web UI at http://<this-device>/ (port 80)");
    }

    log::info!("astrameter-esp32: services running");
    loop {
        tokio::time::sleep(Duration::from_secs(60)).await;
    }
}

#[cfg(target_os = "espidf")]
// Each variant holds the Arc<Emulator> alive for the lifetime of the
// supervisor; we never read it back, just keep the strong count > 0.
#[allow(dead_code)]
enum EsplEmu {
    Ct002(std::sync::Arc<astrameter_emulator_ct002::server::Ct002Emulator>),
    Shelly(std::sync::Arc<astrameter_emulator_shelly::ShellyEmulator>),
}

/// Start the MQTT Insights service if `[MQTT_INSIGHTS]` is configured,
/// wire CT002 / Shelly emulator events into its event channel, and
/// install command handlers + Marstek bindings. Mirrors the host
/// supervisor's `start_insights` + `wire_*_to_insights` calls.
#[cfg(target_os = "espidf")]
async fn start_mqtt_insights(
    config: &astrameter_config::Config,
    device_type: &str,
    bound: &[astrameter_powermeters::BoundPowermeter],
    ct002: Option<std::sync::Arc<astrameter_emulator_ct002::server::Ct002Emulator>>,
    shelly_emus: &[std::sync::Arc<astrameter_emulator_shelly::ShellyEmulator>],
    platform: std::sync::Arc<astrameter_platform::Platform>,
) -> anyhow::Result<Option<std::sync::Arc<astrameter_insights_mqtt::InsightsService>>> {
    use std::sync::Arc;

    use astrameter_insights_mqtt::{
        CommandHandlers, InsightsEvent, InsightsService, MarstekBinding, MqttInsightsConfig,
    };

    let Some(section_name) = config.sections().find(|s| s.starts_with("MQTT_INSIGHTS")) else {
        log::info!("MQTT Insights: no [MQTT_INSIGHTS*] section in config — skipping");
        return Ok(None);
    };
    let Some(section) = config.section(section_name) else {
        return Ok(None);
    };
    log::info!("MQTT Insights: [{section_name}] found, starting service");

    let (broker, port, username, password, tls) = match section.get_opt_string("URI") {
        Some(uri) => {
            let parts = astrameter_config::parse_mqtt_uri(&uri)
                .map_err(|e| anyhow::anyhow!("[{section_name}] URI parse: {e}"))?;
            (
                parts.host,
                parts.port,
                parts.username,
                parts.password,
                parts.tls,
            )
        }
        None => (
            section.get_string("BROKER", "localhost"),
            section.get_int("PORT", 1883)? as u16,
            section.get_opt_string("USERNAME"),
            section.get_opt_string("PASSWORD"),
            section.get_bool("TLS", false)?,
        ),
    };
    let cfg = MqttInsightsConfig {
        broker,
        port,
        username,
        password,
        tls,
        base_topic: section.get_string("BASE_TOPIC", "astrameter"),
        ha_discovery: section.get_bool("HA_DISCOVERY", true)?,
        ha_discovery_prefix: section.get_string("HA_DISCOVERY_PREFIX", "homeassistant"),
        addon_slug: section.get_opt_string("ADDON_SLUG"),
        marstek_mqtt_enabled: section.get_bool("MARSTEK_MQTT_ENABLED", true)?,
        marstek_mqtt_interval: section.get_float("MARSTEK_MQTT_INTERVAL", 300.0)?,
    };
    let service = Arc::new(InsightsService::new(cfg, platform.clone()));

    // CT002 command handlers (set_active / manual_target / auto_target /
    // force_rotation) — same wiring as the host supervisor.
    if let Some(ct) = ct002.clone() {
        let ct_act = ct.clone();
        let ct_mt = ct.clone();
        let ct_at = ct.clone();
        let ct_fr = ct.clone();
        service.set_command_handlers(CommandHandlers {
            set_active: Some(Arc::new(move |_dev: &str, consumer: &str, active: bool| {
                ct_act.set_consumer_active(consumer, active);
            })),
            set_manual_target: Some(Arc::new(move |_dev: &str, consumer: &str, target: f64| {
                ct_mt.set_consumer_manual_target(consumer, target);
            })),
            set_auto_target: Some(Arc::new(move |_dev: &str, consumer: &str, auto: bool| {
                ct_at.set_consumer_auto_target(consumer, auto);
            })),
            force_rotation: Some(Arc::new(move |_dev: &str| {
                ct_fr.force_efficiency_rotation();
            })),
        });
    }

    // Marstek MQTT binding (drives `hame_energy/...` poll responses +
    // the periodic broadcast loop).
    if let Some(ct) = ct002.as_ref() {
        let dt = device_type.to_lowercase();
        if dt == "ct002" || dt == "ct003" {
            let section_name_ct = if dt == "ct003" && config.section("CT003").is_some() {
                "CT003"
            } else {
                "CT002"
            };
            if let Some(cs) = config.section(section_name_ct) {
                let ct_mac_raw = cs.get_string("CT_MAC", "");
                let mac_norm = astrameter_insights_mqtt::marstek::normalize_mac(&ct_mac_raw);
                let ct_type = if dt == "ct003" {
                    "HME-3".to_string()
                } else {
                    "HME-4".to_string()
                };
                let device_id = cs
                    .get_opt_string("DEVICE_ID")
                    .unwrap_or_else(|| ct.device_id());
                let wifi_rssi = cs.get_int("WIFI_RSSI", -50).unwrap_or(-50);
                if !mac_norm.is_empty() {
                    let ct_for_count = ct.clone();
                    let ct_for_csv = ct.clone();
                    service.add_marstek_binding(MarstekBinding {
                        device_id,
                        ct_type,
                        mac: mac_norm,
                        wifi_rssi,
                        ver_v: astrameter_insights_mqtt::marstek::DEFAULT_VER_V,
                        ble_s: 0,
                        fc4_v: astrameter_insights_mqtt::marstek::DEFAULT_FC4_V.to_string(),
                        get_connected_slave_count: Some(Arc::new(move || {
                            ct_for_count.reporting_consumer_count() as i64
                        })),
                        get_cd4_slave_csv: Some(Arc::new(move || {
                            ct_for_csv.reporting_consumer_csv()
                        })),
                    });
                } else if !ct_mac_raw.is_empty() {
                    log::warn!(
                        "[{section_name_ct}] CT_MAC={ct_mac_raw:?} could not be normalised; \
                         Marstek MQTT binding skipped"
                    );
                }
            }
        }
    }

    // Wire CT002 + Shelly event listeners into the insights event
    // channel — without this the InsightsService only sees Marstek
    // poll responses; HA Device Discovery and consumer-state publishes
    // never fire.
    let tx = service.event_sender();
    if let Some(ct) = ct002.as_ref() {
        let tx_ct = tx.clone();
        ct.set_event_listener(Arc::new(
            move |device_id: &str, consumer_id: &str, data: &serde_json::Value| {
                let removed = data
                    .get("_removed")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if removed {
                    let _ = tx_ct.try_send(InsightsEvent::Ct002Remove {
                        device_id: device_id.to_string(),
                        consumer_id: consumer_id.to_string(),
                    });
                    return;
                }
                let _ = tx_ct.try_send(InsightsEvent::Ct002 {
                    device_id: device_id.to_string(),
                    consumer_id: consumer_id.to_string(),
                    data: data.clone(),
                });
                let status = serde_json::json!({
                    "smooth_target": data.get("smooth_target").cloned().unwrap_or(serde_json::Value::Null),
                    "active_control": data.get("active_control").cloned().unwrap_or(serde_json::Value::Null),
                    "consumer_count": data.get("consumer_count").cloned().unwrap_or(serde_json::Value::Null),
                });
                let _ = tx_ct.try_send(InsightsEvent::Ct002DeviceStatus {
                    device_id: device_id.to_string(),
                    data: status,
                });
            },
        ));
    }
    for sh in shelly_emus {
        let tx_sh = tx.clone();
        sh.set_event_listener(Arc::new(
            move |device_id: &str, battery_ip: &str, data: &serde_json::Value| {
                let removed = data
                    .get("_removed")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if removed {
                    let _ = tx_sh.try_send(InsightsEvent::ShellyRemove {
                        device_id: device_id.to_string(),
                        battery_ip: battery_ip.to_string(),
                    });
                    return;
                }
                let _ = tx_sh.try_send(InsightsEvent::Shelly {
                    device_id: device_id.to_string(),
                    battery_ip: battery_ip.to_string(),
                    data: data.clone(),
                });
            },
        ));
    }

    // Meter-watts callback — emits raw (pre-wrapper) values for the
    // Marstek wire format. Same shape as the host supervisor.
    let meters: Vec<(String, Arc<dyn astrameter_core::Powermeter>)> = bound
        .iter()
        .map(|bp| (bp.section.clone(), bp.meter.clone()))
        .collect();
    let meters_arc = Arc::new(meters);
    let meters_cb = meters_arc.clone();
    service
        .start(move |_device_id: &str| {
            let meters_cb = meters_cb.clone();
            Box::pin(async move {
                if let Some((_, m)) = meters_cb.first() {
                    m.get_powermeter_watts_raw().await
                } else {
                    Ok(vec![0.0, 0.0, 0.0])
                }
            })
        })
        .await
        .map_err(|e| anyhow::anyhow!("InsightsService::start: {e}"))?;

    log::info!("MQTT Insights: service started");
    Ok(Some(service))
}

/// Marstek cloud HTTPS auto-registration. Detached so a slow connect
/// doesn't block the boot-time supervisor wiring.
#[cfg(target_os = "espidf")]
fn spawn_marstek_registration(
    config: &astrameter_config::Config,
    device_type: &str,
    platform: std::sync::Arc<astrameter_platform::Platform>,
) {
    let Some(section_name) = config.sections().find(|s| s.starts_with("MARSTEK")) else {
        return;
    };
    let Some(section) = config.section(section_name) else {
        return;
    };
    if !section.get_bool("ENABLE", false).unwrap_or(false) {
        log::info!("[{section_name}] disabled (ENABLE=false) — skipping cloud registration");
        return;
    }
    let base_url = section.get_string("BASE_URL", "https://eu.hamedata.com");
    let mailbox = section.get_string("MAILBOX", "");
    let password = section.get_string("PASSWORD", "");
    if mailbox.is_empty() || password.is_empty() {
        log::warn!("[{section_name}] missing MAILBOX or PASSWORD — skipping registration");
        return;
    }
    let dt_norm = device_type.to_lowercase();
    let device_type_for_reg = if dt_norm == "ct002" || dt_norm == "ct003" {
        dt_norm
    } else {
        log::info!("[{section_name}] only ct002/ct003 supported for cloud registration");
        return;
    };
    let http = platform.http.clone();
    tokio::spawn(async move {
        let client = astrameter_marstek_api::MarstekClient::new(http);
        let cfg = astrameter_marstek_api::MarstekConfig::new(base_url, mailbox, password);
        match client
            .ensure_managed_fake_device(&cfg, &device_type_for_reg)
            .await
        {
            Ok(Some(d)) => log::info!("Marstek registration ok: {d:?}"),
            Ok(None) => log::info!("Marstek: nothing to register for {device_type_for_reg}"),
            Err(e) => log::warn!("Marstek registration failed: {e}"),
        }
    });
}

/// Embedded fallback when NVS has no `astrameter/config` key — first
/// boot, or after a factory wipe.
#[cfg(target_os = "espidf")]
const EMBEDDED_DEFAULT_CONFIG: &str = "[GENERAL]\nDEVICE_TYPE=ct002\n\n[CT002]\nUDP_PORT=12345\n";

/// NVS keys for the (small) state we persist between boots. NVS values
/// are limited to ~4000 bytes — fine for a config.ini that fits in a
/// typical ESP32 SRAM budget anyway.
#[cfg(target_os = "espidf")]
mod nvs_keys {
    pub const NAMESPACE: &str = "astrameter";
    pub const CONFIG: &str = "config";
    pub const WIFI_SSID: &str = "wifi_ssid";
    pub const WIFI_PASS: &str = "wifi_pass";
}

/// Read `config.ini` text from NVS, seeding the embedded default on
/// first boot. Returns the config string ready for `Config::parse`.
#[cfg(target_os = "espidf")]
fn load_config_from_nvs(part: esp_idf_svc::nvs::EspDefaultNvsPartition) -> anyhow::Result<String> {
    use esp_idf_svc::nvs::EspNvs;
    let nvs = EspNvs::new(part, nvs_keys::NAMESPACE, true)
        .map_err(|e| anyhow::anyhow!("open NVS namespace: {e}"))?;
    let mut buf = vec![0u8; 4096];
    match nvs.get_str(nvs_keys::CONFIG, &mut buf) {
        Ok(Some(s)) => {
            let owned = s.to_string();
            log::info!("config loaded from NVS ({} bytes)", owned.len());
            Ok(owned)
        }
        Ok(None) => {
            log::warn!(
                "NVS has no `{}` — seeding embedded default",
                nvs_keys::CONFIG
            );
            nvs.set_str(nvs_keys::CONFIG, EMBEDDED_DEFAULT_CONFIG)
                .map_err(|e| anyhow::anyhow!("seed NVS config: {e}"))?;
            Ok(EMBEDDED_DEFAULT_CONFIG.to_string())
        }
        Err(e) => Err(anyhow::anyhow!("read NVS config: {e}")),
    }
}

/// Bring up the lwIP TCP/IP stack and (optionally) connect to Wi-Fi.
///
/// **Always** initialises `EspWifi` even when no SSID is provisioned,
/// because that's what spins up the lwIP TCP/IP thread + default
/// network interface — without it, the very first UDP `bind()` in
/// the CT002 / Shelly emulator hits
/// `assert failed: tcpip_send_msg_wait_sem ... (Invalid mbox)` and
/// the whole firmware panics.
///
/// `Ok(true)` means we associated with an AP; `Ok(false)` means the
/// stack is up but we're offline (no creds in NVS, or association
/// failed). Either way the firmware keeps running; powermeters that
/// need network connectivity will surface their own runtime errors.
#[cfg(target_os = "espidf")]
fn bring_up_wifi(
    sysloop: &esp_idf_svc::eventloop::EspSystemEventLoop,
    nvs_part: esp_idf_svc::nvs::EspDefaultNvsPartition,
) -> anyhow::Result<bool> {
    use embedded_svc::wifi::{AuthMethod, ClientConfiguration, Configuration};
    use esp_idf_svc::hal::peripherals::Peripherals;
    use esp_idf_svc::nvs::EspNvs;
    use esp_idf_svc::wifi::{BlockingWifi, EspWifi};

    // Resolution order for Wi-Fi credentials:
    //   1. NVS (`astrameter` namespace, keys `wifi_ssid` / `wifi_pass`).
    //      This is what a future SoftAP captive-portal flow would
    //      write, and what `espflash write-bin --partition nvs ...`
    //      writes today.
    //   2. Build-time `WIFI_SSID` / `WIFI_PASSWORD` env vars baked into
    //      the binary via `option_env!`. Set these on the
    //      `cargo +esp build` command line for a quick "just connect to
    //      my home AP" workflow:
    //
    //          WIFI_SSID=MyAP WIFI_PASSWORD=secret \
    //              cargo +esp build --release -p astrameter-esp32 \
    //                  --target xtensa-esp32s3-espidf
    //
    //      Baked credentials are persisted to NVS on first boot so a
    //      later build without the env vars still connects.
    //   3. Neither: stay offline (lwIP is up, no AP association).
    let nvs = EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true)
        .map_err(|e| anyhow::anyhow!("open NVS for wifi: {e}"))?;
    let mut ssid_buf = [0u8; 64];
    let mut pass_buf = [0u8; 128];
    let nvs_ssid = nvs
        .get_str(nvs_keys::WIFI_SSID, &mut ssid_buf)
        .ok()
        .flatten()
        .map(|s| s.to_string())
        .unwrap_or_default();
    let nvs_pass = nvs
        .get_str(nvs_keys::WIFI_PASS, &mut pass_buf)
        .ok()
        .flatten()
        .map(|s| s.to_string())
        .unwrap_or_default();
    let (ssid, password) = if !nvs_ssid.is_empty() {
        log::info!("Wi-Fi credentials loaded from NVS");
        (nvs_ssid, nvs_pass)
    } else if let Some(env_ssid) = option_env!("WIFI_SSID").filter(|s| !s.is_empty()) {
        let env_pass = option_env!("WIFI_PASSWORD").unwrap_or("").to_string();
        log::info!(
            "Wi-Fi credentials loaded from build-time WIFI_SSID env var; \
             persisting to NVS for next boot"
        );
        // Persist so subsequent boots without env vars still connect.
        // Drop the read-only `nvs` handle first; reopen read-write.
        drop(nvs);
        let nvs_rw = EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true)
            .map_err(|e| anyhow::anyhow!("reopen NVS rw for wifi: {e}"))?;
        if let Err(e) = nvs_rw.set_str(nvs_keys::WIFI_SSID, env_ssid) {
            log::warn!("could not write wifi_ssid to NVS: {e}");
        }
        if let Err(e) = nvs_rw.set_str(nvs_keys::WIFI_PASS, &env_pass) {
            log::warn!("could not write wifi_pass to NVS: {e}");
        }
        (env_ssid.to_string(), env_pass)
    } else {
        drop(nvs);
        (String::new(), String::new())
    };

    let peripherals = Peripherals::take().map_err(|e| anyhow::anyhow!("Peripherals::take: {e}"))?;
    let mut wifi = BlockingWifi::wrap(
        EspWifi::new(peripherals.modem, sysloop.clone(), Some(nvs_part.clone()))
            .map_err(|e| anyhow::anyhow!("EspWifi::new: {e}"))?,
        sysloop.clone(),
    )
    .map_err(|e| anyhow::anyhow!("BlockingWifi::wrap: {e}"))?;

    if ssid.is_empty() {
        // No creds: drop into the captive-portal setup flow. This
        // switches Wi-Fi to AP mode (SSID `AstraMeter-Setup-XXXXXX`),
        // spawns an HTTP server at http://192.168.4.1/, and blocks
        // here until the user submits credentials, at which point we
        // persist them to NVS and reboot. The browser's
        // captive-portal-detection trick (sending HTTP 302 for the
        // OS connectivity probes) makes the form auto-open on most
        // devices when they associate to the AP.
        log::warn!(
            "No Wi-Fi credentials provisioned — starting captive-portal setup AP. \
             Connect to the `AstraMeter-Setup-*` Wi-Fi on a phone or laptop and the \
             setup page should open automatically (or browse to http://192.168.4.1/)."
        );
        run_captive_portal(&mut wifi, &nvs_part)?;
        // run_captive_portal calls esp_restart() once credentials are
        // saved, so we shouldn't actually reach here.
        unreachable!("captive portal returned without restarting");
    }

    wifi.set_configuration(&Configuration::Client(ClientConfiguration {
        ssid: ssid
            .as_str()
            .parse()
            .map_err(|_| anyhow::anyhow!("ssid too long"))?,
        password: password
            .as_str()
            .parse()
            .map_err(|_| anyhow::anyhow!("password too long"))?,
        auth_method: if password.is_empty() {
            AuthMethod::None
        } else {
            AuthMethod::WPA2Personal
        },
        ..Default::default()
    }))
    .map_err(|e| anyhow::anyhow!("set wifi config: {e}"))?;
    wifi.start()
        .map_err(|e| anyhow::anyhow!("wifi.start: {e}"))?;
    wifi.connect()
        .map_err(|e| anyhow::anyhow!("wifi.connect: {e}"))?;
    wifi.wait_netif_up()
        .map_err(|e| anyhow::anyhow!("wifi.wait_netif_up: {e}"))?;
    log::info!(
        "Wi-Fi connected; IP info: {:?}",
        wifi.wifi()
            .sta_netif()
            .get_ip_info()
            .map_err(|e| anyhow::anyhow!("sta_netif.get_ip_info: {e}"))?
    );
    std::mem::forget(wifi);
    Ok(true)
}

/// Captive-portal setup flow. Switches Wi-Fi to AP mode, serves a
/// tiny HTML form at `http://192.168.4.1/`, waits for the user to
/// submit credentials, persists them to NVS, and reboots into STA
/// mode.
///
/// The form does a synchronous fetch (`/scan`) to populate a `<select>`
/// of nearby SSIDs the user can pick from. Submitting issues
/// `POST /save` with `ssid` + `password` form fields.
///
/// Browser captive-portal magic: all "is this network up?" probe
/// URLs (Apple's `/hotspot-detect.html`, Google's `/generate_204`,
/// Microsoft's `/ncsi.txt`, …) are answered with a 302 redirect to
/// `/`, which makes most phones / laptops auto-open the setup page
/// the moment they associate.
#[cfg(target_os = "espidf")]
fn run_captive_portal(
    wifi: &mut esp_idf_svc::wifi::BlockingWifi<esp_idf_svc::wifi::EspWifi<'static>>,
    nvs_part: &esp_idf_svc::nvs::EspDefaultNvsPartition,
) -> anyhow::Result<()> {
    use embedded_svc::wifi::{AccessPointConfiguration, AuthMethod, Configuration};
    use esp_idf_svc::nvs::EspNvs;
    use std::net::{Ipv4Addr, SocketAddr, TcpListener};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    // Derive a stable SSID suffix from the STA MAC so multiple
    // ESP32s on the same desk don't shadow each other.
    let mac = wifi
        .wifi()
        .sta_netif()
        .get_mac()
        .map_err(|e| anyhow::anyhow!("get sta mac: {e}"))?;
    let ssid_str = format!(
        "AstraMeter-Setup-{:02X}{:02X}{:02X}",
        mac[3], mac[4], mac[5]
    );

    wifi.set_configuration(&Configuration::AccessPoint(AccessPointConfiguration {
        ssid: ssid_str
            .as_str()
            .parse()
            .map_err(|_| anyhow::anyhow!("ssid too long"))?,
        auth_method: AuthMethod::None,
        ssid_hidden: false,
        channel: 1,
        max_connections: 4,
        ..Default::default()
    }))
    .map_err(|e| anyhow::anyhow!("set ap config: {e}"))?;
    wifi.start()
        .map_err(|e| anyhow::anyhow!("wifi.start (AP): {e}"))?;

    // Read the AP-side IP dynamically — ESP-IDF's default changed from
    // 192.168.4.1 (pre-5.x) to 192.168.71.1 (5.x). The DHCP server
    // assigns clients from this subnet, so we have to redirect them to
    // *that* IP for the captive portal to be reachable.
    let ap_ip = wifi
        .wifi()
        .ap_netif()
        .get_ip_info()
        .map_err(|e| anyhow::anyhow!("get ap ip: {e}"))?
        .ip;
    let portal_url = format!("http://{ap_ip}/");
    log::info!("Captive portal AP live: SSID=`{ssid_str}`, open network, {portal_url}");

    let done = Arc::new(AtomicBool::new(false));
    let saved_creds: Arc<parking_lot::Mutex<Option<(String, String)>>> =
        Arc::new(parking_lot::Mutex::new(None));

    // DNS hijack: every A query → AP IP. Without this, OS connectivity
    // probes never reach our HTTP server and the captive-portal sheet
    // doesn't auto-pop.
    let _dns_thread = start_dns_hijack(ap_ip)?;

    // Raw-TCP HTTP server. We can't use `EspHttpServer` because IDF's
    // httpd has a 512-byte hard-coded request-header buffer
    // (`CONFIG_HTTPD_MAX_REQ_HDR_LEN`), which any real browser blows
    // past instantly ("431 Request Header Fields Too Large"). Writing
    // ~150 lines of TCP+HTTP-parser here sidesteps that completely.

    // Capture the AP-side MAC for the device label on the form.
    let device_label = format!(
        "{:02X}:{:02X}:{:02X}:{:02X}:{:02X}:{:02X}",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]
    );

    let listener = TcpListener::bind(SocketAddr::from((Ipv4Addr::UNSPECIFIED, 80)))
        .map_err(|e| anyhow::anyhow!("bind TCP/80 for portal: {e}"))?;
    listener
        .set_nonblocking(false)
        .map_err(|e| anyhow::anyhow!("set blocking: {e}"))?;
    log::info!("Captive-portal HTTP server listening on TCP/80");

    while !done.load(Ordering::SeqCst) {
        let (stream, peer) = match listener.accept() {
            Ok(p) => p,
            Err(e) => {
                log::warn!("portal accept: {e}");
                continue;
            }
        };
        let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(3)));
        let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(3)));
        if let Err(e) =
            handle_portal_connection(stream, &portal_url, &device_label, &saved_creds, &done)
        {
            log::debug!("portal conn from {peer}: {e}");
        }
    }
    // Give the browser a moment to receive the success page.
    std::thread::sleep(std::time::Duration::from_secs(1));

    let (ssid, password) = saved_creds
        .lock()
        .take()
        .ok_or_else(|| anyhow::anyhow!("done flag set without creds"))?;

    log::info!("Captive portal: received SSID `{ssid}`, persisting to NVS");
    let nvs_rw = EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true)
        .map_err(|e| anyhow::anyhow!("open NVS for save: {e}"))?;
    nvs_rw
        .set_str(nvs_keys::WIFI_SSID, &ssid)
        .map_err(|e| anyhow::anyhow!("write wifi_ssid: {e}"))?;
    nvs_rw
        .set_str(nvs_keys::WIFI_PASS, &password)
        .map_err(|e| anyhow::anyhow!("write wifi_pass: {e}"))?;

    log::info!("Captive portal: restarting to apply Wi-Fi credentials");
    std::thread::sleep(std::time::Duration::from_secs(1));
    unsafe { esp_idf_svc::sys::esp_restart() };
    // esp_restart never returns, but rustc doesn't know that:
    #[allow(unreachable_code)]
    Ok(())
}

/// Read one HTTP request from `stream`, dispatch, write the response,
/// and close. The DNS hijack means *every* hostname resolves to us, so
/// we serve the setup form on `/` and 302-redirect everything else to
/// `/` — which makes phones / laptops auto-pop the captive sheet for
/// any probe URL their OS happens to use.
#[cfg(target_os = "espidf")]
fn handle_portal_connection(
    mut stream: std::net::TcpStream,
    portal_url: &str,
    device_label: &str,
    saved_creds: &std::sync::Arc<parking_lot::Mutex<Option<(String, String)>>>,
    done: &std::sync::Arc<std::sync::atomic::AtomicBool>,
) -> std::io::Result<()> {
    use std::io::{Read, Write};
    use std::sync::atomic::Ordering;

    // Read headers up to \r\n\r\n. 8 KB is plenty for any browser; if
    // we still go over, just close — the user can retry.
    let mut buf = vec![0u8; 8192];
    let mut len = 0;
    let mut header_end = None;
    while len < buf.len() {
        let n = stream.read(&mut buf[len..])?;
        if n == 0 {
            break;
        }
        len += n;
        if let Some(idx) = find_header_end(&buf[..len]) {
            header_end = Some(idx);
            break;
        }
    }
    let Some(headers_end) = header_end else {
        // No complete header — give up politely.
        let _ = stream.write_all(
            b"HTTP/1.0 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
        );
        return Ok(());
    };
    let header_text = std::str::from_utf8(&buf[..headers_end]).unwrap_or("");
    let first_line = header_text.lines().next().unwrap_or("");
    let mut parts = first_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let raw_path = parts.next().unwrap_or("/").to_string();
    let path = raw_path.split('?').next().unwrap_or("/").to_string();

    let mut content_length = 0usize;
    for line in header_text.lines().skip(1) {
        if let Some((k, v)) = line.split_once(':') {
            if k.eq_ignore_ascii_case("content-length") {
                content_length = v.trim().parse::<usize>().unwrap_or(0);
            }
        }
    }

    // Read body for POST /save.
    let mut body = Vec::new();
    if method == "POST" && content_length > 0 {
        // Anything past the header break is already in `buf`.
        let header_total = headers_end + 4; // skip "\r\n\r\n"
        if len > header_total {
            body.extend_from_slice(&buf[header_total..len]);
        }
        while body.len() < content_length && body.len() < 4096 {
            let mut chunk = [0u8; 512];
            let n = stream.read(&mut chunk)?;
            if n == 0 {
                break;
            }
            body.extend_from_slice(&chunk[..n]);
        }
        body.truncate(content_length.min(body.len()));
    }

    let response: Vec<u8> = if method == "POST" && path == "/save" {
        let text = String::from_utf8_lossy(&body);
        let mut ssid = String::new();
        let mut password = String::new();
        for pair in text.split('&') {
            let Some((k, v)) = pair.split_once('=') else {
                continue;
            };
            let decoded = url_decode(v);
            match k {
                "ssid" => ssid = decoded,
                "password" => password = decoded,
                _ => {}
            }
        }
        if ssid.is_empty() {
            http_response(
                400,
                "Bad Request",
                "text/plain; charset=utf-8",
                b"ssid is required",
            )
        } else {
            *saved_creds.lock() = Some((ssid, password));
            done.store(true, Ordering::SeqCst);
            http_response(200, "OK", "text/html; charset=utf-8", SAVED_HTML.as_bytes())
        }
    } else if method == "GET" && path == "/" {
        let html = SETUP_HTML.replace("{{device}}", device_label);
        http_response(200, "OK", "text/html; charset=utf-8", html.as_bytes())
    } else {
        // Catch-all: 302 → setup form. Captive-portal probes
        // (`/hotspot-detect.html`, `/generate_204`, etc.) all funnel
        // through this.
        let mut out = Vec::new();
        let _ = write!(out, "HTTP/1.0 302 Found\r\n");
        let _ = write!(out, "Location: {portal_url}\r\n");
        let _ = write!(out, "Connection: close\r\n");
        let _ = write!(out, "Content-Length: 8\r\n\r\nredirect");
        out
    };
    stream.write_all(&response)?;
    let _ = stream.flush();
    Ok(())
}

#[cfg(target_os = "espidf")]
fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

#[cfg(target_os = "espidf")]
fn http_response(status: u16, reason: &str, content_type: &str, body: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut out = Vec::with_capacity(body.len() + 128);
    let _ = write!(out, "HTTP/1.0 {status} {reason}\r\n");
    let _ = write!(out, "Content-Type: {content_type}\r\n");
    let _ = write!(out, "Content-Length: {}\r\n", body.len());
    let _ = write!(out, "Connection: close\r\n\r\n");
    out.extend_from_slice(body);
    out
}

#[cfg(target_os = "espidf")]
fn url_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b'%' if i + 2 < bytes.len() => {
                let hi = (bytes[i + 1] as char).to_digit(16);
                let lo = (bytes[i + 2] as char).to_digit(16);
                if let (Some(h), Some(l)) = (hi, lo) {
                    out.push((h * 16 + l) as u8);
                    i += 3;
                } else {
                    out.push(bytes[i]);
                    i += 1;
                }
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

/// Spawn a minimal DNS hijack on UDP/53 that resolves *every* A query
/// to `ap_ip`. This is what makes phones / laptops auto-pop the
/// captive portal: their OS-level connectivity-check expects to
/// resolve a known hostname (e.g. `connectivitycheck.gstatic.com`)
/// to a real server, then GET a known URL on it; if either the DNS
/// resolution or the GET goes sideways, the OS shows the captive
/// portal sheet. We hijack DNS so the connectivity probe lands on
/// our HTTP server, which then 302-redirects to `/`.
///
/// Returns the worker `JoinHandle` so the caller can drop / abort it
/// after the form is submitted (here we just let it run until
/// `esp_restart`).
#[cfg(target_os = "espidf")]
fn start_dns_hijack(ap_ip: std::net::Ipv4Addr) -> anyhow::Result<std::thread::JoinHandle<()>> {
    use std::net::{SocketAddr, UdpSocket};
    let sock = UdpSocket::bind(SocketAddr::from(([0, 0, 0, 0], 53)))
        .map_err(|e| anyhow::anyhow!("bind UDP/53 for DNS hijack: {e}"))?;
    sock.set_read_timeout(Some(std::time::Duration::from_secs(1)))
        .ok();
    let ap_bytes = ap_ip.octets();
    let handle = std::thread::Builder::new()
        .name("dns-hijack".into())
        .stack_size(8 * 1024)
        .spawn(move || {
            log_task_handle("dns-hijack");
            let mut buf = [0u8; 512];
            loop {
                let (n, peer) = match sock.recv_from(&mut buf) {
                    Ok(p) => p,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
                    Err(e) if e.kind() == std::io::ErrorKind::TimedOut => continue,
                    Err(e) => {
                        log::warn!("dns-hijack recv: {e}");
                        continue;
                    }
                };
                if n < 12 {
                    continue;
                }
                let mut reply = build_dns_reply(&buf[..n], ap_bytes);
                if reply.is_empty() {
                    continue;
                }
                // Send response (best effort).
                let _ = sock.send_to(&mut reply, peer);
            }
        })
        .map_err(|e| anyhow::anyhow!("spawn dns thread: {e}"))?;
    log::info!("DNS hijack listening on UDP/53 → {ap_ip}");
    Ok(handle)
}

/// Build a synthetic DNS A response that points the asked hostname at
/// `ap_ip`. We copy the question section verbatim, flip the header
/// flags to "response, no error, recursion available", then append a
/// single answer RR with TTL=60 pointing at `ap_ip`.
#[cfg(target_os = "espidf")]
fn build_dns_reply(query: &[u8], ap_ip: [u8; 4]) -> Vec<u8> {
    if query.len() < 12 {
        return Vec::new();
    }
    // Walk the question section to find its end (one QNAME label list
    // terminated by a 0 byte, then QTYPE + QCLASS = 4 more bytes).
    let mut pos = 12;
    while pos < query.len() {
        let len = query[pos] as usize;
        if len == 0 {
            pos += 1;
            break;
        }
        if len & 0xc0 != 0 {
            // Compression in the question is illegal but defensive.
            return Vec::new();
        }
        pos += 1 + len;
    }
    pos += 4; // QTYPE + QCLASS
    if pos > query.len() {
        return Vec::new();
    }

    let mut out = Vec::with_capacity(pos + 16);
    out.extend_from_slice(&query[..pos]);
    // Header tweaks: bit-15 (QR) set, RA set, ANCOUNT = 1.
    out[2] |= 0x80; // QR
    out[3] = (out[3] & !0x0F) | 0x80; // RCODE=0, RA=1 (bit-7 of byte 3)
    out[6] = 0x00;
    out[7] = 0x01; // ANCOUNT = 1
                   // Answer section: name pointer to offset 12 (start of question),
                   // TYPE=A (1), CLASS=IN (1), TTL=60, RDLENGTH=4, RDATA=ap_ip.
    out.extend_from_slice(&[0xc0, 0x0c]); // pointer to QNAME
    out.extend_from_slice(&[0x00, 0x01]); // TYPE A
    out.extend_from_slice(&[0x00, 0x01]); // CLASS IN
    out.extend_from_slice(&[0x00, 0x00, 0x00, 0x3c]); // TTL = 60
    out.extend_from_slice(&[0x00, 0x04]); // RDLENGTH
    out.extend_from_slice(&ap_ip);
    out
}

#[cfg(target_os = "espidf")]
const SETUP_HTML: &str = r#"<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AstraMeter Wi-Fi setup</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 24em; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { font-size: 1.2em; }
  label { display: block; margin-top: 1em; font-weight: 600; }
  input[type=text], input[type=password] { width: 100%; padding: .5em; font-size: 1em; border: 1px solid #aaa; border-radius: .25em; box-sizing: border-box; }
  button { margin-top: 1.5em; width: 100%; padding: .75em; font-size: 1em; background: #007aff; color: white; border: 0; border-radius: .25em; }
  small { color: #666; }
</style>
</head><body>
<h1>AstraMeter Wi-Fi setup</h1>
<p><small>Device: {{device}}</small></p>
<form method="POST" action="/save">
  <label for="ssid">Network name (SSID)</label>
  <input id="ssid" name="ssid" type="text" autocomplete="off" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="off">
  <button type="submit">Save &amp; restart</button>
</form>
</body></html>"#;

#[cfg(target_os = "espidf")]
const SAVED_HTML: &str = r#"<!doctype html>
<html><head><meta charset="utf-8"><title>AstraMeter saved</title>
<style>body{font-family:sans-serif;max-width:24em;margin:2em auto;padding:0 1em}</style>
</head><body>
<h1>Saved</h1>
<p>Credentials stored. The device will reboot in a second and connect to your network.</p>
</body></html>"#;

#[cfg(target_os = "espidf")]
async fn start_sntp_and_wait_for_sync() -> anyhow::Result<()> {
    use esp_idf_svc::sntp::{EspSntp, OperatingMode, SntpConf, SyncMode, SyncStatus};
    use std::time::Duration;
    let sntp = EspSntp::new(&SntpConf {
        servers: ["pool.ntp.org"],
        operating_mode: OperatingMode::Poll,
        sync_mode: SyncMode::Immediate,
    })
    .map_err(|e| anyhow::anyhow!("EspSntp::new: {e}"))?;
    for _ in 0..30 {
        if matches!(sntp.get_sync_status(), SyncStatus::Completed) {
            log::info!("SNTP sync OK");
            std::mem::forget(sntp);
            return Ok(());
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    anyhow::bail!("SNTP did not sync within 30s")
}

/// Runtime web UI for editing `config.ini` in NVS and resetting the
/// Wi-Fi credentials. Listens on TCP/80 on the STA interface and
/// serves the exact same HTML + JSON API the host build serves
/// (`crates/web/assets/config_editor.html` +
/// `astrameter_web::config_ui::CONFIG_EDITOR_HTML`), so the editor UI
/// matches the Python original 1:1. Uses the raw-TCP HTTP approach
/// from the captive portal because esp-idf-svc's httpd has the same
/// 512-byte header limit that bites browsers everywhere else.
///
/// Endpoints (host-compatible shape):
///   * `GET  /`              — `CONFIG_EDITOR_HTML` (full editor).
///   * `GET  /api/config`    — `{sections, order}` JSON, parsed out
///                             of the NVS-stored `config.ini`.
///   * `POST /api/config`    — same shape, rebuilt into `config.ini`
///                             text, validated via
///                             `astrameter_config::Config::parse`,
///                             then persisted to NVS + `esp_restart()`.
///   * `GET  /api/key-types` — `SECTION_KEY_TYPES_JSON`, the schema
///                             the editor uses to pick input widgets.
///   * `POST /api/restart`   — `esp_restart()` after a small grace.
///   * `GET  /health`        — `{status, service, version, healthy}`.
///   * `POST /api/wifi/reset` — clears `wifi_ssid` / `wifi_pass` in
///                             NVS so the next boot drops back to the
///                             captive-portal setup AP.
#[cfg(target_os = "espidf")]
fn run_config_web_server(nvs_part: esp_idf_svc::nvs::EspDefaultNvsPartition) {
    use std::net::{Ipv4Addr, SocketAddr, TcpListener};

    log_task_handle("config-web");
    let listener = match TcpListener::bind(SocketAddr::from((Ipv4Addr::UNSPECIFIED, 80))) {
        Ok(l) => l,
        Err(e) => {
            log::error!("config web: bind TCP/80: {e}");
            return;
        }
    };
    if let Err(e) = listener.set_nonblocking(false) {
        log::warn!("config web: set blocking: {e}");
    }
    log::info!("config web: listening on TCP/80");
    loop {
        let (stream, peer) = match listener.accept() {
            Ok(p) => p,
            Err(e) => {
                log::warn!("config web accept: {e}");
                continue;
            }
        };
        let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(3)));
        let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(3)));
        if let Err(e) = handle_config_connection(stream, &nvs_part) {
            log::debug!("config web conn from {peer}: {e}");
        }
    }
}

#[cfg(target_os = "espidf")]
fn handle_config_connection(
    mut stream: std::net::TcpStream,
    nvs_part: &esp_idf_svc::nvs::EspDefaultNvsPartition,
) -> std::io::Result<()> {
    use std::io::{Read, Write};

    let mut buf = vec![0u8; 8192];
    let mut len = 0;
    let mut header_end = None;
    while len < buf.len() {
        let n = stream.read(&mut buf[len..])?;
        if n == 0 {
            break;
        }
        len += n;
        if let Some(idx) = find_header_end(&buf[..len]) {
            header_end = Some(idx);
            break;
        }
    }
    let Some(headers_end) = header_end else {
        let _ = stream.write_all(
            b"HTTP/1.0 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
        );
        return Ok(());
    };
    let header_text = std::str::from_utf8(&buf[..headers_end]).unwrap_or("");
    let first_line = header_text.lines().next().unwrap_or("");
    let mut parts = first_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let raw_path = parts.next().unwrap_or("/").to_string();
    let path = raw_path.split('?').next().unwrap_or("/").to_string();

    let mut content_length = 0usize;
    for line in header_text.lines().skip(1) {
        if let Some((k, v)) = line.split_once(':') {
            if k.eq_ignore_ascii_case("content-length") {
                content_length = v.trim().parse::<usize>().unwrap_or(0);
            }
        }
    }

    let mut body = Vec::new();
    if matches!(method.as_str(), "POST" | "PUT") && content_length > 0 && content_length < 64 * 1024
    {
        let header_total = headers_end + 4;
        if len > header_total {
            body.extend_from_slice(&buf[header_total..len]);
        }
        while body.len() < content_length {
            let mut chunk = [0u8; 1024];
            let n = stream.read(&mut chunk)?;
            if n == 0 {
                break;
            }
            body.extend_from_slice(&chunk[..n]);
        }
        body.truncate(content_length.min(body.len()));
    }

    let response: Vec<u8> = match (method.as_str(), path.as_str()) {
        // Editor HTML — same asset the host serves
        // (`crates/web/assets/config_editor.html` →
        // `astrameter_web::config_ui::CONFIG_EDITOR_HTML`).
        ("GET", "/") | ("GET", "/config") | ("GET", "/config/") => http_response(
            200,
            "OK",
            "text/html; charset=utf-8",
            astrameter_web::config_ui::CONFIG_EDITOR_HTML.as_bytes(),
        ),

        ("GET", "/api/config") | ("GET", "/api/config/") => {
            match read_nvs_config_as_dict(nvs_part) {
                Ok(json) => http_response(
                    200,
                    "OK",
                    "application/json; charset=utf-8",
                    json.as_bytes(),
                ),
                Err(e) => http_response(
                    500,
                    "Internal Server Error",
                    "application/json; charset=utf-8",
                    format!(r#"{{"error":"{}"}}"#, escape_json(&e.to_string())).as_bytes(),
                ),
            }
        }

        ("POST", "/api/config") | ("POST", "/api/config/") => {
            match write_nvs_config_from_dict(nvs_part, &body) {
                Ok(()) => {
                    log::info!("config web: saved config via /api/config; restarting");
                    let out = http_response(
                        200,
                        "OK",
                        "application/json; charset=utf-8",
                        br#"{"success":true}"#,
                    );
                    stream.write_all(&out)?;
                    let _ = stream.flush();
                    std::thread::sleep(std::time::Duration::from_secs(1));
                    unsafe { esp_idf_svc::sys::esp_restart() };
                    #[allow(unreachable_code)]
                    Vec::new()
                }
                Err(e) => http_response(
                    400,
                    "Bad Request",
                    "application/json; charset=utf-8",
                    format!(
                        r#"{{"success":false,"error":"{}"}}"#,
                        escape_json(&e.to_string())
                    )
                    .as_bytes(),
                ),
            }
        }

        ("GET", "/api/key-types") | ("GET", "/api/key-types/") => http_response(
            200,
            "OK",
            "application/json; charset=utf-8",
            astrameter_web::config_ui::SECTION_KEY_TYPES_JSON.as_bytes(),
        ),

        ("POST", "/api/restart") | ("POST", "/api/restart/") => {
            log::info!("config web: /api/restart — rebooting");
            let out = http_response(
                202,
                "Accepted",
                "application/json; charset=utf-8",
                br#"{"success":true}"#,
            );
            stream.write_all(&out)?;
            let _ = stream.flush();
            std::thread::sleep(std::time::Duration::from_secs(1));
            unsafe { esp_idf_svc::sys::esp_restart() };
            #[allow(unreachable_code)]
            Vec::new()
        }

        ("GET", "/health") | ("GET", "/health/") => {
            let body = format!(
                r#"{{"status":"healthy","service":"astrameter","version":"{}","healthy":true}}"#,
                astrameter_core::VERSION
            );
            http_response(
                200,
                "OK",
                "application/json; charset=utf-8",
                body.as_bytes(),
            )
        }

        ("POST", "/api/wifi/reset") | ("POST", "/api/wifi/reset/") => {
            let nvs_rw =
                match esp_idf_svc::nvs::EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true) {
                    Ok(n) => n,
                    Err(e) => {
                        return Err(std::io::Error::other(format!("nvs open rw: {e}")));
                    }
                };
            let _ = nvs_rw.remove(nvs_keys::WIFI_SSID);
            let _ = nvs_rw.remove(nvs_keys::WIFI_PASS);
            log::info!("config web: Wi-Fi creds cleared; restarting");
            let out = http_response(
                200,
                "OK",
                "application/json; charset=utf-8",
                br#"{"success":true,"message":"wifi creds cleared; rebooting to captive portal"}"#,
            );
            stream.write_all(&out)?;
            let _ = stream.flush();
            std::thread::sleep(std::time::Duration::from_secs(1));
            unsafe { esp_idf_svc::sys::esp_restart() };
            #[allow(unreachable_code)]
            Vec::new()
        }

        _ => http_response(404, "Not Found", "text/plain; charset=utf-8", b"not found"),
    };
    stream.write_all(&response)?;
    let _ = stream.flush();
    Ok(())
}

/// Read the NVS-stored `config.ini`, parse it, and emit the
/// `{sections, order}` JSON the editor expects (same shape the host
/// `GET /api/config` returns).
#[cfg(target_os = "espidf")]
fn read_nvs_config_as_dict(
    nvs_part: &esp_idf_svc::nvs::EspDefaultNvsPartition,
) -> anyhow::Result<String> {
    use esp_idf_svc::nvs::EspNvs;
    let nvs = EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true)
        .map_err(|e| anyhow::anyhow!("nvs open: {e}"))?;
    let mut cfg_buf = vec![0u8; 4096];
    let raw = nvs
        .get_str(nvs_keys::CONFIG, &mut cfg_buf)
        .ok()
        .flatten()
        .unwrap_or(EMBEDDED_DEFAULT_CONFIG)
        .to_string();
    let cfg = astrameter_config::Config::parse(&raw).map_err(|e| anyhow::anyhow!("parse: {e}"))?;
    let mut sections = serde_json::Map::new();
    let mut order: Vec<serde_json::Value> = Vec::new();
    for name in cfg.sections() {
        let Some(section) = cfg.section(name) else {
            continue;
        };
        let mut keys = serde_json::Map::new();
        for (k, v) in section.entries() {
            keys.insert(k.to_string(), serde_json::Value::String(v.to_string()));
        }
        sections.insert(name.to_string(), serde_json::Value::Object(keys));
        order.push(serde_json::Value::String(name.to_string()));
    }
    let payload = serde_json::json!({"sections": sections, "order": order});
    Ok(serde_json::to_string(&payload)?)
}

/// Accept the editor's `{sections, order}` payload, rebuild it into
/// `config.ini` text, validate, persist to NVS.
#[cfg(target_os = "espidf")]
fn write_nvs_config_from_dict(
    nvs_part: &esp_idf_svc::nvs::EspDefaultNvsPartition,
    body: &[u8],
) -> anyhow::Result<()> {
    use esp_idf_svc::nvs::EspNvs;
    let payload: serde_json::Value =
        serde_json::from_slice(body).map_err(|e| anyhow::anyhow!("bad JSON: {e}"))?;
    let sections = payload
        .get("sections")
        .and_then(|v| v.as_object())
        .ok_or_else(|| anyhow::anyhow!("payload missing sections"))?;
    let order: Vec<String> = payload
        .get("order")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_else(|| sections.keys().cloned().collect());

    let mut text = String::new();
    for name in &order {
        text.push('[');
        text.push_str(name);
        text.push_str("]\n");
        let Some(keys) = sections.get(name).and_then(|v| v.as_object()) else {
            continue;
        };
        for (k, v) in keys {
            text.push_str(k);
            text.push_str(" = ");
            text.push_str(&match v {
                serde_json::Value::String(s) => s.clone(),
                serde_json::Value::Number(n) => n.to_string(),
                serde_json::Value::Bool(b) => b.to_string(),
                serde_json::Value::Null => String::new(),
                other => other.to_string(),
            });
            text.push('\n');
        }
        text.push('\n');
    }

    astrameter_config::Config::parse(&text)
        .map_err(|e| anyhow::anyhow!("rebuilt config didn't parse: {e}"))?;

    let nvs_rw = EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true)
        .map_err(|e| anyhow::anyhow!("nvs open rw: {e}"))?;
    nvs_rw
        .set_str(nvs_keys::CONFIG, &text)
        .map_err(|e| anyhow::anyhow!("nvs write: {e}"))?;
    Ok(())
}

/// Log the current FreeRTOS task handle alongside a human-readable
/// label, so a later stack-overflow report (which only knows the
/// FreeRTOS task name — and Rust pthreads all share the default
/// "pthread" name because `Builder::name` doesn't propagate to the
/// FreeRTOS layer on IDF) can be cross-referenced back to which
/// Rust thread it actually was.
#[cfg(target_os = "espidf")]
fn log_task_handle(label: &str) {
    let h = unsafe { esp_idf_svc::sys::xTaskGetCurrentTaskHandle() };
    log::info!("task[{label}]: handle={h:p}");
}

/// Tracing `Subscriber` that forwards every event to the `log` crate.
/// Installed once at boot so `tracing::info!` / `warn!` / `error!`
/// calls from deps (notably `insights-mqtt`) show up on the serial
/// console via `EspLogger`. Span machinery is no-op — we only care
/// about events for diagnostics.
#[cfg(target_os = "espidf")]
struct TracingToLog;

#[cfg(target_os = "espidf")]
impl tracing_core::Subscriber for TracingToLog {
    fn enabled(&self, _: &tracing_core::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &tracing_core::span::Attributes<'_>) -> tracing_core::span::Id {
        tracing_core::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing_core::span::Id, _: &tracing_core::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing_core::span::Id, _: &tracing_core::span::Id) {}
    fn enter(&self, _: &tracing_core::span::Id) {}
    fn exit(&self, _: &tracing_core::span::Id) {}
    fn event(&self, event: &tracing_core::Event<'_>) {
        struct Visitor(String);
        impl tracing_core::field::Visit for Visitor {
            fn record_debug(&mut self, field: &tracing_core::Field, value: &dyn core::fmt::Debug) {
                use core::fmt::Write;
                if field.name() == "message" {
                    let _ = write!(self.0, "{value:?}");
                } else {
                    let _ = write!(self.0, " {}={value:?}", field.name());
                }
            }
            fn record_str(&mut self, field: &tracing_core::Field, value: &str) {
                use core::fmt::Write;
                if field.name() == "message" {
                    self.0.push_str(value);
                } else {
                    let _ = write!(self.0, " {}={value:?}", field.name());
                }
            }
        }
        let meta = event.metadata();
        let level = match *meta.level() {
            tracing_core::Level::ERROR => log::Level::Error,
            tracing_core::Level::WARN => log::Level::Warn,
            tracing_core::Level::INFO => log::Level::Info,
            tracing_core::Level::DEBUG => log::Level::Debug,
            tracing_core::Level::TRACE => log::Level::Trace,
        };
        let mut v = Visitor(String::new());
        event.record(&mut v);
        log::log!(target: meta.target(), level, "{}", v.0);
    }
}

/// Log internal-SRAM and PSRAM heap state. `pthread_create` allocates
/// task stacks from internal SRAM only, so the "largest free internal
/// block" is the metric that determines whether the next blocking
/// thread can be spawned — total free is misleading once the heap is
/// fragmented.
#[cfg(target_os = "espidf")]
pub(crate) fn log_heap(label: &str) {
    use esp_idf_svc::sys::{
        heap_caps_get_free_size, heap_caps_get_largest_free_block, MALLOC_CAP_INTERNAL,
        MALLOC_CAP_SPIRAM,
    };
    unsafe {
        let int_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
        let int_largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
        let psram_free = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
        let psram_largest = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM);
        log::info!(
            "heap[{label}]: internal free={int_free} largest={int_largest} | psram free={psram_free} largest={psram_largest}"
        );
    }
}

/// Override FreeRTOS's default stack-overflow hook (which is
/// declared `__attribute__((weak))` in port_common.c) so we can
/// surface the offending task's *handle* in addition to its name.
/// All Rust pthreads share the FreeRTOS-level name "pthread", so
/// the name alone doesn't identify the culprit — the handle does
/// (cross-reference against the `task[…]: handle=0x…` lines logged
/// at thread spawn).
#[cfg(target_os = "espidf")]
#[no_mangle]
pub unsafe extern "C" fn vApplicationStackOverflowHook(
    task: esp_idf_svc::sys::TaskHandle_t,
    name: *const core::ffi::c_char,
) {
    let name_ptr = if name.is_null() {
        b"(null)\0".as_ptr() as *const core::ffi::c_char
    } else {
        name
    };
    // `esp_rom_printf` is safe to call from any context (no malloc,
    // no FreeRTOS APIs) — important because we're already in a bad
    // state when this fires.
    unsafe {
        esp_idf_svc::sys::esp_rom_printf(
            b"\n*** AstraMeter STACK OVERFLOW: task=\"%s\" handle=%p ***\n\0".as_ptr()
                as *const core::ffi::c_char,
            name_ptr,
            task,
        );
        esp_idf_svc::sys::esp_system_abort(
            b"stack overflow (see ROM printf line above for task handle)\0".as_ptr()
                as *const core::ffi::c_char,
        );
    }
}

#[cfg(target_os = "espidf")]
fn escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}
