//! MQTT Insights — publishes telemetry to an MQTT broker and emits Home
//! Assistant Device Discovery messages. Includes the Marstek MQTT bridge
//! that answers `App/ctrl` polls so the Marstek mobile app sees the
//! emulator alongside real devices.

#![forbid(unsafe_code)]

use std::sync::Arc;
use std::time::Duration;

use astrameter_core::Result;
use astrameter_platform::Platform;
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};

pub mod discovery;
pub mod marstek;
pub mod service;

pub use marstek::MarstekBinding;
pub use service::{InsightsEvent, InsightsRuntime};

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

/// Convenience holder. The supervisor instantiates it once with the
/// platform handle, then `start()` to wire up the MQTT loop.
/// `(device_id, consumer_id, value) -> ()` callback type.
pub type ConsumerBoolFn = Arc<dyn Fn(&str, &str, bool) + Send + Sync>;
pub type ConsumerFloatFn = Arc<dyn Fn(&str, &str, f64) + Send + Sync>;
pub type DeviceFn = Arc<dyn Fn(&str) + Send + Sync>;

/// Callbacks the supervisor registers so HA control entities can drive
/// emulator state. All are optional; missing handlers are silently
/// ignored when the matching MQTT command arrives.
#[derive(Default, Clone)]
pub struct CommandHandlers {
    pub set_active: Option<ConsumerBoolFn>,
    pub set_manual_target: Option<ConsumerFloatFn>,
    pub set_auto_target: Option<ConsumerBoolFn>,
    pub force_rotation: Option<DeviceFn>,
}

pub struct InsightsService {
    cfg: MqttInsightsConfig,
    platform: Arc<Platform>,
    bindings: Arc<Mutex<Vec<MarstekBinding>>>,
    handlers: Arc<Mutex<CommandHandlers>>,
    event_tx: tokio::sync::mpsc::Sender<InsightsEvent>,
    event_rx: tokio::sync::Mutex<Option<tokio::sync::mpsc::Receiver<InsightsEvent>>>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
}

impl InsightsService {
    pub fn new(cfg: MqttInsightsConfig, platform: Arc<Platform>) -> Self {
        // 1024 slots × ~150 B per InsightsEvent ≈ 150 KiB ceiling.
        // 256 was getting drained too slowly under MQTT-broker
        // pressure (consumer discovery 6.8 KiB publish + WS Drop's
        // 10 s teardown stack up over hours) — see the
        // `dropped Ct002 event for ...: no available capacity`
        // warnings reported by the user.
        let (tx, rx) = tokio::sync::mpsc::channel(1024);
        Self {
            cfg,
            platform,
            bindings: Arc::new(Mutex::new(Vec::new())),
            handlers: Arc::new(Mutex::new(CommandHandlers::default())),
            event_tx: tx,
            event_rx: tokio::sync::Mutex::new(Some(rx)),
            cancel: tokio_util::sync::CancellationToken::new(),
            task: tokio::sync::Mutex::new(None),
        }
    }

    pub fn set_command_handlers(&self, handlers: CommandHandlers) {
        *self.handlers.lock() = handlers;
    }

    pub fn event_sender(&self) -> tokio::sync::mpsc::Sender<InsightsEvent> {
        self.event_tx.clone()
    }

    pub fn add_marstek_binding(&self, binding: MarstekBinding) {
        self.bindings.lock().push(binding);
    }

    pub async fn start<F>(&self, get_meter_watts: F) -> Result<()>
    where
        F: Fn(&str) -> futures::future::BoxFuture<'static, Result<Vec<f64>>>
            + Send
            + Sync
            + 'static,
    {
        use crate::service::MeterWattsFn;
        let get_meter_watts: MeterWattsFn = Arc::new(get_meter_watts);
        let mut g = self.task.lock().await;
        if g.is_some() {
            return Ok(());
        }
        let rx = self.event_rx.lock().await.take().expect("rx taken twice");
        let runtime = InsightsRuntime {
            config: self.cfg.clone(),
            factory: self.platform.mqtt.clone(),
            event_rx: rx,
            marstek_bindings: self.bindings.clone(),
            command_handlers: self.handlers.clone(),
            get_meter_watts,
        };
        let cancel = self.cancel.clone();
        let h = tokio::spawn(async move {
            service::run(runtime, cancel).await;
        });
        *g = Some(h);
        Ok(())
    }

    pub async fn stop(&self) {
        self.cancel.cancel();
        let mut g = self.task.lock().await;
        if let Some(h) = g.take() {
            let _ = tokio::time::timeout(Duration::from_secs(3), h).await;
        }
    }
}
