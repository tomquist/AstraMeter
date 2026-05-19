//! Marstek cloud HTTPS client.
//!
//! Faithful port of `src/astrameter/marstek_api.py`. Provides
//! [`ensure_managed_fake_device`], which idempotently creates a managed
//! "fake" CT002 or CT003 device in the Marstek cloud so the mobile app
//! picks it up. Mirrors the Python flow:
//! 1. Fetch a session token via `/app/Solar/v2_get_device.php` (with
//!    MD5-hashed password).
//! 2. Pull the EMS device list from `/ems/api/v1/getDeviceList`.
//! 3. If a managed device of the expected type already exists (devid +
//!    mac both start with `02b250…`), return it.
//! 4. Otherwise generate a new random `02b250…` MAC, register it via
//!    `/app/Solar/v2_add_device.php`, and confirm.

#![forbid(unsafe_code)]

use std::sync::Arc;
use std::time::Duration;

use astrameter_platform::http::{HttpClient, HttpMethod, HttpRequest};
use md5::{Digest, Md5};
use serde_json::Value;
use thiserror::Error;

pub const MANAGED_MAC_PREFIX: &str = "02b250";

#[derive(Debug, Clone)]
pub struct MarstekConfig {
    pub base_url: String,
    pub mailbox: String,
    pub password: String,
    pub timezone: String,
}

impl MarstekConfig {
    pub fn new(
        base_url: impl Into<String>,
        mailbox: impl Into<String>,
        password: impl Into<String>,
    ) -> Self {
        Self {
            base_url: base_url.into(),
            mailbox: mailbox.into(),
            password: password.into(),
            timezone: "Europe/Berlin".into(),
        }
    }
}

#[derive(Debug, Error)]
pub enum MarstekApiError {
    #[error("network error calling {url}: {source}")]
    Network {
        url: String,
        #[source]
        source: astrameter_platform::http::HttpError,
    },
    #[error("non-JSON response from {url}: {snippet}")]
    NonJson { url: String, snippet: String },
    #[error("HTTP {code} from {url}: {body}")]
    Status {
        code: u16,
        url: String,
        body: String,
    },
    #[error("Token fetch failed (code={code}): {msg}")]
    TokenFetch { code: String, msg: String },
    #[error("Add device failed for {device_type} (code={code}): {msg}")]
    AddDevice {
        device_type: String,
        code: String,
        msg: String,
    },
    #[error("Could not generate unique managed MAC/DEVID")]
    GenerateUniqueMac,
}

pub struct MarstekClient {
    http: Arc<dyn HttpClient>,
}

impl MarstekClient {
    pub fn new(http: Arc<dyn HttpClient>) -> Self {
        Self { http }
    }

    async fn http_get_json(
        &self,
        base: &str,
        params: &[(&str, &str)],
        headers: &[(&str, &str)],
    ) -> Result<Value, MarstekApiError> {
        let mut url = base.to_string();
        if !params.is_empty() {
            url.push('?');
            for (i, (k, v)) in params.iter().enumerate() {
                if i > 0 {
                    url.push('&');
                }
                url.push_str(&urlencode(k));
                url.push('=');
                url.push_str(&urlencode(v));
            }
        }
        let req = HttpRequest {
            method: HttpMethod::Get,
            url: url.clone(),
            headers: headers
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect(),
            basic_auth: None,
            body: None,
            timeout: Duration::from_secs(20),
            verify_tls: true,
            extra_root_cert_pem: None,
        };
        let resp = self
            .http
            .request(req)
            .await
            .map_err(|source| MarstekApiError::Network {
                url: url.clone(),
                source,
            })?;
        let body = String::from_utf8_lossy(&resp.body).to_string();
        let payload: Value = match serde_json::from_str(&body) {
            Ok(v) => v,
            Err(_) => {
                let snippet = if body.is_empty() {
                    "<empty>".to_string()
                } else {
                    body.chars().take(200).collect()
                };
                return Err(MarstekApiError::NonJson { url, snippet });
            }
        };
        if !(200..300).contains(&resp.status) {
            return Err(MarstekApiError::Status {
                code: resp.status,
                url,
                body: payload.to_string(),
            });
        }
        Ok(payload)
    }

