//! HTTP client backed by `esp_idf_svc::http::client::EspHttpConnection`.
//! Synchronous under the hood; we wrap each call in `spawn_blocking` so
//! the tokio reactor can keep running other tasks.

use astrameter_platform::http::{HttpClient, HttpError, HttpMethod, HttpRequest, HttpResponse};
use async_trait::async_trait;
use embedded_svc::http::client::Client;
use esp_idf_svc::http::client::{Configuration, EspHttpConnection};
use std::time::Duration;

pub struct EspHttpClient;

#[async_trait]
impl HttpClient for EspHttpClient {
    async fn request(&self, req: HttpRequest) -> Result<HttpResponse, HttpError> {
        tokio::task::spawn_blocking(move || blocking_request(req))
            .await
            .map_err(|e| HttpError::Other(format!("spawn_blocking: {e}")))?
    }
}

fn blocking_request(req: HttpRequest) -> Result<HttpResponse, HttpError> {
    // mbedTLS lets us set: (a) the global crt bundle, (b) extra single
    // certificate pinned to this request, (c) `use_global_ca_store` OFF
    // for verify-disabled mode. `Configuration` exposes all three.
    //
    // esp-idf-svc 0.52 changed `client_certificate` from `Option<CString>`
    // to `Option<X509<'static>>` — `X509::pem_until_nul` takes a NUL-
    // terminated PEM slice and stores it as &CStr internally. We have to
    // leak the buffer because the X509 borrows from it for the request
    // duration; one-extra-PEM-per-request is fine for the typical use
    // case (one HomeWizard meter pinned for the life of the process).
    let pem_static: Option<&'static [u8]> =
        req.extra_root_cert_pem
            .as_ref()
            .map(|pem| -> &'static [u8] {
                let mut owned = pem.clone();
                if owned.last() != Some(&0) {
                    owned.push(0);
                }
                Box::leak(owned.into_boxed_slice())
            });
    let cfg = Configuration {
        crt_bundle_attach: if req.verify_tls && req.extra_root_cert_pem.is_none() {
            Some(esp_idf_svc::sys::esp_crt_bundle_attach)
        } else {
            None
        },
        client_certificate: pem_static.map(|pem| esp_idf_svc::tls::X509::pem_until_nul(pem)),
        use_global_ca_store: req.verify_tls,
        // `crt_bundle_attach=None + use_global_ca_store=false` disables
        // server certificate validation in mbedtls.
        timeout: Some(req.timeout),
        ..Default::default()
    };

    let conn = EspHttpConnection::new(&cfg)
        .map_err(|e| HttpError::Other(format!("EspHttpConnection::new: {e}")))?;
    let mut client = Client::wrap(conn);
    let method = match req.method {
        HttpMethod::Get => embedded_svc::http::Method::Get,
        HttpMethod::Post => embedded_svc::http::Method::Post,
        HttpMethod::Put => embedded_svc::http::Method::Put,
        HttpMethod::Delete => embedded_svc::http::Method::Delete,
    };
    let mut headers: Vec<(String, String)> = req.headers.clone();
    if let Some((u, p)) = req.basic_auth {
        let creds = format!("{u}:{p}");
        let encoded = base64encode(creds.as_bytes());
        headers.push(("Authorization".into(), format!("Basic {encoded}")));
    }
    let header_refs: Vec<(&str, &str)> = headers
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();

    let mut request = client
        .request(method, req.url.as_str(), &header_refs)
        .map_err(|e| HttpError::Connect(format!("request: {e}")))?;
    if let Some(body) = req.body {
        use embedded_svc::io::Write;
        request
            .write_all(&body)
            .map_err(|e| HttpError::Other(format!("write body: {e}")))?;
    }
    let mut response = request
        .submit()
        .map_err(|e| HttpError::Connect(format!("submit: {e}")))?;
    let status = response.status();
    let mut body = Vec::new();
    let mut buf = [0u8; 1024];
    loop {
        let n = response
            .read(&mut buf)
            .map_err(|e| HttpError::Decode(format!("read: {e}")))?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&buf[..n]);
    }
    Ok(HttpResponse {
        status,
        headers: Vec::new(),
        body,
    })
}

fn base64encode(data: &[u8]) -> String {
    use std::fmt::Write;
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(((data.len() + 2) / 3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = chunk.get(1).copied().unwrap_or(0) as u32;
        let b2 = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        let _ = write!(out, "{}", TABLE[((triple >> 18) & 0x3F) as usize] as char);
        let _ = write!(out, "{}", TABLE[((triple >> 12) & 0x3F) as usize] as char);
        let _ = write!(
            out,
            "{}",
            if chunk.len() > 1 {
                TABLE[((triple >> 6) & 0x3F) as usize] as char
            } else {
                '='
            }
        );
        let _ = write!(
            out,
            "{}",
            if chunk.len() > 2 {
                TABLE[(triple & 0x3F) as usize] as char
            } else {
                '='
            }
        );
    }
    let _ = Duration::from_secs(0);
    out
}
