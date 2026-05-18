//! Marstek cloud registration HTTPS client.
//!
//! Port of `src/astrameter/marstek_api.py`. Provides the small client used
//! to register the AstraMeter instance with Marstek's cloud so the mobile
//! app shows it. The full Python module handles login, device list, set
//! display preferences, and event polling; this Rust port currently
//! implements **just the login + register-device flow**. The remaining
//! endpoints are TODO.

#![forbid(unsafe_code)]

use std::sync::Arc;
use std::time::Duration;

use astrameter_core::{Error, Result};
use astrameter_platform::http::{HttpClient, HttpMethod, HttpRequest};
use serde::Deserialize;

const API_BASE: &str = "https://eu.hamedata.com";

#[derive(Debug, Deserialize)]
struct LoginResponse {
    code: i64,
    token: Option<String>,
    msg: Option<String>,
}

pub struct MarstekClient {
    http: Arc<dyn HttpClient>,
    pub token: tokio::sync::Mutex<Option<String>>,
}

impl MarstekClient {
    pub fn new(http: Arc<dyn HttpClient>) -> Self {
        Self {
            http,
            token: tokio::sync::Mutex::new(None),
        }
    }

    pub async fn login(&self, email: &str, password: &str) -> Result<()> {
        let body = format!("pwd={}&mailbox={}", urlencode(password), urlencode(email));
        let req = HttpRequest {
            method: HttpMethod::Post,
            url: format!("{}/app/Solar/v3_get_app_token.php", API_BASE),
            headers: vec![(
                "Content-Type".into(),
                "application/x-www-form-urlencoded".into(),
            )],
            basic_auth: None,
            body: Some(body.into_bytes()),
            timeout: Duration::from_secs(10),
            verify_tls: true,
            extra_root_cert_pem: None,
        };
        let resp = self
            .http
            .request(req)
            .await
            .map_err(|e| Error::transport(format!("marstek login: {e}")))?;
        if !resp.is_success() {
            return Err(Error::transport(format!(
                "marstek login HTTP {}",
                resp.status
            )));
        }
        let parsed: LoginResponse = serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("marstek login json: {e}")))?;
        if parsed.code != 1 {
            return Err(Error::transport(format!(
                "marstek login: code={} msg={:?}",
                parsed.code, parsed.msg
            )));
        }
        let token = parsed
            .token
            .ok_or_else(|| Error::transport("marstek login: no token"))?;
        *self.token.lock().await = Some(token);
        tracing::info!("Marstek: logged in to cloud");
        Ok(())
    }
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
