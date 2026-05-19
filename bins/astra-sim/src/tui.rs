//! Ratatui TUI — functional port of `simulator/tui.py`.
//!
//! Two modes (matching Python):
//!   * **in-process** — direct access to the running `SimulationRunner`.
//!   * **attach** — connects to a running daemon over HTTP and polls
//!     `/status` once per second.
//!
//! Key bindings: `1..8` toggle loads, `0/9` set SOC to 0/100%, arrows
//! adjust SOC ±10% / solar ±100 W, `s`/`S` solar max/off, `b` cycle
//! selected battery, `p`/`P` max-power ±100 W, `a` auto-mode, `q` quit.

use std::io;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::ExecutableCommand;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Row, Sparkline, Table};
use ratatui::Terminal;
use serde_json::Value;

use crate::battery::tokio_util_local::CancelFlag;
use crate::runner::SimulationRunner;

/// Configurable graph history length (matches Python `_GRAPH_HISTORY`).
const GRAPH_HISTORY: usize = 120;

pub fn run(runner: Arc<SimulationRunner>, cancel: CancelFlag) -> anyhow::Result<()> {
    let mut terminal = setup_terminal()?;
    let res = run_loop(&mut terminal, RunnerSource::InProcess(runner), cancel);
    restore_terminal()?;
    res
}

pub async fn attach(port: u16) -> anyhow::Result<()> {
    let cancel = CancelFlag::new();
    let cancel_for_tui = cancel.clone();
    let port_for_thread = port;
    tokio::task::spawn_blocking(move || {
        let mut terminal = setup_terminal()?;
        let res = run_loop(
            &mut terminal,
            RunnerSource::Daemon(port_for_thread),
            cancel_for_tui,
        );
        restore_terminal()?;
        res
    })
    .await
    .map_err(|e| anyhow::anyhow!("tui thread joined: {e}"))?
}

fn setup_terminal() -> anyhow::Result<Terminal<CrosstermBackend<io::Stdout>>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    stdout.execute(EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    Ok(Terminal::new(backend)?)
}

fn restore_terminal() -> anyhow::Result<()> {
    disable_raw_mode()?;
    io::stdout().execute(LeaveAlternateScreen)?;
    Ok(())
}

enum RunnerSource {
    InProcess(Arc<SimulationRunner>),
    Daemon(u16),
}

#[derive(Default)]
struct UiState {
    selected_battery: usize,
    history: std::collections::VecDeque<f64>,
}

fn run_loop(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    source: RunnerSource,
    cancel: CancelFlag,
) -> anyhow::Result<()> {
    let mut ui = UiState::default();
    let mut last_poll = Instant::now() - Duration::from_secs(2);
    let mut cached_status: Option<Value> = None;
    loop {
        // Refresh status at most once per second.
        if last_poll.elapsed() >= Duration::from_millis(500) {
            cached_status = match &source {
                RunnerSource::InProcess(r) => Some(in_process_status(r)),
                RunnerSource::Daemon(p) => http_get_blocking(*p, "/status").ok(),
            };
            if let Some(v) = &cached_status {
                let total = v
                    .get("grid")
                    .and_then(|g| g.get("total"))
                    .and_then(|t| t.as_f64())
                    .unwrap_or(0.0);
                ui.history.push_back(total);
                while ui.history.len() > GRAPH_HISTORY {
                    ui.history.pop_front();
                }
            }
            last_poll = Instant::now();
        }
        let status = cached_status.clone().unwrap_or(serde_json::Value::Null);

        terminal.draw(|f| draw(f, &status, &ui))?;

        if cancel.is_cancelled() {
            break;
        }
        if event::poll(Duration::from_millis(200))? {
            if let Event::Key(k) = event::read()? {
                if k.kind == KeyEventKind::Release {
                    continue;
                }
                if handle_key(k.code, k.modifiers, &source, &mut ui, &status)? {
                    break;
                }
            }
        }
    }
    Ok(())
}

