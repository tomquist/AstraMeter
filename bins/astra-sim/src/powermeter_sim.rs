//! HTTP powermeter simulator — port of `simulator/powermeter_sim.py`.
//!
//! Exposes:
//!   * `GET  /power`                   — `{phase_a, phase_b, phase_c}` watts
//!   * `GET  /status`                  — full structured snapshot
//!   * `POST /loads/{index}/toggle`    — flip a load on/off
//!   * `POST /solar`                   — `{watts: <num | "off" | "max">}`
//!   * `POST /batteries/{mac}/soc`     — `{soc: 0.0..1.0}`
//!   * `POST /batteries/{mac}/max_power` — `{charge, discharge}`
//!   * `POST /auto`                    — `{enabled: bool}`
//!   * `POST /shutdown`                — request a clean shutdown

use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tokio::sync::Notify;

use crate::battery::tokio_util_local::CancelFlag;
use crate::runner::SimulationRunner;

#[derive(Clone)]
struct AppState {
    runner: Arc<SimulationRunner>,
    shutdown: Arc<Notify>,
    cancel: CancelFlag,
}

/// Spawn the HTTP server. Returns once the bind succeeds; the caller
/// must `await` the returned `JoinHandle` to actually drive it.
pub async fn serve(
    runner: Arc<SimulationRunner>,
    host: &str,
    port: u16,
    cancel: CancelFlag,
) -> anyhow::Result<(SocketAddr, Arc<Notify>, tokio::task::JoinHandle<()>)> {
    let shutdown = Arc::new(Notify::new());
    let state = AppState {
        runner,
        shutdown: shutdown.clone(),
        cancel: cancel.clone(),
    };
    let app = Router::new()
        .route("/power", get(handle_power))
        .route("/status", get(handle_status))
        .route("/loads/:index/toggle", post(handle_toggle_load))
        .route("/solar", post(handle_set_solar))
        .route("/batteries/:mac/soc", post(handle_set_soc))
        .route("/batteries/:mac/max_power", post(handle_set_max_power))
        .route("/auto", post(handle_set_auto))
        .route("/shutdown", post(handle_shutdown))
        .with_state(state);
    let listener = TcpListener::bind(format!("{host}:{port}")).await?;
    let addr = listener.local_addr()?;
    tracing::info!("Powermeter HTTP server listening on {addr}");
    let cancel2 = cancel.clone();
    let handle = tokio::spawn(async move {
        let svc = app.into_make_service();
        let _ = axum::serve(listener, svc)
            .with_graceful_shutdown(async move {
                while !cancel2.is_cancelled() {
                    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                }
            })
            .await;
    });
    Ok((addr, shutdown, handle))
}

async fn handle_power(State(s): State<AppState>) -> Json<Value> {
    Json(s.runner.compute_grid())
}

async fn handle_status(State(s): State<AppState>) -> Json<Value> {
    Json(build_status(&s.runner))
}

fn build_status(runner: &SimulationRunner) -> Value {
    let grid = runner.compute_grid();
    let total: f64 = grid
        .as_object()
        .map(|m| m.values().filter_map(|v| v.as_f64()).sum::<f64>())
        .unwrap_or(0.0);
    let mut grid_obj = grid.as_object().cloned().unwrap_or_default();
    grid_obj.insert("total".into(), json!((total * 10.0).round() / 10.0));
    let lm = runner.load_model.lock().to_json();
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
            runner
                .batteries
                .iter()
                .map(|b| serde_json::to_value(b.snapshot()).unwrap())
                .collect(),
        ),
    );
    Value::Object(out)
}

async fn handle_toggle_load(
    State(s): State<AppState>,
    Path(index): Path<usize>,
) -> impl IntoResponse {
    let mut lm = s.runner.load_model.lock();
    match lm.toggle_load(index) {
        Ok(()) => {
            drop(lm);
            (StatusCode::OK, Json(build_status(&s.runner))).into_response()
        }
        Err(e) => err400(&e),
    }
}

#[derive(Deserialize)]
struct SolarBody {
    watts: Value,
}

async fn handle_set_solar(
    State(s): State<AppState>,
    Json(body): Json<SolarBody>,
) -> impl IntoResponse {
    let watts = match body.watts {
        Value::Number(n) => match n.as_f64() {
            Some(f) => f,
            None => return err400("invalid watts"),
        },
        Value::String(ref txt) => match txt.as_str() {
            "off" => 0.0,
            "max" => s.runner.load_model.lock().solar_max,
            other => match other.parse::<f64>() {
                Ok(f) => f,
                Err(_) => return err400("invalid watts"),
            },
        },
        _ => return err400("missing 'watts'"),
    };
    s.runner.load_model.lock().set_solar(watts);
    (StatusCode::OK, Json(build_status(&s.runner))).into_response()
}

#[derive(Deserialize)]
struct SocBody {
    soc: f64,
}

async fn handle_set_soc(
    State(s): State<AppState>,
    Path(mac): Path<String>,
    Json(body): Json<SocBody>,
) -> impl IntoResponse {
    let mac_u = mac.to_uppercase();
    let b = match s.runner.batteries.iter().find(|b| b.mac == mac_u) {
        Some(b) => b,
        None => return err(StatusCode::NOT_FOUND, "battery not found"),
    };
    if !(0.0..=1.0).contains(&body.soc) {
        return err400("invalid 'soc'");
    }
    b.set_soc(body.soc);
    (StatusCode::OK, Json(build_status(&s.runner))).into_response()
}

#[derive(Deserialize)]
struct MaxPowerBody {
    #[serde(default)]
    charge: Option<i64>,
    #[serde(default)]
    discharge: Option<i64>,
}

async fn handle_set_max_power(
    State(s): State<AppState>,
    Path(mac): Path<String>,
    Json(body): Json<MaxPowerBody>,
) -> impl IntoResponse {
    let mac_u = mac.to_uppercase();
    let b = match s.runner.batteries.iter().find(|b| b.mac == mac_u) {
        Some(b) => b,
        None => return err(StatusCode::NOT_FOUND, "battery not found"),
    };
    if let Some(c) = body.charge {
        if c < 0 {
            return err400("max power must be >= 0");
        }
        b.set_max_charge(c);
    }
    if let Some(d) = body.discharge {
        if d < 0 {
            return err400("max power must be >= 0");
        }
        b.set_max_discharge(d);
    }
    (StatusCode::OK, Json(build_status(&s.runner))).into_response()
}

#[derive(Deserialize)]
struct AutoBody {
    #[serde(default)]
    enabled: bool,
}

async fn handle_set_auto(
    State(s): State<AppState>,
    Json(body): Json<AutoBody>,
) -> impl IntoResponse {
    s.runner.load_model.lock().auto_mode = body.enabled;
    (StatusCode::OK, Json(build_status(&s.runner))).into_response()
}

async fn handle_shutdown(State(s): State<AppState>) -> impl IntoResponse {
    s.shutdown.notify_waiters();
    s.cancel.cancel();
    (StatusCode::OK, Json(json!({"status": "shutting_down"}))).into_response()
}

fn err400(msg: &str) -> axum::response::Response {
    err(StatusCode::BAD_REQUEST, msg)
}
fn err(code: StatusCode, msg: &str) -> axum::response::Response {
    (code, Json(json!({"error": msg}))).into_response()
}
