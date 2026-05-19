//! `MQTT` — port of `src/astrameter/powermeter/mqtt.py`.

use std::sync::Arc;
use std::time::Duration;

use astrameter_config::{parse_mqtt_uri, Section};
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    mqtt::{MqttEvent, MqttFactory, MqttOptions, MqttQos, MqttSession},
    Platform,
};
use async_trait::async_trait;
use futures::StreamExt;
use jsonpath_rust::JsonPathQuery;
use parking_lot::Mutex;
use serde_json::Value;
use tokio::sync::Notify;

pub struct MqttPowermeter {
    broker: String,
    port: u16,
    username: Option<String>,
    password: Option<String>,
    tls: bool,
    subscriptions: Vec<(String, Option<String>)>,
    topic_to_indices: std::collections::HashMap<String, Vec<usize>>,
    factory: Arc<dyn MqttFactory>,
    values: Arc<Mutex<Vec<Option<f64>>>>,
    message_notify: Arc<Notify>,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
    cancel: tokio_util::sync::CancellationToken,
}

#[async_trait]
impl Powermeter for MqttPowermeter {
    async fn start(&self) -> Result<()> {
        let mut task_guard = self.task.lock().await;
        if task_guard.is_some() {
            return Ok(());
        }
        let opts = MqttOptions {
            host: self.broker.clone(),
            port: self.port,
            client_id: format!("astrameter-mqtt-pm-{}", std::process::id()),
            username: self.username.clone(),
            password: self.password.clone(),
            tls: self.tls,
            keep_alive: Duration::from_secs(60),
            clean_session: true,
        };
        let MqttSession { client, mut events } = self
            .factory
            .connect(opts)
            .map_err(|e| Error::transport(format!("mqtt connect: {e}")))?;
        let unique_topics: Vec<String> = self.topic_to_indices.keys().cloned().collect();
        for t in &unique_topics {
            client
                .subscribe(t, MqttQos::AtMostOnce)
                .await
                .map_err(|e| Error::transport(format!("mqtt subscribe {t}: {e}")))?;
        }

        let values = self.values.clone();
        let topic_indices = self.topic_to_indices.clone();
        let subs = self.subscriptions.clone();
        let notify = self.message_notify.clone();
        let cancel = self.cancel.clone();
        let handle = tokio::spawn(async move {
            loop {
                let evt = tokio::select! {
                    _ = cancel.cancelled() => break,
                    e = events.next() => e,
                };
                let (topic_str, payload_bytes) = match evt {
                    Some(Ok(MqttEvent::Publish { topic, payload, .. })) => (topic, payload),
                    Some(Ok(MqttEvent::Other)) => continue,
                    Some(Err(e)) => {
                        tracing::warn!("MQTT poll error: {e}. Reconnecting in 5s");
                        tokio::time::sleep(Duration::from_secs(5)).await;
                        continue;
                    }
                    None => {
                        tracing::warn!("MQTT event stream closed; sleeping before retry");
                        tokio::time::sleep(Duration::from_secs(5)).await;
                        continue;
                    }
                };
                let Some(indices) = topic_indices.get(&topic_str) else {
                    continue;
                };
                let payload = String::from_utf8_lossy(&payload_bytes).to_string();
                let mut parsed: Option<Value> = None;
                for &i in indices {
                    let (_, jp) = &subs[i];
                    let v = match jp {
                        None => payload.trim().parse::<f64>().ok(),
                        Some(p) => {
                            if parsed.is_none() {
                                parsed = serde_json::from_str(&payload).ok();
                            }
                            parsed.as_ref().and_then(|j| {
                                j.clone().path(p).ok().and_then(|res| match &res {
                                    Value::Array(arr) => arr
                                        .first()
                                        .and_then(|v| crate::json_http::value_to_f64(v).ok()),
                                    other => crate::json_http::value_to_f64(other).ok(),
                                })
                            })
                        }
                    };
                    if let Some(f) = v {
                        let mut vals = values.lock();
                        if i < vals.len() {
                            vals[i] = Some(f);
                        }
                    }
                }
                notify.notify_waiters();
            }
        });
        *task_guard = Some(handle);
        Ok(())
    }

