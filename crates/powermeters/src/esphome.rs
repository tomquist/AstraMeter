//! `ESPHOME` — port of `src/astrameter/powermeter/esphome.py`.

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

pub struct ESPHome {
    ip: String,
    port: String,
    domain: String,
    id: String,
    http: Arc<dyn HttpClient>,
}

#[async_trait]
impl Powermeter for ESPHome {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let url = format!(
            "http://{}:{}/{}/{}",
            self.ip, self.port, self.domain, self.id
        );
        let resp = self
            .http
            .request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
            .await
            .map_err(|e| Error::transport(format!("esphome: {e}")))?;
        let json: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("esphome json: {e}")))?;
        let v = json
            .get("value")
            .and_then(|v| v.as_f64())
            .ok_or_else(|| Error::decode("esphome: missing value"))?;
        Ok(vec![v.trunc()])
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    Ok(Arc::new(ESPHome {
        ip: section.get_required("IP")?.to_string(),
        port: section.get_string("PORT", ""),
        domain: section.get_string("DOMAIN", ""),
        id: section.get_string("ID", ""),
        http: platform.http.clone(),
    }))
}
