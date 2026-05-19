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
//!   2. Mount LittleFS partition at `/littlefs`. Seed `config.ini` from
//!      the embedded default if absent.
//!   3. Bring up Wi-Fi STA from `wifi.json`.
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

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .thread_stack_size(16 * 1024)
        .build()?;
    runtime.block_on(async_main())
}

#[cfg(target_os = "espidf")]
async fn async_main() -> anyhow::Result<()> {
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::time::Duration;

    use astrameter_config::Config;
    use astrameter_emulator_ct002::server::{BoundMeter as Ct002BoundMeter, Ct002Emulator};
    use astrameter_emulator_shelly::{BoundMeter as ShellyBoundMeter, ShellyEmulator};
    use astrameter_powermeters::{read_all_powermeter_configs, register_all, PowermeterRegistry};

    mount_littlefs()?;
    let cfg_path = PathBuf::from("/littlefs/config.ini");
    if !cfg_path.exists() {
        log::warn!("config.ini missing on LittleFS — seeding from embedded default");
        std::fs::write(&cfg_path, EMBEDDED_DEFAULT_CONFIG)?;
    }

    bring_up_wifi()?;
    start_sntp_and_wait_for_sync().await?;

    let raw = std::fs::read_to_string(&cfg_path)?;
    let config = Config::parse(&raw)?;
    let platform = Arc::new(astrameter_platform_espidf::build_platform());
    let mut reg = PowermeterRegistry::new();
    register_all(&mut reg);
    let registry = Arc::new(reg);

    let bound = read_all_powermeter_configs(&config, &registry, platform.clone())?;
    for bp in &bound {
        let _ = bp.meter.start().await;
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

    let _emu = match device_type.as_str() {
        "ct002" | "ct003" => {
            let section_name = if device_type == "ct003" && config.section("CT003").is_some() {
                "CT003"
            } else {
                "CT002"
            };
            let section = config
                .section(section_name)
                .ok_or_else(|| anyhow::anyhow!("section [{section_name}] missing"))?;
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

#[cfg(target_os = "espidf")]
const EMBEDDED_DEFAULT_CONFIG: &[u8] = b"[GENERAL]\nDEVICE_TYPE=ct002\n";

#[cfg(target_os = "espidf")]
fn mount_littlefs() -> anyhow::Result<()> {
    // The `esp_littlefs` component isn't part of `esp_idf_svc::sys` by
    // default — it lives in an optional ESP-IDF managed component that
    // would have to be pulled in via `idf_component.yml`. The partition
    // table (partitions.csv) declares this slot as `spiffs`, which IS
    // built into ESP-IDF, so use that. The mount path is kept at
    // `/littlefs` for source-code parity with the migration plan and
    // the host fallback.
    use esp_idf_svc::sys::{esp_vfs_spiffs_conf_t, esp_vfs_spiffs_register, EspError, ESP_OK};
    let base = std::ffi::CString::new("/littlefs")?;
    let partition = std::ffi::CString::new("storage")?;
    let conf = esp_vfs_spiffs_conf_t {
        base_path: base.as_ptr(),
        partition_label: partition.as_ptr(),
        max_files: 5,
        format_if_mount_failed: true,
    };
    let err = unsafe { esp_vfs_spiffs_register(&conf) };
    if err != ESP_OK {
        anyhow::bail!("spiffs mount failed: {:?}", EspError::from(err));
    }
    log::info!("SPIFFS mounted at /littlefs");
    Ok(())
}

#[cfg(target_os = "espidf")]
fn bring_up_wifi() -> anyhow::Result<()> {
    use embedded_svc::wifi::{AuthMethod, ClientConfiguration, Configuration};
    use esp_idf_svc::eventloop::EspSystemEventLoop;
    use esp_idf_svc::hal::peripherals::Peripherals;
    use esp_idf_svc::nvs::EspDefaultNvsPartition;
    use esp_idf_svc::wifi::{BlockingWifi, EspWifi};

    let peripherals = Peripherals::take()?;
    let sysloop = EspSystemEventLoop::take()?;
    let nvs = EspDefaultNvsPartition::take()?;

    let raw = std::fs::read_to_string("/littlefs/wifi.json").unwrap_or_else(|_| "{}".to_string());
    let creds: serde_json::Value = serde_json::from_str(&raw)?;
    let ssid = creds
        .get("ssid")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("wifi.json: ssid missing"))?;
    let password = creds.get("password").and_then(|v| v.as_str()).unwrap_or("");

    let mut wifi = BlockingWifi::wrap(
        EspWifi::new(peripherals.modem, sysloop.clone(), Some(nvs))?,
        sysloop,
    )?;
    wifi.set_configuration(&Configuration::Client(ClientConfiguration {
        ssid: ssid.parse().map_err(|_| anyhow::anyhow!("ssid too long"))?,
        password: password
            .parse()
            .map_err(|_| anyhow::anyhow!("password too long"))?,
        auth_method: if password.is_empty() {
            AuthMethod::None
        } else {
            AuthMethod::WPA2Personal
        },
        ..Default::default()
    }))?;
    wifi.start()?;
    wifi.connect()?;
    wifi.wait_netif_up()?;
    log::info!(
        "Wi-Fi connected; IP info: {:?}",
        wifi.wifi().sta_netif().get_ip_info()?
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
    })?;
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
