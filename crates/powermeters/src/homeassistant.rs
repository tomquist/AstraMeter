//! `HOMEASSISTANT` — port of `src/astrameter/powermeter/homeassistant.py`.
//! WebSocket subscribe_entities protocol.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    http::{HttpClient, HttpRequest},
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
    http: Arc<dyn HttpClient>,
    state: Arc<Mutex<State>>,
    ready_notify: Arc<Notify>,
    message_notify: Arc<Notify>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
    /// JoinHandle for the REST bootstrap task spawned after `auth_ok`.
    /// Aborted on reconnect (in `run_loop`'s reset block) and on
    /// `stop()` so a late response can't resurrect a stale value
    /// after the cache has been cleared. Matches the Python
    /// supervisor's `_fetch_states_task` (PR #383).
    bootstrap_task: Arc<tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>>,
}

#[derive(Default)]
struct State {
    entity_values: HashMap<String, Option<f64>>,
    msg_id: u64,
    subscribe_id: Option<u64>,
}

impl State {}

impl HomeAssistant {
    fn build_ws_url(&self) -> String {
        let scheme = if self.use_https { "wss" } else { "ws" };
        let prefix = self.path_prefix.as_deref().unwrap_or("");
        format!("{scheme}://{}:{}{prefix}/api/websocket", self.ip, self.port)
    }

