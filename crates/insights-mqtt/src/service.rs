//! Service event loop. Owns the MQTT connection, drains an event queue
//! into discovery+state publishes, and routes Marstek poll responses.
//!
//! Faithful port of `src/astrameter/mqtt_insights/service.py` —
//! reduced to the publish loop, event types, and the Marstek bridge.
//! Command-side handling (HA discovery for control switches feeding back
//! into the supervisor) is part of the same module so the InsightsService
//! is end-to-end usable.
//!
//! Backed by the `astrameter_platform::mqtt::MqttClient` trait so this
//! crate doesn't depend on `rumqttc` directly — the host build uses
//! rumqttc via `platform-std`, the ESP32 build uses esp-idf-svc's
//! native MQTT client via `platform-espidf`.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use astrameter_core::{Error, Result};
use astrameter_platform::mqtt::{
    MqttClient, MqttEvent, MqttFactory, MqttOptions, MqttQos, MqttSession,
};
use futures::StreamExt;
use parking_lot::Mutex;
use serde_json::Value;

use crate::discovery;
use crate::marstek::{
    self, app_topics_for, build_cd4_response, build_response, parse_app_topic, parse_poll_payload,
    MarstekBinding, PollContext,
};

pub const RECONNECT_DELAY: Duration = Duration::from_secs(5);

#[derive(Debug, Clone)]
pub enum InsightsEvent {
    Ct002 {
        device_id: String,
        consumer_id: String,
        data: Value,
    },
    Ct002Remove {
        device_id: String,
        consumer_id: String,
    },
    Ct002DeviceStatus {
        device_id: String,
        data: Value,
    },
    Shelly {
        device_id: String,
        battery_ip: String,
        data: Value,
    },
    ShellyRemove {
        device_id: String,
        battery_ip: String,
    },
    ShellyDeviceStatus {
        device_id: String,
        data: Value,
    },
}

/// Function that maps a device id to its current per-phase watts.
pub type MeterWattsFn =
    Arc<dyn Fn(&str) -> futures::future::BoxFuture<'static, Result<Vec<f64>>> + Send + Sync>;

pub struct InsightsRuntime {
    pub config: super::MqttInsightsConfig,
    pub factory: Arc<dyn MqttFactory>,
    pub event_rx: tokio::sync::mpsc::Receiver<InsightsEvent>,
    pub marstek_bindings: Arc<Mutex<Vec<MarstekBinding>>>,
    pub command_handlers: Arc<Mutex<super::CommandHandlers>>,
    pub get_meter_watts: MeterWattsFn,
}

/// Captured for handlers after we destructure the runtime.
struct ServiceCtx {
    config: super::MqttInsightsConfig,
    marstek_bindings: Arc<Mutex<Vec<MarstekBinding>>>,
    command_handlers: Arc<Mutex<super::CommandHandlers>>,
    get_meter_watts: MeterWattsFn,
    /// device_ids whose last `get_powermeter_watts*` call failed. Used to
    /// rate-limit log spam — matches Python's
    /// `_marstek_get_values_failed: set[str]`.
    failed_meters: Arc<Mutex<HashSet<String>>>,
    /// Per-binding in-flight poll guard. Matches Python's
    /// `_marstek_tasks_by_binding` suppression.
    inflight: Arc<Mutex<HashSet<String>>>,
}

#[derive(Default)]
struct DiscoveryCache {
    ct002_consumers: HashSet<String>,
    ct002_devices: HashSet<String>,
    shelly_batteries: HashSet<String>,
    shelly_devices: HashSet<String>,
    /// CT002 consumer keys (`device_id::consumer_id`) for which the
    /// first-sight ARP lookup returned empty. We retry the lookup on every
    /// subsequent event until a MAC is found — matches Python's
    /// `_pending_arp` retry set.
    pending_arp: HashSet<String>,
}