fn handle_key(
    code: KeyCode,
    mods: KeyModifiers,
    source: &RunnerSource,
    ui: &mut UiState,
    status: &Value,
) -> anyhow::Result<bool> {
    match code {
        KeyCode::Char('q') | KeyCode::Char('Q') => return Ok(true),
        KeyCode::Char(c @ '1'..='8') => {
            let idx = (c as u8 - b'0') as usize;
            do_toggle_load(source, idx);
        }
        KeyCode::Char('9') => do_set_soc(source, ui, status, 1.0),
        KeyCode::Char('0') => do_set_soc(source, ui, status, 0.0),
        KeyCode::Char('s') if mods.contains(KeyModifiers::SHIFT) => do_set_solar(source, "off"),
        KeyCode::Char('S') => do_set_solar(source, "off"),
        KeyCode::Char('s') => do_set_solar(source, "max"),
        KeyCode::Char('b') | KeyCode::Char('B') => {
            let n = status
                .get("batteries")
                .and_then(|v| v.as_array())
                .map(|a| a.len())
                .unwrap_or(0)
                .max(1);
            ui.selected_battery = (ui.selected_battery + 1) % n;
        }
        KeyCode::Up => do_adjust_solar(source, status, 100.0),
        KeyCode::Down => do_adjust_solar(source, status, -100.0),
        KeyCode::Left => do_adjust_soc(source, ui, status, -0.1),
        KeyCode::Right => do_adjust_soc(source, ui, status, 0.1),
        KeyCode::Char('p') => do_adjust_max_power(source, ui, status, -100),
        KeyCode::Char('P') => do_adjust_max_power(source, ui, status, 100),
        KeyCode::Char('a') | KeyCode::Char('A') => do_toggle_auto(source, status),
        _ => {}
    }
    Ok(false)
}

fn do_toggle_load(source: &RunnerSource, index: usize) {
    match source {
        RunnerSource::InProcess(r) => {
            let _ = r.load_model.lock().toggle_load(index);
        }
        RunnerSource::Daemon(p) => {
            let _ = http_post_blocking(*p, &format!("/loads/{index}/toggle"), Value::Null);
        }
    }
}

fn do_set_solar(source: &RunnerSource, value: &str) {
    let body = serde_json::json!({"watts": value});
    match source {
        RunnerSource::InProcess(r) => {
            let mut lm = r.load_model.lock();
            let w = match value {
                "off" => 0.0,
                "max" => lm.solar_max,
                other => other.parse::<f64>().unwrap_or(0.0),
            };
            lm.set_solar(w);
        }
        RunnerSource::Daemon(p) => {
            let _ = http_post_blocking(*p, "/solar", body);
        }
    }
}

fn do_adjust_solar(source: &RunnerSource, status: &Value, delta: f64) {
    let current = status
        .get("solar")
        .and_then(|s| s.get("current"))
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);
    let new = (current + delta).max(0.0);
    match source {
        RunnerSource::InProcess(r) => {
            r.load_model.lock().set_solar(new);
        }
        RunnerSource::Daemon(p) => {
            let _ = http_post_blocking(*p, "/solar", serde_json::json!({"watts": new}));
        }
    }
}

fn selected_mac(ui: &UiState, status: &Value) -> Option<String> {
    let arr = status.get("batteries")?.as_array()?;
    arr.get(ui.selected_battery)?
        .get("mac")?
        .as_str()
        .map(String::from)
}

fn do_set_soc(source: &RunnerSource, ui: &UiState, status: &Value, soc: f64) {
    let Some(mac) = selected_mac(ui, status) else {
        return;
    };
    match source {
        RunnerSource::InProcess(r) => {
            if let Some(b) = r.batteries.iter().find(|b| b.mac == mac.to_uppercase()) {
                b.set_soc(soc);
            }
        }
        RunnerSource::Daemon(p) => {
            let _ = http_post_blocking(
                *p,
                &format!("/batteries/{mac}/soc"),
                serde_json::json!({"soc": soc}),
            );
        }
    }
}

fn do_adjust_soc(source: &RunnerSource, ui: &UiState, status: &Value, delta: f64) {
    if selected_mac(ui, status).is_none() {
        return;
    }
    let current = status
        .get("batteries")
        .and_then(|v| v.as_array())
        .and_then(|a| a.get(ui.selected_battery))
        .and_then(|b| b.get("soc"))
        .and_then(|v| v.as_f64())
        .unwrap_or(0.5);
    let new = (current + delta).clamp(0.0, 1.0);
    do_set_soc(source, ui, status, new);
}

