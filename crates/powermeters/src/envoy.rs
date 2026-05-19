//! `ENVOY` — Enphase Envoy + Enlighten cloud JWT. Port of
//! `src/astrameter/powermeter/envoy.py`.

use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    http::{HttpClient, HttpMethod, HttpRequest},
    Platform,
};
use async_trait::async_trait;
use serde_json::Value;
use tokio::sync::Mutex;

const ENLIGHTEN_LOGIN_URL: &str = "https://enlighten.enphaseenergy.com/login/login.json";
const ENTREZ_TOKEN_URL: &str = "https://entrez.enphaseenergy.com/tokens";
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(10);

pub struct Envoy {
    host: String,
    token: Mutex<String>,
    username: String,
    password: String,
    serial: String,
    has_credentials: bool,
    verify_ssl: bool,
    http: Arc<dyn HttpClient>,
}

impl Envoy {
    async fn ensure_token(&self) -> Result<()> {
        if !self.token.lock().await.is_empty() {
            return Ok(());
        }
        if !self.has_credentials {
            return Err(Error::config("Envoy: TOKEN missing and no credentials"));
        }
        self.refresh_token().await
    }

    async fn refresh_token(&self) -> Result<()> {
        let new = obtain_token(
            self.http.as_ref(),
            &self.username,
            &self.password,
            &self.serial,
        )
        .await?;
        *self.token.lock().await = new;
        Ok(())
    }

    async fn get_production(&self) -> Result<Value> {
        let token = self.token.lock().await.clone();
        let url = format!("https://{}/production.json?details=1", self.host);
        let req = HttpRequest::get(&url)
            .with_timeout(DEFAULT_TIMEOUT)
            .with_header("Authorization", format!("Bearer {token}"));
        let mut req = req;
        req.verify_tls = self.verify_ssl;
        let resp = self
            .http
            .request(req)
            .await
            .map_err(|e| Error::transport(format!("envoy: {e}")))?;
        if resp.status == 401 {
            return Err(Error::transport("envoy: 401 (token expired)"));
        }
        if !resp.is_success() {
            return Err(Error::transport(format!("envoy HTTP {}", resp.status)));
        }
        serde_json::from_slice(&resp.body).map_err(|e| Error::decode(format!("envoy json: {e}")))
    }
}

async fn obtain_token(
    http: &dyn HttpClient,
    username: &str,
    password: &str,
    serial: &str,
) -> Result<String> {
    let login_body = format!(
        "user[email]={}&user[password]={}",
        urlencode(username),
        urlencode(password)
    );
    let req = HttpRequest {
        method: HttpMethod::Post,
        url: ENLIGHTEN_LOGIN_URL.into(),
        headers: vec![(
            "Content-Type".into(),
            "application/x-www-form-urlencoded".into(),
        )],
        basic_auth: None,
        body: Some(login_body.into_bytes()),
        timeout: DEFAULT_TIMEOUT,
        verify_tls: true,
        extra_root_cert_pem: None,
    };
    let resp = http
        .request(req)
        .await
        .map_err(|e| Error::transport(format!("envoy login: {e}")))?;
    if !resp.is_success() {
        return Err(Error::transport(format!(
            "envoy login HTTP {}",
            resp.status
        )));
    }
    let login_json: Value = serde_json::from_slice(&resp.body)
        .map_err(|e| Error::decode(format!("envoy login json: {e}")))?;
    let session_id = login_json
        .get("session_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| Error::transport("envoy: login response missing session_id"))?;

    let token_body = serde_json::json!({
        "session_id": session_id,
        "serial_num": serial,
        "username": username,
    });
    let req = HttpRequest {
        method: HttpMethod::Post,
        url: ENTREZ_TOKEN_URL.into(),
        headers: vec![("Content-Type".into(), "application/json".into())],
        basic_auth: None,
        body: Some(serde_json::to_vec(&token_body).unwrap()),
        timeout: DEFAULT_TIMEOUT,
        verify_tls: true,
        extra_root_cert_pem: None,
    };
    let resp = http
        .request(req)
        .await
        .map_err(|e| Error::transport(format!("envoy token: {e}")))?;
    if !resp.is_success() {
        return Err(Error::transport(format!(
            "envoy token HTTP {}",
            resp.status
        )));
    }
    let text = std::str::from_utf8(&resp.body)
        .map_err(|e| Error::decode(format!("envoy token utf8: {e}")))?
        .trim();
    if !text.starts_with("eyJ") || text.matches('.').count() != 2 {
        return Err(Error::transport(format!(
            "envoy: entrez did not return a JWT (body: {:?})",
            &text[..text.len().min(200)]
        )));
    }
    tracing::info!("Envoy: obtained new JWT token from Enlighten cloud");
    Ok(text.to_string())
}

fn urlencode(s: &str) -> String {
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

#[async_trait]
impl Powermeter for Envoy {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        self.ensure_token().await?;
        let token_at_start = self.token.lock().await.clone();
        let data = match self.get_production().await {
            Ok(d) => d,
            Err(Error::Transport(s)) if s.contains("401") && self.has_credentials => {
                // Avoid duplicate Enlighten logins under concurrent 401s:
                // if another caller already refreshed while we were
                // awaiting, skip our own refresh.
                {
                    let guard = self.token.lock().await;
                    if *guard != token_at_start {
                        tracing::debug!("Envoy: token already refreshed by peer; retrying");
                    } else {
                        drop(guard);
                        tracing::info!("Envoy: token rejected (401), refreshing");
                        self.refresh_token().await?;
                    }
                }
                self.get_production().await?
            }
            Err(e) => return Err(e),
        };
        let consumption = data
            .get("consumption")
            .and_then(|v| v.as_array())
            .ok_or_else(|| {
                Error::decode("envoy: production.json missing 'consumption' (CTs required)")
            })?;
        let entry = consumption
            .iter()
            .find(|c| c.get("measurementType").and_then(|v| v.as_str()) == Some("net-consumption"))
            .ok_or_else(|| Error::decode("envoy: missing net-consumption entry"))?;
        if let Some(lines) = entry.get("lines").and_then(|v| v.as_array()) {
            if !lines.is_empty() {
                let mut out = Vec::new();
                for (i, line) in lines.iter().take(3).enumerate() {
                    let w = line.get("wNow").and_then(|v| v.as_f64()).ok_or_else(|| {
                        Error::decode(format!("envoy: malformed line at index {i}"))
                    })?;
                    out.push(w);
                }
                return Ok(out);
            }
        }
        let w = entry
            .get("wNow")
            .and_then(|v| v.as_f64())
            .ok_or_else(|| Error::decode("envoy: net-consumption missing wNow"))?;
        Ok(vec![w])
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let host = section.get_required("HOST")?.to_string();
    let token = section.get_string("TOKEN", "");
    let username = section.get_string("USERNAME", "");
    let password = section.get_string("PASSWORD", "");
    let serial = section.get_string("SERIAL", "");
    let has_credentials = !username.is_empty() && !password.is_empty() && !serial.is_empty();
    if token.is_empty() && !has_credentials {
        return Err(Error::config(
            "Envoy: provide either TOKEN or USERNAME/PASSWORD/SERIAL",
        ));
    }
    let verify_ssl = section.get_bool("VERIFY_SSL", false)?;
    if !verify_ssl {
        tracing::warn!("Envoy: VERIFY_SSL=False — use only on a trusted LAN");
    }
    Ok(Arc::new(Envoy {
        host,
        token: Mutex::new(token),
        username,
        password,
        serial,
        has_credentials,
        verify_ssl,
        http: platform.http.clone(),
    }))
}
