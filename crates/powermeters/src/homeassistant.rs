//! `HOMEASSISTANT` — port of `src/astrameter/powermeter/homeassistant.py`.
//! WebSocket subscribe_entities protocol.

use std::collections::{HashMap, HashSet};
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
use serde_json::{json, Value};
use tokio::sync::Notify;

const WS_HEARTBEAT_SECONDS: u32 = 30;

pub struct HomeAssistant {
    ip: String,
    port: String,
    use_https: bool,
    access_token: String,
    current_power_entity: Vec<String>,
    power_calculate: bool,
    power_input_alias: Vec<String>,
    power_output_alias: Vec<String>,
    path_prefix: Option<String>,
    tracked: HashSet<String>,
    ws: Arc<dyn WebSocketClient>,
    state: Arc<Mutex<State>>,
    ready_notify: Arc<Notify>,
    message_notify: Arc<Notify>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
}

#[derive(Default)]
struct State {
    entity_values: HashMap<String, Option<f64>>,
    msg_id: u64,
    subscribe_id: Option<u64>,
}

impl HomeAssistant {
    fn build_ws_url(&self) -> String {
        let scheme = if self.use_https { "wss" } else { "ws" };
        let prefix = self.path_prefix.as_deref().unwrap_or("");
        format!("{scheme}://{}:{}{prefix}/api/websocket", self.ip, self.port)
    }

    fn collect_entities(&self) -> HashSet<String> {
        let mut set = HashSet::new();
        if self.power_calculate {
            for e in &self.power_input_alias {
                if !e.is_empty() {
                    set.insert(e.clone());
                }
            }
            for e in &self.power_output_alias {
                if !e.is_empty() {
                    set.insert(e.clone());
                }
            }
        } else {
            for e in &self.current_power_entity {
                if !e.is_empty() {
                    set.insert(e.clone());
                }
            }
        }
        set
    }
}

