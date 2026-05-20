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
        // Diagnostic: capture the caller's task handle + remaining
        // stack so we can pinpoint who's overflowing during MQTT init.
        // Stack overflow detection only knows the FreeRTOS task name
        // (which is "pthread" for every Rust thread), but the handle
        // is unique.
        let caller = unsafe { esp_idf_svc::sys::xTaskGetCurrentTaskHandle() };
        let high_water_words = unsafe { esp_idf_svc::sys::uxTaskGetStackHighWaterMark(caller) };
        log::info!(
            "mqtt: connecting to {url} (tls={}, user={:?}, caller_handle={caller:p}, caller_stack_free={} bytes)",
            opts.tls,
            opts.username,
            high_water_words * 4,
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

/// Run `EspAsyncMqttClient::new` on a dedicated FreeRTOS task whose
/// stack lives in **PSRAM**. The mbedTLS context init burns ~80–100 KB
/// of stack, which is too much for the tokio worker pthread (64 KB)
/// and also doesn't fit in internal SRAM once Wi-Fi + HA WebSocket are
/// up. IDF v5.2.3 has no API to put pthread stacks in PSRAM, but
/// `xTaskCreatePinnedToCoreWithCaps` (added in 5.1) accepts a heap-caps
/// mask and routes the stack allocation accordingly.
///
/// The caller blocks on `rx.recv()` until the task finishes; the task
/// `vTaskDelete`s itself after sending the result, releasing the PSRAM
/// stack back to the heap.
fn init_on_temp_thread(
    url: String,
    opts: MqttOptions,
) -> Result<(EspAsyncMqttClient, EspAsyncMqttConnection), MqttError> {
    use esp_idf_svc::sys;

    type InitResult = Result<(EspAsyncMqttClient, EspAsyncMqttConnection), String>;

    struct Args {
        opts: MqttOptions,
        url: String,
        tx: std::sync::mpsc::SyncSender<InitResult>,
    }

    extern "C" fn task_entry(arg: *mut std::ffi::c_void) {
        // SAFETY: `arg` is the `Box::into_raw` pointer the spawner
        // handed us; we take ownership and the box is dropped at end
        // of scope.
        let args: Box<Args> = unsafe { Box::from_raw(arg as *mut Args) };
        let Args { opts, url, tx } = *args;

        // Diagnostic: log our own handle so a stack-overflow report
        // can be cross-referenced.
        let self_handle = unsafe { sys::xTaskGetCurrentTaskHandle() };
        log::info!("mqtt-init task entered: handle={self_handle:p}");

        let result: InitResult = (|| {
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
                // the user has to configure a publicly-trusted cert at
                // the broker, or run plaintext on the LAN.
                cfg.crt_bundle_attach = Some(sys::esp_crt_bundle_attach);
            }
            EspAsyncMqttClient::new(&url, &cfg).map_err(|e| e.to_string())
        })();

        let _ = tx.send(result);
        // SAFETY: passing `NULL` to `vTaskDelete` deletes the calling
        // task. This is the last thing we do — the stack is reclaimed
        // by the IDF idle task once we return from it.
        unsafe { sys::vTaskDelete(std::ptr::null_mut()) };
    }

    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    let args = Box::new(Args { opts, url, tx });
    let arg_ptr = Box::into_raw(args) as *mut std::ffi::c_void;

    let mut handle: sys::TaskHandle_t = std::ptr::null_mut();
    let rc = unsafe {
        sys::xTaskCreatePinnedToCoreWithCaps(
            Some(task_entry),
            b"mqtt-init\0".as_ptr() as *const _,
            128 * 1024,
            arg_ptr,
            5,
            &mut handle,
            // `tskNO_AFFINITY` is `(BaseType_t)0x7FFFFFFF` in FreeRTOS;
            // bindgen mangles it inconsistently across IDF versions, so
            // use the literal directly.
            0x7FFF_FFFFi32,
            sys::MALLOC_CAP_SPIRAM | sys::MALLOC_CAP_8BIT,
        )
    };
    if rc != 1 {
        // pdPASS == 1; reclaim the box on failure so we don't leak.
        unsafe { drop(Box::from_raw(arg_ptr as *mut Args)) };
        return Err(MqttError::Connect(format!(
            "xTaskCreatePinnedToCoreWithCaps(mqtt-init): rc={rc}"
        )));
    }

    rx.recv()
        .map_err(|e| MqttError::Connect(format!("recv mqtt-init: {e}")))?
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
