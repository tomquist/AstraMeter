//! Wrapper-chain application + per-section ClientFilter / WAIT_FOR_NEXT_MESSAGE
//! plumbing. Faithful port of `read_all_powermeter_configs` from
//! `src/astrameter/config/config_loader.py` (lines 153-339).
//!
//! For each section that the registry recognises, applies the wrappers in
//! the same order as Python:
//!   1. `TransformedPowermeter`   — POWER_OFFSET / POWER_MULTIPLIER
//!   2. `ThrottledPowermeter`     — THROTTLE_INTERVAL (GENERAL fallback)
//!   3. `HampelPowermeter`        — HAMPEL_* (GENERAL fallback)
//!   4. `SmoothedPowermeter`      — SMOOTH_TARGET_ALPHA / MAX_SMOOTH_STEP
//!   5. `DeadbandPowermeter`      — DEADBAND (GENERAL fallback)
//!   6. `PidPowermeter`           — PID_KP / KI / KD / OUTPUT_MAX / MODE
//!
//! Plus per-section NETMASK -> ClientFilter and WAIT_FOR_NEXT_MESSAGE flag.

use std::sync::Arc;

use astrameter_config::{ClientFilter, Config};
use astrameter_core::{Powermeter, Result};
use astrameter_platform::Platform;
use astrameter_wrappers::{
    DeadbandPowermeter, HampelPowermeter, PidMode, PidPowermeter, SmoothedPowermeter,
    ThrottledPowermeter, TransformedPowermeter,
};

use crate::PowermeterRegistry;

/// A configured powermeter together with the section's client IP filter and
/// the `wait_for_next_message` flag the consuming emulator should honour.
pub struct BoundPowermeter {
    pub section: String,
    pub meter: Arc<dyn Powermeter>,
    pub client_filter: ClientFilter,
    pub wait_for_next_message: bool,
}

/// Read globals from `[GENERAL]` once.
struct Globals {
    throttle_interval: f64,
    wait_for_next_message: bool,
    smooth_alpha: f64,
    max_smooth_step: f64,
    deadband: f64,
    hampel_window: i64,
    hampel_n_sigma: f64,
    hampel_min_threshold: f64,
    pid_kp: f64,
    pid_ki: f64,
    pid_kd: f64,
    pid_output_max: f64,
    pid_mode: String,
}

fn read_globals(cfg: &Config) -> Result<Globals> {
    let g = cfg.section("GENERAL");
    Ok(Globals {
        throttle_interval: g
            .as_ref()
            .map(|s| s.get_float("THROTTLE_INTERVAL", 0.0))
            .unwrap_or(Ok(0.0))?,
        wait_for_next_message: g
            .as_ref()
            .map(|s| s.get_bool("WAIT_FOR_NEXT_MESSAGE", true))
            .unwrap_or(Ok(true))?,
        smooth_alpha: g
            .as_ref()
            .map(|s| s.get_float("SMOOTH_TARGET_ALPHA", 0.0))
            .unwrap_or(Ok(0.0))?,
        max_smooth_step: g
            .as_ref()
            .map(|s| s.get_float("MAX_SMOOTH_STEP", 0.0))
            .unwrap_or(Ok(0.0))?,
        deadband: g
            .as_ref()
            .map(|s| s.get_float("DEADBAND", 0.0))
            .unwrap_or(Ok(0.0))?,
        hampel_window: g
            .as_ref()
            .map(|s| s.get_int("HAMPEL_WINDOW", 0))
            .unwrap_or(Ok(0))?,
        hampel_n_sigma: g
            .as_ref()
            .map(|s| s.get_float("HAMPEL_N_SIGMA", 3.0))
            .unwrap_or(Ok(3.0))?,
        hampel_min_threshold: g
            .as_ref()
            .map(|s| s.get_float("HAMPEL_MIN_THRESHOLD", 0.0))
            .unwrap_or(Ok(0.0))?,
        pid_kp: g
            .as_ref()
            .map(|s| s.get_float("PID_KP", 0.0))
            .unwrap_or(Ok(0.0))?,
        pid_ki: g
            .as_ref()
            .map(|s| s.get_float("PID_KI", 0.0))
            .unwrap_or(Ok(0.0))?,
        pid_kd: g
            .as_ref()
            .map(|s| s.get_float("PID_KD", 0.0))
            .unwrap_or(Ok(0.0))?,
        pid_output_max: g
            .as_ref()
            .map(|s| s.get_float("PID_OUTPUT_MAX", 800.0))
            .unwrap_or(Ok(800.0))?,
        pid_mode: g
            .as_ref()
            .and_then(|s| s.get_opt_string("PID_MODE"))
            .unwrap_or_else(|| "bias".to_string())
            .trim()
            .to_lowercase(),
    })
}

