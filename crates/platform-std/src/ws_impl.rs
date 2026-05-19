use astrameter_platform::ws::{WebSocketClient, WsConnection, WsError, WsMessage, WsRequest};
use async_trait::async_trait;
use futures::{SinkExt, StreamExt};
use std::sync::Arc;
use tokio_tungstenite::{
    tungstenite::{client::IntoClientRequest, Message},
    Connector, MaybeTlsStream, WebSocketStream,
};

pub struct TungsteniteClient;

impl Default for TungsteniteClient {
    fn default() -> Self {
        Self::new()
    }
}

impl TungsteniteClient {
    pub fn new() -> Self {
        Self
    }
}

struct TungsteniteConn {
    stream: WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>,
}

#[async_trait]
impl WsConnection for TungsteniteConn {
    async fn send(&mut self, msg: WsMessage) -> Result<(), WsError> {
        let tm = match msg {
            WsMessage::Text(s) => Message::Text(s),
            WsMessage::Binary(b) => Message::Binary(b),
            WsMessage::Ping(b) => Message::Ping(b),
            WsMessage::Pong(b) => Message::Pong(b),
            WsMessage::Close => Message::Close(None),
        };
        self.stream
            .send(tm)
            .await
            .map_err(|e| WsError::Protocol(e.to_string()))
    }

    async fn recv(&mut self) -> Result<WsMessage, WsError> {
        loop {
            let msg = self
                .stream
                .next()
                .await
                .ok_or(WsError::Closed)?
                .map_err(|e| WsError::Protocol(e.to_string()))?;
            return Ok(match msg {
                Message::Text(s) => WsMessage::Text(s),
                Message::Binary(b) => WsMessage::Binary(b),
                Message::Ping(b) => WsMessage::Ping(b),
                Message::Pong(b) => WsMessage::Pong(b),
                Message::Close(_) => WsMessage::Close,
                Message::Frame(_) => continue,
            });
        }
    }

    async fn close(&mut self) -> Result<(), WsError> {
        self.stream
            .close(None)
            .await
            .map_err(|e| WsError::Protocol(e.to_string()))
    }
}

#[async_trait]
impl WebSocketClient for TungsteniteClient {
    async fn connect(&self, req: WsRequest) -> Result<Box<dyn WsConnection>, WsError> {
        let mut request = req
            .url
            .as_str()
            .into_client_request()
            .map_err(|e| WsError::Connect(format!("bad ws url: {e}")))?;
        for (name, value) in &req.headers {
            let hname = name
                .parse::<tokio_tungstenite::tungstenite::http::HeaderName>()
                .map_err(|e| WsError::Connect(format!("bad header name {name:?}: {e}")))?;
            let hval = value
                .parse::<tokio_tungstenite::tungstenite::http::HeaderValue>()
                .map_err(|e| WsError::Connect(format!("bad header value {value:?}: {e}")))?;
            request.headers_mut().insert(hname, hval);
        }

        // Build a rustls connector only when we need to override defaults
        // (custom CA, disabled verification, or pinned SNI). HomeWizard
        // requires all three; HomeAssistant typically uses defaults.
        let connector =
            if req.extra_root_cert_pem.is_some() || req.sni_override.is_some() || !req.verify_tls {
                Some(Connector::Rustls(Arc::new(build_rustls_config(&req)?)))
            } else {
                None
            };

        // For `sni_override` we replace the URL's host with the SNI value when
        // building the request so rustls uses that for SNI; the original IP
        // host stays in the Host header (already set by IntoClientRequest).
        // tokio-tungstenite's connect_async_tls_with_config takes the request
        // host for SNI, so we patch the URI here.
        if let Some(sni) = &req.sni_override {
            // Build a new URI keeping scheme/path but swapping authority host.
            let original = request.uri().clone();
            let scheme = original
                .scheme_str()
                .ok_or_else(|| WsError::Connect("ws URI missing scheme".into()))?;
            let path_q = original
                .path_and_query()
                .map(|p| p.as_str().to_string())
                .unwrap_or_else(|| "/".to_string());
            let port = original
                .port()
                .map(|p| format!(":{}", p))
                .unwrap_or_default();
            let new_uri_str = format!("{scheme}://{sni}{port}{path_q}");
            let new_uri = new_uri_str
                .parse::<tokio_tungstenite::tungstenite::http::Uri>()
                .map_err(|e| WsError::Connect(format!("rebuild URI for SNI: {e}")))?;
            *request.uri_mut() = new_uri;
            // Restore the original Host header so the server-side virtual
            // routing still works (HomeWizard's dongle binds to its IP but
            // serves an SNI-validated cert that matches "appliance/...").
            if let Some(orig_host) = original.host() {
                let host_value = match original.port_u16() {
                    Some(p) => format!("{orig_host}:{p}"),
                    None => orig_host.to_string(),
                };
                if let Ok(hv) = host_value.parse() {
                    request
                        .headers_mut()
                        .insert(tokio_tungstenite::tungstenite::http::header::HOST, hv);
                }
            }
        }

        let (stream, _resp) =
            tokio_tungstenite::connect_async_tls_with_config(request, None, false, connector)
                .await
                .map_err(|e| WsError::Connect(e.to_string()))?;
        Ok(Box::new(TungsteniteConn { stream }))
    }
}

fn build_rustls_config(req: &WsRequest) -> Result<rustls::ClientConfig, WsError> {
    let mut roots = rustls::RootCertStore::empty();
    if req.verify_tls {
        // Start from webpki roots so non-custom CAs still validate; add the
        // device cert on top.
        for ta in webpki_roots::TLS_SERVER_ROOTS.iter() {
            roots.roots.push(ta.clone());
        }
        if let Some(pem) = &req.extra_root_cert_pem {
            let added = add_pem_roots(&mut roots, pem)
                .map_err(|e| WsError::Connect(format!("load CA: {e}")))?;
            if added == 0 {
                return Err(WsError::Connect(
                    "no certs found in extra_root_cert_pem".into(),
                ));
            }
        }
        Ok(rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth())
    } else {
        // Verification fully disabled — caller has consented (VERIFY_SSL=False).
        let cfg = rustls::ClientConfig::builder()
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(NoCertVerify))
            .with_no_client_auth();
        Ok(cfg)
    }
}

fn add_pem_roots(roots: &mut rustls::RootCertStore, pem: &[u8]) -> std::io::Result<usize> {
    let mut count = 0;
    let mut reader = std::io::Cursor::new(pem);
    for cert in rustls_pemfile::certs(&mut reader) {
        let cert = cert?;
        if roots.add(cert).is_ok() {
            count += 1;
        }
    }
    Ok(count)
}

#[derive(Debug)]
struct NoCertVerify;

impl rustls::client::danger::ServerCertVerifier for NoCertVerify {
    fn verify_server_cert(
        &self,
        _end_entity: &rustls::pki_types::CertificateDer<'_>,
        _intermediates: &[rustls::pki_types::CertificateDer<'_>],
        _server_name: &rustls::pki_types::ServerName<'_>,
        _ocsp_response: &[u8],
        _now: rustls::pki_types::UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _message: &[u8],
        _cert: &rustls::pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _message: &[u8],
        _cert: &rustls::pki_types::CertificateDer<'_>,
        _dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}
