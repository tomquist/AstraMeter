//! WebSocket client — same `tokio-tungstenite` as host. Mirrors the host
//! implementation: builds a rustls connector when the request asks for a
//! custom CA, SNI override, or disabled verification (HomeWizard needs all
//! three). Without those, falls back to the default rustls connector.

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
                .map_err(|e| WsError::Connect(format!("bad header {name:?}: {e}")))?;
            let hval = value
                .parse::<tokio_tungstenite::tungstenite::http::HeaderValue>()
                .map_err(|e| WsError::Connect(format!("bad value {value:?}: {e}")))?;
            request.headers_mut().insert(hname, hval);
        }

        let connector =
            if req.extra_root_cert_pem.is_some() || req.sni_override.is_some() || !req.verify_tls {
                Some(Connector::Rustls(Arc::new(build_rustls_config(&req)?)))
            } else {
                None
            };

        if let Some(sni) = &req.sni_override {
            let original = request.uri().clone();
            let scheme = original
                .scheme_str()
                .ok_or_else(|| WsError::Connect("ws URI missing scheme".into()))?;
            let path_q = original
                .path_and_query()
                .map(|p| p.as_str().to_string())
                .unwrap_or_else(|| "/".to_string());
            let port = original.port().map(|p| format!(":{p}")).unwrap_or_default();
            let new_uri_str = format!("{scheme}://{sni}{port}{path_q}");
            let new_uri = new_uri_str
                .parse::<tokio_tungstenite::tungstenite::http::Uri>()
                .map_err(|e| WsError::Connect(format!("rebuild URI for SNI: {e}")))?;
            *request.uri_mut() = new_uri;
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
        for ta in webpki_roots::TLS_SERVER_ROOTS.iter() {
            roots.roots.push(ta.clone());
        }
        if let Some(pem) = &req.extra_root_cert_pem {
            let mut reader = std::io::Cursor::new(pem);
            for cert in rustls_pemfile::certs(&mut reader) {
                if let Ok(c) = cert {
                    let _ = roots.add(c);
                }
            }
        }
        Ok(rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth())
    } else {
        Ok(rustls::ClientConfig::builder()
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(NoCertVerify))
            .with_no_client_auth())
    }
}

#[derive(Debug)]
struct NoCertVerify;

impl rustls::client::danger::ServerCertVerifier for NoCertVerify {
    fn verify_server_cert(
        &self,
        _: &rustls::pki_types::CertificateDer<'_>,
        _: &[rustls::pki_types::CertificateDer<'_>],
        _: &rustls::pki_types::ServerName<'_>,
        _: &[u8],
        _: rustls::pki_types::UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        _: &[u8],
        _: &rustls::pki_types::CertificateDer<'_>,
        _: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn verify_tls13_signature(
        &self,
        _: &[u8],
        _: &rustls::pki_types::CertificateDer<'_>,
        _: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        Ok(rustls::client::danger::HandshakeSignatureValid::assertion())
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}
