//! Serial / UART trait. Host backs it with `tokio-serial`; ESP32 with
//! `esp-idf-hal::uart`.

use async_trait::async_trait;

#[derive(Debug, Clone)]
pub struct SerialConfig {
    /// Device path on host (e.g. `/dev/ttyUSB0`) or `UART<n>` on ESP32.
    pub path: String,
    pub baud_rate: u32,
    pub data_bits: u8,
    pub stop_bits: u8,
    pub parity: Parity,
}

impl SerialConfig {
    /// Standard 9600/8N1 used by German SML meters.
    pub fn sml_9600_8n1(path: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            baud_rate: 9600,
            data_bits: 8,
            stop_bits: 1,
            parity: Parity::None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Parity {
    None,
    Odd,
    Even,
}

#[derive(Debug, thiserror::Error)]
pub enum SerialError {
    #[error("serial open error: {0}")]
    Open(String),
    #[error("serial read error: {0}")]
    Read(String),
}

#[async_trait]
pub trait SerialPort: Send + Sync {
    async fn open(&self, cfg: SerialConfig) -> Result<Box<dyn SerialStream>, SerialError>;
}

#[async_trait]
pub trait SerialStream: Send + Sync {
    /// Read up to `buf.len()` bytes. Returns the number actually read.
    async fn read(&mut self, buf: &mut [u8]) -> Result<usize, SerialError>;
}
