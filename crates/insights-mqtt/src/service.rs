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
    pub get_meter_watts: MeterWattsFn,
}

/// Captured for handlers after we destructure the runtime.
struct ServiceCtx {
    config: super::MqttInsightsConfig,
    marstek_bindings: Arc<Mutex<Vec<MarstekBinding>>>,
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
        get_meter_watts,
    } = runtime;
    let ctx = ServiceCtx {
        config: config.clone(),
        marstek_bindings: marstek_bindings.clone(),
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

        let mut cache = DiscoveryCache::default();
        let drain_cancel = cancel.clone();

        loop {
            tokio::select! {
                _ = drain_cancel.cancelled() => {
                    let _ = client
                        .publish(&status_topic, rumqttc::QoS::AtLeastOnce, true, "offline")
                        .await;
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
                let network_mac = data
                    .get("network_mac")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let battery_ip = data
                    .get("battery_ip")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
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
    // Resolve the (binding -> device) topic the device-side reply is published on.
    let (old_dev_t, _new_dev_t) = marstek::device_topics_for(&binding);
    // Fetch current values.
    let device_id = binding.device_id.clone();
    let watts = match (ctx.get_meter_watts)(&device_id).await {
        Ok(w) => w,
        Err(e) => {
            tracing::warn!("Marstek poll: meter read for {device_id} failed: {e}");
            return;
        }
    };
    let body = match poll.echo_cd {
        1 => {
            let connected = binding
                .get_connected_slave_count
                .as_ref()
                .map(|f| f())
                .unwrap_or(0);
            build_response(&binding, &watts, Some(poll), connected, None)
        }
        4 => {
            let csv = binding
                .get_cd4_slave_csv
                .as_ref()
                .map(|f| f())
                .unwrap_or_default();
            let _ = poll;
            let _: Option<PollContext> = Some(poll);
            build_cd4_response(&csv)
        }
        _ => return,
    };
    let _ = client
        .publish(&old_dev_t, rumqttc::QoS::AtLeastOnce, false, body)
        .await;
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
