//! `VZLOGGER` — port of `src/astrameter/powermeter/vzlogger.py`.

use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    http::{HttpClient, HttpRequest},
    Platform,
};
use async_trait::async_trait;
use serde_json::Value;

pub struct VZLogger {
    ip: String,
    port: String,
    uuids: Vec<String>,
    http: Arc<dyn HttpClient>,
}

#[async_trait]
impl Powermeter for VZLogger {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        // Parallel fetch (mirrors Python `asyncio.gather`); latency stays
        // bounded by the slowest UUID rather than scaling linearly.
        let futures: Vec<_> = self
            .uuids
            .iter()
            .map(|uuid| {
                let url = format!("http://{}:{}/{}", self.ip, self.port, uuid);
                let http = self.http.clone();
                async move {
                    http.request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
                        .await
                        .map_err(|e| Error::transport(format!("vzlogger: {e}")))
                }
            })
            .collect();
        let responses = futures::future::join_all(futures).await;
        let mut values = Vec::with_capacity(responses.len());
        for resp in responses {
            let resp = resp?;
            let json: Value = serde_json::from_slice(&resp.body)
                .map_err(|e| Error::decode(format!("vzlogger json: {e}")))?;
            let v = json
                .pointer("/data/0/tuples/0/1")
                .and_then(|v| v.as_f64())
                .ok_or_else(|| Error::decode("vzlogger: missing data[0].tuples[0][1]"))?;
            values.push(v.trunc());
        }
        Ok(values)
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let raw = section.get_string("UUID", "");
    let uuids: Vec<String> = raw
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect();
    if uuids.is_empty() {
        return Err(Error::config(format!(
            "section [{}] requires UUID",
            section.name()
        )));
    }
    Ok(Arc::new(VZLogger {
        ip: section.get_required("IP")?.to_string(),
        port: section.get_string("PORT", ""),
        uuids,
        http: platform.http.clone(),
    }))
}
