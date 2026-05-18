//! MQTT factory trait, backed by `rumqttc` on both targets.

use std::time::Duration;

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

#[derive(Debug, Clone)]
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

/// Factory that hands out connected `rumqttc::AsyncClient` + `EventLoop`
/// pairs. We pass the rumqttc types directly because the API is small and
/// already cross-target.
pub trait MqttFactory: Send + Sync {
    fn connect(
        &self,
        opts: MqttOptions,
    ) -> Result<(rumqttc::AsyncClient, rumqttc::EventLoop), MqttError>;
}
