//! `astra-sim` CLI — port of `simulator/cli.py`.
//!
//! Supported subcommands:
//!   * `run`     — start simulator (TUI by default; `--no-tui` for headless)
//!   * `start`   — daemonise (Unix fork, like Python)
//!   * `stop`    — POST /shutdown on the running daemon
//!   * `attach`  — connect TUI to a running daemon
//!   * `status`  — print `/status` JSON
//!   * `load`    — toggle a load
//!   * `solar`   — set solar (number / "off" / "max")
//!   * `battery` — set per-battery `soc` or `max-power`
//!   * `auto`    — `on` / `off`
//!   * `config`  — print a matching `[CT002]` + `[JSON_HTTP]` snippet
//!
//! Backed by `reqwest` for the HTTP control commands so daemon mode
//! works exactly like the Python original.

use std::path::PathBuf;

use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;

use crate::runner::{quick_config, validate_config, SimulationConfig, SimulationRunner};

pub const DEFAULT_HTTP_PORT: u16 = 8080;

/// PID-file path used by daemonised `astra-sim start`. Mirrors Python's
/// `~/.astra-sim.pid`.
pub fn pid_file() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".astra-sim.pid")
}

pub fn log_file() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".astra-sim.log")
}

#[derive(Debug, Default)]
pub struct Args {
    pub command: Option<String>,
    pub config: Option<PathBuf>,
    pub batteries: u32,
    pub phases: u32,
    pub base_load: Option<String>,
    pub soc: f64,
    pub ct_host: Option<String>,
    pub ct_port: Option<u16>,
    pub http_port: Option<u16>,
    pub no_tui: bool,
    pub time_scale: f64,
    pub power_update_delay: Option<i64>,
    pub verbose: bool,
    pub positional: Vec<String>,
}

pub fn parse_args(raw: Vec<String>) -> Args {
    let mut a = Args {
        batteries: 1,
        phases: 1,
        soc: 0.5,
        time_scale: 1.0,
        ..Args::default()
    };
    let mut iter = raw.into_iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print_help();
                std::process::exit(0);
            }
            "-c" | "--config" => a.config = iter.next().map(PathBuf::from),
            "--batteries" => a.batteries = iter.next().and_then(|s| s.parse().ok()).unwrap_or(1),
            "--phases" => a.phases = iter.next().and_then(|s| s.parse().ok()).unwrap_or(1),
            "--base-load" => a.base_load = iter.next(),
            "--soc" => a.soc = iter.next().and_then(|s| s.parse().ok()).unwrap_or(0.5),
            "--ct-host" => a.ct_host = iter.next(),
            "--ct-port" => a.ct_port = iter.next().and_then(|s| s.parse().ok()),
            "--http-port" => a.http_port = iter.next().and_then(|s| s.parse().ok()),
            "--no-tui" => a.no_tui = true,
            "--time-scale" => {
                a.time_scale = iter.next().and_then(|s| s.parse().ok()).unwrap_or(1.0);
            }
            "--power-update-delay" => {
                a.power_update_delay = iter.next().and_then(|s| s.parse().ok());
            }
            "-v" | "--verbose" => a.verbose = true,
            s if a.command.is_none() && !s.starts_with('-') => a.command = Some(s.to_string()),
            s if !s.starts_with('-') => a.positional.push(s.to_string()),
            s => {
                eprintln!("unknown arg: {s}");
            }
        }
    }
    a
}

