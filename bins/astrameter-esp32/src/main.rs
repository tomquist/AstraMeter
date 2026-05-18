//! AstraMeter ESP32-S3 entry point.
//!
//! Phase 0: a stub. On non-espidf targets the binary just prints a helpful
//! message and exits so `cargo check --workspace` still works on a Linux dev
//! machine. The real boot sequence (Wi-Fi provisioning → LittleFS mount →
//! config load → Supervisor) lands in Phase 8 under `cfg(target_os =
//! "espidf")`.

#[cfg(target_os = "espidf")]
fn main() {
    // esp_idf_svc::sys::link_patches();
    // esp_idf_svc::log::EspLogger::initialize_default();
    // log::info!(
    //     "astrameter-esp32 {} starting",
    //     astrameter_core::VERSION
    // );
    // TODO(phase-8): mount LittleFS, provision Wi-Fi, start Supervisor.
    panic!("phase-0 skeleton: ESP32 boot path lands in Phase 8");
}

#[cfg(not(target_os = "espidf"))]
fn main() {
    eprintln!(
        "astrameter-esp32 {} is a stub on host targets — build with `cargo \
         +esp build -p astrameter-esp32 --target xtensa-esp32s3-espidf`",
        astrameter_core::VERSION,
    );
}
