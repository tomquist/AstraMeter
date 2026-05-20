//! MQTT factory backed by `esp_idf_svc::mqtt::client::EspMqttClient`.
//!
//! Implements the platform `MqttFactory` / `MqttClient` traits using the
//! ESP-IDF native MQTT client (which uses mbedTLS for transport). This
//! avoids pulling `rumqttc` → `tokio-rustls` → `ring` into the firmware,
//! where ring has no working cross-compile path for
//! `xtensa-esp32s3-espidf`.
//!
//! We deliberately use the **sync** `EspMqttClient` rather than the
//! `EspAsyncMqttClient` wrapper. The async wrapper offloads publishes
//! to a dedicated FreeRTOS task whose stack is hard-coded to 4 KiB in
//! esp-idf-svc 0.52.1 (`wrap_with_caps`). The IDF's own
//! `esp_mqtt_client_publish` blocks (and burns several KiB of stack)
//! when transmitting payloads larger than the in-frame buffer; a
//! 6.8 KiB HA Discovery payload overflows the 4 KiB worker stack,
//! silently corrupts adjacent heap, and the worker task wedges on
//! whatever lock it last touched — leaving every subsequent
//! `publish().await` hung forever.
//!
//! The sync client lets us call `enqueue` directly, which only adds
//! the message to the IDF outbox (no socket write on our thread); the
//! IDF's internal MQTT task handles the network with its own
//! (configurable) stack. We funnel incoming events via the `new_cb`
//! callback into a tokio mpsc channel for the platform's
//! `MqttEventStream`.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use astrameter_platform::mqtt::{
    MqttClient, MqttError, MqttEvent, MqttEventStream, MqttFactory, MqttOptions, MqttQos,
    MqttSession,
};
use async_trait::async_trait;
use esp_idf_svc::mqtt::client::{EspMqttClient, EventPayload, MqttClientConfiguration, QoS};
use futures::stream;
use parking_lot::Mutex;

pub struct EspMqttFactory;

impl MqttFactory for EspMqttFactory {
    fn connect(&self, opts: MqttOptions) -> Result<MqttSession, MqttError> {
        let scheme = if opts.tls { "mqtts" } else { "mqtt" };
        let url = format!("{scheme}://{}:{}", opts.host, opts.port);
        let caller = unsafe { esp_idf_svc::sys::xTaskGetCurrentTaskHandle() };
        let high_water_words = unsafe { esp_idf_svc::sys::uxTaskGetStackHighWaterMark(caller) };
        log::info!(
            "mqtt: connecting to {url} (tls={}, user={:?}, caller_handle={caller:p}, caller_stack_free={} bytes)",
            opts.tls,
            opts.username,
            high_water_words * 4,
        );

        let connected = Arc::new(AtomicBool::new(false));
        let (event_tx, event_rx) = tokio::sync::mpsc::unbounded_channel::<OwnedMqttEvent>();

        // `EspMqttClient::new_cb` calls into mbedTLS / esp_mqtt
        // initialisation, which burns ~80–100 KB of stack on its own.
        // The tokio worker pthread that drives us is only 64 KB so it
        // can't hold that on top of the runtime + active futures.
        // Run the constructor on a sacrificial FreeRTOS task whose
        // stack lives in PSRAM (via `xTaskCreatePinnedToCoreWithCaps`);
        // it exits as soon as the client is built, so the big stack
        // only exists briefly during connect.
        let client =
            init_on_temp_thread(url.clone(), opts, connected.clone(), event_tx, url.clone())?;

        let events = build_event_stream(event_rx, url.clone());

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

/// Owned form of a single IDF MQTT event, copied out of the borrowed
/// `EspMqttEvent` before being shipped through the mpsc channel to the
/// platform event stream consumer.
enum OwnedMqttEvent {
    Connected { session_present: bool },
    Disconnected,
    Received { topic: String, data: Vec<u8> },
    Error(String),
    Other,
}

/// Run `EspMqttClient::new_cb` on a dedicated FreeRTOS task whose
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
    connected: Arc<AtomicBool>,
    event_tx: tokio::sync::mpsc::UnboundedSender<OwnedMqttEvent>,
    url_for_log: String,
) -> Result<EspMqttClient<'static>, MqttError> {
    use esp_idf_svc::sys;