fn print_help() {
    println!(
        "Usage: astra-sim <command> [options]\n\
         \n\
         Commands:\n\
         \x20 run                     Start simulator (TUI by default; --no-tui for headless)\n\
         \x20 start                   Daemonise (Unix fork)\n\
         \x20 stop                    Stop running daemon (POST /shutdown)\n\
         \x20 attach                  Attach TUI to running daemon\n\
         \x20 status                  Print /status as JSON\n\
         \x20 load toggle <index>     Toggle load at 1-based index\n\
         \x20 solar set <value>       Set solar (W / \"off\" / \"max\")\n\
         \x20 battery <mac> soc <v>   Set battery SOC (0.0..1.0)\n\
         \x20 battery <mac> max-power <charge> <discharge>\n\
         \x20 auto <on|off>           Toggle auto-load mode\n\
         \x20 config                  Print a matching astrameter config snippet\n\
         \n\
         Options:\n\
         \x20 -c, --config FILE       JSON simulator config\n\
         \x20     --batteries N       Number of batteries (default 1)\n\
         \x20     --phases {{1|3}}    Number of phases (default 1)\n\
         \x20     --base-load LIST    Per-phase base load, comma separated\n\
         \x20     --soc V             Initial SOC (default 0.5)\n\
         \x20     --ct-host HOST      CT002 host (default 127.0.0.1)\n\
         \x20     --ct-port PORT      CT002 UDP port (default 12345)\n\
         \x20     --http-port PORT    HTTP API port (default 8080)\n\
         \x20     --no-tui            Headless mode\n\
         \x20     --time-scale N      Speed up sim time (e.g. 10 = 10x)\n\
         \x20     --power-update-delay N  Delay applied CT-derived target by N ticks\n\
         \x20 -v, --verbose"
    );
}

/// Build a runtime config from CLI args (`run`/`start` subcommands).
pub fn build_config(args: &Args) -> Result<SimulationConfig> {
    let mut cfg = if let Some(path) = &args.config {
        let raw =
            std::fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
        let data: Value = serde_json::from_str(&raw).context("parse JSON config")?;
        parse_json_config(&data)?
    } else {
        let base_load = args.base_load.as_ref().map(|s| {
            s.split(',')
                .filter_map(|p| p.trim().parse::<f64>().ok())
                .collect::<Vec<_>>()
        });
        quick_config(
            args.batteries,
            args.phases,
            base_load,
            args.soc,
            args.ct_host.as_deref().unwrap_or("127.0.0.1"),
            args.ct_port.unwrap_or(12345),
            args.http_port.unwrap_or(DEFAULT_HTTP_PORT),
            args.power_update_delay.unwrap_or(0),
        )
    };
    if let Some(p) = args.http_port {
        cfg.http_port = p;
    }
    if let Some(p) = args.ct_port {
        cfg.ct_port = p;
    }
    if let Some(h) = &args.ct_host {
        cfg.ct_host = h.clone();
    }
    if args.time_scale != 1.0 {
        cfg.time_scale = args.time_scale;
    }
    if let Some(d) = args.power_update_delay {
        cfg.power_update_delay_ticks = d;
        for b in &mut cfg.batteries {
            b.power_update_delay_ticks = d;
        }
    }
    validate_config(&cfg)?;
    Ok(cfg)
}

