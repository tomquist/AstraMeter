//! MQTT Insights — publishes telemetry to an MQTT broker and (optionally)
//! emits Home Assistant auto-discovery messages.
//!
//! **Status:** this is a structural skeleton. The Python implementation
//! publishes ~80 HA entities (per-battery state, control switches,
//! diagnostics) plus a Marstek-MQTT bridge. The Rust port currently
//! publishes the overall power total under a single topic and a single HA
//! sensor discovery message. The remaining entities are TODO and tracked
//! against the Python source `src/astrameter/mqtt_insights/`.

#![forbid(unsafe_code)]

use std::sync::Arc;
use std::time::Duration;

use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    mqtt::{MqttFactory, MqttOptions},
    Platform,
};
use serde::{Deserialize, Serialize};

pub mod discovery;
pub mod marstek;
pub mod service;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MqttInsightsConfig {
    pub broker: String,
    pub port: u16,
    pub username: Option<String>,
    pub password: Option<String>,
    pub tls: bool,
    pub base_topic: String,
    pub ha_discovery: bool,
    pub ha_discovery_prefix: String,
    pub addon_slug: Option<String>,
    pub marstek_mqtt_enabled: bool,
    pub marstek_mqtt_interval: f64,
}

impl Default for MqttInsightsConfig {
    fn default() -> Self {
        Self {
            broker: "localhost".into(),
            port: 1883,
            username: None,
            password: None,
            tls: false,
            base_topic: "astrameter".into(),
            ha_discovery: true,
            ha_discovery_prefix: "homeassistant".into(),
            addon_slug: None,
            marstek_mqtt_enabled: true,
            marstek_mqtt_interval: 300.0,
        }
    }
}

pub struct InsightsService {
    cfg: MqttInsightsConfig,
    meters: Vec<Arc<dyn Powermeter>>,
    platform: Arc<Platform>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
}

impl InsightsService {
    pub fn new(
        cfg: MqttInsightsConfig,
        meters: Vec<Arc<dyn Powermeter>>,
        platform: Arc<Platform>,
    ) -> Self {
        Self {
            cfg,
            meters,
            platform,
            cancel: tokio_util::sync::CancellationToken::new(),
            task: tokio::sync::Mutex::new(None),
        }
    }

    pub async fn start(&self) -> Result<()> {
        let g = self.task.lock().await;
        if g.is_some() {
            return Ok(());
        }
        drop(g);
        let mqtt_opts = MqttOptions {
            host: self.cfg.broker.clone(),
            port: self.cfg.port,
            client_id: "astrameter-insights".into(),
            username: self.cfg.username.clone(),
            password: self.cfg.password.clone(),
            tls: self.cfg.tls,
            keep_alive: Duration::from_secs(60),
            clean_session: true,
        };
        let factory: Arc<dyn MqttFactory> = self.platform.mqtt.clone();
        let (client, mut eventloop) = factory
            .connect(mqtt_opts)
            .map_err(|e| Error::transport(format!("insights mqtt: {e}")))?;

        let base = self.cfg.base_topic.clone();
        let ha_prefix = self.cfg.ha_discovery_prefix.clone();
        let ha_discovery = self.cfg.ha_discovery;

        // Publish HA discovery once for the total-power sensor.
        if ha_discovery {
            let disc_topic = format!("{ha_prefix}/sensor/astrameter_total/config");
            let payload = serde_json::json!({
                "name": "AstraMeter total power",
                "state_topic": format!("{base}/total_power"),
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "unique_id": "astrameter_total_power",
            });
            let _ = client
                .publish(
                    disc_topic,
                    rumqttc::QoS::AtLeastOnce,
                    true,
                    payload.to_string(),
                )
                .await;
        }

        let meters = self.meters.clone();
        let cancel = self.cancel.clone();
        let task = tokio::spawn(async move {
            // Driving the eventloop is required even when we don't subscribe.
            let cancel_drive = cancel.clone();
            let _drive = tokio::spawn(async move {
                loop {
                    if cancel_drive.is_cancelled() {
                        break;
                    }
                    if eventloop.poll().await.is_err() {
                        tokio::time::sleep(Duration::from_secs(1)).await;
                    }
                }
            });
            let mut ticker = tokio::time::interval(Duration::from_secs(2));
            loop {
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    _ = ticker.tick() => {}
                }
                let mut total = 0.0;
                for m in &meters {
                    if let Ok(v) = m.get_powermeter_watts().await {
                        total += v.iter().sum::<f64>();
                    }
                }
                let topic = format!("{base}/total_power");
                let _ = client
                    .publish(
                        topic,
                        rumqttc::QoS::AtMostOnce,
                        false,
                        format!("{:.1}", total),
                    )
                    .await;
            }
        });
        *self.task.lock().await = Some(task);
        Ok(())
    }

    pub async fn stop(&self) {
        self.cancel.cancel();
        let mut g = self.task.lock().await;
        if let Some(h) = g.take() {
            let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
        }
    }
}