pub async fn run(runtime: InsightsRuntime, cancel: tokio_util::sync::CancellationToken) {
    let InsightsRuntime {
        config,
        factory,
        mut event_rx,
        marstek_bindings,
        command_handlers,
        get_meter_watts,
    } = runtime;
    let ctx = ServiceCtx {
        config: config.clone(),
        marstek_bindings: marstek_bindings.clone(),
        command_handlers: command_handlers.clone(),
        get_meter_watts,
        failed_meters: Arc::new(Mutex::new(HashSet::new())),
        inflight: Arc::new(Mutex::new(HashSet::new())),
    };
    loop {
        let session = match factory.connect(MqttOptions {
            host: config.broker.clone(),
            port: config.port,
            client_id: "astrameter-insights".into(),
            username: config.username.clone(),
            password: config.password.clone(),
            tls: config.tls,
            keep_alive: Duration::from_secs(60),
            clean_session: true,
        }) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("insights MQTT connect error: {e}; retrying in 5s");
                if tokio::time::timeout(RECONNECT_DELAY, cancel.cancelled())
                    .await
                    .is_err()
                {
                    continue;
                } else {
                    return;
                }
            }
        };
        let MqttSession { client, mut events } = session;

        // Publish system online + subscribe to Marstek app polls + command sets.
        let status_topic = format!("{}/status", ctx.config.base_topic);
        let _ = client
            .publish(
                &status_topic,
                MqttQos::AtLeastOnce,
                true,
                b"online".to_vec(),
            )
            .await;
        let topics_to_subscribe: Vec<(String, String)> = {
            let bindings = ctx.marstek_bindings.lock();
            bindings.iter().map(app_topics_for).collect()
        };
        for (old_t, new_t) in &topics_to_subscribe {
            let _ = client.subscribe(old_t, MqttQos::AtLeastOnce).await;
            let _ = client.subscribe(new_t, MqttQos::AtLeastOnce).await;
        }
        // Subscribe to HA-discovery command topics so HA switches/numbers
        // and the force-rotation button can drive the emulator.
        let base = ctx.config.base_topic.clone();
        let _ = client
            .subscribe(
                &format!("{base}/ct002/+/consumer/+/set"),
                MqttQos::AtLeastOnce,
            )
            .await;
        let _ = client
            .subscribe(&format!("{base}/ct002/+/set"), MqttQos::AtLeastOnce)
            .await;

        let mut cache = DiscoveryCache::default();
        let drain_cancel = cancel.clone();

        // Periodic Marstek broadcast — matches Python's
        // `_marstek_broadcast_loop`. Spawned per-connection so it dies
        // automatically on disconnect.
        let broadcast_handle =
            if ctx.config.marstek_mqtt_enabled && ctx.config.marstek_mqtt_interval > 0.0 {
                let interval = Duration::from_secs_f64(ctx.config.marstek_mqtt_interval.max(0.1));
                let bcast_client = client.clone();
                let bcast_bindings = ctx.marstek_bindings.clone();
                let bcast_get = ctx.get_meter_watts.clone();
                let bcast_failed = ctx.failed_meters.clone();
                let bcast_inflight = ctx.inflight.clone();
                let bcast_cancel = drain_cancel.clone();
                Some(tokio::spawn(async move {
                    marstek_broadcast_loop(
                        bcast_client,
                        bcast_bindings,
                        bcast_get,
                        bcast_failed,
                        bcast_inflight,
                        interval,
                        bcast_cancel,
                    )
                    .await;
                }))
            } else {
                None
            };

        loop {
            tokio::select! {
                _ = drain_cancel.cancelled() => {
                    let _ = client
                        .publish(&status_topic, MqttQos::AtLeastOnce, true, b"offline".to_vec())
                        .await;
                    let _ = client.disconnect().await;
                    if let Some(h) = broadcast_handle.as_ref() { h.abort(); }
                    return;
                }
                event = event_rx.recv() => {
                    let Some(event) = event else {
                        return;
                    };
                    if let Err(e) = handle_event(&ctx, &*client, &mut cache, event).await {
                        tracing::warn!("insights event handling: {e}");
                    }
                }
                poll = events.next() => {
                    match poll {
                        Some(Ok(MqttEvent::Publish { topic, payload, retain: _ })) => {
                            handle_incoming(&ctx, &*client, &topic, &payload).await;
                        }
                        Some(Ok(MqttEvent::Other)) => {}
                        Some(Err(e)) => {
                            tracing::warn!("insights MQTT poll: {e}; reconnecting in 5s");
                            if let Some(h) = broadcast_handle.as_ref() { h.abort(); }
                            tokio::time::sleep(RECONNECT_DELAY).await;
                            break;
                        }
                        None => {
                            tracing::warn!("insights MQTT event stream closed; reconnecting in 5s");
                            if let Some(h) = broadcast_handle.as_ref() { h.abort(); }
                            tokio::time::sleep(RECONNECT_DELAY).await;
                            break;
                        }
                    }
                }
            }
        }
    }
}