#[async_trait]
impl Powermeter for HomeAssistant {
    async fn start(&self) -> Result<()> {
        let mut g = self.task.lock().await;
        if g.is_some() {
            return Ok(());
        }
        let url = self.build_ws_url();
        let tracked = self.tracked.clone();
        let access_token = self.access_token.clone();
        let state = self.state.clone();
        let ready = self.ready_notify.clone();
        let message = self.message_notify.clone();
        let cancel = self.cancel.clone();
        let ws = self.ws.clone();
        let handle = tokio::spawn(async move {
            run_loop(
                url,
                tracked,
                access_token,
                state,
                ready,
                message,
                cancel,
                ws,
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
        let state = self.state.lock();
        let lookup = |id: &str| -> Result<f64> {
            state
                .entity_values
                .get(id)
                .and_then(|v| *v)
                .ok_or_else(|| Error::transport(format!("HA sensor {id} has no state")))
        };
        if !self.power_calculate {
            self.current_power_entity
                .iter()
                .map(|e| lookup(e))
                .collect()
        } else {
            let mut out = Vec::new();
            for (i, in_e) in self.power_input_alias.iter().enumerate() {
                let out_e = self.power_output_alias.get(i).cloned().unwrap_or_default();
                let pi = lookup(in_e)?;
                let po = lookup(&out_e)?;
                out.push(pi - po);
            }
            Ok(out)
        }
    }

    async fn wait_for_message(&self, timeout: Duration) -> Result<()> {
        let n = self.ready_notify.clone();
        let state = self.state.clone();
        let tracked = self.tracked.clone();
        let ready = move || {
            tracked
                .iter()
                .all(|e| state.lock().entity_values.get(e).and_then(|v| *v).is_some())
        };
        if ready() {
            return Ok(());
        }
        tokio::time::timeout(timeout, n.notified())
            .await
            .map(|_| ())
            .map_err(|_| Error::Timeout {
                millis: timeout.as_millis() as u64,
            })
    }

    async fn wait_for_next_message(&self, timeout: Duration) -> Result<()> {
        let n = self.message_notify.clone();
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
    tracked: HashSet<String>,
    access_token: String,
    state: Arc<Mutex<State>>,
    ready: Arc<Notify>,
    message: Arc<Notify>,
    cancel: tokio_util::sync::CancellationToken,
    ws: Arc<dyn WebSocketClient>,
) {
    loop {
        let req = WsRequest {
            url: url.clone(),
            headers: vec![],
            sni_override: None,
            verify_tls: true,
            extra_root_cert_pem: None,
            heartbeat_secs: Some(WS_HEARTBEAT_SECONDS),
        };
        match ws.connect(req).await {
            Ok(mut conn) => {
                tracing::info!("Home Assistant WebSocket connected to {url}");
                // Reset protocol state.
                {
                    let mut s = state.lock();
                    s.msg_id = 0;
                    s.subscribe_id = None;
                    for v in s.entity_values.values_mut() {
                        *v = None;
                    }
                }
                loop {
                    let msg = tokio::select! {
                        _ = cancel.cancelled() => break,
                        m = conn.recv() => m,
                    };
                    match msg {
                        Ok(WsMessage::Text(s)) => {
                            handle_message(
                                &s,
                                &mut *conn,
                                &access_token,
                                &tracked,
                                &state,
                                &ready,
                                &message,
                            )
                            .await;
                        }
                        Ok(WsMessage::Close) => break,
                        Ok(_) => continue,
                        Err(e) => {
                            tracing::warn!("HA ws recv: {e}");
                            break;
                        }
                    }
                }
                let _ = conn.close().await;
            }
            Err(e) => {
                tracing::error!("HA ws connect: {e}");
            }
        }
        if cancel.is_cancelled() {
            return;
        }
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}

async fn handle_message(
    raw: &str,
    conn: &mut dyn astrameter_platform::ws::WsConnection,
    access_token: &str,
    tracked: &HashSet<String>,
    state: &Arc<Mutex<State>>,
    ready: &Arc<Notify>,
    message: &Arc<Notify>,
) {
    let msg: Value = match serde_json::from_str(raw) {
        Ok(v) => v,
        Err(e) => {
            tracing::error!("HA decode: {e}");
            return;
        }
    };
    let mt = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");
    match mt {
        "auth_required" => {
            let _ = conn
                .send(WsMessage::Text(
                    json!({"type": "auth", "access_token": access_token}).to_string(),
                ))
                .await;
        }
        "auth_ok" => {
            tracing::info!("Home Assistant: authenticated");
            let id = {
                let mut s = state.lock();
                s.msg_id += 1;
                s.subscribe_id = Some(s.msg_id);
                s.msg_id
            };
            let mut ents: Vec<&String> = tracked.iter().collect();
            ents.sort();
            let _ = conn
                .send(WsMessage::Text(
                    json!({
                        "id": id,
                        "type": "subscribe_entities",
                        "entity_ids": ents,
                    })
                    .to_string(),
                ))
                .await;
        }
        "auth_invalid" => {
            tracing::error!(
                "HA auth failed: {}",
                msg.get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("(no message)")
            );
        }
        "event" => {
            if let Some(ev) = msg.get("event") {
                apply_event(ev, tracked, state, ready, message);
            }
        }
        _ => {}
    }
}

fn apply_event(
    ev: &Value,
    tracked: &HashSet<String>,
    state: &Arc<Mutex<State>>,
    ready: &Arc<Notify>,
    message: &Arc<Notify>,
) {
    let mut changed = false;
    if let Some(adds) = ev.get("a").and_then(|v| v.as_object()) {
        for (eid, st) in adds {
            if !tracked.contains(eid) {
                continue;
            }
            if let Some(s) = st.get("s") {
                update_value(state, eid, s);
                changed = true;
            }
        }
    }
    if let Some(changes) = ev.get("c").and_then(|v| v.as_object()) {
        for (eid, diff) in changes {
            if !tracked.contains(eid) {
                continue;
            }
            if let Some(plus) = diff.get("+") {
                if let Some(s) = plus.get("s") {
                    update_value(state, eid, s);
                    changed = true;
                } else if (plus.get("lu").is_some() || plus.get("lc").is_some())
                    && state
                        .lock()
                        .entity_values
                        .get(eid)
                        .and_then(|v| *v)
                        .is_some()
                {
                    message.notify_waiters();
                }
            }
        }
    }
    if let Some(rems) = ev.get("r").and_then(|v| v.as_array()) {
        for v in rems {
            if let Some(eid) = v.as_str() {
                if tracked.contains(eid) {
                    state.lock().entity_values.insert(eid.to_string(), None);
                    changed = true;
                }
            }
        }
    }
    if changed {
        message.notify_waiters();
        let all_ready = {
            let s = state.lock();
            tracked
                .iter()
                .all(|e| s.entity_values.get(e).and_then(|v| *v).is_some())
        };
        if all_ready {
            ready.notify_waiters();
        }
    }
}

fn update_value(state: &Arc<Mutex<State>>, eid: &str, state_val: &Value) {
    let v: Option<f64> = match state_val {
        Value::Null => None,
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    };
    state.lock().entity_values.insert(eid.to_string(), v);
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let split = |s: &str| -> Vec<String> {
        s.split(',')
            .map(|p| p.trim())
            .filter(|p| !p.is_empty())
            .map(|p| p.to_string())
            .collect()
    };
    let current = split(section.get_str("CURRENT_POWER_ENTITY", ""));
    let inputs = split(section.get_str("POWER_INPUT_ALIAS", ""));
    let outputs = split(section.get_str("POWER_OUTPUT_ALIAS", ""));
    let power_calculate = section.get_bool("POWER_CALCULATE", false)?;
    if power_calculate && inputs.len() != outputs.len() {
        return Err(Error::config(
            "HOMEASSISTANT POWER_INPUT_ALIAS / POWER_OUTPUT_ALIAS count differs",
        ));
    }
    let prefix = section.get_opt_string("API_PATH_PREFIX");
    let ha = HomeAssistant {
        ip: section.get_required("IP")?.to_string(),
        port: section.get_string("PORT", ""),
        use_https: section.get_bool("HTTPS", false)?,
        access_token: section.get_required("ACCESSTOKEN")?.to_string(),
        current_power_entity: current,
        power_calculate,
        power_input_alias: inputs,
        power_output_alias: outputs,
        path_prefix: prefix,
        tracked: HashSet::new(),
        ws: platform.ws.clone(),
        state: Arc::new(Mutex::new(State::default())),
        ready_notify: Arc::new(Notify::new()),
        message_notify: Arc::new(Notify::new()),
        cancel: tokio_util::sync::CancellationToken::new(),
        task: tokio::sync::Mutex::new(None),
    };
    let tracked = ha.collect_entities();
    // Seed entity_values map.
    {
        let mut s = ha.state.lock();
        for e in &tracked {
            s.entity_values.insert(e.clone(), None);
        }
    }
    let mut ha = ha;
    ha.tracked = tracked;
    Ok(Arc::new(ha))
}