    type InitResult = Result<EspMqttClient<'static>, String>;

    struct Args {
        opts: MqttOptions,
        url: String,
        url_for_log: String,
        connected: Arc<AtomicBool>,
        event_tx: tokio::sync::mpsc::UnboundedSender<OwnedMqttEvent>,
        tx: std::sync::mpsc::SyncSender<InitResult>,
    }

    extern "C" fn task_entry(arg: *mut std::ffi::c_void) {
        // SAFETY: `arg` is the `Box::into_raw` pointer the spawner
        // handed us; we take ownership and the box is dropped at end
        // of scope.
        let args: Box<Args> = unsafe { Box::from_raw(arg as *mut Args) };
        let Args {
            opts,
            url,
            url_for_log,
            connected,
            event_tx,
            tx,
        } = *args;

        let self_handle = unsafe { sys::xTaskGetCurrentTaskHandle() };
        log::info!("mqtt-init task entered: handle={self_handle:p}");

        let result: InitResult = (|| {
            let mut cfg = MqttClientConfiguration {
                client_id: Some(opts.client_id.as_str()),
                keep_alive_interval: Some(opts.keep_alive),
                // Bump both rx and tx buffers from the IDF default
                // 1024 to 16 KiB so HA Discovery payloads (~6.8 KiB
                // per CT002 consumer) fit in a single PUBLISH frame
                // without fragmentation. The buffers come out of the
                // default heap, which routes to PSRAM under our
                // `CONFIG_SPIRAM_USE_MALLOC` setup.
                buffer_size: 16 * 1024,
                out_buffer_size: 16 * 1024,
                ..Default::default()
            };
            if let Some(u) = opts.username.as_deref() {
                cfg.username = Some(u);
            }
            if let Some(p) = opts.password.as_deref() {
                cfg.password = Some(p);
            }
            if opts.tls {
                cfg.crt_bundle_attach = Some(sys::esp_crt_bundle_attach);
            }

            let cb_connected = connected.clone();
            let cb_event_tx = event_tx.clone();
            let cb_url = url_for_log.clone();
            EspMqttClient::new_cb(&url, &cfg, move |event| {
                handle_event(&event, &cb_connected, &cb_event_tx, &cb_url);
            })
            .map_err(|e| e.to_string())
        })();

        let _ = tx.send(result);
        unsafe { sys::vTaskDelete(std::ptr::null_mut()) };
    }

    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    let args = Box::new(Args {
        opts,
        url,
        url_for_log,
        connected,
        event_tx,
        tx,
    });
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
        unsafe { drop(Box::from_raw(arg_ptr as *mut Args)) };
        return Err(MqttError::Connect(format!(
            "xTaskCreatePinnedToCoreWithCaps(mqtt-init): rc={rc}"
        )));
    }

    rx.recv()
        .map_err(|e| MqttError::Connect(format!("recv mqtt-init: {e}")))?
        .map_err(|e| MqttError::Connect(format!("esp mqtt new: {e}")))
}

/// IDF MQTT event callback. Runs in the IDF MQTT task; needs to be
/// short and non-blocking. We copy the borrowed data into owned types
/// and ship via the mpsc channel for the async event stream consumer.
fn handle_event(
    event: &esp_idf_svc::mqtt::client::EspMqttEvent<'_>,
    connected: &Arc<AtomicBool>,
    tx: &tokio::sync::mpsc::UnboundedSender<OwnedMqttEvent>,
    url: &str,
) {
    let owned = match event.payload() {
        EventPayload::Connected(session_present) => {
            log::info!("mqtt[{url}]: Connected (session_present={session_present})");
            connected.store(true, Ordering::SeqCst);
            OwnedMqttEvent::Connected { session_present }
        }
        EventPayload::Disconnected => {
            log::warn!(
                "mqtt[{url}]: Disconnected — check broker reach, port, \
                 username/password, and that the broker accepts plaintext / TLS \
                 as you configured"
            );
            connected.store(false, Ordering::SeqCst);
            OwnedMqttEvent::Disconnected
        }
        EventPayload::Received { topic, data, .. } => {
            let topic_owned = topic.unwrap_or_default().to_string();
            log::info!(
                "mqtt[{url}]: received {} bytes on {topic_owned}",
                data.len()
            );
            OwnedMqttEvent::Received {
                topic: topic_owned,
                data: data.to_vec(),
            }
        }
        EventPayload::Error(e) => {
            log::error!("mqtt[{url}]: Error event: {e:?}");
            OwnedMqttEvent::Error(format!("{e:?}"))
        }
        EventPayload::BeforeConnect => {
            log::debug!("mqtt[{url}]: BeforeConnect");
            OwnedMqttEvent::Other
        }
        _ => OwnedMqttEvent::Other,
    };
    let _ = tx.send(owned);
}

