//! ESP32 (esp-idf-svc / esp-idf-hal) implementations of
//! `astrameter-platform` traits.
//!
//! This crate's content is conditionally compiled: on non-espidf targets
//! it is intentionally empty, so `cargo check --workspace` works on a
//! stock Linux dev box without `espup` installed. The real impls live
//! under `cfg(target_os = "espidf")`.
//!
//! **Status:** Phase 8 of the migration. The current code provides the
//! module layout and `build_platform()` factory signature; concrete
//! impls (HTTP via `EspHttpConnection`, UART, LittleFS-backed config,
//! Wi-Fi bring-up) are scaffolded as TODOs and require on-target
//! development.

#![cfg_attr(not(target_os = "espidf"), allow(dead_code))]
#![forbid(unsafe_code)]

#[cfg(target_os = "espidf")]
mod imp {
    use astrameter_platform::Platform;

    pub fn build_platform() -> Platform {
        // Phase 8 wires concrete impls:
        //   * HTTP        : esp_idf_svc::http::client::EspHttpConnection
        //   * HTTP server : esp_idf_svc::http::server::EspHttpServer
        //   * MQTT        : rumqttc (same crate as host) over Wi-Fi sockets
        //   * UDP         : std::net::UdpSocket wrapped via tokio
        //   * Multicast   : socket2 + lwIP IP_ADD_MEMBERSHIP
        //   * Serial      : esp_idf_hal::uart
        //   * Timer       : tokio::time on the esp-idf pthread layer
        //   * Filesystem  : LittleFS partition mounted at /littlefs
        unimplemented!(
            "Phase 8: platform-espidf needs concrete impls (see crate docs)"
        );
    }
}

#[cfg(target_os = "espidf")]
pub use imp::build_platform;
