//! `IOBROKER` — port of `src/astrameter/powermeter/iobroker.py`.

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

pub struct IoBroker {
    ip: String,
    port: String,
    current_power_alias: String,
    power_calculate: bool,
    power_input_alias: String,
    power_output_alias: String,
    http: Arc<dyn HttpClient>,
}

fn find_val(json: &Value, id: &str) -> Option<f64> {
    if let Some(arr) = json.as_array() {
        for item in arr {
            if item.get("id").and_then(|v| v.as_str()) == Some(id) {
                return item
                    .get("val")
                    .and_then(|v| v.as_f64().or_else(|| v.as_i64().map(|i| i as f64)));
            }
        }
    }
    None
}

#[async_trait]
impl Powermeter for IoBroker {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let base = format!("http://{}:{}", self.ip, self.port);
        if !self.power_calculate {
            let url = format!("{}/getBulk/{}", base, self.current_power_alias);
            let resp = self
                .http
                .request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
                .await
                .map_err(|e| Error::transport(format!("iobroker: {e}")))?;
            let json: Value = serde_json::from_slice(&resp.body)
                .map_err(|e| Error::decode(format!("iobroker json: {e}")))?;
            let v = find_val(&json, &self.current_power_alias).ok_or_else(|| {
                Error::decode(format!(
                    "iobroker: alias {:?} not found",
                    self.current_power_alias
                ))
            })?;
            Ok(vec![v.trunc()])
        } else {
            let url = format!(
                "{}/getBulk/{},{}",
                base, self.power_input_alias, self.power_output_alias
            );
            let resp = self
                .http
                .request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
                .await
                .map_err(|e| Error::transport(format!("iobroker: {e}")))?;
            let json: Value = serde_json::from_slice(&resp.body)
                .map_err(|e| Error::decode(format!("iobroker json: {e}")))?;
            let p_in = find_val(&json, &self.power_input_alias).unwrap_or(0.0);
            let p_out = find_val(&json, &self.power_output_alias).unwrap_or(0.0);
            Ok(vec![(p_in - p_out).trunc()])
        }
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    Ok(Arc::new(IoBroker {
        ip: section.get_required("IP")?.to_string(),
        port: section.get_string("PORT", ""),
        current_power_alias: section.get_string("CURRENT_POWER_ALIAS", ""),
        power_calculate: section.get_bool("POWER_CALCULATE", false)?,
        power_input_alias: section.get_string("POWER_INPUT_ALIAS", ""),
        power_output_alias: section.get_string("POWER_OUTPUT_ALIAS", ""),
        http: platform.http.clone(),
    }))
}
