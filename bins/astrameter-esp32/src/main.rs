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

    // The ESP-IDF "main" task only gets ~3.5 KB of stack by default, and
    // `sdkconfig.defaults` (where we'd raise `CONFIG_ESP_MAIN_TASK_STACK_SIZE`)
    // isn't being applied by `esp-idf-sys` against our crate (same root
    // cause as `partitions.csv` not loading). Side-step the whole issue
    // by spawning a worker pthread with a large stack and running the
    // tokio runtime there. `std::thread::Builder::stack_size` on
    // ESP-IDF maps to `pthread_attr_setstacksize` → FreeRTOS task stack,
    // so this is honoured.
    let worker = std::thread::Builder::new()
        .name("astrameter".into())
        .stack_size(64 * 1024)
        .spawn(|| -> anyhow::Result<()> {
            log::info!("step: build tokio runtime");
            // Tokio's IO driver (mio → epoll) doesn't initialise on
            // ESP-IDF — `enable_io()` returns
            // `Permission denied (os error 13)`. Build the runtime with
            // `enable_time()` only. Network sockets go through
            // `platform-espidf::net_impl`'s blocking-`std::net` +
            // `spawn_blocking` path so the lack of an IO driver doesn't
            // break the emulators.
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_time()
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
/// Wi-Fi credentials. Listens on TCP/80 on the STA interface. Uses the
/// same raw-TCP HTTP approach as the captive portal so we're not bound
/// by `esp_http_server`'s 512-byte request-header limit.
///
/// Endpoints:
///   * `GET  /`              — HTML form with the current `config.ini`
///                             in a `<textarea>` plus a Save button +
///                             a "Reset Wi-Fi" button.
///   * `GET  /api/config`    — `text/plain` body = current config.ini.
///   * `POST /api/config`    — body is the new config.ini text; saved
///                             to NVS, then `esp_restart()`.
///   * `POST /api/wifi/reset` — clears `wifi_ssid` / `wifi_pass` in
///                             NVS so the next boot drops back to the
///                             captive-portal setup AP.
#[cfg(target_os = "espidf")]
fn run_config_web_server(nvs_part: esp_idf_svc::nvs::EspDefaultNvsPartition) {
    use std::net::{Ipv4Addr, SocketAddr, TcpListener};

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
    use esp_idf_svc::nvs::EspNvs;
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
        ("GET", "/") => {
            // Form page: textarea pre-filled with current config.ini.
            let nvs = match EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true) {
                Ok(n) => n,
                Err(e) => {
                    return Err(std::io::Error::other(format!("nvs open: {e}")));
                }
            };
            let mut cfg_buf = vec![0u8; 4096];
            let cfg = nvs
                .get_str(nvs_keys::CONFIG, &mut cfg_buf)
                .ok()
                .flatten()
                .unwrap_or(EMBEDDED_DEFAULT_CONFIG);
            // Escape `<` / `&` for the textarea body.
            let escaped: String = cfg
                .chars()
                .map(|c| match c {
                    '<' => "&lt;".to_string(),
                    '&' => "&amp;".to_string(),
                    other => other.to_string(),
                })
                .collect();
            let html = CONFIG_HTML.replace("{{config}}", &escaped);
            http_response(200, "OK", "text/html; charset=utf-8", html.as_bytes())
        }
        ("GET", "/api/config") => {
            let nvs = match EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true) {
                Ok(n) => n,
                Err(e) => {
                    return Err(std::io::Error::other(format!("nvs open: {e}")));
                }
            };
            let mut cfg_buf = vec![0u8; 4096];
            let cfg = nvs
                .get_str(nvs_keys::CONFIG, &mut cfg_buf)
                .ok()
                .flatten()
                .unwrap_or(EMBEDDED_DEFAULT_CONFIG);
            http_response(200, "OK", "text/plain; charset=utf-8", cfg.as_bytes())
        }
        ("POST", "/api/config") => {
            let new_cfg = String::from_utf8_lossy(&body).to_string();
            // Try to parse so we don't persist garbage.
            if astrameter_config::Config::parse(&new_cfg).is_err() {
                http_response(
                    400,
                    "Bad Request",
                    "text/plain; charset=utf-8",
                    b"config.ini is not parseable; not saved",
                )
            } else {
                let nvs_rw = match EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true) {
                    Ok(n) => n,
                    Err(e) => {
                        return Err(std::io::Error::other(format!("nvs open rw: {e}")));
                    }
                };
                if let Err(e) = nvs_rw.set_str(nvs_keys::CONFIG, &new_cfg) {
                    return Err(std::io::Error::other(format!("nvs save config: {e}")));
                }
                log::info!(
                    "config web: saved {} bytes to NVS; restarting",
                    new_cfg.len()
                );
                let out =
                    http_response(200, "OK", "text/plain; charset=utf-8", b"saved; rebooting");
                stream.write_all(&out)?;
                let _ = stream.flush();
                std::thread::sleep(std::time::Duration::from_secs(1));
                unsafe { esp_idf_svc::sys::esp_restart() };
                #[allow(unreachable_code)]
                Vec::new()
            }
        }
        ("POST", "/api/wifi/reset") => {
            let nvs_rw = match EspNvs::new(nvs_part.clone(), nvs_keys::NAMESPACE, true) {
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
                "text/plain; charset=utf-8",
                b"wifi creds cleared; rebooting to captive portal",
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

/// HTML for the runtime config editor. Single page with a `<textarea>`
/// pre-filled with the current config.ini and a "Reset Wi-Fi" button.
#[cfg(target_os = "espidf")]
const CONFIG_HTML: &str = r#"<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AstraMeter config</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 50em; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { font-size: 1.2em; }
  textarea { width: 100%; height: 20em; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .9em; padding: .5em; box-sizing: border-box; border: 1px solid #aaa; border-radius: .25em; }
  .row { display: flex; gap: .5em; margin-top: 1em; }
  button { padding: .75em 1.2em; font-size: 1em; border: 0; border-radius: .25em; cursor: pointer; }
  .primary { background: #007aff; color: white; }
  .danger  { background: #d33; color: white; }
  #status { margin-top: 1em; min-height: 1.5em; color: #666; }
</style>
</head><body>
<h1>AstraMeter config (NVS)</h1>
<form id="cfg">
  <textarea id="ini" name="ini">{{config}}</textarea>
  <div class="row">
    <button type="submit" class="primary">Save &amp; reboot</button>
    <button type="button" class="danger" id="reset">Reset Wi-Fi (back to captive portal)</button>
  </div>
</form>
<div id="status"></div>
<script>
const $ = id => document.getElementById(id);
$("cfg").addEventListener("submit", async ev => {
  ev.preventDefault();
  $("status").textContent = "Saving…";
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "text/plain" },
      body: $("ini").value
    });
    const t = await r.text();
    $("status").textContent = r.ok
      ? "Saved; device is rebooting. Re-open this page in ~10s."
      : "Error " + r.status + ": " + t;
  } catch (e) {
    $("status").textContent = "Saved; device is rebooting. Re-open this page in ~10s.";
  }
});
$("reset").addEventListener("click", async () => {
  if (!confirm("Clear Wi-Fi creds and reboot into setup mode?")) return;
  $("status").textContent = "Clearing…";
  try {
    await fetch("/api/wifi/reset", { method: "POST" });
  } catch (e) {}
  $("status").textContent = "Done. Device will reboot into AstraMeter-Setup-* AP.";
});
</script>
</body></html>"#;