    /// Per-entity REST URL: `<scheme>://<ip>:<port><prefix>/api/states/<entity_id>`.
    /// Matches the Python supervisor's `_build_state_url` (PR #383).
    fn build_state_base_url(&self) -> String {
        let scheme = if self.use_https { "https" } else { "http" };
        let prefix = self.path_prefix.as_deref().unwrap_or("");
        format!("{scheme}://{}:{}{prefix}/api/states/", self.ip, self.port)
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
        let state_base_url = self.build_state_base_url();
        let tracked = self.tracked.clone();
        let access_token = self.access_token.clone();
        let state = self.state.clone();
        let ready = self.ready_notify.clone();
        let message = self.message_notify.clone();
        let cancel = self.cancel.clone();
        let ws = self.ws.clone();
        let http = self.http.clone();
        let bootstrap_task = self.bootstrap_task.clone();
        let handle = tokio::spawn(async move {
            run_loop(
                url,
                state_base_url,
                tracked,
                access_token,
                state,
                ready,
                message,
                cancel,
                ws,
                http,
                bootstrap_task,
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
        // Abort any in-flight REST bootstrap so it can't keep firing
        // requests against the (no-longer-tracked) entities, and so
        // that on a subsequent start() we don't accidentally see a
        // late response resurrect a stale value.
        let mut b = self.bootstrap_task.lock().await;
        if let Some(h) = b.take() {
            h.abort();
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
    state_base_url: String,
    tracked: HashSet<String>,
    access_token: String,
    state: Arc<Mutex<State>>,
    ready: Arc<Notify>,
    message: Arc<Notify>,
    cancel: tokio_util::sync::CancellationToken,
    ws: Arc<dyn WebSocketClient>,
    http: Arc<dyn HttpClient>,
    bootstrap_task: Arc<tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>>,
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
                // Reset protocol state. Abort any REST bootstrap from
                // the previous connection so a late response can't
                // resurrect a stale value after we just cleared the
                // cache. Matches Python `_reset_for_reconnect`
                // (PR #383).
                {
                    let mut s = state.lock();
                    s.msg_id = 0;
                    s.subscribe_id = None;
                    for v in s.entity_values.values_mut() {
                        *v = None;
                    }
                }
                {
                    let mut b = bootstrap_task.lock().await;
                    if let Some(h) = b.take() {
                        h.abort();
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
                                &http,
                                &state_base_url,
                                &bootstrap_task,
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

#[allow(clippy::too_many_arguments)]
async fn handle_message(
    raw: &str,
    conn: &mut dyn astrameter_platform::ws::WsConnection,
    access_token: &str,
    tracked: &HashSet<String>,
    state: &Arc<Mutex<State>>,
    ready: &Arc<Notify>,
    message: &Arc<Notify>,
    http: &Arc<dyn HttpClient>,
    state_base_url: &str,
    bootstrap_task: &Arc<tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>>,
) {
    let msg: Value = match serde_json::from_str(raw) {
        Ok(v) => v,
        Err(e) => {
            tracing::error!("HA decode: {e}");
            return;
        }
    };
    let mt = msg.get("type").and_then(|v| v.as_str()).unwrap_or("");
    tracing::debug!("HA recv: {mt} ({} bytes)", raw.len());
    match mt {
        "auth_required" => {
            tracing::info!("HA: auth_required received, sending token");
            let send_result = conn
                .send(WsMessage::Text(
                    json!({"type": "auth", "access_token": access_token}).to_string(),
                ))
                .await;
            if let Err(e) = send_result {
                tracing::error!("HA: failed to send auth: {e}");
            }
        }
        "auth_ok" => {
            tracing::info!("Home Assistant: authenticated");
            let sub_id = {
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
                        "id": sub_id,
                        "type": "subscribe_entities",
                        "entity_ids": ents,
                    })
                    .to_string(),
                ))
                .await;
            // `subscribe_entities` is supposed to push an initial
            // snapshot, but in setups where the entity isn't loaded
            // yet at subscribe time, no initial event arrives and
            // `wait_for_message` blocks until timeout. Seed the cache
            // once via per-entity REST `/api/states/<entity_id>`
            // fetches (vs. WebSocket `get_states`, which would ship
            // every entity in HA). Matches Python PR #383.
            {
                let mut b = bootstrap_task.lock().await;
                if let Some(h) = b.take() {
                    h.abort();
                }
                let http = http.clone();
                let tracked = tracked.clone();
                let access_token = access_token.to_string();
                let state_base_url = state_base_url.to_string();
                let state = state.clone();
                let ready = ready.clone();
                let message = message.clone();
                *b = Some(tokio::spawn(async move {
                    fetch_initial_states(
                        &http,
                        &state_base_url,
                        &access_token,
                        &tracked,
                        &state,
                        &ready,
                        &message,
                    )
                    .await;
                }));
            }
        }
        "auth_invalid" => {
            tracing::error!(
                "HA auth failed: {}",
                msg.get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("(no message)")
            );
        }
        "result" => {
            let id = msg.get("id").and_then(|v| v.as_i64());
            let sub_id = state.lock().subscribe_id.map(|x| x as i64);
            if id.is_some() && id == sub_id {
                let success = msg
                    .get("success")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if !success {
                    let err = msg.get("error").cloned().unwrap_or(serde_json::Value::Null);
                    tracing::error!("HA subscribe_entities failed: {err}");
                }
            }
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

/// Per-entity REST bootstrap. `subscribe_entities` is supposed to
/// push an initial snapshot, but doesn't when an entity isn't loaded
/// yet at subscribe time. We GET each tracked entity's
/// `/api/states/<entity_id>` once to seed the cache.
///
/// Skips entities that the WS snapshot has already populated and
/// swallows any per-entity error (404, network, decode) so a single
/// missing entity doesn't break the whole bootstrap. Matches the
/// Python supervisor's `_fetch_initial_states` (PR #383).
async fn fetch_initial_states(
    http: &Arc<dyn HttpClient>,
    state_base_url: &str,
    access_token: &str,
    tracked: &HashSet<String>,
    state: &Arc<Mutex<State>>,
    ready: &Arc<Notify>,
    message: &Arc<Notify>,
) {
    let mut eids: Vec<&String> = tracked.iter().collect();
    eids.sort();
    let mut changed = false;
    for eid in eids {
        // Skip if the WS snapshot already populated this entity.
        if state
            .lock()
            .entity_values
            .get(eid)
            .and_then(|v| *v)
            .is_some()
        {
            continue;
        }
        let url = format!("{state_base_url}{eid}");
        let req = HttpRequest::get(url.clone())
            .with_header("Authorization", format!("Bearer {access_token}"));
        let resp = match http.request(req).await {
            Ok(r) => r,
            Err(e) => {
                tracing::debug!("HA REST state fetch for {eid} failed: {e}");
                continue;
            }
        };
        if resp.status != 200 {
            tracing::debug!(
                "HA REST state fetch for {eid} returned status {}",
                resp.status
            );
            continue;
        }
        let body: Value = match serde_json::from_slice(&resp.body) {
            Ok(v) => v,
            Err(e) => {
                tracing::debug!("HA REST state fetch for {eid} decode: {e}");
                continue;
            }
        };
        if let Some(st) = body.get("state") {
            update_value(state, eid, st);
            changed = true;
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
        http: platform.http.clone(),
        state: Arc::new(Mutex::new(State::default())),
        ready_notify: Arc::new(Notify::new()),
        message_notify: Arc::new(Notify::new()),
        cancel: tokio_util::sync::CancellationToken::new(),
        task: tokio::sync::Mutex::new(None),
        bootstrap_task: Arc::new(tokio::sync::Mutex::new(None)),
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
