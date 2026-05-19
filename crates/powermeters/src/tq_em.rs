//! `TQ_EM` — port of `src/astrameter/powermeter/tq_em.py`.

use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    http::{HttpClient, HttpMethod, HttpRequest, HttpResponse},
    Platform,
};
use async_trait::async_trait;
use parking_lot::Mutex;
use serde_json::Value;

const MAX_IDLE_SECS: f64 = 60.0 * 30.0; // 30 min

// OBIS keys mirror tq_em.py's `_TOTAL_KEYS` / `_PHASE_KEYS`.
const KEY_TOTAL_FROM_GRID: &str = "1-0:1.4.0*255";
const KEY_TOTAL_TO_GRID: &str = "1-0:2.4.0*255";
const KEY_L1_FROM: &str = "1-0:21.4.0*255";
const KEY_L1_TO: &str = "1-0:22.4.0*255";
const KEY_L2_FROM: &str = "1-0:41.4.0*255";
const KEY_L2_TO: &str = "1-0:42.4.0*255";
const KEY_L3_FROM: &str = "1-0:61.4.0*255";
const KEY_L3_TO: &str = "1-0:62.4.0*255";

pub struct TqEm {
    host: String,
    password: String,
    timeout: Duration,
    http: Arc<dyn HttpClient>,
    state: tokio::sync::Mutex<()>,
    last_use: Mutex<Option<std::time::Instant>>,
    serial: Mutex<Option<String>>,
}

impl TqEm {
    fn url(&self, path: &str) -> String {
        format!("http://{}{}", self.host.trim_end_matches('/'), path)
    }

    async fn login(&self) -> Result<()> {
        let resp = self
            .http
            .request(HttpRequest::get(self.url("/start.php")).with_timeout(self.timeout))
            .await
            .map_err(|e| Error::transport(format!("tq_em login GET: {e}")))?;
        if !resp.is_success() {
            return Err(Error::transport(format!("tq_em login {}", resp.status)));
        }
        let j: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("tq_em login json: {e}")))?;
        let serial = j
            .get("serial")
            .or_else(|| j.get("ieq_serial"))
            .and_then(|v| v.as_str())
            .ok_or_else(|| Error::decode("tq_em: missing serial in /start.php"))?
            .to_string();
        *self.serial.lock() = Some(serial.clone());

        if j.get("authentication").and_then(|v| v.as_bool()) == Some(true) {
            return Ok(());
        }

        let mut body = format!("login={}&save_login=1", urlencode_form(&serial));
        if !self.password.is_empty() {
            body.push_str("&password=");
            body.push_str(&urlencode_form(&self.password));
        }
        let req = HttpRequest {
            method: HttpMethod::Post,
            url: self.url("/start.php"),
            headers: vec![(
                "Content-Type".into(),
                "application/x-www-form-urlencoded".into(),
            )],
            basic_auth: None,
            body: Some(body.into_bytes()),
            timeout: self.timeout,
            verify_tls: true,
            extra_root_cert_pem: None,
        };
        let resp = self
            .http
            .request(req)
            .await
            .map_err(|e| Error::transport(format!("tq_em login POST: {e}")))?;
        if !resp.is_success() {
            return Err(Error::transport(format!(
                "tq_em login POST {}",
                resp.status
            )));
        }
        let j: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("tq_em login POST json: {e}")))?;
        if j.get("authentication").and_then(|v| v.as_bool()) != Some(true) {
            return Err(Error::transport("tq_em: authentication failed"));
        }
        Ok(())
    }

    async fn fetch_data(&self) -> Result<Result<Value, SessionExpired>> {
        let resp: HttpResponse = self
            .http
            .request(
                HttpRequest::get(self.url("/mum-webservice/data.php")).with_timeout(self.timeout),
            )
            .await
            .map_err(|e| Error::transport(format!("tq_em data: {e}")))?;
        if resp.status == 401 || resp.status == 403 {
            return Ok(Err(SessionExpired));
        }
        if !resp.is_success() {
            return Err(Error::transport(format!("tq_em data {}", resp.status)));
        }
        let j: Value = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("tq_em data json: {e}")))?;
        if j.get("status").and_then(|v| v.as_i64()).unwrap_or(0) >= 900 {
            return Ok(Err(SessionExpired));
        }
        Ok(Ok(j))
    }
}

struct SessionExpired;

fn obis_f(j: &Value, key: &str) -> f64 {
    j.get(key)
        .and_then(|v| {
            v.as_f64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        })
        .unwrap_or(0.0)
}

#[async_trait]
impl Powermeter for TqEm {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let _guard = self.state.lock().await;
        let now = std::time::Instant::now();
        let needs_login = self.serial.lock().is_none()
            || self
                .last_use
                .lock()
                .map(|t| now.duration_since(t).as_secs_f64() > MAX_IDLE_SECS)
                .unwrap_or(true);
        if needs_login {
            self.login().await?;
        }
        *self.last_use.lock() = Some(now);

        let data = match self.fetch_data().await? {
            Ok(d) => d,
            Err(SessionExpired) => {
                self.login().await?;
                self.fetch_data()
                    .await?
                    .map_err(|_| Error::transport("tq_em: session re-expired"))?
            }
        };

        let has_phase = [
            KEY_L1_FROM,
            KEY_L1_TO,
            KEY_L2_FROM,
            KEY_L2_TO,
            KEY_L3_FROM,
            KEY_L3_TO,
        ]
        .iter()
        .any(|k| data.get(*k).is_some());
        if has_phase {
            return Ok(vec![
                obis_f(&data, KEY_L1_TO) - obis_f(&data, KEY_L1_FROM),
                obis_f(&data, KEY_L2_TO) - obis_f(&data, KEY_L2_FROM),
                obis_f(&data, KEY_L3_TO) - obis_f(&data, KEY_L3_FROM),
            ]);
        }
        if data.get(KEY_TOTAL_TO_GRID).is_some() || data.get(KEY_TOTAL_FROM_GRID).is_some() {
            return Ok(vec![
                obis_f(&data, KEY_TOTAL_TO_GRID) - obis_f(&data, KEY_TOTAL_FROM_GRID),
            ]);
        }
        Err(Error::decode("tq_em: required OBIS values missing"))
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let timeout_secs = section.get_float("TIMEOUT", 5.0)?;
    Ok(Arc::new(TqEm {
        host: section.get_required("IP")?.to_string(),
        password: section.get_string("PASSWORD", ""),
        timeout: Duration::from_secs_f64(timeout_secs.max(0.5)),
        http: platform.http.clone(),
        state: tokio::sync::Mutex::new(()),
        last_use: Mutex::new(None),
        serial: Mutex::new(None),
    }))
}

/// `application/x-www-form-urlencoded` quoting for POST body fields.
/// Matches Python `urllib.parse.urlencode` defaults (spaces -> %20 or +;
/// we use %20 for consistency with the rest of the codebase).
fn urlencode_form(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
