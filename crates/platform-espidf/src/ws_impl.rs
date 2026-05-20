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
        let url_for_log = url.clone();
        log::info!(
            "ws: connecting to {url_for_log} (tls={is_tls}, verify_tls={}, sni_override={:?}, custom_ca={})",
            req.verify_tls,
            req.sni_override,
            pem_static.is_some()
        );
        let client = EspWebSocketClient::new(
            &url,
            &cfg,
            Duration::from_secs(10),
            move |evt: &Result<esp_idf_svc::ws::client::WebSocketEvent<'_>, _>| match evt {
                Ok(ev) => match &ev.event_type {
                    WebSocketEventType::BeforeConnect => {
                        log::debug!("ws[{url_for_log}]: BeforeConnect");
                    }
                    WebSocketEventType::Connected => {
                        log::info!("ws[{url_for_log}]: Connected");
                        connected_for_cb.store(true, Ordering::SeqCst);
                    }
                    WebSocketEventType::Text(s) => {
                        log::info!(
                            "ws[{url_for_log}]: recv Text ({} bytes): {}",
                            s.len(),
                            &s[..s.len().min(200)]
                        );
                        let _ = tx_for_cb.send(Ok(WsMessage::Text((*s).to_string())));
                    }
                    WebSocketEventType::Binary(b) => {
                        log::info!("ws[{url_for_log}]: recv Binary ({} bytes)", b.len());
                        let _ = tx_for_cb.send(Ok(WsMessage::Binary(b.to_vec())));
                    }
                    WebSocketEventType::Ping => {
                        let _ = tx_for_cb.send(Ok(WsMessage::Ping(Vec::new())));
                    }
                    WebSocketEventType::Pong => {
                        let _ = tx_for_cb.send(Ok(WsMessage::Pong(Vec::new())));
                    }
                    WebSocketEventType::Close(reason) => {
                        log::warn!("ws[{url_for_log}]: Close (reason={reason:?})");
                        connected_for_cb.store(false, Ordering::SeqCst);
                        let _ = tx_for_cb.send(Ok(WsMessage::Close));
                    }
                    WebSocketEventType::Closed => {
                        log::warn!("ws[{url_for_log}]: Closed by peer");
                        connected_for_cb.store(false, Ordering::SeqCst);
                        let _ = tx_for_cb.send(Ok(WsMessage::Close));
                    }
                    WebSocketEventType::Disconnected => {
                        log::warn!(
                            "ws[{url_for_log}]: Disconnected — check Wi-Fi reach, URL host:port, \
                             scheme (ws:// vs wss://), and that the peer is actually a WebSocket \
                             endpoint"
                        );
                        connected_for_cb.store(false, Ordering::SeqCst);
                        let _ = tx_for_cb.send(Err(WsError::Closed));
                    }
                },
                Err(e) => {
                    // Upstream esp-idf-svc 0.52 has two well-known cases
                    // that fire as "Err" here but are NOT real protocol
                    // errors — they'd kill the connection if forwarded:
                    //
                    //   * ESP_ERR_NOT_SUPPORTED (262): the close-reason
                    //     decoder in WebSocketClosingReason::new reads
                    //     the EVENT pointer value as if it were a u16
                    //     reason code, so ~every Close frame produces
                    //     a "reason code" outside 1000..=1015 and the
                    //     enum decoder returns NOT_SUPPORTED. The Close
                    //     itself still arrives as a normal `Close(...)` /
                    //     `Closed` event right after.
                    //   * ESP_ERR_INVALID_ARG (258): IDF 5.x fires extra
                    //     event IDs (BEGIN/FINISH) that esp-idf-svc 0.52
                    //     doesn't recognise yet. Informational, the
                    //     underlying state machine is fine.
                    //
                    // Log them at DEBUG so they show up if the user is
                    // actively troubleshooting, but don't push them into
                    // the mpsc — otherwise the HA powermeter (which
                    // reads from there) would treat every Close frame as
                    // a fatal `recv` error and skip the actual auth
                    // exchange.
                    use esp_idf_svc::sys::{ESP_ERR_INVALID_ARG, ESP_ERR_NOT_SUPPORTED};
                    let code = e.0.code();
                    if code == ESP_ERR_NOT_SUPPORTED || code == ESP_ERR_INVALID_ARG {
                        log::debug!("ws[{url_for_log}]: harmless upstream event error: {e:?}");
                    } else {
                        let msg = format!("{e:?}");
                        log::error!("ws[{url_for_log}]: event error: {msg}");
                        let _ = tx_for_cb.send(Err(WsError::Protocol(msg)));
                    }
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
            client: Some(Arc::new(ParkingMutex::new(client))),
            rx,
            connected,
        }))
    }
}