async fn handle_event(
    ctx: &ServiceCtx,
    client: &dyn MqttClient,
    cache: &mut DiscoveryCache,
    event: InsightsEvent,
) -> Result<()> {
    let base = &ctx.config.base_topic;
    let ha_prefix = &ctx.config.ha_discovery_prefix;
    let ha = ctx.config.ha_discovery;
    let addon_slug = ctx.config.addon_slug.as_deref();
    match event {
        InsightsEvent::Ct002 {
            device_id,
            consumer_id,
            data,
        } => {
            if ha && !cache.ct002_devices.contains(&device_id) {
                let (topic, payload) = discovery::build_ct002_device_discovery(
                    base, &device_id, ha_prefix, addon_slug,
                );
                publish_json(client, &topic, &payload, true).await?;
                cache.ct002_devices.insert(device_id.clone());
            }
            let cache_key = format!("{device_id}::{consumer_id}");
            // ARP retry: republish consumer-discovery on every event for
            // entries whose first lookup returned empty until we resolve
            // a MAC. Matches Python `_pending_arp`.
            let need_first_discovery = !cache.ct002_consumers.contains(&cache_key);
            let need_arp_retry = cache.pending_arp.contains(&cache_key);
            if ha && (need_first_discovery || need_arp_retry) {
                let device_type = data
                    .get("device_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let battery_ip = data
                    .get("battery_ip")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let payload_mac = data
                    .get("network_mac")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let mac_via_arp = if payload_mac.is_empty() && !battery_ip.is_empty() {
                    arp_lookup(battery_ip).await
                } else {
                    String::new()
                };
                let network_mac = if !payload_mac.is_empty() {
                    payload_mac.as_str()
                } else {
                    mac_via_arp.as_str()
                };
                if need_first_discovery {
                    cache.ct002_consumers.insert(cache_key.clone());
                    if !battery_ip.is_empty() && network_mac.is_empty() {
                        cache.pending_arp.insert(cache_key.clone());
                    }
                }
                if !network_mac.is_empty() {
                    cache.pending_arp.remove(&cache_key);
                }
                if need_first_discovery || !network_mac.is_empty() {
                    let (topic, payload) = discovery::build_ct002_consumer_discovery(
                        base,
                        &device_id,
                        &consumer_id,
                        ha_prefix,
                        device_type,
                        network_mac,
                        battery_ip,
                    );
                    publish_json(client, &topic, &payload, true).await?;
                }
            }
            let state_topic = format!("{base}/ct002/{device_id}/consumer/{consumer_id}");
            let avail_topic = format!("{state_topic}/availability");
            tracing::info!("publishing CT002 state to {state_topic}");
            publish_json(client, &state_topic, &data, false).await?;
            let _ = client
                .publish(&avail_topic, MqttQos::AtLeastOnce, true, b"online".to_vec())
                .await;
        }
        InsightsEvent::Ct002Remove {
            device_id,
            consumer_id,
        } => {
            let state_topic = format!("{base}/ct002/{device_id}/consumer/{consumer_id}");
            let avail_topic = format!("{state_topic}/availability");
            let _ = client
                .publish(
                    &avail_topic,
                    MqttQos::AtLeastOnce,
                    true,
                    b"offline".to_vec(),
                )
                .await;
            let key = format!("{device_id}::{consumer_id}");
            cache.ct002_consumers.remove(&key);
            cache.pending_arp.remove(&key);
        }
        InsightsEvent::Ct002DeviceStatus { device_id, data } => {
            let state_topic = format!("{base}/ct002/{device_id}/status");
            publish_json(client, &state_topic, &data, false).await?;
        }
        InsightsEvent::Shelly {
            device_id,
            battery_ip,
            data,
        } => {
            if ha && !cache.shelly_devices.contains(&device_id) {
                let (topic, payload) = discovery::build_shelly_device_discovery(
                    base, &device_id, ha_prefix, addon_slug,
                );
                publish_json(client, &topic, &payload, true).await?;
                cache.shelly_devices.insert(device_id.clone());
            }
            let key = format!("{device_id}::{battery_ip}");
            if ha && !cache.shelly_batteries.contains(&key) {
                let (topic, payload) = discovery::build_shelly_battery_discovery(
                    base,
                    &device_id,
                    &battery_ip,
                    ha_prefix,
                );
                publish_json(client, &topic, &payload, true).await?;
                cache.shelly_batteries.insert(key);
            }
            let safe_ip = battery_ip.replace('.', "_");
            let state_topic = format!("{base}/shelly/{device_id}/battery/{safe_ip}");
            let avail_topic = format!("{state_topic}/availability");
            publish_json(client, &state_topic, &data, false).await?;
            let _ = client
                .publish(&avail_topic, MqttQos::AtLeastOnce, true, b"online".to_vec())
                .await;
        }
        InsightsEvent::ShellyRemove {
            device_id,
            battery_ip,
        } => {
            let safe_ip = battery_ip.replace('.', "_");
            let avail_topic = format!("{base}/shelly/{device_id}/battery/{safe_ip}/availability");
            let _ = client
                .publish(
                    &avail_topic,
                    MqttQos::AtLeastOnce,
                    true,
                    b"offline".to_vec(),
                )
                .await;
        }
        InsightsEvent::ShellyDeviceStatus { device_id, data } => {
            let state_topic = format!("{base}/shelly/{device_id}/status");
            publish_json(client, &state_topic, &data, false).await?;
        }
    }
    Ok(())
}

async fn handle_incoming(ctx: &ServiceCtx, client: &dyn MqttClient, topic: &str, payload: &[u8]) {
    // First check HA-discovery command topics.
    let base = ctx.config.base_topic.as_str();
    let trimmed_topic = topic.trim_end_matches('/');
    if let Some(rest) = trimmed_topic.strip_prefix(&format!("{base}/ct002/")) {
        if let Some(rest) = rest.strip_suffix("/set") {
            handle_ct002_command(ctx, rest, payload).await;
            return;
        }
    }
    // Marstek App poll?
    let Some((ct_type, mac)) = parse_app_topic(topic) else {
        return;
    };
    let binding_match = {
        let bindings = ctx.marstek_bindings.lock();
        bindings
            .iter()
            .find(|b| b.mac == mac && b.ct_type == ct_type)
            .cloned()
    };
    let Some(binding) = binding_match else {
        return;
    };
    let Some(poll) = parse_poll_payload(payload) else {
        return;
    };
    serve_marstek_poll(
        client,
        &binding,
        poll,
        &ctx.get_meter_watts,
        &ctx.failed_meters,
        &ctx.inflight,
    )
    .await;
}

async fn serve_marstek_poll(
    client: &dyn MqttClient,
    binding: &MarstekBinding,
    poll: PollContext,
    get_meter_watts: &MeterWattsFn,
    failed_meters: &Arc<Mutex<HashSet<String>>>,
    inflight: &Arc<Mutex<HashSet<String>>>,
) {
    // In-flight guard — skip if a previous poll for this binding is still
    // running. Matches Python `_marstek_tasks_by_binding` suppression.
    {
        let mut g = inflight.lock();
        if !g.insert(binding.device_id.clone()) {
            tracing::debug!(
                "Marstek MQTT: skipping poll for {} — prior handler still running",
                binding.device_id
            );
            return;
        }
    }
    let _guard = scopeguard_remove(inflight, binding.device_id.clone());

    let (old_dev_t, new_dev_t) = marstek::device_topics_for(binding);
    let device_id = binding.device_id.clone();
    let body = match poll.echo_cd {
        1 => {
            let watts = match get_meter_watts(&device_id).await {
                Ok(w) => {
                    let mut fm = failed_meters.lock();
                    if fm.remove(&device_id) {
                        tracing::info!("Marstek MQTT: poll value fetch recovered for {device_id}");
                    }
                    w
                }
                Err(e) => {
                    let first_failure = failed_meters.lock().insert(device_id.clone());
                    if first_failure {
                        tracing::warn!(
                            "Marstek MQTT: poll value fetch failed for {device_id} ({e}); \
                             suppressing further failures until recovery"
                        );
                    }
                    return;
                }
            };
            let connected = binding
                .get_connected_slave_count
                .as_ref()
                .map(|f| f())
                .unwrap_or(0);
            build_response(binding, &watts, Some(poll), connected, None)
        }
        4 => {
            let csv = binding
                .get_cd4_slave_csv
                .as_ref()
                .map(|f| f())
                .unwrap_or_default();
            build_cd4_response(&csv)
        }
        _ => return,
    };
    // Publish to BOTH legacy `hame_energy/...` and new `marstek_energy/...`
    // device topics so old and new app builds both see replies.
    let _ = client
        .publish(&old_dev_t, MqttQos::AtMostOnce, false, body.clone())
        .await;
    let _ = client
        .publish(&new_dev_t, MqttQos::AtMostOnce, false, body)
        .await;
}

/// RAII-style helper: remove `key` from `set` on drop.
fn scopeguard_remove(set: &Arc<Mutex<HashSet<String>>>, key: String) -> impl Drop + '_ {
    struct Guard<'a> {
        set: &'a Arc<Mutex<HashSet<String>>>,
        key: String,
    }
    impl Drop for Guard<'_> {
        fn drop(&mut self) {
            self.set.lock().remove(&self.key);
        }
    }
    Guard { set, key }
}

