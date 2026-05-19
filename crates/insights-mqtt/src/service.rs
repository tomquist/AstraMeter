//! Service event loop. Owns the MQTT connection, drains an event queue
//! into discovery+state publishes, and routes Marstek poll responses.
//!
//! Faithful port of `src/astrameter/mqtt_insights/service.py` —
//! reduced to the publish loop, event types, and the Marstek bridge.
//! Command-side handling (HA discovery for control switches feeding back
//! into the supervisor) is part of the same module so the InsightsService
//! is end-to-end usable.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use astrameter_core::{Error, Result};
use astrameter_platform::mqtt::{MqttFactory, MqttOptions};
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
}

#[derive(Default)]
struct DiscoveryCache {
    ct002_consumers: HashSet<String>,
    ct002_devices: HashSet<String>,
    shelly_batteries: HashSet<String>,
    shelly_devices: HashSet<String>,
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
    };
    loop {
        let connect_result = factory.connect(MqttOptions {
            host: config.broker.clone(),
            port: config.port,
            client_id: "astrameter-insights".into(),
            username: config.username.clone(),
            password: config.password.clone(),
            tls: config.tls,
            keep_alive: Duration::from_secs(60),
            clean_session: true,
        });
        let (client, mut eventloop) = match connect_result {
            Ok(c) => c,
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

        // Publish system online + subscribe to Marstek app polls + command sets.
        let status_topic = format!("{}/status", ctx.config.base_topic);
        let _ = client
            .publish(
                status_topic.clone(),
                rumqttc::QoS::AtLeastOnce,
                true,
                "online",
            )
            .await;
        let topics_to_subscribe: Vec<(String, String)> = {
            let bindings = ctx.marstek_bindings.lock();
            bindings.iter().map(app_topics_for).collect()
        };
        for (old_t, new_t) in &topics_to_subscribe {
            let _ = client.subscribe(old_t, rumqttc::QoS::AtLeastOnce).await;
            let _ = client.subscribe(new_t, rumqttc::QoS::AtLeastOnce).await;
        }
        // Subscribe to HA-discovery command topics so HA switches/numbers
        // and the force-rotation button can drive the emulator.
        let base = ctx.config.base_topic.clone();
        let _ = client
            .subscribe(
                format!("{base}/ct002/+/consumer/+/set"),
                rumqttc::QoS::AtLeastOnce,
            )
            .await;
        let _ = client
            .subscribe(format!("{base}/ct002/+/set"), rumqttc::QoS::AtLeastOnce)
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
                let bcast_cancel = drain_cancel.clone();
                Some(tokio::spawn(async move {
                    marstek_broadcast_loop(
                        bcast_client,
                        bcast_bindings,
                        bcast_get,
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
                        .publish(&status_topic, rumqttc::QoS::AtLeastOnce, true, "offline")
                        .await;
                    if let Some(h) = broadcast_handle.as_ref() { h.abort(); }
                    return;
                }
                event = event_rx.recv() => {
                    let Some(event) = event else {
                        return;
                    };
                    if let Err(e) = handle_event(&ctx, &client, &mut cache, event).await {
                        tracing::warn!("insights event handling: {e}");
                    }
                }
                poll = eventloop.poll() => {
                    match poll {
                        Ok(rumqttc::Event::Incoming(rumqttc::Packet::Publish(p))) => {
                            handle_incoming(&ctx, &client, &p.topic, &p.payload).await;
                        }
                        Ok(_) => {}
                        Err(e) => {
                            tracing::warn!("insights MQTT poll: {e}; reconnecting in 5s");
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
    client: &rumqttc::AsyncClient,
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
            if ha && !cache.ct002_consumers.contains(&cache_key) {
                let device_type = data
                    .get("device_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let battery_ip = data
                    .get("battery_ip")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                // Prefer payload-supplied MAC; fall back to a /proc/net/arp
                // lookup keyed on `battery_ip` so consumer entities surface
                // a connections=[mac]. This mirrors `_arp_lookup` in the
                // Python service.
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
                cache.ct002_consumers.insert(cache_key);
            }
            let state_topic = format!("{base}/ct002/{device_id}/consumer/{consumer_id}");
            let avail_topic = format!("{state_topic}/availability");
            publish_json(client, &state_topic, &data, false).await?;
            let _ = client
                .publish(&avail_topic, rumqttc::QoS::AtLeastOnce, true, "online")
                .await;
        }
        InsightsEvent::Ct002Remove {
            device_id,
            consumer_id,
        } => {
            let state_topic = format!("{base}/ct002/{device_id}/consumer/{consumer_id}");
            let avail_topic = format!("{state_topic}/availability");
            let _ = client
                .publish(&avail_topic, rumqttc::QoS::AtLeastOnce, true, "offline")
                .await;
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
                .publish(&avail_topic, rumqttc::QoS::AtLeastOnce, true, "online")
                .await;
        }
        InsightsEvent::ShellyRemove {
            device_id,
            battery_ip,
        } => {
            let safe_ip = battery_ip.replace('.', "_");
            let avail_topic = format!("{base}/shelly/{device_id}/battery/{safe_ip}/availability");
            let _ = client
                .publish(&avail_topic, rumqttc::QoS::AtLeastOnce, true, "offline")
                .await;
        }
        InsightsEvent::ShellyDeviceStatus { device_id, data } => {
            let state_topic = format!("{base}/shelly/{device_id}/status");
            publish_json(client, &state_topic, &data, false).await?;
        }
    }
    Ok(())
}

async fn handle_incoming(
    ctx: &ServiceCtx,
    client: &rumqttc::AsyncClient,
    topic: &str,
    payload: &[u8],
) {
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
    serve_marstek_poll(client, &binding, poll, &ctx.get_meter_watts).await;
}

async fn serve_marstek_poll(
    client: &rumqttc::AsyncClient,
    binding: &MarstekBinding,
    poll: PollContext,
    get_meter_watts: &MeterWattsFn,
) {
    let (old_dev_t, new_dev_t) = marstek::device_topics_for(binding);
    let device_id = binding.device_id.clone();
    let body = match poll.echo_cd {
        1 => {
            let watts = match get_meter_watts(&device_id).await {
                Ok(w) => w,
                Err(e) => {
                    tracing::warn!("Marstek poll: meter read for {device_id} failed: {e}");
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
        .publish(&old_dev_t, rumqttc::QoS::AtMostOnce, false, body.clone())
        .await;
    let _ = client
        .publish(&new_dev_t, rumqttc::QoS::AtMostOnce, false, body)
        .await;
}

/// Periodically issue a synthetic `cd=1` poll for every binding so the
/// Marstek app sees up-to-date power without relying on its own polls.
async fn marstek_broadcast_loop(
    client: rumqttc::AsyncClient,
    bindings: Arc<Mutex<Vec<MarstekBinding>>>,
    get_meter_watts: MeterWattsFn,
    interval: Duration,
    cancel: tokio_util::sync::CancellationToken,
) {
    loop {
        let snapshot: Vec<MarstekBinding> = bindings.lock().clone();
        for binding in &snapshot {
            serve_marstek_poll(
                &client,
                binding,
                PollContext {
                    echo_cd: 1,
                    slave_id: None,
                },
                &get_meter_watts,
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
    client: &rumqttc::AsyncClient,
    topic: &str,
    payload: &Value,
    retain: bool,
) -> Result<()> {
    let body =
        serde_json::to_vec(payload).map_err(|e| Error::Other(format!("encode {topic}: {e}")))?;
    client
        .publish(topic, rumqttc::QoS::AtLeastOnce, retain, body)
        .await
        .map_err(|e| Error::transport(format!("publish {topic}: {e}")))
}
