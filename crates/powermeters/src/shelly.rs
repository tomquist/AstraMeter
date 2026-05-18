//! `SHELLY` powermeter client — port of `src/astrameter/powermeter/shelly.py`.

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

#[derive(Debug, Clone, Copy)]
enum ShellyKind {
    OnePm,
    Plus1Pm,
    Em, // EM or 3EM
    Em3Pro,
}

pub struct Shelly {
    kind: ShellyKind,
    ip: String,
    user: String,
    pass_: String,
    meter_index: String,
    http: Arc<dyn HttpClient>,
}

impl Shelly {
    fn basic_auth(&self, mut req: HttpRequest) -> HttpRequest {
        if !self.user.is_empty() || !self.pass_.is_empty() {
            req = req.with_basic_auth(&self.user, &self.pass_);
        }
        req
    }

    async fn get_json(&self, path: &str) -> Result<Value> {
        let url = format!("http://{}{}", self.ip, path);
        let req = self.basic_auth(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)));
        let resp = self
            .http
            .request(req)
            .await
            .map_err(|e| Error::transport(format!("shelly: {e}")))?;
        if !resp.is_success() {
            return Err(Error::transport(format!("shelly HTTP {}", resp.status)));
        }
        serde_json::from_slice(&resp.body).map_err(|e| Error::decode(format!("shelly json: {e}")))
    }

    async fn get_rpc(&self, path: &str) -> Result<Value> {
        // Digest auth is not yet implemented in the platform-std HTTP client.
        // Shelly devices accept Basic auth for the same RPC endpoints when
        // the device is configured for it; we use Basic auth here. A future
        // commit can add a Digest-aware HttpClient variant.
        let url = format!("http://{}/rpc{}", self.ip, path);
        let req = self.basic_auth(HttpRequest::get(&url).with_timeout(Duration::from_secs(10)));
        let resp = self
            .http
            .request(req)
            .await
            .map_err(|e| Error::transport(format!("shelly rpc: {e}")))?;
        if !resp.is_success() {
            return Err(Error::transport(format!("shelly HTTP {}", resp.status)));
        }
        serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("shelly rpc json: {e}")))
    }
}

fn watts_field(v: &Value, field: &str) -> Result<f64> {
    v.get(field)
        .and_then(|x| x.as_f64().or_else(|| x.as_i64().map(|i| i as f64)))
        .ok_or_else(|| Error::decode(format!("shelly: missing {field}")))
}

#[async_trait]
impl Powermeter for Shelly {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        match self.kind {
            ShellyKind::OnePm => {
                if !self.meter_index.is_empty() {
                    let m = self
                        .get_json(&format!("/meter/{}", self.meter_index))
                        .await?;
                    Ok(vec![watts_field(&m, "power")?.trunc()])
                } else {
                    let st = self.get_json("/status").await?;
                    let meters = st
                        .get("meters")
                        .and_then(|v| v.as_array())
                        .ok_or_else(|| Error::decode("shelly: meters[]"))?;
                    Ok(meters
                        .iter()
                        .map(|m| watts_field(m, "power").map(|v| v.trunc()))
                        .collect::<Result<Vec<_>>>()?)
                }
            }
            ShellyKind::Plus1Pm => {
                let r = self.get_rpc("/Switch.GetStatus?id=0").await?;
                Ok(vec![watts_field(&r, "apower")?.trunc()])
            }
            ShellyKind::Em => {
                if !self.meter_index.is_empty() {
                    let m = self
                        .get_json(&format!("/emeter/{}", self.meter_index))
                        .await?;
                    Ok(vec![watts_field(&m, "power")?.trunc()])
                } else {
                    let st = self.get_json("/status").await?;
                    let emeters = st
                        .get("emeters")
                        .and_then(|v| v.as_array())
                        .ok_or_else(|| Error::decode("shelly: emeters[]"))?;
                    Ok(emeters
                        .iter()
                        .map(|m| watts_field(m, "power").map(|v| v.trunc()))
                        .collect::<Result<Vec<_>>>()?)
                }
            }
            ShellyKind::Em3Pro => {
                let r = self.get_rpc("/EM.GetStatus?id=0").await?;
                Ok(vec![watts_field(&r, "total_act_power")?.trunc()])
            }
        }
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let kind = match section.get_str("TYPE", "") {
        "1PM" => ShellyKind::OnePm,
        "PLUS1PM" => ShellyKind::Plus1Pm,
        "EM" | "3EM" => ShellyKind::Em,
        "3EMPro" => ShellyKind::Em3Pro,
        other => {
            return Err(Error::config(format!(
                "unknown Shelly TYPE {other:?} in [{}]",
                section.name()
            )))
        }
    };
    Ok(Arc::new(Shelly {
        kind,
        ip: section.get_required("IP")?.to_string(),
        user: section.get_string("USER", ""),
        pass_: section.get_string("PASS", ""),
        meter_index: section.get_string("METER_INDEX", ""),
        http: platform.http.clone(),
    }))
}