/// Parse the same JSON layout that the Python `parse_config` accepts.
/// Top-level keys: `ct`, `http`, `powermeter`, `batteries`,
/// `power_update_delay_ticks`, `auto_mode`, `auto_interval`,
/// `log_interval`, `time_scale`.
fn parse_json_config(data: &Value) -> Result<SimulationConfig> {
    let mut cfg = SimulationConfig::default();
    if let Some(ct) = data.get("ct") {
        if let Some(v) = ct.get("mac").and_then(|v| v.as_str()) {
            cfg.ct_mac = v.to_string();
        }
        if let Some(v) = ct.get("host").and_then(|v| v.as_str()) {
            cfg.ct_host = v.to_string();
        }
        if let Some(v) = ct.get("port").and_then(|v| v.as_u64()) {
            cfg.ct_port = v as u16;
        }
    }
    if let Some(http) = data.get("http") {
        if let Some(v) = http.get("host").and_then(|v| v.as_str()) {
            cfg.http_host = v.to_string();
        }
        if let Some(v) = http.get("port").and_then(|v| v.as_u64()) {
            cfg.http_port = v as u16;
        }
    }
    if let Some(pm) = data.get("powermeter") {
        if let Some(arr) = pm.get("base_load").and_then(|v| v.as_array()) {
            cfg.base_load = arr.iter().filter_map(|v| v.as_f64()).collect();
        }
        if let Some(v) = pm.get("base_noise").and_then(|v| v.as_f64()) {
            cfg.base_noise = v;
        }
        if let Some(v) = pm.get("solar_max").and_then(|v| v.as_f64()) {
            cfg.solar_max = v;
        }
        if let Some(arr) = pm.get("solar_phases").and_then(|v| v.as_array()) {
            cfg.solar_phases = arr
                .iter()
                .filter_map(|v| v.as_str()?.chars().next())
                .collect();
        }
        if let Some(arr) = pm.get("loads").and_then(|v| v.as_array()) {
            cfg.loads = arr
                .iter()
                .filter_map(|v| serde_json::from_value(v.clone()).ok())
                .collect();
        }
    }
    if let Some(arr) = data.get("batteries").and_then(|v| v.as_array()) {
        let default_delay = data
            .get("power_update_delay_ticks")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        cfg.power_update_delay_ticks = default_delay;
        for bd in arr {
            let mut bc: crate::runner::BatteryConfigDoc = serde_json::from_value(bd.clone())
                .map_err(|e| anyhow!("invalid battery entry: {e}"))?;
            if !bd
                .as_object()
                .is_some_and(|m| m.contains_key("power_update_delay_ticks"))
            {
                bc.power_update_delay_ticks = default_delay;
            }
            cfg.batteries.push(bc);
        }
    }
    if let Some(b) = data.get("auto_mode").and_then(|v| v.as_bool()) {
        cfg.auto_mode = b;
    }
    if let Some(arr) = data.get("auto_interval").and_then(|v| v.as_array()) {
        if arr.len() == 2 {
            let lo = arr[0].as_f64().unwrap_or(10.0);
            let hi = arr[1].as_f64().unwrap_or(30.0);
            cfg.auto_interval = (lo, hi);
        }
    }
    if let Some(v) = data.get("log_interval").and_then(|v| v.as_f64()) {
        cfg.log_interval = v;
    }
    if let Some(v) = data.get("time_scale").and_then(|v| v.as_f64()) {
        cfg.time_scale = v;
    }
    Ok(cfg)
}

/// `astra-sim config` — print a matching astrameter config.ini.
pub fn cmd_config_snippet(http_port: u16, ct_port: u16, phases: u32) -> String {
    let json_paths = if phases == 1 {
        "$.phase_a".to_string()
    } else {
        "$.phase_a,$.phase_b,$.phase_c".to_string()
    };
    format!(
        "[GENERAL]\nDEVICE_TYPE = ct002\n\n\
         [CT002]\nUDP_PORT = {ct_port}\nACTIVE_CONTROL = True\n\n\
         [JSON_HTTP]\nURL = http://localhost:{http_port}/power\nJSON_PATHS = {json_paths}\n"
    )
}

/// Run the simulator in-process. Spawns batteries + HTTP server +
/// log/auto loops, returns once the shutdown signal fires.
pub async fn run_in_process(cfg: SimulationConfig, with_tui: bool) -> Result<()> {
    let runner = std::sync::Arc::new(SimulationRunner::new(cfg)?);
    let cancel = crate::battery::tokio_util_local::CancelFlag::new();
    let (_addr, shutdown, http_handle) = crate::powermeter_sim::serve(
        runner.clone(),
        &runner.config.http_host.clone(),
        runner.config.http_port,
        cancel.clone(),
    )
    .await?;
    let mut battery_handles = Vec::new();
    for b in &runner.batteries {
        let bc = b.clone();
        let cc = cancel.clone();
        battery_handles.push(tokio::spawn(async move { bc.run(cc).await }));
    }
    let log_handle = tokio::spawn(crate::runner::log_loop(runner.clone(), cancel.clone()));
    let auto_handle = tokio::spawn(crate::runner::auto_loop(runner.clone(), cancel.clone()));

    if with_tui {
        let tui_runner = runner.clone();
        let tui_cancel = cancel.clone();
        // ratatui needs a blocking thread because crossterm's event read is sync.
        let tui_join = tokio::task::spawn_blocking(move || crate::tui::run(tui_runner, tui_cancel));
        let _ = tui_join.await;
        cancel.cancel();
    } else {
        // Wait for either Ctrl-C or POST /shutdown.
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = shutdown.notified() => {}
        }
        cancel.cancel();
    }
    let _ = http_handle.await;
    for h in battery_handles {
        let _ = h.await;
    }
    log_handle.abort();
    auto_handle.abort();
    Ok(())
}

