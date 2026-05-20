//! HTTP client backed by `esp_idf_svc::http::client::EspHttpConnection`.
//! Synchronous under the hood.
//!
//! We deliberately do NOT use `tokio::task::spawn_blocking`. The IDF
//! pthread layer always allocates task stacks from internal SRAM
//! (`xTaskCreatePinnedToCore`, not the *WithCaps* variant — so
//! `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY` doesn't help). A
//! mbedTLS RSA verify during an HTTPS handshake needs ~24 KiB of
//! stack, and committing a 24 KiB internal block in the tokio
//! blocking pool every time we do HTTP exhausts the internal heap
//! when Wi-Fi, lwIP, mbedTLS scratch, and the long-lived CT002 UDP
//! recv blocking thread are already resident.
//!
//! Instead spawn a dedicated FreeRTOS task per request via
//! `xTaskCreatePinnedToCoreWithCaps(..., MALLOC_CAP_SPIRAM)`, which
//! does honour PSRAM placement. The 24 KiB stack costs nothing on
//! our 8 MiB PSRAM. The task vTaskDelete()s itself once it sends
//! the result back over a `std::sync::mpsc` channel.

use std::sync::mpsc::sync_channel;
use std::time::Duration;

use astrameter_platform::http::{HttpClient, HttpError, HttpMethod, HttpRequest, HttpResponse};
use async_trait::async_trait;
use embedded_svc::http::client::Client;
use esp_idf_svc::http::client::{Configuration, EspHttpConnection};

pub struct EspHttpClient;

#[async_trait]
impl HttpClient for EspHttpClient {
    async fn request(&self, req: HttpRequest) -> Result<HttpResponse, HttpError> {
        log_heap_before_dispatch(&req);
        // Run the blocking call on a dedicated PSRAM-stack FreeRTOS
        // task so the tokio blocking pool doesn't have to size for
        // mbedTLS' RSA-verify-heavy handshake. Block the calling
        // tokio task on the result via `spawn_blocking` (cheap — the
        // closure just `recv()`s a `Result<…>` immediately).
        tokio::task::spawn_blocking(move || dispatch_on_psram_task(req))
            .await
            .map_err(|e| HttpError::Other(format!("spawn_blocking(join): {e}")))?
    }
}

/// Log internal-SRAM state before each request so we can correlate
/// memory pressure against HTTP activity.
fn log_heap_before_dispatch(req: &HttpRequest) {
    use esp_idf_svc::sys::{
        heap_caps_get_free_size, heap_caps_get_largest_free_block, MALLOC_CAP_INTERNAL,
    };
    let (int_free, int_largest) = unsafe {
        (
            heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
            heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
        )
    };
    let method = match req.method {
        HttpMethod::Get => "GET",
        HttpMethod::Post => "POST",
        HttpMethod::Put => "PUT",
        HttpMethod::Delete => "DELETE",
    };
    log::info!(
        "http_impl: dispatch (PSRAM task) for {method} {} (internal free={int_free} largest={int_largest})",
        req.url
    );
}

/// Spawn a sacrificial FreeRTOS task with a 24 KiB stack allocated
/// from PSRAM, run the sync HTTP request inside it, and ferry the
/// result back to the caller via a `std::sync::mpsc::sync_channel`.
/// The task `vTaskDelete`s itself after sending the result.
fn dispatch_on_psram_task(req: HttpRequest) -> Result<HttpResponse, HttpError> {
    use esp_idf_svc::sys;

    type DispatchResult = Result<HttpResponse, HttpError>;

    struct Args {
        req: HttpRequest,
        tx: std::sync::mpsc::SyncSender<DispatchResult>,
    }

    extern "C" fn task_entry(arg: *mut std::ffi::c_void) {
        // SAFETY: `arg` is the `Box::into_raw` pointer the spawner
        // handed us; we take ownership and the box is dropped at
        // end of scope.
        let args: Box<Args> = unsafe { Box::from_raw(arg as *mut Args) };
        let Args { req, tx } = *args;
        let result = blocking_request(req);
        let _ = tx.send(result);
        // SAFETY: `NULL` to `vTaskDelete` deletes the calling task.
        // After this point the stack is reclaimed by the IDF idle
        // task; nothing on this stack is referenced anywhere else.
        unsafe { sys::vTaskDelete(std::ptr::null_mut()) };
    }

    let (tx, rx) = sync_channel(1);
    let args = Box::new(Args { req, tx });
    let arg_ptr = Box::into_raw(args) as *mut std::ffi::c_void;

    let mut handle: sys::TaskHandle_t = std::ptr::null_mut();
    let rc = unsafe {
        sys::xTaskCreatePinnedToCoreWithCaps(
            Some(task_entry),
            b"http-req\0".as_ptr() as *const _,
            24 * 1024,
            arg_ptr,
            5,
            &mut handle,
            // `tskNO_AFFINITY` is `(BaseType_t)0x7FFFFFFF` in
            // FreeRTOS; bindgen mangles it inconsistently across
            // IDF versions, so use the literal directly.
            0x7FFF_FFFFi32,
            sys::MALLOC_CAP_SPIRAM | sys::MALLOC_CAP_8BIT,
        )
    };
    if rc != 1 {
        // pdPASS == 1; reclaim the box on failure so we don't leak.
        unsafe { drop(Box::from_raw(arg_ptr as *mut Args)) };
        return Err(HttpError::Other(format!(
            "xTaskCreatePinnedToCoreWithCaps(http-req): rc={rc}"
        )));
    }

    rx.recv()
        .map_err(|e| HttpError::Other(format!("recv http-req: {e}")))?
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
