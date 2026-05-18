//! WebSocket client trait. Both host and ESP32 use `tokio-tungstenite` with
//! `rustls`, behind this trait so callers don't pin the dependency directly.

use async_trait::async_trait;

#[derive(Debug, Clone)]
pub struct WsRequest {
    pub url: String,
    /// Additional headers (e.g. `Authorization`).
    pub headers: Vec<(String, String)>,
    /// Override TLS Server Name Indication (HomeWizard uses this for cert pinning).
    pub sni_override: Option<String>,
    pub verify_tls: bool,
    pub extra_root_cert_pem: Option<Vec<u8>>,
    /// If `Some`, the client sends ping frames at this interval.
    pub heartbeat_secs: Option<u32>,
}

impl WsRequest {
    pub fn new(url: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            headers: Vec::new(),
            sni_override: None,
            verify_tls: true,
            extra_root_cert_pem: None,
            heartbeat_secs: None,
        }
    }
}

#[derive(Debug, Clone)]
pub enum WsMessage {
    Text(String),
    Binary(Vec<u8>),
    Ping(Vec<u8>),
    Pong(Vec<u8>),
    Close,
}

#[derive(Debug, thiserror::Error)]
pub enum WsError {
    #[error("WebSocket connect error: {0}")]
    Connect(String),
    #[error("WebSocket protocol error: {0}")]
    Protocol(String),
    #[error("WebSocket closed")]
    Closed,
}

#[async_trait]
pub trait WsConnection: Send + Sync {
    async fn send(&mut self, msg: WsMessage) -> Result<(), WsError>;
    async fn recv(&mut self) -> Result<WsMessage, WsError>;
    async fn close(&mut self) -> Result<(), WsError>;
}

#[async_trait]
pub trait WebSocketClient: Send + Sync {
    async fn connect(&self, req: WsRequest) -> Result<Box<dyn WsConnection>, WsError>;
}
