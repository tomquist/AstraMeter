//! `TASMOTA` — port of `src/astrameter/powermeter/tasmota.py`.

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

pub struct Tasmota {
    ip: String,
    user: String,
    pass_: String,
    json_status: String,
    json_payload_mqtt_prefix: String,
    json_power_labels: Vec<String>,
    json_power_input_labels: Vec<String>,
    json_power_output_labels: Vec<String>,
    json_power_calculate: bool,
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
            .parse::<f64>()
            .map(|f| f as i64)
            .map_err(|e| Error::decode(format!("not numeric: {e}"))),
        _ => Err(Error::decode("not numeric")),
    }
}

#[async_trait]
impl Powermeter for Tasmota {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let path = if self.user.is_empty() {
            "/cm?cmnd=status%2010".to_string()
        } else {
            let user = urlencoding::encode(&self.user);
            let pw = urlencoding::encode(&self.pass_);
            format!("/cm?user={user}&password={pw}&cmnd=status+10")
        };
        let url = format!("http://{}{}", self.ip, path);
        let resp = self
            .http
            .request(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)))
            .await
            .map_err(|e| Error::transport(format!("tasmota: {e}")))?;
        if !resp.is_success() {
            return Err(Error::transport(format!("tasmota HTTP {}", resp.status)));
        }
        let json: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("tasmota json: {e}")))?;
        let inner = json
            .get(&self.json_status)
            .and_then(|v| v.get(&self.json_payload_mqtt_prefix))
            .ok_or_else(|| {
                Error::decode(format!(
                    "tasmota: missing {}.{} in response",
                    self.json_status, self.json_payload_mqtt_prefix
                ))
            })?;
        if !self.json_power_calculate {
            let mut out = Vec::new();
            for label in &self.json_power_labels {
                let v = inner
                    .get(label)
                    .ok_or_else(|| Error::decode(format!("tasmota: missing label {label}")))?;
                out.push(coerce_int(v)? as f64);
            }
            Ok(out)
        } else {
            if self.json_power_input_labels.len() != self.json_power_output_labels.len() {
                return Err(Error::config(
                    "JSON_POWER_INPUT_MQTT_LABEL and JSON_POWER_OUTPUT_MQTT_LABEL count mismatch",
                ));
            }
            let mut out = Vec::new();
            for (i_lbl, o_lbl) in self
                .json_power_input_labels
                .iter()
                .zip(self.json_power_output_labels.iter())
            {
                let pi = coerce_int(
                    inner
                        .get(i_lbl)
                        .ok_or_else(|| Error::decode(format!("tasmota: missing {i_lbl}")))?,
                )?;
                let po = coerce_int(
                    inner
                        .get(o_lbl)
                        .ok_or_else(|| Error::decode(format!("tasmota: missing {o_lbl}")))?,
                )?;
                out.push((pi - po) as f64);
            }
            Ok(out)
        }
    }
}

fn split(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let calc = section.get_bool("JSON_POWER_CALCULATE", false)?;
    let in_labels = split(section.get_str("JSON_POWER_INPUT_MQTT_LABEL", ""));
    let out_labels = split(section.get_str("JSON_POWER_OUTPUT_MQTT_LABEL", ""));
    if calc && in_labels.len() != out_labels.len() {
        return Err(Error::config(
            "JSON_POWER_INPUT_MQTT_LABEL / JSON_POWER_OUTPUT_MQTT_LABEL count differ",
        ));
    }
    let pow_labels = split(section.get_str("JSON_POWER_MQTT_LABEL", ""));
    if !calc && pow_labels.is_empty() {
        return Err(Error::config(format!(
            "section [{}] requires JSON_POWER_MQTT_LABEL",
            section.name()
        )));
    }
    Ok(Arc::new(Tasmota {
        ip: section.get_required("IP")?.to_string(),
        user: section.get_string("USER", ""),
        pass_: section.get_string("PASS", ""),
        json_status: section.get_required("JSON_STATUS")?.to_string(),
        json_payload_mqtt_prefix: section
            .get_required("JSON_PAYLOAD_MQTT_PREFIX")?
            .to_string(),
        json_power_labels: pow_labels,
        json_power_input_labels: in_labels,
        json_power_output_labels: out_labels,
        json_power_calculate: calc,
        http: platform.http.clone(),
    }))
}

// Minimal in-tree URL encoder so we don't pull `urlencoding` just for one
// callsite. Handles the subset Tasmota needs.
mod urlencoding {
    pub fn encode(s: &str) -> String {
        let mut out = String::with_capacity(s.len());
        for b in s.bytes() {
            match b {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                    out.push(b as char)
                }
                _ => out.push_str(&format!("%{:02X}", b)),
            }
        }
        out
    }
}
