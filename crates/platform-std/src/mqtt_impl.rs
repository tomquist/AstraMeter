//! Host MQTT impl backed by `rumqttc`. Wraps the rumqttc `AsyncClient` +
//! `EventLoop` pair behind the new `MqttClient` / `MqttEventStream`
//! abstraction so the service code is identical across host and ESP32.

use std::sync::Arc;

use astrameter_platform::mqtt::{
    MqttClient, MqttError, MqttEvent, MqttEventStream, MqttFactory, MqttOptions, MqttQos,
    MqttSession,
};
use async_trait::async_trait;
use futures::stream;
use parking_lot::Mutex;

pub struct RumqttcFactory;

impl MqttFactory for RumqttcFactory {
    fn connect(&self, opts: MqttOptions) -> Result<MqttSession, MqttError> {
        let mut mqttoptions = rumqttc::MqttOptions::new(opts.client_id, opts.host, opts.port);
        mqttoptions.set_keep_alive(opts.keep_alive);
        mqttoptions.set_clean_session(opts.clean_session);
        if let (Some(u), Some(p)) = (opts.username.as_ref(), opts.password.as_ref()) {
            mqttoptions.set_credentials(u, p);
        }
        if opts.tls {
            mqttoptions
                .set_transport(rumqttc::Transport::Tls(rumqttc::TlsConfiguration::default()));
        }
        let (client, eventloop) = rumqttc::AsyncClient::new(mqttoptions, 32);
        let client_arc: Arc<dyn MqttClient> = Arc::new(RumqttcClient {
            inner: client,
            disconnected: Mutex::new(false),
        });
        let events = build_event_stream(eventloop);
        Ok(MqttSession {
            client: client_arc,
            events,
        })
    }
}

struct RumqttcClient {
    inner: rumqttc::AsyncClient,
    disconnected: Mutex<bool>,
}

fn map_qos(q: MqttQos) -> rumqttc::QoS {
    match q {
        MqttQos::AtMostOnce => rumqttc::QoS::AtMostOnce,
        MqttQos::AtLeastOnce => rumqttc::QoS::AtLeastOnce,
        MqttQos::ExactlyOnce => rumqttc::QoS::ExactlyOnce,
    }
}

#[async_trait]
impl MqttClient for RumqttcClient {
    async fn publish(
        &self,
        topic: &str,
        qos: MqttQos,
        retain: bool,
        payload: Vec<u8>,
    ) -> Result<(), MqttError> {
        if *self.disconnected.lock() {
            return Err(MqttError::Closed);
        }
        self.inner
            .publish(topic, map_qos(qos), retain, payload)
            .await
            .map_err(|e| MqttError::Publish(e.to_string()))
    }

    async fn subscribe(&self, topic: &str, qos: MqttQos) -> Result<(), MqttError> {
        if *self.disconnected.lock() {
            return Err(MqttError::Closed);
        }
        self.inner
            .subscribe(topic, map_qos(qos))
            .await
            .map_err(|e| MqttError::Subscribe(e.to_string()))
    }

    async fn disconnect(&self) -> Result<(), MqttError> {
        *self.disconnected.lock() = true;
        // rumqttc has no graceful disconnect that's always available; best
        // effort: send a DISCONNECT packet, ignore failures.
        let _ = self.inner.disconnect().await;
        Ok(())
    }
}

/// Turn a rumqttc `EventLoop` into a `Stream<Item = Result<MqttEvent, _>>`.
///
/// We use [`stream::unfold`] so the loop is driven lazily by the consumer's
/// poll cadence. Once `EventLoop::poll` returns `Err`, the stream yields
/// that error and ends — the service is expected to reconnect via the
/// factory.
fn build_event_stream(eventloop: rumqttc::EventLoop) -> MqttEventStream {
    Box::pin(stream::unfold(Some(eventloop), |mut state| async move {
        let mut el = state.take()?;
        match el.poll().await {
            Ok(rumqttc::Event::Incoming(rumqttc::Packet::Publish(p))) => {
                let event = MqttEvent::Publish {
                    topic: p.topic,
                    payload: p.payload.to_vec(),
                    retain: p.retain,
                };
                Some((Ok(event), Some(el)))
            }
            Ok(_) => Some((Ok(MqttEvent::Other), Some(el))),
            // Drop the eventloop on error so the next poll terminates the
            // stream.
            Err(e) => Some((Err(MqttError::Connect(e.to_string())), None)),
        }
    }))
}
