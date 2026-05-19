//! ESP32 (esp-idf-svc / esp-idf-hal) implementations of
//! `astrameter-platform` traits.
//!
//! On non-espidf targets the crate intentionally exposes no symbols so
//! `cargo check --workspace` works on stock Linux. Real impls live under
//! `cfg(target_os = "espidf")`.
//!
//! The build for ESP32 needs `espup install` + the Xtensa toolchain. From
//! the repo root:
//!
//! ```text
//! cargo +esp build --release -p astrameter-esp32 \
//!     --target xtensa-esp32s3-espidf
//! ```

#![cfg_attr(not(target_os = "espidf"), allow(dead_code))]

#[cfg(target_os = "espidf")]
mod http_impl;
#[cfg(target_os = "espidf")]
mod mqtt_impl;
#[cfg(target_os = "espidf")]
mod net_impl;
#[cfg(target_os = "espidf")]
mod serial_impl;
#[cfg(target_os = "espidf")]
mod time_impl;
#[cfg(target_os = "espidf")]
mod ws_impl;

#[cfg(target_os = "espidf")]
mod imp {
    use astrameter_platform::Platform;
    use std::sync::Arc;

    /// Build a fully-wired platform suitable for use from the ESP32 boot
    /// path. The caller must have already initialised Wi-Fi (so sockets
    /// resolve hostnames) and SNTP (so wall-clock timestamps in
    /// `Timer::unix_secs()` are meaningful for HA discovery and the
    /// Marstek bridge).
    pub fn build_platform() -> Platform {
        Platform {
            http: Arc::new(super::http_impl::EspHttpClient),
            mqtt: Arc::new(super::mqtt_impl::RumqttcFactory),
            ws: Arc::new(super::ws_impl::TungsteniteClient::new()),
            udp: Arc::new(super::net_impl::TokioUdpBind),
            tcp: Arc::new(super::net_impl::TokioTcpConnect),
            serial: Arc::new(super::serial_impl::EspUartSerial),
            timer: Arc::new(super::time_impl::TokioTimer),
        }
    }
}

#[cfg(target_os = "espidf")]
pub use imp::build_platform;
