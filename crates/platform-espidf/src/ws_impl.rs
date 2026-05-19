//! WebSocket client stub for ESP32.
//!
//! The host build uses `tokio-tungstenite + rustls + ring`, but ring
//! doesn't cross-compile to `xtensa-esp32s3-espidf` (its build.rs
//! defaults to big-endian asm for that target). Until we route this
//! through `esp_websocket_client_*` (which uses the ESP-IDF bundled
//! mbedTLS for TLS), the espidf build returns an explicit error for
//! anyone who configures a powermeter that needs WebSocket transport
//! (currently `homeassistant` and `homewizard`). Pick a different
//! powermeter type, or move those meters to the host.

use astrameter_platform::ws::{WebSocketClient, WsConnection, WsError, WsRequest};
use async_trait::async_trait;

pub struct TungsteniteClient;

impl Default for TungsteniteClient {
    fn default() -> Self {
        Self::new()
    }
}

impl TungsteniteClient {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl WebSocketClient for TungsteniteClient {
    async fn connect(&self, _req: WsRequest) -> Result<Box<dyn WsConnection>, WsError> {
        Err(WsError::Connect(
            "WebSocket TLS is not implemented on ESP32 yet (rustls/ring has no \
             xtensa cross-compile path). Use a non-WebSocket powermeter \
             (e.g. mqtt, modbus, sml, json_http) on this target."
                .to_string(),
        ))
    }
}
