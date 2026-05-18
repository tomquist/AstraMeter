//! `EMLOG` — port of `src/astrameter/powermeter/emlog.py`.

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

pub struct Emlog {
    ip: String,
    meter_index: String,
    power_calculate: bool,
    http: Arc<dyn HttpClient>,
}

fn coerce_int(v: &Value) -> Result<i64> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .ok_or_else(|| Error::decode("not an int")),
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .or_else(|_| s.trim().parse::<f64>().map(|f| f as i64))
            .map_err(|e| Error::decode(format!("not an int: {e}"))),
        _ => Err(Error::decode(format!("can't coerce {v} to int"))),
    }
}

#[async_trait]
impl Powermeter for Emlog {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let url = format!(
            "http://{}/pages/getinformation.php?heute&meterindex={}",
            self.ip, self.meter_index
        );
        let resp = self
            .http
            .request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
            .await
            .map_err(|e| Error::transport(format!("emlog: {e}")))?;
        let json: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("emlog json: {e}")))?;
        let power_in = coerce_int(
            json.get("Leistung170")
                .ok_or_else(|| Error::decode("emlog: missing Leistung170"))?,
        )?;
        if !self.power_calculate {
            return Ok(vec![power_in as f64]);
        }
        let power_out = coerce_int(
            json.get("Leistung270")
                .ok_or_else(|| Error::decode("emlog: missing Leistung270"))?,
        )?;
        Ok(vec![(power_in - power_out) as f64])
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    Ok(Arc::new(Emlog {
        ip: section.get_required("IP")?.to_string(),
        meter_index: section.get_string("METER_INDEX", ""),
        power_calculate: section.get_bool("JSON_POWER_CALCULATE", false)?,
        http: platform.http.clone(),
    }))
}
