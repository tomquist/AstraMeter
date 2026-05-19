//! WebSocket client backed by `esp_websocket_client` (via
//! `esp_idf_svc::ws::client::EspWebSocketClient`).
//!
//! The host build uses `tokio-tungstenite + rustls + ring`, but ring's
//! C/asm cross-compile doesn't work on `xtensa-esp32s3-espidf`. Use
//! ESP-IDF's own `esp_websocket_client` component instead — that
//! handles TLS via the IDF's bundled mbedTLS and is the same path the
//! IDF uses for any cloud-MQTT / WS connection.
//!
//! Lifetime / threading notes:
//!   * `EspWebSocketClient` is callback-driven and synchronous. We
//!     wrap it by spawning a `std::thread` that pumps
//!     `EspWebSocketConnection::next()` into a `tokio::sync::mpsc`
//!     so the rest of the firmware can `await` incoming frames.
//!   * The PEM bytes for the optional custom root CA have to outlive
//!     the C config struct passed to `esp_websocket_client_start`,
//!     so we `Box::leak` them to `'static`. One extra PEM per
//!     connection is acceptable for the usual `homewizard` / one-
//!     `homeassistant` configs.
//!   * `sni_override` (HomeWizard's `appliance/p1dongle/<serial>`):
//!     `esp_websocket_client_config_t` doesn't expose a true SNI
//!     override, but we can set `skip_cert_common_name_check = true`
//!     so the connection succeeds when the cert's CN/SAN doesn't
//!     match the IP the user connects to. The cert chain itself is
//!     still validated against the provided CA.

use astrameter_platform::ws::{WebSocketClient, WsConnection, WsError, WsMessage, WsRequest};
use async_trait::async_trait;
use embedded_svc::ws::FrameType;
use esp_idf_svc::tls::X509;
use esp_idf_svc::ws::client::{
    EspWebSocketClient, EspWebSocketClientConfig, EspWebSocketTransport, WebSocketEventType,
};
use parking_lot::Mutex as ParkingMutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

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
    async fn connect(&self, req: WsRequest) -> Result<Box<dyn WsConnection>, WsError> {
        // Convert headers to the `Key: Value\r\nKey: Value\r\n` blob the
        // esp_websocket_client_config_t::headers field expects.
        let headers_blob: Option<String> = if req.headers.is_empty() {
            None
        } else {
            let mut s = String::new();
            for (k, v) in &req.headers {
                s.push_str(k);
                s.push_str(": ");
                s.push_str(v);
                s.push_str("\r\n");
            }
            Some(s)
        };

        // Custom root CA PEM — needs '\0' terminator and must outlive
        // the C config + the spawned websocket task.
        let pem_static: Option<&'static [u8]> = req.extra_root_cert_pem.as_ref().map(|pem| {
            let mut owned = pem.clone();
            if owned.last() != Some(&0) {
                owned.push(0);
            }
            Box::leak(owned.into_boxed_slice()) as &'static [u8]
        });

        // We need owned strings to live in the closure for the duration
        // of the WS task; `EspWebSocketClientConfig` borrows from them.
        let url = req.url.clone();
        let headers_owned = headers_blob;
        let heartbeat = req.heartbeat_secs;
        let skip_cn = req.sni_override.is_some() || !req.verify_tls;

        // URL scheme picks transport. `wss://` → SSL, anything else →
        // plain TCP. Hard-coding SSL fails instantly on `ws://`
        // (HomeAssistant's default `ws://homeassistant.local:8123/api/websocket`).
        let scheme_lower = url.split("://").next().unwrap_or("").to_ascii_lowercase();
        let is_tls = matches!(scheme_lower.as_str(), "wss" | "https");
        let transport = if is_tls {
            EspWebSocketTransport::TransportOverSSL
        } else {
            EspWebSocketTransport::TransportOverTCP
        };
        let crt_bundle = if is_tls && req.verify_tls && pem_static.is_none() {
            Some(
                esp_idf_svc::sys::esp_crt_bundle_attach
                    as unsafe extern "C" fn(*mut std::ffi::c_void) -> i32,
            )
        } else {
            None
        };
        let server_cert: Option<X509<'static>> = if is_tls {
            pem_static.map(X509::pem_until_nul)
        } else {
            None
        };

        // Build the config + open the client + connection. Returns a
        // `'static` client + the event sink.
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<Result<WsMessage, WsError>>();
        let cfg = EspWebSocketClientConfig {
            transport,
            disable_auto_reconnect: false,
            use_global_ca_store: is_tls && req.verify_tls,
            skip_cert_common_name_check: is_tls && skip_cn,
            crt_bundle_attach: crt_bundle,
            server_cert,
            headers: headers_owned.as_deref(),
            ping_interval_sec: Duration::from_secs(heartbeat.unwrap_or(30) as u64),
            network_timeout_ms: Duration::from_secs(10),
            reconnect_timeout_ms: Duration::from_secs(5),
            buffer_size: 4096,
            ..Default::default()
        };

        // Wait for the IDF to signal CONNECTED before we hand the
        // wrapper back. Otherwise the first `send()` races the
        // handshake and esp_websocket_client returns
        // "Websocket client is not connected".
        let connected = Arc::new(AtomicBool::new(false));
        let connected_for_cb = connected.clone();
        let tx_for_cb = tx.clone();
        let client = EspWebSocketClient::new(
            &url,
            &cfg,
            Duration::from_secs(10),
            move |evt: &Result<esp_idf_svc::ws::client::WebSocketEvent<'_>, _>| match evt {
                Ok(ev) => match &ev.event_type {
                    WebSocketEventType::Connected => {
                        connected_for_cb.store(true, Ordering::SeqCst);
                    }
                    WebSocketEventType::BeforeConnect => {}
                    WebSocketEventType::Text(s) => {
                        let _ = tx_for_cb.send(Ok(WsMessage::Text((*s).to_string())));
                    }
                    WebSocketEventType::Binary(b) => {
                        let _ = tx_for_cb.send(Ok(WsMessage::Binary(b.to_vec())));
                    }
                    WebSocketEventType::Ping => {
                        let _ = tx_for_cb.send(Ok(WsMessage::Ping(Vec::new())));
                    }
                    WebSocketEventType::Pong => {
                        let _ = tx_for_cb.send(Ok(WsMessage::Pong(Vec::new())));
                    }
                    WebSocketEventType::Close(_) | WebSocketEventType::Closed => {
                        connected_for_cb.store(false, Ordering::SeqCst);
                        let _ = tx_for_cb.send(Ok(WsMessage::Close));
                    }
                    WebSocketEventType::Disconnected => {
                        connected_for_cb.store(false, Ordering::SeqCst);
                        let _ = tx_for_cb.send(Err(WsError::Closed));
                    }
                },
                Err(e) => {
                    let msg = format!("{e:?}");
                    let _ = tx_for_cb.send(Err(WsError::Protocol(msg)));
                }
            },
        )
        .map_err(|e| WsError::Connect(format!("EspWebSocketClient::new: {e}")))?;

        // Wait up to 10 s for the IDF to fire `Connected`. We block
        // the calling tokio task via `tokio::task::yield_now` polls so
        // the rest of the runtime keeps running.
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while !connected.load(Ordering::SeqCst) {
            if std::time::Instant::now() > deadline {
                return Err(WsError::Connect(format!(
                    "handshake did not complete within 10 s for {url}"
                )));
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }

        Ok(Box::new(EspWsConn {
            client: Arc::new(ParkingMutex::new(client)),
            rx,
            connected,
        }))
    }
}