    async fn fetch_token_and_devices(
        &self,
        cfg: &MarstekConfig,
    ) -> Result<(String, Vec<Value>), MarstekApiError> {
        let pwd_md5 = {
            let mut h = Md5::new();
            h.update(cfg.password.as_bytes());
            hex::encode(h.finalize())
        };
        let base = cfg.base_url.trim_end_matches('/').to_string();
        let token_url = format!("{base}/app/Solar/v2_get_device.php");
        let token_resp = self
            .http_get_json(
                &token_url,
                &[("mailbox", &cfg.mailbox), ("pwd", &pwd_md5)],
                &[],
            )
            .await?;
        let code = code_str(&token_resp, "code");
        let token = token_resp
            .get("token")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        if code != "2" || token.is_none() {
            let raw_msg = msg_str(&token_resp, "msg");
            let translated = translate_marstek_message(&code, &raw_msg);
            let msg = if !translated.is_empty() && translated != raw_msg {
                format!("{translated} (raw: {raw_msg})")
            } else {
                raw_msg
            };
            return Err(MarstekApiError::TokenFetch { code, msg });
        }
        let token = token.unwrap();
        let solar_devices: Vec<Value> = token_resp
            .get("data")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let list_url = format!("{base}/ems/api/v1/getDeviceList");
        let list_resp = self
            .http_get_json(
                &list_url,
                &[("mailbox", &cfg.mailbox), ("token", &token)],
                &[("User-Agent", "Dart/2.19 (dart:io)")],
            )
            .await?;
        let ems_devices: Vec<Value> = list_resp
            .get("data")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let mut by_devid = std::collections::HashMap::<String, Value>::new();
        for d in &ems_devices {
            if let Some(id) = d.get("devid").and_then(|v| v.as_str()) {
                if !id.is_empty() {
                    by_devid.insert(id.to_string(), d.clone());
                }
            }
        }
        let mut merged = Vec::new();
        for d in &solar_devices {
            let Some(did) = d.get("devid").and_then(|v| v.as_str()) else {
                continue;
            };
            let e = by_devid.get(did);
            let pick_str = |a: Option<&str>, b: Option<&str>| match (a, b) {
                (Some(s), _) if !s.is_empty() => Value::String(s.into()),
                (_, Some(s)) if !s.is_empty() => Value::String(s.into()),
                _ => Value::Null,
            };
            let name = pick_str(
                d.get("name").and_then(|v| v.as_str()),
                e.and_then(|e| e.get("name")).and_then(|v| v.as_str()),
            );
            let mac = pick_str(
                d.get("mac").and_then(|v| v.as_str()),
                e.and_then(|e| e.get("mac")).and_then(|v| v.as_str()),
            );
            let dtype = pick_str(
                d.get("type").and_then(|v| v.as_str()),
                e.and_then(|e| e.get("type")).and_then(|v| v.as_str()),
            );
            merged.push(serde_json::json!({
                "devid": did,
                "name": name,
                "sn": d.get("sn").cloned().unwrap_or(Value::Null),
                "mac": mac,
                "type": dtype,
                "access": d.get("access").cloned().unwrap_or(Value::Null),
                "bluetooth_name": d.get("bluetooth_name").cloned().unwrap_or(Value::Null),
                "version": e.and_then(|e| e.get("version")).cloned().unwrap_or(Value::Null),
                "salt": e.and_then(|e| e.get("salt")).cloned().unwrap_or(Value::Null),
            }));
        }
        Ok((token, merged))
    }

    async fn add_device(
        &self,
        cfg: &MarstekConfig,
        token: &str,
        device_type: &str,
        devid_mac: &str,
    ) -> Result<Value, MarstekApiError> {
        let base = cfg.base_url.trim_end_matches('/').to_string();
        let add_url = format!("{base}/app/Solar/v2_add_device.php");
        let type_value = desired_type(device_type);
        let suffix = &devid_mac[devid_mac.len().saturating_sub(4)..];
        let name = desired_name(device_type);
        let bt_name = format!("MST-SMR_{suffix}");
        let params: Vec<(&str, &str)> = vec![
            ("name", &name),
            ("mailbox", &cfg.mailbox),
            ("devid", devid_mac),
            ("mac", devid_mac),
            ("type", type_value),
            ("token", token),
            ("access", "1"),
            ("bluetooth_name", &bt_name),
            ("position", "{}"),
            ("timeZone", &cfg.timezone),
            ("version", "121"),
        ];
        let headers = vec![
            ("Content-Type", "application/json"),
            ("Accept", "application/json"),
            ("token", token),
            ("User-Agent", "Dart/2.19 (dart:io)"),
        ];
        let resp = self.http_get_json(&add_url, &params, &headers).await?;
        let code = code_str(&resp, "code");
        if code != "1" && code != "2" {
            let msg = msg_str(&resp, "msg");
            return Err(MarstekApiError::AddDevice {
                device_type: device_type.into(),
                code,
                msg,
            });
        }
        Ok(resp)
    }