fn parse_float_list(value: &str, key: &str, section: &str) -> Result<Vec<f64>> {
    astrameter_config::parse_float_list(value, key, section)
}

/// Build every powermeter declared in `config`, applying the full Python
/// wrapper chain to each. Sections the registry doesn't recognise are
/// silently skipped (matches Python `create_powermeter` returning `None`).
/// MQTT_INSIGHTS is excluded from the meter list.
pub fn read_all_powermeter_configs(
    config: &Config,
    registry: &PowermeterRegistry,
    platform: Arc<Platform>,
) -> Result<Vec<BoundPowermeter>> {
    let globals = read_globals(config)?;
    let mut out = Vec::new();
    for section_name in config.sections().collect::<Vec<_>>() {
        if section_name.starts_with("MQTT_INSIGHTS") {
            continue;
        }
        let Some(section) = config.section(section_name) else {
            continue;
        };
        let Some(factory) = registry.lookup(section_name) else {
            continue;
        };
        let mut meter = match factory(&section, platform.clone()) {
            Ok(m) => m,
            Err(e) => {
                tracing::error!("section [{}] factory failed: {e}", section_name);
                continue;
            }
        };

        // 1. TransformedPowermeter (POWER_OFFSET / POWER_MULTIPLIER).
        let has_offset = section.has("POWER_OFFSET");
        let has_multiplier = section.has("POWER_MULTIPLIER");
        if has_offset || has_multiplier {
            let offsets = parse_float_list(
                section.get_str("POWER_OFFSET", "0"),
                "POWER_OFFSET",
                section_name,
            )?;
            let multipliers = parse_float_list(
                section.get_str("POWER_MULTIPLIER", "1"),
                "POWER_MULTIPLIER",
                section_name,
            )?;
            tracing::info!(
                "Applying power transform (multiplier={:?}, offset={:?}) to {section_name}",
                multipliers,
                offsets
            );
            meter = Arc::new(TransformedPowermeter::new(meter, offsets, multipliers)?);
        }

        // 2. ThrottledPowermeter (THROTTLE_INTERVAL, GENERAL fallback).
        let throttle = section.get_float("THROTTLE_INTERVAL", globals.throttle_interval)?;
        if throttle > 0.0 {
            let source = if section.has("THROTTLE_INTERVAL") {
                "section-specific"
            } else {
                "global"
            };
            tracing::info!("Applying {source} throttling ({throttle:.1}s) to {section_name}");
            meter = Arc::new(ThrottledPowermeter::new(
                meter,
                std::time::Duration::from_secs_f64(throttle),
            ));
        }

        // 3. HampelPowermeter (HAMPEL_*).
        let hampel_window = section.get_int("HAMPEL_WINDOW", globals.hampel_window)?;
        if hampel_window > 0 {
            let n_sigma = section.get_float("HAMPEL_N_SIGMA", globals.hampel_n_sigma)?;
            let min_threshold =
                section.get_float("HAMPEL_MIN_THRESHOLD", globals.hampel_min_threshold)?;
            let source = if section.has("HAMPEL_WINDOW") {
                "section-specific"
            } else {
                "global"
            };
            tracing::info!(
                "Applying {source} Hampel filter (window={hampel_window}, n_sigma={n_sigma:.2}, min={min_threshold:.0}W) to {section_name}"
            );
            meter = Arc::new(HampelPowermeter::new(
                meter,
                hampel_window as usize,
                n_sigma,
                min_threshold,
            )?);
        }

        // 4. SmoothedPowermeter.
        let alpha = section.get_float("SMOOTH_TARGET_ALPHA", globals.smooth_alpha)?;
        if alpha > 0.0 {
            let alpha = alpha.clamp(0.01, 1.0);
            let max_step = section.get_float("MAX_SMOOTH_STEP", globals.max_smooth_step)?;
            let source = if section.has("SMOOTH_TARGET_ALPHA") {
                "section-specific"
            } else {
                "global"
            };
            tracing::info!(
                "Applying {source} EMA smoothing (alpha={alpha:.2}, max_step={max_step:.0}) to {section_name}"
            );
            meter = Arc::new(SmoothedPowermeter::new(meter, alpha, max_step));
        }

        // 5. DeadbandPowermeter.
        let deadband = section.get_float("DEADBAND", globals.deadband)?;
        if deadband > 0.0 {
            let source = if section.has("DEADBAND") {
                "section-specific"
            } else {
                "global"
            };
            tracing::info!("Applying {source} deadband ({deadband:.0}W) to {section_name}");
            meter = Arc::new(DeadbandPowermeter::new(meter, deadband));
        }

        // 6. PidPowermeter.
        let pid_kp = section.get_float("PID_KP", globals.pid_kp)?;
        if pid_kp > 0.0 {
            let pid_ki = section.get_float("PID_KI", globals.pid_ki)?;
            let pid_kd = section.get_float("PID_KD", globals.pid_kd)?;
            let pid_output_max = section.get_float("PID_OUTPUT_MAX", globals.pid_output_max)?;
            let pid_mode_raw = section
                .get_opt_string("PID_MODE")
                .unwrap_or_else(|| globals.pid_mode.clone());
            let pid_mode = PidMode::from_str_ci(&pid_mode_raw)?;
            let source = if section.has("PID_KP") {
                "section-specific"
            } else {
                "global"
            };
            tracing::info!(
                "Applying {source} PID (Kp={pid_kp}, Ki={pid_ki}, Kd={pid_kd}, max={pid_output_max}W, mode={pid_mode_raw}) to {section_name}"
            );
            meter = Arc::new(PidPowermeter::new(
                meter,
                pid_kp,
                pid_ki,
                pid_kd,
                pid_output_max,
                pid_mode,
            )?);
        }

        // Per-section CLIENT_FILTER (NETMASK) and WAIT_FOR_NEXT_MESSAGE.
        let netmask = section.get_str("NETMASK", "0.0.0.0/0");
        let client_filter = ClientFilter::from_csv(netmask)?;
        let wait_for_next =
            section.get_bool("WAIT_FOR_NEXT_MESSAGE", globals.wait_for_next_message)?;

        out.push(BoundPowermeter {
            section: section_name.to_string(),
            meter,
            client_filter,
            wait_for_next_message: wait_for_next,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use astrameter_config::Config;
    use astrameter_platform_std::build_platform;
    use std::sync::Arc;

    fn make_cfg(text: &str) -> Config {
        Config::parse(text).unwrap()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn applies_power_offset_and_multiplier() {
        let cfg = make_cfg(
            "[SCRIPT]\nCOMMAND = printf '100\\n'\nPOWER_OFFSET = 50\nPOWER_MULTIPLIER = 2\n",
        );
        let mut reg = crate::PowermeterRegistry::new();
        crate::register_all(&mut reg);
        let platform = Arc::new(build_platform());
        let bound = read_all_powermeter_configs(&cfg, &reg, platform).unwrap();
        assert_eq!(bound.len(), 1);
        let watts = bound[0].meter.get_powermeter_watts().await.unwrap();
        // (100 * 2) + 50 = 250
        assert_eq!(watts, vec![250.0]);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn applies_global_throttle_interval() {
        let cfg =
            make_cfg("[GENERAL]\nTHROTTLE_INTERVAL = 0.05\n[SCRIPT]\nCOMMAND = printf '7\\n'\n");
        let mut reg = crate::PowermeterRegistry::new();
        crate::register_all(&mut reg);
        let platform = Arc::new(build_platform());
        let bound = read_all_powermeter_configs(&cfg, &reg, platform).unwrap();
        // First read warm-cache, second read should hit throttle cache (not fail).
        let _ = bound[0].meter.get_powermeter_watts().await.unwrap();
        let _ = bound[0].meter.get_powermeter_watts().await.unwrap();
    }

    #[test]
    fn netmask_per_section() {
        let cfg = make_cfg("[SCRIPT]\nCOMMAND = echo 1\nNETMASK = 10.0.0.0/8,192.168.1.0/24\n");
        let mut reg = crate::PowermeterRegistry::new();
        crate::register_all(&mut reg);
        let platform = Arc::new(build_platform());
        let bound = read_all_powermeter_configs(&cfg, &reg, platform).unwrap();
        assert!(bound[0].client_filter.matches("10.5.5.5".parse().unwrap()));
        assert!(bound[0]
            .client_filter
            .matches("192.168.1.50".parse().unwrap()));
        assert!(!bound[0]
            .client_filter
            .matches("172.16.0.1".parse().unwrap()));
    }
}
