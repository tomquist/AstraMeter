//! Host implementations of `astrameter-platform` traits.

#![forbid(unsafe_code)]

mod http_impl;
mod mqtt_impl;
mod net_impl;
mod serial_impl;
mod time_impl;
mod ws_impl;

use std::sync::Arc;

use astrameter_platform::Platform;

pub use http_impl::ReqwestHttpClient;
pub use mqtt_impl::RumqttcFactory;
pub use net_impl::{TokioTcpConnect, TokioUdpBind};
pub use serial_impl::TokioSerial;
pub use time_impl::TokioTimer;
pub use ws_impl::TungsteniteClient;

/// Convenience helper: build a fully-wired host platform.
pub fn build_platform() -> Platform {
    Platform {
        http: Arc::new(ReqwestHttpClient::new()),
        mqtt: Arc::new(RumqttcFactory),
        ws: Arc::new(TungsteniteClient::new()),
        udp: Arc::new(TokioUdpBind),
        tcp: Arc::new(TokioTcpConnect),
        serial: Arc::new(TokioSerial),
        timer: Arc::new(TokioTimer),
    }
}
