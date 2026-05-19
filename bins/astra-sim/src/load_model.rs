//! Interactive load model — port of `simulator/load_model.py`.
//!
//! Provides a base load per phase with random jitter, a list of
//! toggleable discrete loads and an adjustable solar input. Mutated
//! in-place by the TUI / HTTP control layer.

use rand::Rng;
use serde::{Deserialize, Serialize};

pub const PHASES: [char; 3] = ['A', 'B', 'C'];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Load {
    pub name: String,
    pub power: f64,
    pub phase: char,
    #[serde(default)]
    pub active: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoadModel {
    pub base_load: Vec<f64>,
    pub base_noise: f64,
    pub loads: Vec<Load>,
    pub solar_power: f64,
    pub solar_max: f64,
    pub solar_phases: Vec<char>,
    pub auto_mode: bool,
    pub auto_interval: (f64, f64),
}

impl Default for LoadModel {
    fn default() -> Self {
        Self {
            base_load: vec![100.0, 100.0, 100.0],
            base_noise: 20.0,
            loads: Vec::new(),
            solar_power: 0.0,
            solar_max: 2000.0,
            solar_phases: vec!['A'],
            auto_mode: false,
            auto_interval: (10.0, 30.0),
        }
    }
}

impl LoadModel {
    /// Per-phase grid contribution (load + noise − solar). The battery
    /// output is **not** subtracted here; the powermeter simulator
    /// subtracts it separately so it can be displayed independently.
    pub fn get_grid_contribution(&self) -> [f64; 3] {
        let mut rng = rand::thread_rng();
        let mut out = [0.0; 3];
        for (i, phase) in PHASES.iter().enumerate() {
            let base = *self.base_load.get(i).unwrap_or(&0.0);
            let load_sum: f64 = self
                .loads
                .iter()
                .filter(|l| l.active && l.phase == *phase)
                .map(|l| l.power)
                .sum();
            let solar = self.solar_on_phase(*phase);
            // Only apply noise to phases with positive load (matches Python).
            let noise = if base > 0.0 || load_sum > 0.0 {
                rng.gen_range(-self.base_noise..=self.base_noise)
            } else {
                0.0
            };
            out[i] = base + noise + load_sum - solar;
        }
        out
    }

    pub fn toggle_load(&mut self, one_based: usize) -> Result<(), String> {
        if one_based == 0 || one_based > self.loads.len() {
            return Err(format!("Load index out of range: {one_based}"));
        }
        let i = one_based - 1;
        self.loads[i].active = !self.loads[i].active;
        Ok(())
    }

    pub fn set_solar(&mut self, watts: f64) {
        self.solar_power = watts.clamp(0.0, self.solar_max);
    }

    /// Randomly mutate loads (30% flip probability) and solar level.
    /// Called by auto-mode timer.
    pub fn auto_step(&mut self) {
        let mut rng = rand::thread_rng();
        for ld in &mut self.loads {
            if rng.gen::<f64>() < 0.3 {
                ld.active = !ld.active;
            }
        }
        self.solar_power = rng.gen_range(0.0..=self.solar_max);
    }

    fn solar_on_phase(&self, phase: char) -> f64 {
        if self.solar_phases.contains(&phase) && !self.solar_phases.is_empty() {
            self.solar_power / self.solar_phases.len() as f64
        } else {
            0.0
        }
    }

    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "loads": self.loads.iter().map(|l| serde_json::json!({
                "name": l.name,
                "power": l.power,
                "phase": l.phase.to_string(),
                "active": l.active,
            })).collect::<Vec<_>>(),
            "solar": {
                "current": round1(self.solar_power),
                "max": self.solar_max,
                "phases": self.solar_phases.iter().map(|c| c.to_string()).collect::<Vec<_>>(),
            },
            "auto_mode": self.auto_mode,
        })
    }
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toggle_out_of_range_errors() {
        let mut m = LoadModel::default();
        m.loads.push(Load {
            name: "x".into(),
            power: 10.0,
            phase: 'A',
            active: false,
        });
        assert!(m.toggle_load(2).is_err());
        assert!(m.toggle_load(0).is_err());
        assert!(m.toggle_load(1).is_ok());
        assert!(m.loads[0].active);
    }

    #[test]
    fn solar_clamps_to_range() {
        let mut m = LoadModel::default();
        m.set_solar(-100.0);
        assert_eq!(m.solar_power, 0.0);
        m.set_solar(1e6);
        assert_eq!(m.solar_power, m.solar_max);
    }

    #[test]
    fn solar_splits_across_phases() {
        let m = LoadModel {
            solar_power: 600.0,
            solar_phases: vec!['A', 'B', 'C'],
            ..LoadModel::default()
        };
        let c = m.get_grid_contribution();
        // each phase gets -200W solar (load_sum=0, base=100, noise<=±20)
        for v in c {
            assert!((v - (100.0 - 200.0)).abs() < 25.0);
        }
    }
}