// ── HTTP client helpers ─────────────────────────────────────────────

fn api_url(port: u16, path: &str) -> String {
    format!("http://localhost:{port}{path}")
}

async fn http_get(port: u16, path: &str) -> Result<Value> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()?;
    let r = client.get(api_url(port, path)).send().await?;
    let v: Value = r.json().await?;
    Ok(v)
}

async fn http_post(port: u16, path: &str, body: Value) -> Result<Value> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()?;
    let r = client.post(api_url(port, path)).json(&body).send().await?;
    let v: Value = r.json().await?;
    Ok(v)
}

// ── Subcommands ─────────────────────────────────────────────────────

pub async fn cmd_stop(args: &Args) -> Result<()> {
    let port = args.http_port.unwrap_or(DEFAULT_HTTP_PORT);
    match http_post(port, "/shutdown", Value::Null).await {
        Ok(v) => println!("{}", serde_json::to_string_pretty(&v)?),
        Err(_) => {
            // Fallback: SIGTERM via PID file (matches Python).
            let pid_path = pid_file();
            if pid_path.exists() {
                let pid: i32 = std::fs::read_to_string(&pid_path)?
                    .trim()
                    .parse()
                    .context("parse PID file")?;
                #[cfg(unix)]
                {
                    use std::process::Command;
                    let st = Command::new("kill").arg(pid.to_string()).status()?;
                    if st.success() {
                        println!("Sent SIGTERM to PID {pid}");
                    } else {
                        bail!("Failed to stop daemon (kill exit {st})");
                    }
                }
                #[cfg(not(unix))]
                {
                    let _ = pid;
                    bail!("daemon mode is Unix-only");
                }
            } else {
                bail!("Daemon not running (no PID file)");
            }
        }
    }
    let _ = std::fs::remove_file(pid_file());
    Ok(())
}

pub async fn cmd_status(args: &Args) -> Result<()> {
    let port = args.http_port.unwrap_or(DEFAULT_HTTP_PORT);
    let v = http_get(port, "/status").await?;
    println!("{}", serde_json::to_string_pretty(&v)?);
    Ok(())
}

pub async fn cmd_load(args: &Args) -> Result<()> {
    if args.positional.len() < 2 || args.positional[0] != "toggle" {
        bail!("Usage: astra-sim load toggle <index>");
    }
    let index: usize = args.positional[1].parse().context("invalid index")?;
    let port = args.http_port.unwrap_or(DEFAULT_HTTP_PORT);
    let v = http_post(port, &format!("/loads/{index}/toggle"), Value::Null).await?;
    println!("{}", serde_json::to_string_pretty(&v)?);
    Ok(())
}

pub async fn cmd_solar(args: &Args) -> Result<()> {
    if args.positional.len() < 2 || args.positional[0] != "set" {
        bail!("Usage: astra-sim solar set <watts|off|max>");
    }
    let port = args.http_port.unwrap_or(DEFAULT_HTTP_PORT);
    let body = match args.positional[1].as_str() {
        "off" => serde_json::json!({"watts": "off"}),
        "max" => serde_json::json!({"watts": "max"}),
        other => match other.parse::<f64>() {
            Ok(f) => serde_json::json!({"watts": f}),
            Err(_) => bail!("invalid watts: {other}"),
        },
    };
    let v = http_post(port, "/solar", body).await?;
    println!("{}", serde_json::to_string_pretty(&v)?);
    Ok(())
}

