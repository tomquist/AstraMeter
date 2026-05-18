//! AstraMeter powermeter implementations.
//!
//! Every module in this crate calls `register()` into the
//! [`astrameter_core::PowermeterRegistry`] at startup, so users can pick any
//! meter at runtime via `config.ini` without a firmware rebuild.
//!
//! The `script` module is host-only (no subprocess on `esp-idf`) and is
//! conditionally compiled.

#![forbid(unsafe_code)]

use astrameter_core::PowermeterRegistry;

// Phase 1-3 fills these in with real implementations. The empty modules below
// reserve filenames matching the Python sources so reviewers can diff 1:1.

pub mod amisreader;
pub mod emlog;
pub mod envoy;
pub mod esphome;
pub mod homeassistant;
pub mod homewizard;
pub mod iobroker;
pub mod json_http;
pub mod modbus;
pub mod mqtt;
#[cfg(not(target_os = "espidf"))]
pub mod script;
pub mod shelly;
pub mod shrdzm;
pub mod sma_energy_meter;
pub mod sml;
pub mod tasmota;
pub mod tq_em;
pub mod vzlogger;

/// Register every available powermeter factory into `reg`. Called once at
/// startup from each binary.
pub fn register_all(_reg: &mut PowermeterRegistry) {
    // Phase 1 wires each module's `register()` here. For example:
    //
    //     shelly::register(reg);
    //     mqtt::register(reg);
    //     #[cfg(not(target_os = "espidf"))]
    //     script::register(reg);
    //
    // Keeping this empty for the skeleton commit.
}
