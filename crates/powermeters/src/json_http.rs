//! `JSON_HTTP` powermeter — generic JSON-over-HTTP endpoint with JSONPath
//! extraction. Port of `src/astrameter/powermeter/json_http.py`.

use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    http::{HttpClient, HttpRequest},
    Platform,
};
use async_trait::async_trait;
use jsonpath_rust::JsonPathQuery;
use serde_json::Value;

pub struct JsonHttpPowermeter {
    url: String,
    json_paths: Vec<String>,
    username: Option<String>,
    password: Option<String>,
    headers: Vec<(String, String)>,
    http: Arc<dyn HttpClient>,
}

impl JsonHttpPowermeter {
    pub fn new(
        url: String,
        json_paths: Vec<String>,
        username: Option<String>,
        password: Option<String>,
        headers: Vec<(String, String)>,
        http: Arc<dyn HttpClient>,
    ) -> Self {
        Self {
            url,
            json_paths,
            username,
            password,
            headers,
            http,
        }
    }

    async fn fetch_json(&self) -> Result<Value> {
        let mut req = HttpRequest::get(&self.url).with_timeout(Duration::from_secs(10));
        for (k, v) in &self.headers {
            req = req.with_header(k, v);
        }
        if let (Some(u), Some(p)) = (&self.username, &self.password) {
            req = req.with_basic_auth(u, p);
        } else if let Some(u) = &self.username {
            req = req.with_basic_auth(u, "");
        } else if let Some(p) = &self.password {
            req = req.with_basic_auth("", p);
        }
        let resp = self
            .http
            .request(req)
            .await
            .map_err(|e| Error::transport(format!("HTTP request error: {e}")))?;
        if !resp.is_success() {
            return Err(Error::transport(format!("HTTP {}", resp.status)));
        }
        serde_json::from_slice::<Value>(&resp.body)
            .map_err(|e| Error::decode(format!("Invalid JSON response: {e}")))
    }
}

#[async_trait]
impl Powermeter for JsonHttpPowermeter {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let data = self.fetch_json().await?;
        let mut values = Vec::with_capacity(self.json_paths.len());
        for path in &self.json_paths {
            let matches = data
                .clone()
                .path(path)
                .map_err(|e| Error::decode(format!("JSONPath error for {path:?}: {e}")))?;
            let first = match &matches {
                Value::Array(arr) => arr.first().ok_or_else(|| {
                    Error::decode(format!("No match found for JSON path {path:?}"))
                })?,
                other => other,
            };
            values.push(value_to_f64(first)?);
        }
        Ok(values)
    }
}

pub(crate) fn value_to_f64(v: &Value) -> Result<f64> {
    match v {
        Value::Number(n) => n
            .as_f64()
            .ok_or_else(|| Error::decode(format!("number not representable as f64: {v}"))),
        Value::String(s) => parse_numeric_string(s)
            .ok_or_else(|| Error::decode(format!("string {s:?} not a float"))),
        Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        _ => Err(Error::decode(format!("can't coerce {v} to f64"))),
    }
}

/// Lenient float parser: tries the whole string first, then strips a
/// trailing unit suffix (everything that isn't a digit, `.`, `+`, `-` or
/// `e`/`E`). This mirrors Python's `jsonpath_ng.ext` ``.sub(/[^0-9.\-]+$/, )``
/// pattern that Tasmota / SMA-derived endpoints expect.
fn parse_numeric_string(raw: &str) -> Option<f64> {
    let trimmed = raw.trim();
    if let Ok(v) = trimmed.parse::<f64>() {
        return Some(v);
    }
    // Find the longest prefix that parses as a float — skip past leading
    // sign, then walk while the char looks numeric.
    let mut end = 0;
    let chars: Vec<char> = trimmed.chars().collect();
    while end < chars.len() {
        let c = chars[end];
        let in_numeric =
            c.is_ascii_digit() || c == '.' || c == '-' || c == '+' || c == 'e' || c == 'E';
        if !in_numeric {
            break;
        }
        end += 1;
    }
    if end == 0 {
        return None;
    }
    let head: String = chars[..end].iter().collect();
    head.trim().parse::<f64>().ok()
}

/// Parse a `HEADERS = "Name: value; Other: x"` string into key/value pairs.
pub(crate) fn parse_headers(raw: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for part in raw.split(';') {
        if let Some((k, v)) = part.split_once(':') {
            let k = k.trim();
            let v = v.trim();
            if !k.is_empty() {
                out.push((k.to_string(), v.to_string()));
            }
        }
    }
    out
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let url = section.get_required("URL")?.to_string();
    let paths_raw = section.get_str("JSON_PATHS", "");
    let json_paths: Vec<String> = paths_raw
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect();
    if json_paths.is_empty() {
        return Err(Error::config(format!(
            "section [{}] requires at least one JSON_PATHS entry",
            section.name()
        )));
    }
    let username = section.get_opt_string("USERNAME");
    let password = section.get_opt_string("PASSWORD");
    let headers_raw = section.get_str("HEADERS", "");
    let headers = parse_headers(headers_raw);
    Ok(Arc::new(JsonHttpPowermeter::new(
        url,
        json_paths,
        username,
        password,
        headers,
        platform.http.clone(),
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_plain_float() {
        assert_eq!(parse_numeric_string("331.74"), Some(331.74));
        assert_eq!(parse_numeric_string("-12"), Some(-12.0));
    }

    #[test]
    fn strips_trailing_unit() {
        // Matches Python ``$.state.`sub(/[^0-9.\\-]+$/, )` `` behaviour.
        assert_eq!(parse_numeric_string("331.74 W"), Some(331.74));
        assert_eq!(parse_numeric_string("100kWh"), Some(100.0));
    }

    #[test]
    fn rejects_non_numeric() {
        assert!(parse_numeric_string("hello").is_none());
        assert!(parse_numeric_string("").is_none());
    }

    #[test]
    fn coerces_bool_and_number() {
        assert_eq!(value_to_f64(&serde_json::json!(true)).unwrap(), 1.0);
        assert_eq!(value_to_f64(&serde_json::json!(42)).unwrap(), 42.0);
        assert_eq!(value_to_f64(&serde_json::json!("2.5")).unwrap(), 2.5);
    }
}
