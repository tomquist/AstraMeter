//! WebSocket client — same `tokio-tungstenite` as host. The
//! `rustls-tls-webpki-roots` build of tokio-tungstenite compiles for
//! esp-idf because rustls + the `ring` provider both support the
//! xtensa-esp32s3-espidf target.

use astrameter_platform::ws::{WebSocketClient, WsConnection, WsError, WsMessage, WsRequest};
use async_trait::async_trait;
use futures::{SinkExt, StreamExt};
use tokio_tungstenite::{tungstenite::Message, MaybeTlsStream, WebSocketStream};

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

struct TungsteniteConn {
    stream: WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>,
}

#[async_trait]
impl WsConnection for TungsteniteConn {
    async fn send(&mut self, msg: WsMessage) -> Result<(), WsError> {
        let tm = match msg {
            WsMessage::Text(s) => Message::Text(s),
            WsMessage::Binary(b) => Message::Binary(b),
            WsMessage::Ping(b) => Message::Ping(b),
            WsMessage::Pong(b) => Message::Pong(b),
            WsMessage::Close => Message::Close(None),
        };
        self.stream
            .send(tm)
            .await
            .map_err(|e| WsError::Protocol(e.to_string()))
    }

    async fn recv(&mut self) -> Result<WsMessage, WsError> {
        loop {
            let msg = self
                .stream
                .next()
                .await
                .ok_or(WsError::Closed)?
                .map_err(|e| WsError::Protocol(e.to_string()))?;
            return Ok(match msg {
                Message::Text(s) => WsMessage::Text(s),
                Message::Binary(b) => WsMessage::Binary(b),
                Message::Ping(b) => WsMessage::Ping(b),
                Message::Pong(b) => WsMessage::Pong(b),
                Message::Close(_) => WsMessage::Close,
                Message::Frame(_) => continue,
            });
        }
    }

    async fn close(&mut self) -> Result<(), WsError> {
        self.stream
            .close(None)
            .await
            .map_err(|e| WsError::Protocol(e.to_string()))
    }
}

#[async_trait]
impl WebSocketClient for TungsteniteClient {
    async fn connect(&self, req: WsRequest) -> Result<Box<dyn WsConnection>, WsError> {
        // ESP32 build keeps it minimal — defaults TLS via webpki roots;
        // HomeWizard-style custom CA / SNI override is a TODO for the
        // ESP32 path (host has the full implementation).
        let _ = req.extra_root_cert_pem;
        let _ = req.sni_override;
        let _ = req.verify_tls;
        let _ = req.heartbeat_secs;
        let _ = req.headers;
        let (stream, _resp) = tokio_tungstenite::connect_async(&req.url)
            .await
            .map_err(|e| WsError::Connect(e.to_string()))?;
        Ok(Box::new(TungsteniteConn { stream }))
    }
}
