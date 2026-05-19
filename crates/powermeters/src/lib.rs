//! AstraMeter powermeter implementations + registry.
//!
//! Every implementation is linked into the binary on both targets and
//! selected at runtime by section-name prefix, so the web UI can swap
//! meters without a firmware rebuild.

#![forbid(unsafe_code)]

use std::collections::HashMap;
use std::sync::Arc;

use astrameter_config::Section;
#[cfg(target_os = "espidf")]
use astrameter_core::Error;
use astrameter_core::{Powermeter, Result};
use astrameter_platform::Platform;

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

pub mod pipeline;
pub use pipeline::{read_all_powermeter_configs, BoundPowermeter};

/// Builder function for a single powermeter section.
pub type PowermeterFactory = fn(&Section<'_>, Arc<Platform>) -> Result<Arc<dyn Powermeter>>;

/// Runtime section-prefix dispatch table. Mirrors `create_powermeter()` in
/// `src/astrameter/config/config_loader.py`.
#[derive(Default)]
pub struct PowermeterRegistry {
    entries: HashMap<&'static str, PowermeterFactory>,
    order: Vec<&'static str>,
}

impl PowermeterRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register `factory` for a section-name prefix. Order of registration
    /// determines tie-breaking when one prefix is itself a prefix of another
    /// (e.g. `MQTT` vs `MQTT_INSIGHTS`): registered prefixes are tried in
    /// insertion order, and `MQTT_INSIGHTS` should be registered first so it
    /// wins over the more permissive `MQTT`.
    pub fn register(&mut self, prefix: &'static str, factory: PowermeterFactory) {
        if !self.entries.contains_key(prefix) {
            self.order.push(prefix);
        }
        self.entries.insert(prefix, factory);
    }

    pub fn lookup(&self, section_name: &str) -> Option<PowermeterFactory> {
        // Iterate in registration order so callers can control precedence
        // (e.g. MQTT_INSIGHTS before MQTT).
        for prefix in &self.order {
            if section_name.starts_with(prefix) {
                if let Some(f) = self.entries.get(prefix) {
                    return Some(*f);
                }
            }
        }
        None
    }

    pub fn prefixes(&self) -> impl Iterator<Item = &&'static str> {
        self.order.iter()
    }
}

/// Register every available powermeter into `reg` in the order that mirrors
/// `create_powermeter()` in Python. MQTT_INSIGHTS is treated separately by
/// the insights service and is **not** registered here; the bare `MQTT`
/// prefix below explicitly skips MQTT_INSIGHTS-prefixed sections via
/// `lookup`'s order.
pub fn register_all(reg: &mut PowermeterRegistry) {
    // Note: the registry uses first-match-by-prefix in insertion order, so
    // register the more specific prefixes first when there's overlap.
    reg.register("SHELLY", shelly::create);
    reg.register("TASMOTA", tasmota::create);
    reg.register("SHRDZM", shrdzm::create);
    reg.register("EMLOG", emlog::create);
    reg.register("IOBROKER", iobroker::create);
    reg.register("HOMEASSISTANT", homeassistant::create);
    reg.register("VZLOGGER", vzlogger::create);
    #[cfg(not(target_os = "espidf"))]
    reg.register("SCRIPT", script::create);
    #[cfg(target_os = "espidf")]
    reg.register("SCRIPT", script_unsupported);
    reg.register("SML", sml::create);
    reg.register("ESPHOME", esphome::create);
    reg.register("AMIS_READER", amisreader::create);
    reg.register("MODBUS", modbus::create);
    reg.register("JSON_HTTP", json_http::create);
    reg.register("TQ_EM", tq_em::create);
    reg.register("HOMEWIZARD", homewizard::create);
    reg.register("ENVOY", envoy::create);
    reg.register("SMA_ENERGY_METER", sma_energy_meter::create);
    // MQTT comes last so MQTT_INSIGHTS-prefixed sections (handled elsewhere)
    // can be filtered before falling through here.
    reg.register("MQTT", mqtt::create);
}

#[cfg(target_os = "espidf")]
fn script_unsupported(
    _section: &Section<'_>,
    _platform: Arc<Platform>,
) -> Result<Arc<dyn Powermeter>> {
    Err(Error::UnsupportedOnPlatform(
        "SCRIPT powermeter is unavailable on ESP32 (no subprocess support)",
    ))
}

// Re-export the Error name so the `script_unsupported` shim compiles cleanly
// across targets without rust-analyzer complaining about unused imports.
#[cfg(not(target_os = "espidf"))]
#[allow(unused_imports)]
use astrameter_core::Error as _Error;
