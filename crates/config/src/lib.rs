//! Configuration loading.
//!
//! Mirrors Python `configparser` semantics from
//! `src/astrameter/config/config_loader.py`: INI sections + flat (section,
//! key) string lookups with `getfloat`/`getint`/`getboolean` coercion.
//! Comments round-trip via `rust-ini`'s preservation mode so the web config
//! editor can rewrite without losing user-authored comments.

#![forbid(unsafe_code)]

use std::path::Path;
use std::str::FromStr;

use astrameter_core::{Error, Result};

pub mod ini_wrap;
pub mod mqtt_uri;
pub mod netmask;

pub use ini_wrap::{Config, Section};
pub use mqtt_uri::{parse_mqtt_uri, MqttUriParts};
pub use netmask::ClientFilter;

/// Read a config file from disk.
pub fn load_file(path: &Path) -> Result<Config> {
    let raw = std::fs::read_to_string(path)
        .map_err(|e| Error::config(format!("failed to read {}: {e}", path.display())))?;
    Config::parse(&raw)
}

/// Parse a comma-separated list of floats. Empty input yields `[0.0]`,
/// mirroring `parse_float_list` in the Python loader.
pub fn parse_float_list(value: &str, key: &str, section: &str) -> Result<Vec<f64>> {
    let mut out = Vec::new();
    for tok in value.split(',') {
        let t = tok.trim();
        if t.is_empty() {
            continue;
        }
        let f = f64::from_str(t).map_err(|_| {
            Error::config(format!("invalid {key} value '{t}' in section [{section}]"))
        })?;
        out.push(f);
    }
    if out.is_empty() {
        out.push(0.0);
    }
    Ok(out)
}

/// Split `value` on `,`, trim whitespace, drop empties. Mirrors the Python
/// helper used by HOMEASSISTANT, VZLOGGER, TASMOTA, MQTT, JSON_HTTP.
pub fn split_labels(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}
