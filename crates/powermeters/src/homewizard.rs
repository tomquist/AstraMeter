//! `HOMEWIZARD` — port of `src/astrameter/powermeter/homewizard.py`.

use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    ws::{WebSocketClient, WsMessage, WsRequest},
    Platform,
};
use async_trait::async_trait;
use parking_lot::Mutex;
use serde_json::Value;
use tokio::sync::Notify;

const HOMEWIZARD_CA_PEM: &[u8] = include_bytes!("../assets/homewizard_ca.pem");

const WS_HEARTBEAT_SECONDS: u32 = 30;
const DEFAULT_MAX_AGE_SECS: f64 = 30.0;
const WATCHDOG_TIMEOUT_SECS: f64 = 45.0;

pub struct HomeWizard {
    ip: String,
    token: String,
    serial: String,
    verify_ssl: bool,
    max_age_secs: f64,
    ws: Arc<dyn WebSocketClient>,
    timer: Arc<dyn astrameter_platform::Timer>,
    state: Arc<Mutex<State>>,
    notify: Arc<Notify>,
    fresh_notify: Arc<Notify>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
}

#[derive(Default)]
struct State {
    values: Option<Vec<f64>>,
    last_at: Option<f64>,
}

#[async_trait]
impl Powermeter for HomeWizard {
    async fn start(&self) -> Result<()> {
        let mut g = self.task.lock().await;
        if g.is_some() {
            return Ok(());
        }
        let url = format!("wss://{}/api/ws", self.ip);
        let sni = format!("appliance/p1dongle/{}", self.serial);
        let state = self.state.clone();
        let notify = self.notify.clone();
        let fresh = self.fresh_notify.clone();
        let timer = self.timer.clone();
        let cancel = self.cancel.clone();
        let ws = self.ws.clone();
        let token = self.token.clone();
        let verify_ssl = self.verify_ssl;
        let handle = tokio::spawn(async move {
            run_loop(
                url, sni, token, verify_ssl, ws, timer, state, notify, fresh, cancel,
            )
            .await;
        });
        *g = Some(handle);
        Ok(())
    }

    async fn stop(&self) -> Result<()> {
        self.cancel.cancel();
        let mut g = self.task.lock().await;
        if let Some(h) = g.take() {
            let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
        }
        Ok(())
    }

    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let st = self.state.lock();
        let values = st.values.clone().ok_or(Error::NoValue)?;
        if self.max_age_secs > 0.0 {
            if let Some(t) = st.last_at {
                let age = self.timer.monotonic_secs() - t;
                if age > self.max_age_secs {
                    return Err(Error::Stale {
                        age_secs: age,
                        max_secs: self.max_age_secs,
                    });
                }
            }
        }
        Ok(values)
    }

    async fn wait_for_message(&self, timeout: Duration) -> Result<()> {
        if self.state.lock().values.is_some() {
            return Ok(());
        }
        let n = self.notify.clone();
        tokio::time::timeout(timeout, n.notified())
            .await
            .map(|_| ())
            .map_err(|_| Error::Timeout {
                millis: timeout.as_millis() as u64,
            })
    }

    async fn wait_for_next_message(&self, timeout: Duration) -> Result<()> {
        let n = self.notify.clone();
        tokio::time::timeout(timeout, n.notified())
            .await
            .map(|_| ())
            .map_err(|_| Error::Timeout {
                millis: timeout.as_millis() as u64,
            })
    }
}