struct EspClient {
    /// Sync MQTT client. `enqueue` is non-blocking (just adds the
    /// message to the IDF outbox; the IDF MQTT task handles the
    /// network), so a `parking_lot::Mutex` is safe — we never hold
    /// it across `.await`s.
    inner: Arc<Mutex<EspMqttClient<'static>>>,
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

impl EspClient {
    /// Wait briefly for the IDF callback to flip `connected` to true.
    /// `factory.connect()` returns before the broker handshake
    /// completes (the IDF connect is async + driven by the C
    /// callback), so the InsightsService's very first calls to
    /// `subscribe`/`publish` race against that — and the IDF rejects
    /// both with -1 ("client not connected") if it hasn't seen the
    /// CONNACK yet. Spin asynchronously here so callers see a clean
    /// success after the handshake instead of silent failures.
    async fn await_connected(&self, op: &'static str) -> Result<(), MqttError> {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !self.connected.load(Ordering::SeqCst) {
            if std::time::Instant::now() > deadline {
                return Err(match op {
                    "subscribe" => {
                        MqttError::Subscribe("broker did not signal Connected within 10s".into())
                    }
                    _ => MqttError::Publish("broker did not signal Connected within 10s".into()),
                });
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        Ok(())
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
        self.await_connected("publish").await?;
        let mut guard = self.inner.lock();
        guard
            .enqueue(topic, map_qos(qos), retain, &payload)
            .map(|_| ())
            .map_err(|e| MqttError::Publish(e.to_string()))
    }

    async fn subscribe(&self, topic: &str, qos: MqttQos) -> Result<(), MqttError> {
        self.await_connected("subscribe").await?;
        log::info!("mqtt: subscribing to {topic}");
        let mut guard = self.inner.lock();
        guard
            .subscribe(topic, map_qos(qos))
            .map(|_| ())
            .map_err(|e| MqttError::Subscribe(e.to_string()))
    }

    async fn disconnect(&self) -> Result<(), MqttError> {
        // EspMqttClient drops its underlying handle on Drop; no
        // explicit disconnect needed.
        Ok(())
    }
}

/// Drain the mpsc channel populated by the IDF MQTT callback and
/// convert each `OwnedMqttEvent` into the platform's `MqttEvent`
/// shape.
fn build_event_stream(
    rx: tokio::sync::mpsc::UnboundedReceiver<OwnedMqttEvent>,
    url: String,
) -> MqttEventStream {
    Box::pin(stream::unfold((rx, url), |(mut rx, url)| async move {
        let event = rx.recv().await?;
        let mapped = match event {
            OwnedMqttEvent::Connected { .. } => Ok(MqttEvent::Other),
            OwnedMqttEvent::Disconnected => {
                // Surface disconnect as a terminating error so the
                // service reconnects via the factory.
                Err(MqttError::Connect("disconnected".into()))
            }
            OwnedMqttEvent::Received { topic, data } => Ok(MqttEvent::Publish {
                topic,
                payload: data,
                // The IDF MQTT client doesn't expose the retain
                // flag on incoming messages — set it to false.
                // Subscribers in this codebase don't branch on it.
                retain: false,
            }),
            OwnedMqttEvent::Error(_) => Ok(MqttEvent::Other),
            OwnedMqttEvent::Other => Ok(MqttEvent::Other),
        };
        let terminate = matches!(mapped, Err(_));
        Some((mapped, (if terminate { drained_rx() } else { rx }, url)))
    }))
}

/// Helper: empty receiver to plug into the stream state when we're
/// terminating after a disconnect event.
fn drained_rx() -> tokio::sync::mpsc::UnboundedReceiver<OwnedMqttEvent> {
    let (_tx, rx) = tokio::sync::mpsc::unbounded_channel();
    rx
}
