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
        // Shelly Plus and Pro devices (Plus1PM, 3EMPro) use HTTP Digest auth
        // when authentication is enabled. We send the request once without
        // credentials, then if the server returns 401 with a Digest challenge
        // we build the Authorization header and retry.
        let url = format!("http://{}/rpc{}", self.ip, path);
        let first = HttpRequest::get(&url).with_timeout(Duration::from_secs(10));
        let resp = self
            .http
            .request(first)
            .await
            .map_err(|e| Error::transport(format!("shelly rpc: {e}")))?;
        let resp = if resp.status == 401 && !self.user.is_empty() {
            let challenge = resp
                .headers
                .iter()
                .find(|(k, _)| k.eq_ignore_ascii_case("www-authenticate"))
                .map(|(_, v)| v.clone())
                .unwrap_or_default();
            let auth = digest_auth_header(
                &challenge,
                &self.user,
                &self.pass_,
                "GET",
                &format!("/rpc{path}"),
            )?;
            let mut req = HttpRequest::get(&url).with_timeout(Duration::from_secs(10));
            req = req.with_header("Authorization", auth);
            self.http
                .request(req)
                .await
                .map_err(|e| Error::transport(format!("shelly rpc retry: {e}")))?
        } else {
            resp
        };
        if !resp.is_success() {
            return Err(Error::transport(format!("shelly HTTP {}", resp.status)));
        }
        serde_json::from_slice(&resp.body)
            .map_err(|e| Error::decode(format!("shelly rpc json: {e}")))
    }
}

/// Build an HTTP Digest `Authorization` header from a `WWW-Authenticate`
/// challenge string. Handles MD5-only (the algorithm Shelly devices use)
/// and the `qop=auth` form they emit.
fn digest_auth_header(
    challenge: &str,
    user: &str,
    pass: &str,
    method: &str,
    uri: &str,
) -> Result<String> {
    use md5::{Digest, Md5};
    use rand::Rng;
    if !challenge.to_ascii_lowercase().contains("digest") {
        return Err(Error::transport(format!(
            "shelly: unexpected challenge {challenge:?}"
        )));
    }
    let kv = parse_challenge(challenge);
    let realm = kv.get("realm").map(String::as_str).unwrap_or("");
    let nonce = kv.get("nonce").map(String::as_str).unwrap_or("");
    let qop = kv.get("qop").map(String::as_str).unwrap_or("");
    let algorithm = kv
        .get("algorithm")
        .map(String::as_str)
        .unwrap_or("MD5")
        .to_uppercase();
    if algorithm != "MD5" && algorithm != "MD5-SESS" {
        return Err(Error::transport(format!(
            "shelly: unsupported digest algorithm {algorithm}"
        )));
    }
    let mut h1 = Md5::new();
    h1.update(format!("{user}:{realm}:{pass}").as_bytes());
    let ha1 = hex::encode(h1.finalize());
    let mut h2 = Md5::new();
    h2.update(format!("{method}:{uri}").as_bytes());
    let ha2 = hex::encode(h2.finalize());

    let nc = "00000001";
    let mut rng = rand::thread_rng();
    let cnonce: String = (0..16)
        .map(|_| {
            let n: u8 = rng.gen_range(0..16);
            std::char::from_digit(n as u32, 16).unwrap()
        })
        .collect();

    let response = if qop.is_empty() {
        let mut h = Md5::new();
        h.update(format!("{ha1}:{nonce}:{ha2}").as_bytes());
        hex::encode(h.finalize())
    } else {
        let mut h = Md5::new();
        h.update(format!("{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}").as_bytes());
        hex::encode(h.finalize())
    };

    let mut out = format!(
        r#"Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}", algorithm={algorithm}"#
    );
    if !qop.is_empty() {
        out.push_str(&format!(r#", qop=auth, nc={nc}, cnonce="{cnonce}""#));
    }
    if let Some(opaque) = kv.get("opaque") {
        out.push_str(&format!(r#", opaque="{opaque}""#));
    }
    Ok(out)
}

fn parse_challenge(s: &str) -> std::collections::HashMap<String, String> {
    let mut out = std::collections::HashMap::new();
    let body = s
        .trim()
        .strip_prefix("Digest ")
        .or_else(|| s.trim().strip_prefix("digest "))
        .unwrap_or(s.trim());
    let mut i = 0;
    let bytes = body.as_bytes();
    while i < bytes.len() {
        // skip whitespace + comma
        while i < bytes.len() && matches!(bytes[i], b' ' | b'\t' | b',') {
            i += 1;
        }
        // read key
        let key_start = i;
        while i < bytes.len() && bytes[i] != b'=' && bytes[i] != b',' {
            i += 1;
        }
        if i >= bytes.len() {
            break;
        }
        let key = body[key_start..i].trim().to_ascii_lowercase();
        if bytes[i] != b'=' {
            // valueless flag; skip.
            continue;
        }
        i += 1; // consume '='
                // read value (quoted or token)
        let value = if i < bytes.len() && bytes[i] == b'"' {
            i += 1;
            let v_start = i;
            while i < bytes.len() && bytes[i] != b'"' {
                i += 1;
            }
            let v = body[v_start..i].to_string();
            if i < bytes.len() {
                i += 1; // skip closing quote
            }
            v
        } else {
            let v_start = i;
            while i < bytes.len() && bytes[i] != b',' {
                i += 1;
            }
            body[v_start..i].trim().to_string()
        };
        out.insert(key, value);
    }
    out
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_digest_challenge_basic() {
        let kv =
            parse_challenge(r#"Digest realm="shelly", nonce="abc", qop="auth", algorithm=MD5"#);
        assert_eq!(kv.get("realm").map(String::as_str), Some("shelly"));
        assert_eq!(kv.get("nonce").map(String::as_str), Some("abc"));
        assert_eq!(kv.get("qop").map(String::as_str), Some("auth"));
        assert_eq!(kv.get("algorithm").map(String::as_str), Some("MD5"));
    }

    #[test]
    fn digest_header_matches_rfc_example() {
        let header = digest_auth_header(
            r#"Digest realm="testrealm@host.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", qop=auth, algorithm=MD5"#,
            "Mufasa",
            "Circle Of Life",
            "GET",
            "/dir/index.html",
        )
        .unwrap();
        assert!(header.starts_with("Digest username=\"Mufasa\""));
        assert!(header.contains("nonce=\"dcd98b7102dd2f0e8b11d0f600bfb0c093\""));
        assert!(header.contains("qop=auth"));
    }
}
