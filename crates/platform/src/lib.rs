//! Platform abstraction layer.
//!
//! Service crates depend only on the traits in this crate; concrete impls
//! live in `astrameter-platform-std` (host) and `astrameter-platform-espidf`
//! (ESP32). Phase 1 fleshes these traits out.

#![forbid(unsafe_code)]

pub mod http;
pub mod mqtt;
pub mod net;
pub mod serial;
pub mod time;
pub mod ws;
