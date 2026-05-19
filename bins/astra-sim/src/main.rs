//! `astra-sim` — Marstek battery + powermeter simulator.
//!
//! Drop-in port of the Python `astra-sim` dev tool. Talks to an
//! astrameter CT002 emulator (via UDP) while serving a JSON HTTP
//! powermeter endpoint that astrameter reads. Includes an interactive
//! ratatui TUI for one-keystroke control of loads, solar and battery
//! state.

mod battery;
mod cli;
mod load_model;
mod powermeter_sim;
mod protocol;
mod runner;
mod tui;

use anyhow::Result;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<()> {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let mut args = cli::parse_args(raw);
    if args.command.is_none() {
        args.command = Some("run".into());
    }
    init_logging(args.verbose, !args.no_tui);

    match args.command.as_deref().unwrap_or("run") {
        "run" => {
            let cfg = cli::build_config(&args)?;
            cli::run_in_process(cfg, !args.no_tui).await?;
        }
        "start" => start_daemon(&args).await?,
        "stop" => cli::cmd_stop(&args).await?,
        "attach" => cli::cmd_attach(&args).await?,
        "status" => cli::cmd_status(&args).await?,
        "load" => cli::cmd_load(&args).await?,
        "solar" => cli::cmd_solar(&args).await?,
        "battery" => cli::cmd_battery(&args).await?,
        "auto" => cli::cmd_auto(&args).await?,
        "config" => {
            let snippet = cli::cmd_config_snippet(
                args.http_port.unwrap_or(cli::DEFAULT_HTTP_PORT),
                args.ct_port.unwrap_or(12345),
                if args.phases == 0 { 3 } else { args.phases },
            );
            print!("{snippet}");
        }
        other => {
            eprintln!("unknown command: {other}");
            std::process::exit(2);
        }
    }
    Ok(())
}

fn init_logging(verbose: bool, tui: bool) {
    // Headless mode default = INFO; TUI mode default = WARN so log lines
    // don't corrupt the terminal. `-v` always wins → DEBUG.
    let level = if verbose {
        "debug"
    } else if tui {
        "warn"
    } else {
        "info"
    };
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(level));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .try_init();
}

/// `astra-sim start` — fork a background daemon (Unix only). Mirrors
/// Python's `cmd_start`.
#[cfg(unix)]
async fn start_daemon(args: &cli::Args) -> Result<()> {
    let pid_path = cli::pid_file();
    if pid_path.exists() {
        let pid: i32 = std::fs::read_to_string(&pid_path)?
            .trim()
            .parse()
            .unwrap_or(0);
        // `kill -0` probe; if alive, refuse.
        if pid > 0 && kill_zero(pid) {
            eprintln!("Daemon already running (PID {pid})");
            std::process::exit(1);
        }
        let _ = std::fs::remove_file(&pid_path);
    }

    // Spawn an in-process child by re-executing ourselves with `--no-tui`
    // and the right CLI flags. That avoids unsafe-fork in async contexts.
    let exe = std::env::current_exe()?;
    let mut cmd = tokio::process::Command::new(exe);
    cmd.arg("run").arg("--no-tui");
    if let Some(c) = &args.config {
        cmd.arg("-c").arg(c);
    }
    if let Some(p) = args.http_port {
        cmd.arg("--http-port").arg(p.to_string());
    }
    if let Some(p) = args.ct_port {
        cmd.arg("--ct-port").arg(p.to_string());
    }
    if let Some(d) = args.power_update_delay {
        cmd.arg("--power-update-delay").arg(d.to_string());
    }
    // Redirect stdio to a log file (matches Python).
    let log = cli::log_file();
    let log_w = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)?;
    cmd.stdout(log_w.try_clone()?).stderr(log_w);
    cmd.stdin(std::process::Stdio::null());
    let child = cmd.spawn()?;
    let pid = child.id().unwrap_or(0);
    std::fs::write(&pid_path, format!("{pid}\n"))?;
    println!("Daemon started (PID {pid})");
    Ok(())
}

#[cfg(not(unix))]
async fn start_daemon(_args: &cli::Args) -> Result<()> {
    anyhow::bail!("`astra-sim start` daemon mode is Unix-only");
}

#[cfg(unix)]
fn kill_zero(pid: i32) -> bool {
    // Send signal 0 — checks existence without actually signalling.
    unsafe { libc_kill(pid, 0) == 0 }
}

#[cfg(unix)]
extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
}
