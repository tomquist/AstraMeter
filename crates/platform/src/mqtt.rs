//! MQTT abstraction.
//!
//! Service crates depend on the [`MqttClient`] trait + [`MqttEvent`] stream
//! returned by [`MqttFactory::connect`]. The host build wires a rumqttc-backed
//! impl in `platform-std`; the ESP32 build wires `esp-idf-svc`'s native MQTT
//! client in `platform-espidf` so the firmware doesn't drag in the
//! `rustls`/`ring` stack that has no xtensa cross-compile path.

use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use futures::Stream;

#[derive(Debug, Clone)]
pub struct MqttOptions {
    pub host: String,
    pub port: u16,
    pub client_id: String,
    pub username: Option<String>,
    pub password: Option<String>,
    pub tls: bool,
    pub keep_alive: Duration,
    pub clean_session: bool,
}

impl MqttOptions {
    pub fn new(host: impl Into<String>, port: u16, client_id: impl Into<String>) -> Self {
        Self {
            host: host.into(),
            port,
            client_id: client_id.into(),
            username: None,
            password: None,
            tls: false,
            keep_alive: Duration::from_secs(60),
            clean_session: true,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MqttQos {
    AtMostOnce,
    AtLeastOnce,
    ExactlyOnce,
}

#[derive(Debug, thiserror::Error)]
pub enum MqttError {
    #[error("MQTT connection error: {0}")]
    Connect(String),
    #[error("MQTT publish error: {0}")]
    Publish(String),
    #[error("MQTT subscribe error: {0}")]
    Subscribe(String),
    #[error("MQTT closed")]
    Closed,
}

/// Inbound events the service loop reacts to. Currently just incoming
/// `Publish` packets; the loop treats anything else as a no-op or a
/// reconnect trigger (signalled by the stream yielding `Err(_)`).
#[derive(Debug, Clone)]
pub enum MqttEvent {
    Publish {
        topic: String,
        payload: Vec<u8>,
        retain: bool,
    },
    /// Heartbeat / PingResp / SubAck etc. — surfaced so the service loop
    /// can drive watchdogs without us enumerating every transport-level
    /// event type.
    Other,
}

/// Owned, send-able stream of MQTT events. `Err(_)` means the connection
/// dropped; the service is expected to reconnect via the factory.
pub type MqttEventStream =
    Pin<Box<dyn Stream<Item = Result<MqttEvent, MqttError>> + Send + 'static>>;

#[async_trait]
pub trait MqttClient: Send + Sync {
    async fn publish(
        &self,
        topic: &str,
        qos: MqttQos,
        retain: bool,
        payload: Vec<u8>,
    ) -> Result<(), MqttError>;

    async fn subscribe(&self, topic: &str, qos: MqttQos) -> Result<(), MqttError>;

    /// Best-effort clean shutdown. Implementations may no-op.
    async fn disconnect(&self) -> Result<(), MqttError>;
}

pub struct MqttSession {
    pub client: Arc<dyn MqttClient>,
    pub events: MqttEventStream,
}

/// Factory that opens a fresh MQTT session for the given options.
pub trait MqttFactory: Send + Sync {
    fn connect(&self, opts: MqttOptions) -> Result<MqttSession, MqttError>;
}
