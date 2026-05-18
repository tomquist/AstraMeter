use astrameter_platform::http::{HttpClient, HttpError, HttpMethod, HttpRequest, HttpResponse};
use async_trait::async_trait;

pub struct ReqwestHttpClient {
    base: reqwest::Client,
}

impl Default for ReqwestHttpClient {
    fn default() -> Self {
        Self::new()
    }
}

impl ReqwestHttpClient {
    pub fn new() -> Self {
        Self {
            base: reqwest::Client::builder()
                .user_agent(concat!("astrameter/", env!("CARGO_PKG_VERSION")))
                .build()
                .expect("reqwest client build"),
        }
    }

    fn client_for(&self, req: &HttpRequest) -> Result<reqwest::Client, HttpError> {
        if req.verify_tls && req.extra_root_cert_pem.is_none() {
            return Ok(self.base.clone());
        }
        let mut builder = reqwest::Client::builder()
            .user_agent(concat!("astrameter/", env!("CARGO_PKG_VERSION")))
            .danger_accept_invalid_certs(!req.verify_tls);
        if let Some(pem) = &req.extra_root_cert_pem {
            let cert = reqwest::Certificate::from_pem(pem)
                .map_err(|e| HttpError::Other(format!("invalid extra root cert: {e}")))?;
            builder = builder.add_root_certificate(cert);
        }
        builder
            .build()
            .map_err(|e| HttpError::Other(format!("reqwest client build: {e}")))
    }
}

#[async_trait]
impl HttpClient for ReqwestHttpClient {
    async fn request(&self, req: HttpRequest) -> Result<HttpResponse, HttpError> {
        let client = self.client_for(&req)?;
        let method = match req.method {
            HttpMethod::Get => reqwest::Method::GET,
            HttpMethod::Post => reqwest::Method::POST,
            HttpMethod::Put => reqwest::Method::PUT,
            HttpMethod::Delete => reqwest::Method::DELETE,
        };
        let mut rb = client.request(method, &req.url).timeout(req.timeout);
        for (name, value) in &req.headers {
            rb = rb.header(name, value);
        }
        if let Some((user, pass)) = &req.basic_auth {
            rb = rb.basic_auth(user, Some(pass));
        }
        if let Some(body) = req.body {
            rb = rb.body(body);
        }

        let resp = rb.send().await.map_err(map_send_error)?;
        let status = resp.status().as_u16();
        let headers = resp
            .headers()
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("").to_string()))
            .collect();
        let body = resp
            .bytes()
            .await
            .map_err(|e| HttpError::Decode(e.to_string()))?
            .to_vec();
        Ok(HttpResponse {
            status,
            headers,
            body,
        })
    }
}

fn map_send_error(e: reqwest::Error) -> HttpError {
    if e.is_timeout() {
        HttpError::Timeout { millis: 0 }
    } else if e.is_connect() {
        HttpError::Connect(e.to_string())
    } else if let Some(status) = e.status() {
        HttpError::Status(status.as_u16(), e.to_string())
    } else {
        HttpError::Other(e.to_string())
    }
}