/// Periodically issue a synthetic `cd=1` poll for every binding so the
/// Marstek app sees up-to-date power without relying on its own polls.
#[allow(clippy::too_many_arguments)]
async fn marstek_broadcast_loop(
    client: Arc<dyn MqttClient>,
    bindings: Arc<Mutex<Vec<MarstekBinding>>>,
    get_meter_watts: MeterWattsFn,
    failed_meters: Arc<Mutex<HashSet<String>>>,
    inflight: Arc<Mutex<HashSet<String>>>,
    interval: Duration,
    cancel: tokio_util::sync::CancellationToken,
) {
    loop {
        let snapshot: Vec<MarstekBinding> = bindings.lock().clone();
        for binding in &snapshot {
            serve_marstek_poll(
                &*client,
                binding,
                PollContext {
                    echo_cd: 1,
                    slave_id: None,
                },
                &get_meter_watts,
                &failed_meters,
                &inflight,
            )
            .await;
        }
        if tokio::time::timeout(interval, cancel.cancelled())
            .await
            .is_ok()
        {
            return;
        }
    }
}

/// Handle a CT002 command MQTT message.
/// `rest` is everything after `<base>/ct002/` and before `/set`:
///   * `<device_id>` — device-level (force_rotation)
///   * `<device_id>/consumer/<consumer_id>` — consumer-level (active /
///     auto_target / manual_target)
async fn handle_ct002_command(ctx: &ServiceCtx, rest: &str, payload: &[u8]) {
    let parts: Vec<&str> = rest.split('/').collect();
    let body: Value = match serde_json::from_slice(payload) {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!("ct002 command: bad JSON {e}");
            return;
        }
    };
    let handlers = ctx.command_handlers.lock().clone();
    match parts.as_slice() {
        [device_id] => {
            if body
                .get("force_rotation")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
            {
                if let Some(cb) = &handlers.force_rotation {
                    cb(device_id);
                }
            }
        }
        [device_id, "consumer", consumer_id] => {
            if let Some(active) = body.get("active").and_then(|v| v.as_bool()) {
                if let Some(cb) = &handlers.set_active {
                    cb(device_id, consumer_id, active);
                }
            }
            if let Some(auto) = body.get("auto_target").and_then(|v| v.as_bool()) {
                if let Some(cb) = &handlers.set_auto_target {
                    cb(device_id, consumer_id, auto);
                }
            }
            if let Some(t) = body.get("manual_target").and_then(|v| v.as_f64()) {
                if let Some(cb) = &handlers.set_manual_target {
                    cb(device_id, consumer_id, t);
                }
            }
        }
        _ => tracing::debug!("ct002 command: unrecognised path {rest:?}"),
    }
}

/// Best-effort `/proc/net/arp` lookup. Returns the MAC in `AA:BB:...` format
/// or an empty string if not found / not Linux. Mirrors `_arp_lookup` from
/// the Python service.
async fn arp_lookup(ip: &str) -> String {
    let ip = ip.to_string();
    tokio::task::spawn_blocking(move || {
        let contents = match std::fs::read_to_string("/proc/net/arp") {
            Ok(s) => s,
            Err(_) => return String::new(),
        };
        for line in contents.lines().skip(1) {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 4 && parts[0] == ip && parts[3] != "00:00:00:00:00:00" {
                return parts[3].to_uppercase();
            }
        }
        String::new()
    })
    .await
    .unwrap_or_default()
}

async fn publish_json(
    client: &dyn MqttClient,
    topic: &str,
    payload: &Value,
    retain: bool,
) -> Result<()> {
    let body =
        serde_json::to_vec(payload).map_err(|e| Error::Other(format!("encode {topic}: {e}")))?;
    client
        .publish(topic, MqttQos::AtLeastOnce, retain, body)
        .await
        .map_err(|e| Error::transport(format!("publish {topic}: {e}")))
}
