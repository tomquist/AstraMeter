//! Platform abstraction layer.
//!
//! Service crates depend on the traits in this crate; concrete impls live in
//! `astrameter-platform-std` (host) and `astrameter-platform-espidf` (ESP32).
//! Binaries assemble a [`Platform`] aggregate at startup and pass it through
//! the service tree as `Arc<Platform>`.

#![forbid(unsafe_code)]

pub mod http;
pub mod mqtt;
pub mod net;
pub mod serial;
pub mod time;
pub mod ws;

use std::sync::Arc;

pub use http::HttpClient;
pub use mqtt::{MqttFactory, MqttOptions};
pub use net::{TcpConnect, UdpBind, UdpSocket};
pub use serial::SerialPort;
pub use time::Timer;
pub use ws::WebSocketClient;

/// Bundle of platform-specific implementations passed through the service tree.
#[derive(Clone)]
pub struct Platform {
    pub http: Arc<dyn HttpClient>,
    pub mqtt: Arc<dyn MqttFactory>,
    pub ws: Arc<dyn WebSocketClient>,
    pub udp: Arc<dyn UdpBind>,
    pub tcp: Arc<dyn TcpConnect>,
    pub serial: Arc<dyn SerialPort>,
    pub timer: Arc<dyn Timer>,
}
