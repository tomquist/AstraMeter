//! MQTT factory backed by `esp_idf_svc::mqtt::client::EspAsyncMqttClient`.
//!
//! Implements the platform `MqttFactory` / `MqttClient` traits using the
//! ESP-IDF native MQTT client (which uses mbedTLS for transport). This
//! avoids pulling `rumqttc` → `tokio-rustls` → `ring` into the firmware,
//! where ring has no working cross-compile path for
//! `xtensa-esp32s3-espidf`.

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
        let owned_user = opts.username.clone();
        let owned_pass = opts.password.clone();
        let mut cfg = MqttClientConfiguration {
            client_id: Some(opts.client_id.as_str()),
            keep_alive_interval: Some(opts.keep_alive),
            ..Default::default()
        };
        if let Some(u) = owned_user.as_deref() {
            cfg.username = Some(u);
        }
        if let Some(p) = owned_pass.as_deref() {
            cfg.password = Some(p);
        }
        if opts.tls {
            // Use the ESP-IDF bundled CA store. Self-signed certs aren't
            // supported on this transport — the user has to configure a
            // publicly-trusted cert at the broker, or run plaintext on
            // the LAN.
            cfg.crt_bundle_attach = Some(esp_idf_svc::sys::esp_crt_bundle_attach);
        }

        let (client, connection) = EspAsyncMqttClient::new(&url, &cfg)
            .map_err(|e| MqttError::Connect(format!("esp mqtt new: {e}")))?;
        let arc_client: Arc<dyn MqttClient> = Arc::new(EspClient {
            inner: Arc::new(Mutex::new(client)),
        });
        let events = build_event_stream(connection);
        Ok(MqttSession {
            client: arc_client,
            events,
        })
    }
}

struct EspClient {
    /// `EspAsyncMqttClient`'s async methods take `&mut self`, so we
    /// guard it with a `tokio::sync::Mutex` (safe to hold across
    /// `.await`s, unlike a `parking_lot::Mutex`).
    inner: Arc<Mutex<EspAsyncMqttClient>>,
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
        let mut guard = self.inner.lock().await;
        guard
            .publish(topic, map_qos(qos), retain, &payload)
            .await
            .map(|_| ())
            .map_err(|e| MqttError::Publish(e.to_string()))
    }

    async fn subscribe(&self, topic: &str, qos: MqttQos) -> Result<(), MqttError> {
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

/// Convert the esp-idf-svc connection into a `Stream` of platform events.
fn build_event_stream(connection: EspAsyncMqttConnection) -> MqttEventStream {
    Box::pin(stream::unfold(Some(connection), |state| async move {
        let mut conn = state?;
        match conn.next().await {
            Ok(msg) => {
                let event = match msg.payload() {
                    EventPayload::Received { topic, data, .. } => MqttEvent::Publish {
                        topic: topic.unwrap_or_default().to_string(),
                        payload: data.to_vec(),
                        // The IDF MQTT client doesn't expose the retain
                        // flag on incoming messages — set it to false.
                        // Subscribers in this codebase don't branch on it.
                        retain: false,
                    },
                    EventPayload::Disconnected => {
                        // Surface disconnect as a terminating error so
                        // the service reconnects via the factory.
                        return Some((Err(MqttError::Connect("disconnected".into())), None));
                    }
                    _ => MqttEvent::Other,
                };
                Some((Ok(event), Some(conn)))
            }
            Err(e) => Some((Err(MqttError::Connect(e.to_string())), None)),
        }
    }))
}
