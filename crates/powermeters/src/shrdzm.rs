//! `SHRDZM` — port of `src/astrameter/powermeter/shrdzm.py`.

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

pub struct Shrdzm {
    ip: String,
    user: String,
    pass_: String,
    http: Arc<dyn HttpClient>,
}

fn coerce(v: &Value) -> Result<f64> {
    match v {
        Value::Number(n) => n.as_f64().ok_or_else(|| Error::decode("not numeric")),
        Value::String(s) => s
            .trim()
            .parse::<f64>()
            .map_err(|e| Error::decode(format!("not a float: {e}"))),
        _ => Err(Error::decode("can't coerce")),
    }
}

#[async_trait]
impl Powermeter for Shrdzm {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let url = format!(
            "http://{}/getLastData?user={}&password={}",
            self.ip, self.user, self.pass_
        );
        let resp = self
            .http
            .request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
            .await
            .map_err(|e| Error::transport(format!("shrdzm: {e}")))?;
        let json: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("shrdzm json: {e}")))?;
        let plus = coerce(
            json.get("1.7.0")
                .ok_or_else(|| Error::decode("shrdzm: missing 1.7.0"))?,
        )?;
        let minus = coerce(
            json.get("2.7.0")
                .ok_or_else(|| Error::decode("shrdzm: missing 2.7.0"))?,
        )?;
        Ok(vec![(plus - minus).trunc()])
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    Ok(Arc::new(Shrdzm {
        ip: section.get_required("IP")?.to_string(),
        user: section.get_string("USER", ""),
        pass_: section.get_string("PASS", ""),
        http: platform.http.clone(),
    }))
}
