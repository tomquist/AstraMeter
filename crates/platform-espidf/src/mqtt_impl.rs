//! MQTT factory backed by `esp_idf_svc::mqtt::client::EspAsyncMqttClient`.
//!
//! Implements the platform `MqttFactory` / `MqttClient` traits using the
//! ESP-IDF native MQTT client (which uses mbedTLS for transport). This
//! avoids pulling `rumqttc` → `tokio-rustls` → `ring` into the firmware,
//! where ring has no working cross-compile path for
//! `xtensa-esp32s3-espidf`.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use astrameter_platform::mqtt::{
    MqttClient, MqttError, MqttEvent, MqttEventStream, MqttFactory, MqttOptions, MqttQos,
    MqttSession,
};
use async_trait::async_trait;
use esp_idf_svc::mqtt::client::{
    EspAsyncMqttClient, EspAsyncMqttConnection, EventPayload, MqttClientConfiguration, QoS,
};
use futures::stream;
use tokio::sync::Mutex;

pub struct EspMqttFactory;

impl MqttFactory for EspMqttFactory {
    fn connect(&self, opts: MqttOptions) -> Result<MqttSession, MqttError> {
        let scheme = if opts.tls { "mqtts" } else { "mqtt" };
        let url = format!("{scheme}://{}:{}", opts.host, opts.port);
        log::info!(
            "mqtt: connecting to {url} (tls={}, user={:?})",
            opts.tls,
            opts.username
        );

        // `EspAsyncMqttClient::new` calls into mbedTLS / esp_mqtt
        // initialisation, which burns ~80–100 KB of stack on its own.
        // The tokio worker pthread that drives us is only 64 KB so it
        // can't hold that on top of the runtime + active futures.
        // Run the constructor on a sacrificial pthread sized for the
        // peak; it exits as soon as the client is built, so the big
        // stack only exists briefly during connect.
        let (client, connection) = init_on_temp_thread(url.clone(), opts)?;

        // The IDF MQTT client connects asynchronously; the
        // `Connected`/`Disconnected` events arrive on the connection's
        // poll stream. The `connected` flag is flipped by
        // `build_event_stream` once the insights service starts driving
        // that stream. `EspClient::publish` / `subscribe` short-circuit
        // until then, so callers get a clean "broker disconnected"
        // error instead of the cryptic IDF "client is not connected"
        // string during the handshake window.
        let connected = Arc::new(AtomicBool::new(false));
        let events = build_event_stream(connection, connected.clone(), url);

        let arc_client: Arc<dyn MqttClient> = Arc::new(EspClient {
            inner: Arc::new(Mutex::new(client)),
            connected,
        });
        Ok(MqttSession {
            client: arc_client,
            events,
        })
    }
}

/// Run `EspAsyncMqttClient::new` on a dedicated pthread with a 128 KB
/// stack and `join` before returning — the `join` is what reclaims
/// the temp thread's stack, so the 128 KB only overlaps the caller's
/// stack for the duration of mbedTLS init.
fn init_on_temp_thread(
    url: String,
    opts: MqttOptions,
) -> Result<(EspAsyncMqttClient, EspAsyncMqttConnection), MqttError> {
    let handle = std::thread::Builder::new()
        .name("mqtt-init".into())
        .stack_size(128 * 1024)
        .spawn(move || -> Result<_, String> {
            let mut cfg = MqttClientConfiguration {
                client_id: Some(opts.client_id.as_str()),
                keep_alive_interval: Some(opts.keep_alive),
                ..Default::default()
            };
            if let Some(u) = opts.username.as_deref() {
                cfg.username = Some(u);
            }
            if let Some(p) = opts.password.as_deref() {
                cfg.password = Some(p);
            }
            if opts.tls {
                // Self-signed certs aren't supported on this transport —
                // the user has to configure a publicly-trusted cert at the
                // broker, or run plaintext on the LAN.
                cfg.crt_bundle_attach = Some(esp_idf_svc::sys::esp_crt_bundle_attach);
            }
            EspAsyncMqttClient::new(&url, &cfg).map_err(|e| e.to_string())
        })
        .map_err(|e| MqttError::Connect(format!("spawn mqtt-init thread: {e}")))?;
    handle
        .join()
        .map_err(|_| MqttError::Connect("mqtt-init thread panicked".into()))?
        .map_err(|e| MqttError::Connect(format!("esp mqtt new: {e}")))
}