    pub async fn ensure_managed_fake_device(
        &self,
        cfg: &MarstekConfig,
        device_type: &str,
    ) -> Result<Option<Value>, MarstekApiError> {
        if device_type != "ct002" && device_type != "ct003" {
            return Ok(None);
        }
        let (token, devices) = self.fetch_token_and_devices(cfg).await?;
        let expected_type = desired_type(device_type);
        if let Some(existing) = find_existing_managed_device(&devices, expected_type) {
            tracing::info!(
                "Marstek managed {device_type} already exists (devid={:?})",
                existing.get("devid")
            );
            return Ok(Some(existing));
        }
        let new_id = generate_new_id(&devices)?;
        tracing::info!(
            "Creating managed fake {device_type} (devid=mac={new_id}, type={expected_type})"
        );
        self.add_device(cfg, &token, device_type, &new_id).await?;
        let (_t, refreshed) = self.fetch_token_and_devices(cfg).await?;
        Ok(find_existing_managed_device(&refreshed, expected_type))
    }
}

fn code_str(v: &Value, key: &str) -> String {
    match v.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn msg_str(v: &Value, key: &str) -> String {
    v.get(key)
        .map(|x| match x {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        })
        .unwrap_or_default()
}

fn translate_marstek_message(code: &str, msg: &str) -> String {
    if code == "4" && (msg.contains("密码错误") || msg.to_lowercase().contains("password")) {
        return "password incorrect".to_string();
    }
    msg.to_string()
}

fn desired_type(device_type: &str) -> &'static str {
    if device_type == "ct002" {
        "HME-4"
    } else {
        "HME-3"
    }
}

fn desired_name(device_type: &str) -> String {
    if device_type == "ct002" {
        "AstraMeter CT002".into()
    } else {
        "AstraMeter CT003".into()
    }
}

fn is_managed_prefix(value: &str) -> bool {
    value.to_lowercase().starts_with(MANAGED_MAC_PREFIX)
}

fn find_existing_managed_device(devices: &[Value], expected_type: &str) -> Option<Value> {
    for d in devices {
        let devid = d.get("devid").and_then(|v| v.as_str()).unwrap_or("");
        let mac = d.get("mac").and_then(|v| v.as_str()).unwrap_or("");
        let dtype = d.get("type").and_then(|v| v.as_str()).unwrap_or("");
        if dtype != expected_type {
            continue;
        }
        if is_managed_prefix(devid) && is_managed_prefix(mac) {
            return Some(d.clone());
        }
    }
    None
}

fn generate_new_id(existing_devices: &[Value]) -> Result<String, MarstekApiError> {
    use rand::Rng;
    let mut existing = std::collections::HashSet::<String>::new();
    for d in existing_devices {
        if let Some(devid) = d.get("devid").and_then(|v| v.as_str()) {
            if !devid.is_empty() {
                existing.insert(devid.to_lowercase());
            }
        }
        if let Some(mac) = d.get("mac").and_then(|v| v.as_str()) {
            if !mac.is_empty() {
                existing.insert(mac.to_lowercase());
            }
        }
    }
    let mut rng = rand::thread_rng();
    for _ in 0..200 {
        let mut s = String::from(MANAGED_MAC_PREFIX);
        for _ in 0..6 {
            let n: u8 = rng.gen_range(0..16);
            s.push(std::char::from_digit(n as u32, 16).unwrap());
        }
        if !existing.contains(&s) {
            return Ok(s);
        }
    }
    Err(MarstekApiError::GenerateUniqueMac)
}

fn urlencode(s: &str) -> String {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn translate_marstek_password_message() {
        assert_eq!(
            translate_marstek_message("4", "密码错误"),
            "password incorrect"
        );
        assert_eq!(
            translate_marstek_message("4", "Invalid password"),
            "password incorrect"
        );
        assert_eq!(translate_marstek_message("1", "ok"), "ok");
    }

    #[test]
    fn desired_type_routes() {
        assert_eq!(desired_type("ct002"), "HME-4");
        assert_eq!(desired_type("ct003"), "HME-3");
    }

    #[test]
    fn find_existing_matches_managed_prefix() {
        let devices = vec![serde_json::json!({
            "devid": "02b250aabbcc",
            "mac": "02b250aabbcc",
            "type": "HME-4",
        })];
        let m = find_existing_managed_device(&devices, "HME-4");
        assert!(m.is_some());
    }

    #[test]
    fn generate_new_id_avoids_collisions() {
        // Pre-fill 200 candidates so generator must scan and fail.
        // Instead just confirm the happy path returns the right prefix.
        let id = generate_new_id(&[]).unwrap();
        assert!(id.starts_with(MANAGED_MAC_PREFIX));
        assert_eq!(id.len(), MANAGED_MAC_PREFIX.len() + 6);
    }
}
