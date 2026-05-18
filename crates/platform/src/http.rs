//! HTTP client trait.

use async_trait::async_trait;
use serde::Serialize;
use std::time::Duration;

/// Single-request HTTP client. Concrete impls live in `platform-std`
/// (reqwest) and `platform-espidf` (`EspHttpConnection`).
#[derive(Debug, Clone, Serialize)]
pub struct HttpRequest {
    pub method: HttpMethod,
    pub url: String,
    pub headers: Vec<(String, String)>,
    /// Optional HTTP basic auth (user, pass).
    pub basic_auth: Option<(String, String)>,
    pub body: Option<Vec<u8>>,
    pub timeout: Duration,
    /// When `false`, accept self-signed certs (only used by `envoy` /
    /// HomeWizard `VERIFY_SSL=False`).
    pub verify_tls: bool,
    /// Additional root certificates (PEM bytes). Used by HomeWizard.
    pub extra_root_cert_pem: Option<Vec<u8>>,
}

impl HttpRequest {
    pub fn get(url: impl Into<String>) -> Self {
        Self {
            method: HttpMethod::Get,
            url: url.into(),
            headers: Vec::new(),
            basic_auth: None,
            body: None,
            timeout: Duration::from_secs(10),
            verify_tls: true,
            extra_root_cert_pem: None,
        }
    }

    pub fn post(url: impl Into<String>, body: Vec<u8>) -> Self {
        Self {
            method: HttpMethod::Post,
            url: url.into(),
            headers: Vec::new(),
            basic_auth: None,
            body: Some(body),
            timeout: Duration::from_secs(10),
            verify_tls: true,
            extra_root_cert_pem: None,
        }
    }

    pub fn with_basic_auth(mut self, user: impl Into<String>, pass: impl Into<String>) -> Self {
        self.basic_auth = Some((user.into(), pass.into()));
        self
    }

    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum HttpMethod {
    Get,
    Post,
    Put,
    Delete,
}

#[derive(Debug, Clone)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn is_success(&self) -> bool {
        self.status >= 200 && self.status < 300
    }

    pub fn text(&self) -> Result<&str, std::str::Utf8Error> {
        std::str::from_utf8(&self.body)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum HttpError {
    #[error("HTTP {0}: {1}")]
    Status(u16, String),
    #[error("connection error: {0}")]
    Connect(String),
    #[error("timeout after {millis}ms")]
    Timeout { millis: u64 },
    #[error("body decode error: {0}")]
    Decode(String),
    #[error("HTTP error: {0}")]
    Other(String),
}

#[async_trait]
pub trait HttpClient: Send + Sync {
    async fn request(&self, req: HttpRequest) -> Result<HttpResponse, HttpError>;
}
