//! `AMIS_READER` powermeter — port of `src/astrameter/powermeter/amisreader.py`.

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

pub struct AmisReader {
    ip: String,
    http: Arc<dyn HttpClient>,
}

#[async_trait]
impl Powermeter for AmisReader {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let url = format!("http://{}/rest", self.ip);
        let resp = self
            .http
            .request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
            .await
            .map_err(|e| Error::transport(format!("amisreader: {e}")))?;
        let json: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("amisreader json: {e}")))?;
        let saldo = json
            .get("saldo")
            .and_then(|v| {
                v.as_f64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            })
            .ok_or_else(|| Error::decode("amisreader: missing saldo"))?;
        Ok(vec![saldo.trunc()])
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let ip = section.get_required("IP")?.to_string();
    Ok(Arc::new(AmisReader {
        ip,
        http: platform.http.clone(),
    }))
}