struct EspWsConn {
    client: Arc<ParkingMutex<EspWebSocketClient<'static>>>,
    rx: tokio::sync::mpsc::UnboundedReceiver<Result<WsMessage, WsError>>,
    /// Mirrors the IDF Connected/Disconnected callbacks. `send` checks
    /// it before calling into the underlying client so we don't
    /// surface the cryptic "Websocket client is not connected" error
    /// during a transient reconnect.
    connected: Arc<AtomicBool>,
}

#[async_trait]
impl WsConnection for EspWsConn {
    async fn send(&mut self, msg: WsMessage) -> Result<(), WsError> {
        // Wait briefly for a transient reconnect to complete so a
        // caller calling `send` right after a `Closed`/`Disconnected`
        // event doesn't immediately see another `Closed`.
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while !self.connected.load(Ordering::SeqCst) {
            if std::time::Instant::now() > deadline {
                return Err(WsError::Closed);
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let client = self.client.clone();
        let send_result = tokio::task::spawn_blocking(move || -> Result<(), WsError> {
            let mut g = client.lock();
            match msg {
                WsMessage::Text(s) => g
                    .send(FrameType::Text(false), s.as_bytes())
                    .map_err(|e| WsError::Protocol(format!("send text: {e}"))),
                WsMessage::Binary(b) => g
                    .send(FrameType::Binary(false), &b)
                    .map_err(|e| WsError::Protocol(format!("send binary: {e}"))),
                WsMessage::Ping(b) => g
                    .send(FrameType::Ping, &b)
                    .map_err(|e| WsError::Protocol(format!("send ping: {e}"))),
                WsMessage::Pong(b) => g
                    .send(FrameType::Pong, &b)
                    .map_err(|e| WsError::Protocol(format!("send pong: {e}"))),
                WsMessage::Close => Ok(()),
            }
        })
        .await
        .map_err(|e| WsError::Protocol(format!("join: {e}")))?;
        send_result
    }

    async fn recv(&mut self) -> Result<WsMessage, WsError> {
        match self.rx.recv().await {
            Some(r) => r,
            None => Err(WsError::Closed),
        }
    }

    async fn close(&mut self) -> Result<(), WsError> {
        // `EspWebSocketClient` cleans up on Drop; we don't have an
        // explicit close that's reliable across versions, so the
        // graceful path is to let the wrapper go out of scope.
        Ok(())
    }
}
