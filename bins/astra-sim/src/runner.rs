//! Simulation orchestrator — wires batteries, load model and HTTP
//! server together. Port of `simulator/runner.py`.

use std::sync::Arc;

use parking_lot::Mutex;
use rand::Rng;
use serde::{Deserialize, Serialize};

use crate::battery::{BatteryConfig, BatterySimulator};
use crate::load_model::{Load, LoadModel};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BatteryConfigDoc {
    pub mac: String,
    pub phase: char,
    #[serde(default = "d_max_charge")]
    pub max_charge_power: i64,
    #[serde(default = "d_max_charge")]
    pub max_discharge_power: i64,
    #[serde(default = "d_capacity")]
    pub capacity_wh: f64,
    #[serde(default = "d_soc")]
    pub initial_soc: f64,
    #[serde(default = "d_ramp")]
    pub ramp_rate: f64,
    #[serde(default = "d_poll")]
    pub poll_interval: f64,
    #[serde(default)]
    pub power_update_delay_ticks: i64,
}

fn d_max_charge() -> i64 {
    800
}
fn d_capacity() -> f64 {
    2560.0
}
fn d_soc() -> f64 {
    0.5
}
fn d_ramp() -> f64 {
    200.0
}
fn d_poll() -> f64 {
    1.0
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SimulationConfig {
    pub batteries: Vec<BatteryConfigDoc>,
    #[serde(default)]
    pub power_update_delay_ticks: i64,
    #[serde(default = "d_ct_mac")]
    pub ct_mac: String,
    #[serde(default = "d_host")]
    pub ct_host: String,
    #[serde(default = "d_ct_port")]
    pub ct_port: u16,
    #[serde(default = "d_host")]
    pub http_host: String,
    #[serde(default = "d_http_port")]
    pub http_port: u16,
    #[serde(default = "d_base_load")]
    pub base_load: Vec<f64>,
    #[serde(default = "d_noise")]
    pub base_noise: f64,
    #[serde(default)]
    pub loads: Vec<Load>,
    #[serde(default = "d_solar_max")]
    pub solar_max: f64,
    #[serde(default = "d_solar_phases")]
    pub solar_phases: Vec<char>,
    #[serde(default)]
    pub auto_mode: bool,
    #[serde(default = "d_auto_interval")]
    pub auto_interval: (f64, f64),
    #[serde(default = "d_log_interval")]
    pub log_interval: f64,
    #[serde(default = "d_time_scale")]
    pub time_scale: f64,
}

fn d_ct_mac() -> String {
    "112233445566".into()
}
fn d_host() -> String {
    "127.0.0.1".into()
}
fn d_ct_port() -> u16 {
    12345
}
fn d_http_port() -> u16 {
    8080
}
fn d_base_load() -> Vec<f64> {
    vec![100.0, 100.0, 100.0]
}
fn d_noise() -> f64 {
    20.0
}
fn d_solar_max() -> f64 {
    2000.0
}
fn d_solar_phases() -> Vec<char> {
    vec!['A']
}
fn d_auto_interval() -> (f64, f64) {
    (10.0, 30.0)
}
fn d_log_interval() -> f64 {
    5.0
}
fn d_time_scale() -> f64 {
    1.0
}

impl Default for SimulationConfig {
    fn default() -> Self {
        Self {
            batteries: Vec::new(),
            power_update_delay_ticks: 0,
            ct_mac: d_ct_mac(),
            ct_host: d_host(),
            ct_port: d_ct_port(),
            http_host: d_host(),
            http_port: d_http_port(),
            base_load: d_base_load(),
            base_noise: d_noise(),
            loads: Vec::new(),
            solar_max: d_solar_max(),
            solar_phases: d_solar_phases(),
            auto_mode: false,
            auto_interval: d_auto_interval(),
            log_interval: d_log_interval(),
            time_scale: d_time_scale(),
        }
    }
}

/// Validate a configuration. Returns `Err` on any problem.
pub fn validate_config(cfg: &SimulationConfig) -> anyhow::Result<()> {
    let mut seen = std::collections::HashSet::new();
    for bc in &cfg.batteries {
        if !"ABC".contains(bc.phase) {
            anyhow::bail!("Battery {}: invalid phase {:?}", bc.mac, bc.phase);
        }
        if !(0.0..=1.0).contains(&bc.initial_soc) {
            anyhow::bail!(
                "Battery {}: initial_soc must be 0.0-1.0, got {}",
                bc.mac,
                bc.initial_soc
            );
        }
        if bc.max_charge_power < 0 || bc.max_discharge_power < 0 {
            anyhow::bail!("Battery {}: power values must be >= 0", bc.mac);
        }
        if bc.power_update_delay_ticks < 0 {
            anyhow::bail!("Battery {}: power_update_delay_ticks must be >= 0", bc.mac);
        }
        let mac = bc.mac.to_uppercase();
        if mac.len() != 12 || !mac.chars().all(|c| c.is_ascii_hexdigit()) {
            anyhow::bail!("Battery MAC must be 12 hex chars, got {:?}", bc.mac);
        }
        if !seen.insert(mac.clone()) {
            anyhow::bail!("Duplicate battery MAC: {}", bc.mac);
        }
    }
    for ld in &cfg.loads {
        if !"ABC".contains(ld.phase) {
            anyhow::bail!("Load {:?}: invalid phase {:?}", ld.name, ld.phase);
        }
    }
    for p in &cfg.solar_phases {
        if !"ABC".contains(*p) {
            anyhow::bail!("Invalid solar phase {:?}", p);
        }
    }
    if cfg.time_scale <= 0.0 {
        anyhow::bail!("time_scale must be positive, got {}", cfg.time_scale);
    }
    Ok(())
}

/// Build the `quick_config` Python helper used by `astra-sim run` with
/// no JSON file.
#[allow(clippy::too_many_arguments)]
pub fn quick_config(
    num_batteries: u32,
    num_phases: u32,
    base_load: Option<Vec<f64>>,
    initial_soc: f64,
    ct_host: &str,
    ct_port: u16,
    http_port: u16,
    power_update_delay_ticks: i64,
) -> SimulationConfig {
    let phases: Vec<char> = ['A', 'B', 'C']
        .iter()
        .take(num_phases as usize)
        .copied()
        .collect();
    let mut batteries = Vec::new();
    for i in 0..num_batteries {
        let mac = format!("02B250{:06X}", i + 1);
        let phase = phases[i as usize % phases.len()];
        batteries.push(BatteryConfigDoc {
            mac,
            phase,
            max_charge_power: 800,
            max_discharge_power: 800,
            capacity_wh: 2560.0,
            initial_soc,
            ramp_rate: 200.0,
            poll_interval: 1.0,
            power_update_delay_ticks,
        });
    }
    let default_loads = vec![
        Load {
            name: "LED lights".into(),
            power: 30.0,
            phase: 'A',
            active: false,
        },
        Load {
            name: "TV + entertainment".into(),
            power: 80.0,
            phase: if num_phases >= 2 { 'B' } else { 'A' },
            active: false,
        },
        Load {
            name: "Router + NAS".into(),
            power: 40.0,
            phase: 'A',
            active: false,
        },
        Load {
            name: "Microwave".into(),
            power: 800.0,
            phase: 'A',
            active: false,
        },
        Load {
            name: "Washing machine".into(),
            power: 400.0,
            phase: if num_phases >= 2 { 'B' } else { 'A' },
            active: false,
        },
    ];
    let base_load = base_load.unwrap_or_else(|| {
        if num_phases == 1 {
            vec![300.0, 0.0, 0.0]
        } else {
            vec![100.0, 100.0, 100.0]
        }
    });
    SimulationConfig {
        batteries,
        power_update_delay_ticks,
        ct_host: ct_host.into(),
        ct_port,
        http_port,
        base_load,
        loads: default_loads,
        solar_phases: phases,
        ..SimulationConfig::default()
    }
}

/// Live state shared between the battery loops, the HTTP server, and
/// the TUI.
pub struct SimulationRunner {
    pub config: SimulationConfig,
    pub load_model: Arc<Mutex<LoadModel>>,
    pub batteries: Vec<Arc<BatterySimulator>>,
}

impl SimulationRunner {
    pub fn new(config: SimulationConfig) -> anyhow::Result<Self> {
        let lm = LoadModel {
            base_load: config.base_load.clone(),
            base_noise: config.base_noise,
            loads: config.loads.clone(),
            solar_phases: config.solar_phases.clone(),
            solar_max: config.solar_max,
            auto_mode: config.auto_mode,
            auto_interval: config.auto_interval,
            ..LoadModel::default()
        };
        let mut batteries = Vec::new();
        for bc in &config.batteries {
            let bcfg = BatteryConfig {
                mac: bc.mac.clone(),
                phase: bc.phase,
                ct_mac: config.ct_mac.clone(),
                ct_host: config.ct_host.clone(),
                ct_port: config.ct_port,
                meter_dev_type: "HMG-50".into(),
                ct_dev_type: "HME-4".into(),
                max_charge_power: bc.max_charge_power,
                max_discharge_power: bc.max_discharge_power,
                capacity_wh: bc.capacity_wh,
                initial_soc: bc.initial_soc,
                ramp_rate: bc.ramp_rate,
                poll_interval: bc.poll_interval,
                min_power_threshold: 20.0,
                startup_delay: 2.0,
                inspection_count: 1,
                time_scale: config.time_scale,
                power_update_delay_ticks: bc.power_update_delay_ticks,
            };
            batteries.push(BatterySimulator::new(bcfg)?);
        }
        Ok(Self {
            config,
            load_model: Arc::new(Mutex::new(lm)),
            batteries,
        })
    }

    /// Per-phase grid contribution minus per-phase battery output —
    /// matches `PowermeterSimulator.compute_grid()`.
    pub fn compute_grid(&self) -> serde_json::Value {
        let contribution = self.load_model.lock().get_grid_contribution();
        let mut out = serde_json::Map::new();
        for (i, phase) in ['A', 'B', 'C'].iter().enumerate() {
            let battery_sum: f64 = self
                .batteries
                .iter()
                .filter(|b| b.phase == *phase)
                .map(|b| b.current_power())
                .sum();
            let v = (contribution[i] - battery_sum) * 10.0;
            let v = v.round() / 10.0;
            out.insert(
                format!("phase_{}", phase.to_ascii_lowercase()),
                serde_json::json!(v),
            );
        }
        serde_json::Value::Object(out)
    }
}

/// Periodic auto-mode mutator (matches Python `_auto_loop`).
pub async fn auto_loop(
    runner: Arc<SimulationRunner>,
    cancel: crate::battery::tokio_util_local::CancelFlag,
) {
    let ts = runner.config.time_scale.max(0.1);
    loop {
        if cancel.is_cancelled() {
            return;
        }
        let (lo, hi) = runner.load_model.lock().auto_interval;
        let wait = rand::thread_rng().gen_range(lo..=hi) / ts;
        tokio::time::sleep(std::time::Duration::from_secs_f64(wait)).await;
        let mut lm = runner.load_model.lock();
        if lm.auto_mode {
            lm.auto_step();
        }
    }
}

/// Periodic log-line emitter (matches Python `_log_loop`).
pub async fn log_loop(
    runner: Arc<SimulationRunner>,
    cancel: crate::battery::tokio_util_local::CancelFlag,
) {
    let ts = runner.config.time_scale.max(0.1);
    loop {
        if cancel.is_cancelled() {
            return;
        }
        tokio::time::sleep(std::time::Duration::from_secs_f64(
            runner.config.log_interval / ts,
        ))
        .await;
        let grid = runner.compute_grid();
        let mut parts = vec![format!(
            "grid=[{:.0}, {:.0}, {:.0}]",
            grid.get("phase_a").and_then(|v| v.as_f64()).unwrap_or(0.0),
            grid.get("phase_b").and_then(|v| v.as_f64()).unwrap_or(0.0),
            grid.get("phase_c").and_then(|v| v.as_f64()).unwrap_or(0.0),
        )];
        for b in &runner.batteries {
            let snap = b.snapshot();
            let last4 = &snap.mac[snap.mac.len().saturating_sub(4)..];
            parts.push(format!(
                "{last4}:{}/{}W/{:.0}%",
                snap.phase,
                snap.power,
                snap.soc * 100.0
            ));
        }
        tracing::info!("{}", parts.join(" | "));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_rejects_dupe_mac() {
        let mut cfg = quick_config(1, 1, None, 0.5, "127.0.0.1", 12345, 8080, 0);
        let dup = cfg.batteries[0].clone();
        cfg.batteries.push(dup);
        assert!(validate_config(&cfg).is_err());
    }

    #[test]
    fn quick_config_one_phase_drops_noise_on_idle() {
        let cfg = quick_config(1, 1, None, 0.5, "127.0.0.1", 12345, 8080, 0);
        assert_eq!(cfg.base_load, vec![300.0, 0.0, 0.0]);
        assert_eq!(cfg.solar_phases, vec!['A']);
    }

    #[test]
    fn validate_accepts_lowercase_mac() {
        let cfg = SimulationConfig {
            batteries: vec![BatteryConfigDoc {
                mac: "aabbccddeeff".into(),
                phase: 'A',
                max_charge_power: 800,
                max_discharge_power: 800,
                capacity_wh: 2560.0,
                initial_soc: 0.5,
                ramp_rate: 200.0,
                poll_interval: 1.0,
                power_update_delay_ticks: 0,
            }],
            ..SimulationConfig::default()
        };
        assert!(validate_config(&cfg).is_ok());
    }
}