struct EspClient {
    /// `EspAsyncMqttClient`'s async methods take `&mut self`, so we
    /// guard it with a `tokio::sync::Mutex` (safe to hold across
    /// `.await`s, unlike a `parking_lot::Mutex`).
    inner: Arc<Mutex<EspAsyncMqttClient>>,
    /// Mirrors the IDF Connected/Disconnected events. `publish` /
    /// `subscribe` short-circuit-error when we're not connected so a
    /// caller doesn't see the cryptic IDF "not connected" message in
    /// the middle of a reconnect cycle.
    connected: Arc<AtomicBool>,
}

fn map_qos(q: MqttQos) -> QoS {
    match q {
        MqttQos::AtMostOnce => QoS::AtMostOnce,
        MqttQos::AtLeastOnce => QoS::AtLeastOnce,
        MqttQos::ExactlyOnce => QoS::ExactlyOnce,
    }
}

#[async_trait]
impl MqttClient for EspClient {
    async fn publish(
        &self,
        topic: &str,
        qos: MqttQos,
        retain: bool,
        payload: Vec<u8>,
    ) -> Result<(), MqttError> {
        if !self.connected.load(Ordering::SeqCst) {
            return Err(MqttError::Publish("broker disconnected".into()));
        }
        let mut guard = self.inner.lock().await;
        guard
            .publish(topic, map_qos(qos), retain, &payload)
            .await
            .map(|_| ())
            .map_err(|e| MqttError::Publish(e.to_string()))
    }

    async fn subscribe(&self, topic: &str, qos: MqttQos) -> Result<(), MqttError> {
        if !self.connected.load(Ordering::SeqCst) {
            return Err(MqttError::Subscribe("broker disconnected".into()));
        }
        let mut guard = self.inner.lock().await;
        guard
            .subscribe(topic, map_qos(qos))
            .await
            .map(|_| ())
            .map_err(|e| MqttError::Subscribe(e.to_string()))
    }

    async fn disconnect(&self) -> Result<(), MqttError> {
        // EspAsyncMqttClient drops its underlying handle on Drop; no
        // explicit disconnect needed.
        Ok(())
    }
}

/// Convert the esp-idf-svc connection into a `Stream` of platform events
/// and side-effect the shared `connected` flag on every Connected /
/// Disconnected event so `publish` / `subscribe` can short-circuit.
fn build_event_stream(
    connection: EspAsyncMqttConnection,
    connected: Arc<AtomicBool>,
    url: String,
) -> MqttEventStream {
    Box::pin(stream::unfold(
        (Some(connection), connected, url),
        |(state, connected, url)| async move {
            let mut conn = state?;
            match conn.next().await {
                Ok(msg) => {
                    let event = match msg.payload() {
                        EventPayload::Connected(session_present) => {
                            log::info!(
                                "mqtt[{url}]: Connected (session_present={session_present})"
                            );
                            connected.store(true, Ordering::SeqCst);
                            MqttEvent::Other
                        }
                        EventPayload::Received { topic, data, .. } => MqttEvent::Publish {
                            topic: topic.unwrap_or_default().to_string(),
                            payload: data.to_vec(),
                            // The IDF MQTT client doesn't expose the retain
                            // flag on incoming messages — set it to false.
                            // Subscribers in this codebase don't branch on it.
                            retain: false,
                        },
                        EventPayload::Disconnected => {
                            log::warn!(
                                "mqtt[{url}]: Disconnected — check broker reach, port, \
                                 username/password, and that the broker accepts plaintext / TLS \
                                 as you configured"
                            );
                            connected.store(false, Ordering::SeqCst);
                            // Surface disconnect as a terminating error so
                            // the service reconnects via the factory.
                            return Some((
                                Err(MqttError::Connect("disconnected".into())),
                                (None, connected, url),
                            ));
                        }
                        EventPayload::Error(e) => {
                            log::error!("mqtt[{url}]: Error event: {e:?}");
                            MqttEvent::Other
                        }
                        EventPayload::BeforeConnect => {
                            log::debug!("mqtt[{url}]: BeforeConnect");
                            MqttEvent::Other
                        }
                        _ => MqttEvent::Other,
                    };
                    Some((Ok(event), (Some(conn), connected, url)))
                }
                Err(e) => {
                    log::error!("mqtt[{url}]: connection poll error: {e}");
                    connected.store(false, Ordering::SeqCst);
                    Some((
                        Err(MqttError::Connect(e.to_string())),
                        (None, connected, url),
                    ))
                }
            }
        },
    ))
}
