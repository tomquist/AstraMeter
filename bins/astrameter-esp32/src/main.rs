//! AstraMeter ESP32-S3 entry point.
//!
//! On non-espidf targets this binary is a stub that prints how to build it.
//! The real boot path lives under `cfg(target_os = "espidf")` and is
//! scaffolded but not wired through yet — see the migration plan, Phase 8.

#[cfg(target_os = "espidf")]
fn main() -> anyhow::Result<()> {
    // esp_idf_svc::sys::link_patches();
    // esp_idf_svc::log::EspLogger::initialize_default();
    // log::info!("AstraMeter ESP32 {} boot", astrameter_core::VERSION);
    //
    // Phase 8 wiring:
    //   1. Mount LittleFS at /littlefs and read /littlefs/config.ini.
    //   2. Bring up Wi-Fi (STA + provisioning AP fallback).
    //   3. Build platform via astrameter_platform_espidf::build_platform().
    //   4. Build PowermeterRegistry, spawn supervisor, start web server.
    //   5. Implement OTA route via esp_idf_svc::ota::EspOta.
    //
    // Until those land the binary intentionally panics so it's obvious the
    // ESP32 path isn't ready for production.
    anyhow::bail!("ESP32 boot path not wired yet (Phase 8 work-in-progress)")
}

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