fn do_adjust_max_power(source: &RunnerSource, ui: &UiState, status: &Value, delta: i64) {
    let Some(mac) = selected_mac(ui, status) else {
        return;
    };
    let charge = status
        .get("batteries")
        .and_then(|v| v.as_array())
        .and_then(|a| a.get(ui.selected_battery))
        .and_then(|b| b.get("max_charge"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let discharge = status
        .get("batteries")
        .and_then(|v| v.as_array())
        .and_then(|a| a.get(ui.selected_battery))
        .and_then(|b| b.get("max_discharge"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let new_c = (charge + delta).max(0);
    let new_d = (discharge + delta).max(0);
    match source {
        RunnerSource::InProcess(r) => {
            if let Some(b) = r.batteries.iter().find(|b| b.mac == mac.to_uppercase()) {
                b.set_max_charge(new_c);
                b.set_max_discharge(new_d);
            }
        }
        RunnerSource::Daemon(p) => {
            let _ = http_post_blocking(
                *p,
                &format!("/batteries/{mac}/max_power"),
                serde_json::json!({"charge": new_c, "discharge": new_d}),
            );
        }
    }
}

fn do_toggle_auto(source: &RunnerSource, status: &Value) {
    let enabled = !status
        .get("auto_mode")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    match source {
        RunnerSource::InProcess(r) => {
            r.load_model.lock().auto_mode = enabled;
        }
        RunnerSource::Daemon(p) => {
            let _ = http_post_blocking(*p, "/auto", serde_json::json!({"enabled": enabled}));
        }
    }
}

// ── Rendering ─────────────────────────────────────────────────────

fn draw(f: &mut ratatui::Frame, status: &Value, ui: &UiState) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),  // header
            Constraint::Length(6),  // grid table
            Constraint::Length(12), // graph
            Constraint::Min(6),     // batteries
            Constraint::Length(8),  // loads
            Constraint::Length(3),  // footer
        ])
        .split(f.area());

    draw_header(f, chunks[0], status);
    draw_grid_table(f, chunks[1], status);
    draw_graph(f, chunks[2], ui);
    draw_battery_table(f, chunks[3], status, ui);
    draw_load_panel(f, chunks[4], status);
    draw_footer(f, chunks[5]);
}

fn draw_header(f: &mut ratatui::Frame, area: Rect, status: &Value) {
    let auto = status
        .get("auto_mode")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let solar = status
        .get("solar")
        .and_then(|s| s.get("current"))
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);
    let header = format!(
        " astra-sim — solar {:>4.0}W  auto={}",
        solar,
        if auto { "ON " } else { "off" }
    );
    let p = Paragraph::new(header).style(Style::default().fg(Color::Cyan));
    f.render_widget(p, area);
}

fn draw_grid_table(f: &mut ratatui::Frame, area: Rect, status: &Value) {
    let g = status.get("grid");
    let val = |k: &str| {
        g.and_then(|v| v.get(k))
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0)
    };
    let header_row = Row::new(vec!["Grid (W)", "Phase A", "Phase B", "Phase C", "Total"])
        .style(Style::default().add_modifier(Modifier::BOLD));
    let row = Row::new(vec![
        "".to_string(),
        format!("{:.0}", val("phase_a")),
        format!("{:.0}", val("phase_b")),
        format!("{:.0}", val("phase_c")),
        format!("{:.0}", val("total")),
    ]);
    let widths = [
        Constraint::Length(10),
        Constraint::Length(10),
        Constraint::Length(10),
        Constraint::Length(10),
        Constraint::Length(10),
    ];
    let table = Table::new(vec![row], widths)
        .header(header_row)
        .block(Block::default().borders(Borders::ALL).title("Grid"));
    f.render_widget(table, area);
}

fn draw_graph(f: &mut ratatui::Frame, area: Rect, ui: &UiState) {
    let data: Vec<u64> = ui.history.iter().map(|v| v.abs().round() as u64).collect();
    let sp = Sparkline::default()
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title("Grid total |W| (last 120 samples)"),
        )
        .data(&data)
        .style(Style::default().fg(Color::LightGreen));
    f.render_widget(sp, area);
}

