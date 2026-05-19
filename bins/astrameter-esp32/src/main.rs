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
//!   1. `esp_idf_svc::sys::link_patches()` + tracing/log bridge.
//!   2. Mount LittleFS partition at `/littlefs`. Seed `config.ini` from
//!      the embedded `config.ini.example` if absent.
//!   3. Bring up Wi-Fi STA from `wifi.json` (or fall back to provisioning
//!      AP — TODO).
//!   4. Start SNTP and wait until at least one timestamp arrives, so HA
//!      discovery and Marstek MQTT publish meaningful timestamps.
//!   5. Build the `Platform`, instantiate the `PowermeterRegistry`, and
//!      hand both to the same supervisor pattern as the host binary.

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

    use astrameter_powermeters::{register_all, PowermeterRegistry};

    mount_littlefs()?;
    let cfg_path = PathBuf::from("/littlefs/config.ini");
    if !cfg_path.exists() {
        log::warn!("config.ini missing on LittleFS — seeding from embedded default");
        std::fs::write(&cfg_path, EMBEDDED_DEFAULT_CONFIG)?;
    }

    bring_up_wifi()?;
    start_sntp_and_wait_for_sync().await?;

    let platform = Arc::new(astrameter_platform_espidf::build_platform());
    let mut reg = PowermeterRegistry::new();
    register_all(&mut reg);
    let registry = Arc::new(reg);
    let _ = (platform, registry, cfg_path);

    // TODO: instantiate the Supervisor here. Currently the firmware boots
    // through Wi-Fi + SNTP but doesn't yet drive the service tree; that
    // wiring mirrors bins/astrameter-host/src/main.rs but with the
    // EspHttpServer router instead of axum.
    log::info!("astrameter-esp32: idle (supervisor wiring is a TODO)");

    loop {
        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
    }
}

#[cfg(target_os = "espidf")]
const EMBEDDED_DEFAULT_CONFIG: &[u8] = b"[GENERAL]\nDEVICE_TYPE=ct002\n";

#[cfg(target_os = "espidf")]
fn mount_littlefs() -> anyhow::Result<()> {
    use esp_idf_svc::sys::{esp_vfs_littlefs_conf_t, esp_vfs_littlefs_register, EspError, ESP_OK};
    let base = std::ffi::CString::new("/littlefs")?;
    let partition = std::ffi::CString::new("storage")?;
    let conf = esp_vfs_littlefs_conf_t {
        base_path: base.as_ptr(),
        partition_label: partition.as_ptr(),
        format_if_mount_failed: 1,
        ..Default::default()
    };
    let err = unsafe { esp_vfs_littlefs_register(&conf) };
    if err != ESP_OK {
        anyhow::bail!("littlefs mount failed: {:?}", EspError::from(err));
    }
    log::info!("LittleFS mounted at /littlefs");
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

    // Read SSID / password from a tiny `/littlefs/wifi.json`. Production
    // setups should use `wifi_provisioning` SoftAP instead.
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
