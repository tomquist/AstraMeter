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
    log::info!("AstraMeter ESP32 {} booting", astrameter_core::VERSION);

    log::info!("step: build tokio runtime");
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .thread_stack_size(16 * 1024)
        .build()
        .map_err(|e| anyhow::anyhow!("tokio runtime build: {e}"))?;
    log::info!("step: enter async_main");
    let result = runtime.block_on(async_main());
    if let Err(e) = &result {
        // Log before propagating so the error is visible even if the
        // outer espidf wrapper just prints `Error: ...` and reboots.
        log::error!("async_main exited: {e:?}");
    }
    result
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

    log::info!("step: bring up Wi-Fi");
    if let Err(e) = bring_up_wifi(&sysloop, nvs_part.clone()) {
        // Don't abort the boot — log loudly and keep going so the user
        // sees the next step. Without Wi-Fi most powermeters will fail
        // their initial connect, but the firmware itself stays alive
        // for OTA recovery and serial debugging.
        log::error!("Wi-Fi bring-up failed: {e}. Continuing without network.");
    } else {
        log::info!("step: Wi-Fi up; starting SNTP");
        if let Err(e) = start_sntp_and_wait_for_sync().await {
            log::warn!("SNTP sync skipped: {e}");
        }
    }

    log::info!("step: build platform");
    let platform = Arc::new(astrameter_platform_espidf::build_platform());

    log::info!("step: register powermeters");
    let mut reg = PowermeterRegistry::new();
    register_all(&mut reg);
    let registry = Arc::new(reg);

    log::info!("step: bind powermeters from config");
    let bound = read_all_powermeter_configs(&config, &registry, platform.clone())
        .map_err(|e| anyhow::anyhow!("bind powermeters: {e}"))?;
    for bp in &bound {
        if let Err(e) = bp.meter.start().await {
            log::warn!("powermeter [{}] start: {e}", bp.section);
        }
    }

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

    log::info!("step: start emulator (DEVICE_TYPE={device_type})");
    let _emu = match device_type.as_str() {
        "ct002" | "ct003" => {
            let section_name = if device_type == "ct003" && config.section("CT003").is_some() {
                "CT003"
            } else {
                "CT002"
            };
            let section = match config.section(section_name) {
                Some(s) => s,
                None => {
                    log::warn!("[{section_name}] missing — skipping emulator");
                    return Ok(());
                }
            };
            let udp_port = section.get_int("UDP_PORT", 12345)? as u16;
            let ct_mac = section.get_string("CT_MAC", "");
            let meters: Vec<Ct002BoundMeter> = bound
                .iter()
                .map(|bp| Ct002BoundMeter {
                    meter: bp.meter.clone(),
                    filter: bp.client_filter.clone(),
                    wait_for_next: bp.wait_for_next_message,
                })
                .collect();
            let emu = Arc::new(Ct002Emulator::new(
                udp_port,
                ct_mac,
                meters,
                astrameter_emulator_ct002::balancer::BalancerConfig::default(),
                platform.clone(),
            ));
            emu.start().await?;
            Some(EsplEmu::Ct002(emu))
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
            Some(EsplEmu::Shelly(emu))
        }
        other => {
            log::warn!("DEVICE_TYPE={other:?} not recognised; no emulator started");
            None
        }
    };

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

#[cfg(target_os = "espidf")]
fn bring_up_wifi(
    sysloop: &esp_idf_svc::eventloop::EspSystemEventLoop,
    nvs_part: esp_idf_svc::nvs::EspDefaultNvsPartition,
) -> anyhow::Result<()> {
    use embedded_svc::wifi::{AuthMethod, ClientConfiguration, Configuration};
    use esp_idf_svc::hal::peripherals::Peripherals;
    use esp_idf_svc::nvs::EspNvs;
    use esp_idf_svc::wifi::{BlockingWifi, EspWifi};

    // Pull Wi-Fi creds from the same NVS namespace as `config`.
    let nvs = EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true)
        .map_err(|e| anyhow::anyhow!("open NVS for wifi: {e}"))?;
    let mut ssid_buf = [0u8; 64];
    let mut pass_buf = [0u8; 128];
    let ssid = nvs
        .get_str(nvs_keys::WIFI_SSID, &mut ssid_buf)
        .ok()
        .flatten()
        .map(|s| s.to_string())
        .unwrap_or_default();
    let password = nvs
        .get_str(nvs_keys::WIFI_PASS, &mut pass_buf)
        .ok()
        .flatten()
        .map(|s| s.to_string())
        .unwrap_or_default();
    if ssid.is_empty() {
        anyhow::bail!(
            "no Wi-Fi SSID in NVS (namespace={}, key={}). Set with `esp-idf-mfg-util` or \
             the future captive portal.",
            nvs_keys::NAMESPACE,
            nvs_keys::WIFI_SSID
        );
    }

    let peripherals = Peripherals::take().map_err(|e| anyhow::anyhow!("Peripherals::take: {e}"))?;
    let mut wifi = BlockingWifi::wrap(
        EspWifi::new(peripherals.modem, sysloop.clone(), Some(nvs_part))
            .map_err(|e| anyhow::anyhow!("EspWifi::new: {e}"))?,
        sysloop.clone(),
    )
    .map_err(|e| anyhow::anyhow!("BlockingWifi::wrap: {e}"))?;
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
    Ok(())
}

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