struct EspWsConn {
    /// `Option` so our `Drop` can `take()` the inner Arc and run a
    /// graceful teardown that tolerates `ESP_FAIL` from
    /// `esp_websocket_client_close` (which fires whenever the peer
    /// closed first — esp-idf-svc 0.52.1's own `Drop` impl `unwrap()`s
    /// that and aborts the firmware).
    client: Option<Arc<ParkingMutex<EspWebSocketClient<'static>>>>,
    rx: tokio::sync::mpsc::UnboundedReceiver<Result<WsMessage, WsError>>,
    /// Mirrors the IDF Connected/Disconnected callbacks. `send` checks
    /// it before calling into the underlying client so we don't
    /// surface the cryptic "Websocket client is not connected" error
    /// during a transient reconnect.
    connected: Arc<AtomicBool>,
}

impl Drop for EspWsConn {
    fn drop(&mut self) {
        // Take the Arc out of the Option so the upstream Drop doesn't
        // run via the field destructor. If we hold the only ref we can
        // unwrap the Arc + Mutex and run a teardown that tolerates
        // ESP_FAIL — esp-idf-svc 0.52.1's `Drop for EspWebSocketClient`
        // calls `esp_websocket_client_close(handle, timeout).unwrap()`,
        // which panics whenever the peer closed first (IDF returns
        // ESP_FAIL with "Client was not started"). That panic aborts
        // the whole firmware, so we have to bypass it.
        //
        // Trade-off: we `mem::forget` the inner `EspWebSocketClient` to
        // skip its `Drop`, which leaks the boxed event callback
        // (~200 B per connection). That's acceptable for a process
        // that lives for days — the alternative is a hard abort on
        // every HA-initiated WS close. Remove this workaround when
        // upgrading past whatever esp-idf-svc version fixes the
        // unwrap upstream.
        use esp_idf_svc::handle::RawHandle;
        let Some(arc) = self.client.take() else {
            return;
        };
        match Arc::try_unwrap(arc) {
            Ok(mutex) => {
                let client = mutex.into_inner();
                let handle = client.handle();
                std::mem::forget(client);
                unsafe {
                    // `close` returns ESP_FAIL if the peer closed
                    // already; `destroy` returns ESP_FAIL if `close`
                    // didn't fully tear down the task. Both are
                    // expected during peer-initiated close — ignore
                    // them. The IDF still frees the client struct on
                    // `destroy` even when it reports failure.
                    let _ = esp_idf_svc::sys::esp_websocket_client_close(handle, 0);
                    let _ = esp_idf_svc::sys::esp_websocket_client_destroy(handle);
                }
            }
            Err(arc) => {
                // Other refs still alive (in-flight `send` typically).
                // We can't safely tear down here — drop our share and
                // hope the last holder is on a path that doesn't
                // trigger the upstream `Drop` panic. In our codebase
                // `send` clones the Arc only across one
                // `spawn_blocking`, which is bounded.
                log::warn!(
                    "ws_impl: EspWsConn dropped while other refs alive ({} extra) — upstream Drop will run and may panic on ESP_FAIL",
                    Arc::strong_count(&arc) - 1
                );
                drop(arc);
            }
        }
    }
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
        let Some(client) = self.client.clone() else {
            return Err(WsError::Closed);
        };
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