    async fn stop(&self) -> Result<()> {
        self.cancel.cancel();
        let mut task_guard = self.task.lock().await;
        if let Some(h) = task_guard.take() {
            let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
        }
        Ok(())
    }

    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let vals = self.values.lock();
        let mut out = Vec::with_capacity(vals.len());
        for v in vals.iter() {
            match v {
                Some(f) => out.push(*f),
                None => return Err(Error::NoValue),
            }
        }
        Ok(out)
    }

    async fn wait_for_message(&self, timeout: Duration) -> Result<()> {
        if self.values.lock().iter().all(|v| v.is_some()) {
            return Ok(());
        }
        // Loop until ALL configured topics have a value, mirroring Python
        // (mqtt.py:1210-1225). A single wakeup is not enough for multi-
        // topic configs because one publish only fills one slot.
        let notify = self.message_notify.clone();
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            let now = tokio::time::Instant::now();
            if now >= deadline {
                return Err(Error::Timeout {
                    millis: timeout.as_millis() as u64,
                });
            }
            let remaining = deadline - now;
            match tokio::time::timeout(remaining, notify.notified()).await {
                Ok(()) => {
                    if self.values.lock().iter().all(|v| v.is_some()) {
                        return Ok(());
                    }
                }
                Err(_) => {
                    return Err(Error::Timeout {
                        millis: timeout.as_millis() as u64,
                    });
                }
            }
        }
    }

    async fn wait_for_next_message(&self, timeout: Duration) -> Result<()> {
        let notify = self.message_notify.clone();
        match tokio::time::timeout(timeout, notify.notified()).await {
            Ok(()) => Ok(()),
            Err(_) => Err(Error::Timeout {
                millis: timeout.as_millis() as u64,
            }),
        }
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let (broker, port, username, password, tls) = match section.get_opt_string("URI") {
        Some(uri) => {
            let parts = parse_mqtt_uri(&uri)?;
            (
                parts.host,
                parts.port,
                parts.username,
                parts.password,
                parts.tls,
            )
        }
        None => (
            section.get_required("BROKER")?.to_string(),
            section.get_int("PORT", 1883)? as u16,
            section.get_opt_string("USERNAME"),
            section.get_opt_string("PASSWORD"),
            section.get_bool("TLS", false)?,
        ),
    };

    let topics_raw = section
        .get_opt_string("TOPICS")
        .unwrap_or_else(|| section.get_string("TOPIC", ""));
    let mut topics: Vec<String> = topics_raw
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect();
    if topics.is_empty() {
        return Err(Error::config(format!(
            "section [{}] requires TOPIC or TOPICS",
            section.name()
        )));
    }

    let paths_raw = section
        .get_opt_string("JSON_PATHS")
        .or_else(|| section.get_opt_string("JSON_PATH"));
    let mut paths: Vec<Option<String>> = match paths_raw {
        None => vec![None; topics.len()],
        Some(s) => {
            let ps: Vec<Option<String>> = s
                .split(',')
                .map(|x| x.trim())
                .filter(|x| !x.is_empty())
                .map(|x| Some(x.to_string()))
                .collect();
            if ps.is_empty() {
                vec![None; topics.len()]
            } else {
                ps
            }
        }
    };

    if topics.len() == 1 && paths.len() > 1 {
        topics = vec![topics[0].clone(); paths.len()];
    } else if topics.len() > 1 && paths.len() == 1 {
        paths = vec![paths[0].clone(); topics.len()];
    }
    if topics.len() != paths.len() {
        return Err(Error::config(format!(
            "section [{}]: topic count ({}) and JSON path count ({}) must match",
            section.name(),
            topics.len(),
            paths.len()
        )));
    }

    let subscriptions: Vec<(String, Option<String>)> = topics.into_iter().zip(paths).collect();
    let mut topic_to_indices: std::collections::HashMap<String, Vec<usize>> =
        std::collections::HashMap::new();
    for (i, (t, _)) in subscriptions.iter().enumerate() {
        topic_to_indices.entry(t.clone()).or_default().push(i);
    }

    Ok(Arc::new(MqttPowermeter {
        broker,
        port,
        username,
        password,
        tls,
        values: Arc::new(Mutex::new(vec![None; subscriptions.len()])),
        subscriptions,
        topic_to_indices,
        factory: platform.mqtt.clone(),
        message_notify: Arc::new(Notify::new()),
        task: tokio::sync::Mutex::new(None),
        cancel: tokio_util::sync::CancellationToken::new(),
    }))
}