pub async fn cmd_battery(args: &Args) -> Result<()> {
    if args.positional.len() < 2 {
        bail!("Usage: astra-sim battery <mac> {{soc <v> | max-power <chg> <dis>}}");
    }
    let mac = &args.positional[0];
    let action = &args.positional[1];
    let port = args.http_port.unwrap_or(DEFAULT_HTTP_PORT);
    let (path, body) = match action.as_str() {
        "soc" => {
            if args.positional.len() != 3 {
                bail!("Usage: astra-sim battery <mac> soc <value>");
            }
            let soc: f64 = args.positional[2].parse().context("invalid soc")?;
            (
                format!("/batteries/{mac}/soc"),
                serde_json::json!({"soc": soc}),
            )
        }
        "max-power" => {
            if args.positional.len() != 4 {
                bail!("Usage: astra-sim battery <mac> max-power <charge> <discharge>");
            }
            let chg: i64 = args.positional[2].parse().context("invalid charge")?;
            let dis: i64 = args.positional[3].parse().context("invalid discharge")?;
            (
                format!("/batteries/{mac}/max_power"),
                serde_json::json!({"charge": chg, "discharge": dis}),
            )
        }
        _ => bail!("Unknown action: {action}"),
    };
    let v = http_post(port, &path, body).await?;
    println!("{}", serde_json::to_string_pretty(&v)?);
    Ok(())
}

pub async fn cmd_auto(args: &Args) -> Result<()> {
    if args.positional.is_empty() {
        bail!("Usage: astra-sim auto <on|off>");
    }
    let enabled = matches!(
        args.positional[0].to_lowercase().as_str(),
        "on" | "true" | "1"
    );
    let port = args.http_port.unwrap_or(DEFAULT_HTTP_PORT);
    let v = http_post(port, "/auto", serde_json::json!({"enabled": enabled})).await?;
    println!("{}", serde_json::to_string_pretty(&v)?);
    Ok(())
}

pub async fn cmd_attach(args: &Args) -> Result<()> {
    let port = args.http_port.unwrap_or(DEFAULT_HTTP_PORT);
    // Probe connection.
    http_get(port, "/status")
        .await
        .with_context(|| format!("Cannot connect to daemon on port {port}"))?;
    crate::tui::attach(port).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_config_power_update_delay_ticks() {
        // Mirrors Python `test_parse_config_power_update_delay_ticks`.
        let data: Value = serde_json::json!({
            "power_update_delay_ticks": 3,
            "batteries": [
                {"mac": "02B250000001", "phase": "A"},
                {"mac": "02B250000002", "phase": "B", "power_update_delay_ticks": 1},
            ],
        });
        let cfg = parse_json_config(&data).unwrap();
        validate_config(&cfg).unwrap();
        assert_eq!(cfg.power_update_delay_ticks, 3);
        assert_eq!(cfg.batteries[0].power_update_delay_ticks, 3);
        assert_eq!(cfg.batteries[1].power_update_delay_ticks, 1);
    }

    #[test]
    fn config_snippet_three_phase() {
        let s = cmd_config_snippet(8080, 12345, 3);
        assert!(s.contains("JSON_PATHS = $.phase_a,$.phase_b,$.phase_c"));
        assert!(s.contains("URL = http://localhost:8080/power"));
        assert!(s.contains("UDP_PORT = 12345"));
    }

    #[test]
    fn config_snippet_single_phase() {
        let s = cmd_config_snippet(9090, 12345, 1);
        assert!(s.contains("JSON_PATHS = $.phase_a"));
        assert!(!s.contains("phase_b"));
    }
}