fn draw_battery_table(f: &mut ratatui::Frame, area: Rect, status: &Value, ui: &UiState) {
    let header = Row::new(vec!["#", "MAC", "Phase", "Power", "Target", "SOC", "Max ±"])
        .style(Style::default().add_modifier(Modifier::BOLD));
    let mut rows = Vec::new();
    if let Some(arr) = status.get("batteries").and_then(|v| v.as_array()) {
        for (i, b) in arr.iter().enumerate() {
            let marker = if i == ui.selected_battery { ">" } else { " " };
            let mac = b.get("mac").and_then(|v| v.as_str()).unwrap_or("");
            let phase = b.get("phase").and_then(|v| v.as_str()).unwrap_or("");
            let power = b.get("power").and_then(|v| v.as_i64()).unwrap_or(0);
            let target = b.get("target").and_then(|v| v.as_i64()).unwrap_or(0);
            let soc = b.get("soc").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let chg = b.get("max_charge").and_then(|v| v.as_i64()).unwrap_or(0);
            let dis = b.get("max_discharge").and_then(|v| v.as_i64()).unwrap_or(0);
            let row = Row::new(vec![
                marker.to_string(),
                mac.to_string(),
                phase.to_string(),
                format!("{power:+}W"),
                format!("{target:+}W"),
                format!("{:.0}%", soc * 100.0),
                format!("-{chg}/+{dis}"),
            ]);
            let row = if i == ui.selected_battery {
                row.style(
                    Style::default()
                        .fg(Color::Black)
                        .bg(Color::White)
                        .add_modifier(Modifier::BOLD),
                )
            } else {
                row
            };
            rows.push(row);
        }
    }
    let widths = [
        Constraint::Length(2),
        Constraint::Length(14),
        Constraint::Length(6),
        Constraint::Length(8),
        Constraint::Length(8),
        Constraint::Length(6),
        Constraint::Length(14),
    ];
    let table = Table::new(rows, widths)
        .header(header)
        .block(Block::default().borders(Borders::ALL).title("Batteries"));
    f.render_widget(table, area);
}

fn draw_load_panel(f: &mut ratatui::Frame, area: Rect, status: &Value) {
    let mut lines = Vec::new();
    if let Some(arr) = status.get("loads").and_then(|v| v.as_array()) {
        for (i, ld) in arr.iter().enumerate() {
            let name = ld.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let power = ld.get("power").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let phase = ld.get("phase").and_then(|v| v.as_str()).unwrap_or("");
            let active = ld.get("active").and_then(|v| v.as_bool()).unwrap_or(false);
            let mark = if active { "[x]" } else { "[ ]" };
            let style = if active {
                Style::default().fg(Color::Green)
            } else {
                Style::default().fg(Color::DarkGray)
            };
            lines.push(Line::from(vec![Span::styled(
                format!(" {}  {mark} {name}  {power:.0}W  (phase {phase})", i + 1),
                style,
            )]));
        }
    }
    let p = Paragraph::new(lines).block(Block::default().borders(Borders::ALL).title("Loads"));
    f.render_widget(p, area);
}

fn draw_footer(f: &mut ratatui::Frame, area: Rect) {
    let txt = " 1..8 toggle  9/0 SOC 100/0  ←→ SOC±10%  ↑↓ solar±100  s/S solar max/off  b cycle bat  p/P max±100  a auto  q quit";
    let p = Paragraph::new(txt).style(Style::default().fg(Color::Gray));
    f.render_widget(p, area);
}

// ── In-process / HTTP helpers ─────────────────────────────────────

fn in_process_status(r: &SimulationRunner) -> Value {
    let grid = r.compute_grid();
    let total: f64 = grid
        .as_object()
        .map(|m| m.values().filter_map(|v| v.as_f64()).sum())
        .unwrap_or(0.0);
    let mut grid_obj = grid.as_object().cloned().unwrap_or_default();
    grid_obj.insert(
        "total".into(),
        serde_json::json!((total * 10.0).round() / 10.0),
    );
    let lm = r.load_model.lock().to_json();
    let mut out = serde_json::Map::new();
    out.insert("grid".into(), Value::Object(grid_obj));
    if let Value::Object(m) = lm {
        for (k, v) in m {
            out.insert(k, v);
        }
    }
    out.insert(
        "batteries".into(),
        Value::Array(
            r.batteries
                .iter()
                .map(|b| serde_json::to_value(b.snapshot()).unwrap())
                .collect(),
        ),
    );
    Value::Object(out)
}

fn http_get_blocking(port: u16, path: &str) -> anyhow::Result<Value> {
    let url = format!("http://localhost:{port}{path}");
    let resp = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()?
        .get(url)
        .send()?;
    Ok(resp.json()?)
}

fn http_post_blocking(port: u16, path: &str, body: Value) -> anyhow::Result<Value> {
    let url = format!("http://localhost:{port}{path}");
    let resp = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()?
        .post(url)
        .json(&body)
        .send()?;
    Ok(resp.json()?)
}