#[allow(clippy::too_many_arguments)]
async fn run_loop(
    url: String,
    sni: String,
    token: String,
    verify_ssl: bool,
    ws: Arc<dyn WebSocketClient>,
    timer: Arc<dyn astrameter_platform::Timer>,
    state: Arc<Mutex<State>>,
    notify: Arc<Notify>,
    fresh: Arc<Notify>,
    cancel: tokio_util::sync::CancellationToken,
) {
    loop {
        let req = WsRequest {
            url: url.clone(),
            headers: vec![],
            sni_override: Some(sni.clone()),
            verify_tls: verify_ssl,
            extra_root_cert_pem: if verify_ssl {
                Some(HOMEWIZARD_CA_PEM.to_vec())
            } else {
                None
            },
            heartbeat_secs: Some(WS_HEARTBEAT_SECONDS),
        };
        match ws.connect(req).await {
            Ok(mut conn) => {
                tracing::info!("HomeWizard WebSocket connected");
                // Per-connection cancel token: the watchdog cancels THIS
                // token to force a reconnect without taking down the whole
                // task (which the outer `cancel` controls).
                let conn_cancel = tokio_util::sync::CancellationToken::new();
                let wd_fresh = fresh.clone();
                let wd_conn_cancel = conn_cancel.clone();
                let wd_outer_cancel = cancel.clone();
                let watchdog = tokio::spawn(async move {
                    loop {
                        let r = tokio::time::timeout(
                            Duration::from_secs_f64(WATCHDOG_TIMEOUT_SECS),
                            wd_fresh.notified(),
                        )
                        .await;
                        if r.is_err() {
                            tracing::warn!(
                                "HomeWizard watchdog: no measurement for {WATCHDOG_TIMEOUT_SECS:.0}s; reconnecting"
                            );
                            wd_conn_cancel.cancel();
                            break;
                        }
                        if wd_outer_cancel.is_cancelled() {
                            break;
                        }
                    }
                });

                loop {
                    let msg = tokio::select! {
                        _ = cancel.cancelled() => break,
                        _ = conn_cancel.cancelled() => break,
                        m = conn.recv() => m,
                    };
                    match msg {
                        Ok(WsMessage::Text(s)) => {
                            handle_text(&s, &mut *conn, &token, &state, &timer, &notify, &fresh)
                                .await;
                        }
                        Ok(WsMessage::Close) => break,
                        Ok(_) => continue,
                        Err(e) => {
                            tracing::warn!("HomeWizard ws recv: {e}");
                            break;
                        }
                    }
                }
                watchdog.abort();
                let _ = conn.close().await;
            }
            Err(e) => {
                tracing::error!("HomeWizard ws connect: {e}");
            }
        }
        if cancel.is_cancelled() {
            return;
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}

async fn handle_text(
    raw: &str,
    conn: &mut dyn astrameter_platform::ws::WsConnection,
    token: &str,
    state: &Arc<Mutex<State>>,
    timer: &Arc<dyn astrameter_platform::Timer>,
    notify: &Arc<Notify>,
    fresh: &Arc<Notify>,
) {
    let msg: Value = match serde_json::from_str(raw) {
        Ok(v) => v,
        Err(_) => return,
    };
    match msg.get("type").and_then(|v| v.as_str()).unwrap_or("") {
        "authorization_requested" => {
            let _ = conn
                .send(WsMessage::Text(
                    serde_json::json!({"type":"authorization","data": token}).to_string(),
                ))
                .await;
        }
        "authorized" => {
            tracing::info!("HomeWizard: authorized");
            let _ = conn
                .send(WsMessage::Text(
                    serde_json::json!({"type":"subscribe","data":"measurement"}).to_string(),
                ))
                .await;
        }
        "measurement" => {
            if let Some(data) = msg.get("data") {
                let values = if data.get("power_l1_w").is_some() {
                    vec![
                        data.get("power_l1_w")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0),
                        data.get("power_l2_w")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0),
                        data.get("power_l3_w")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0),
                    ]
                } else if let Some(p) = data.get("power_w").and_then(|v| v.as_f64()) {
                    vec![p]
                } else {
                    return;
                };
                let now = timer.monotonic_secs();
                {
                    let mut s = state.lock();
                    s.values = Some(values);
                    s.last_at = Some(now);
                }
                notify.notify_waiters();
                fresh.notify_waiters();
            }
        }
        _ => {}
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let verify_ssl = section.get_bool("VERIFY_SSL", true)?;
    if !verify_ssl {
        tracing::warn!("HomeWizard: VERIFY_SSL=False — use only on a trusted LAN");
    }
    let max_age_secs = section.get_float("MAX_MEASUREMENT_AGE_SECONDS", DEFAULT_MAX_AGE_SECS)?;
    Ok(Arc::new(HomeWizard {
        ip: section.get_required("IP")?.to_string(),
        token: section.get_required("TOKEN")?.to_string(),
        serial: section.get_string("SERIAL", ""),
        verify_ssl,
        max_age_secs: max_age_secs.max(0.0),
        ws: platform.ws.clone(),
        timer: platform.timer.clone(),
        state: Arc::new(Mutex::new(State::default())),
        notify: Arc::new(Notify::new()),
        fresh_notify: Arc::new(Notify::new()),
        cancel: tokio_util::sync::CancellationToken::new(),
        task: tokio::sync::Mutex::new(None),
    }))
}
