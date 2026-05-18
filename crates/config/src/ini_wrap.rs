//! INI wrapper providing `configparser`-style accessors.

use astrameter_core::{Error, Result};
use ini::Ini;
use std::str::FromStr;

/// In-memory configuration. Round-trips comments via `rust-ini`.
pub struct Config {
    inner: Ini,
}

impl Config {
    pub fn parse(text: &str) -> Result<Self> {
        let inner =
            Ini::load_from_str(text).map_err(|e| Error::config(format!("INI parse error: {e}")))?;
        Ok(Self { inner })
    }

    pub fn raw(&self) -> &Ini {
        &self.inner
    }

    pub fn raw_mut(&mut self) -> &mut Ini {
        &mut self.inner
    }

    /// List all section names (skipping the implicit empty/default section).
    pub fn sections(&self) -> impl Iterator<Item = &str> {
        self.inner.sections().flatten()
    }

    pub fn section<'a>(&'a self, name: &'a str) -> Option<Section<'a>> {
        self.inner
            .section(Some(name))
            .map(|props| Section { name, props })
    }
}

impl std::fmt::Display for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut buf = Vec::new();
        self.inner.write_to(&mut buf).map_err(|_| std::fmt::Error)?;
        let s = std::str::from_utf8(&buf).map_err(|_| std::fmt::Error)?;
        f.write_str(s)
    }
}

/// View into a single section.
pub struct Section<'a> {
    name: &'a str,
    props: &'a ini::Properties,
}

impl<'a> Section<'a> {
    pub fn name(&self) -> &str {
        self.name
    }

    pub fn has(&self, key: &str) -> bool {
        self.props.get(key).is_some()
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        self.props.get(key)
    }

    pub fn get_str(&self, key: &str, default: &'a str) -> &'a str {
        self.props.get(key).unwrap_or(default)
    }

    pub fn get_string(&self, key: &str, default: &str) -> String {
        self.props
            .get(key)
            .map(|s| s.to_string())
            .unwrap_or_else(|| default.to_string())
    }

    pub fn get_opt_string(&self, key: &str) -> Option<String> {
        self.props
            .get(key)
            .map(|s| s.to_string())
            .filter(|s| !s.is_empty())
    }

    pub fn get_float(&self, key: &str, default: f64) -> Result<f64> {
        match self.props.get(key) {
            Some(v) => f64::from_str(v.trim()).map_err(|_| invalid(self.name, key, v)),
            None => Ok(default),
        }
    }

    pub fn get_int(&self, key: &str, default: i64) -> Result<i64> {
        match self.props.get(key) {
            Some(v) => i64::from_str(v.trim()).map_err(|_| invalid(self.name, key, v)),
            None => Ok(default),
        }
    }

    pub fn get_uint(&self, key: &str, default: u32) -> Result<u32> {
        match self.props.get(key) {
            Some(v) => u32::from_str(v.trim()).map_err(|_| invalid(self.name, key, v)),
            None => Ok(default),
        }
    }

    pub fn get_bool(&self, key: &str, default: bool) -> Result<bool> {
        match self.props.get(key) {
            Some(v) => parse_bool(v).ok_or_else(|| invalid(self.name, key, v)),
            None => Ok(default),
        }
    }

    pub fn get_required(&self, key: &str) -> Result<&str> {
        self.props
            .get(key)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                Error::config(format!(
                    "section [{}] is missing required key {key}",
                    self.name
                ))
            })
    }
}

fn invalid(section: &str, key: &str, value: &str) -> Error {
    Error::config(format!(
        "section [{section}] has invalid value for {key}: {value:?}"
    ))
}

/// Python `configparser` `getboolean` semantics: 1/yes/true/on (case
/// insensitive) -> true; 0/no/false/off -> false.
fn parse_bool(s: &str) -> Option<bool> {
    let t = s.trim().to_ascii_lowercase();
    match t.as_str() {
        "1" | "yes" | "true" | "on" => Some(true),
        "0" | "no" | "false" | "off" => Some(false),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_and_bool_parsing_matches_python_configparser() {
        let cfg = Config::parse(
            r#"
[GENERAL]
THROTTLE_INTERVAL = 1.5
DISABLED = no
ENABLED = Yes
"#,
        )
        .unwrap();
        let s = cfg.section("GENERAL").unwrap();
        assert_eq!(s.get_float("THROTTLE_INTERVAL", 0.0).unwrap(), 1.5);
        assert!(!s.get_bool("DISABLED", true).unwrap());
        assert!(s.get_bool("ENABLED", false).unwrap());
        assert_eq!(s.get_float("MISSING", 42.0).unwrap(), 42.0);
    }

    #[test]
    fn round_trip_preserves_known_keys() {
        let cfg = Config::parse("[X]\nKEY=value\n").unwrap();
        let dumped = cfg.to_string();
        assert!(dumped.contains("KEY=value"));
    }
}
