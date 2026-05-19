//! Serial / UART using `esp_idf_hal::uart`. The HAL provides only a
//! synchronous read API; reads are wrapped in `spawn_blocking`.
//!
//! `SerialConfig::path` is interpreted as the logical UART name
//! (`UART0`/`UART1`/`UART2`). The firmware boot path is responsible for
//! constructing a `UartDriver` for each UART it wants to expose to SML
//! and depositing it into [`UART_REGISTRY`] via [`register_uart`]; the
//! `open` call then plucks the driver out of the registry.

use astrameter_platform::serial::{SerialConfig, SerialError, SerialPort, SerialStream};
use async_trait::async_trait;
use parking_lot::Mutex;
use std::collections::HashMap;
use std::sync::Arc;

/// Owned UART driver, type-erased so the registry stays free of generics.
pub trait UartLike: Send + Sync {
    /// Blocking read up to `buf.len()` bytes. Returns the number actually
    /// read (0 on timeout).
    fn read(&self, buf: &mut [u8], timeout_ms: u32) -> Result<usize, String>;
}

static UART_REGISTRY: Mutex<Option<HashMap<String, Arc<dyn UartLike>>>> = Mutex::new(None);

/// Make a `UartDriver` available to SML/`open` calls. Call once per UART
/// from the firmware boot path before the supervisor starts.
///
/// Currently unused — the boot path in `bins/astrameter-esp32/src/main.rs`
/// doesn't yet wire any UARTs. Keep it public so a future SML/UART path
/// just has to call this from boot.
#[allow(dead_code)]
pub fn register_uart(name: &str, uart: Arc<dyn UartLike>) {
    let mut guard = UART_REGISTRY.lock();
    let map = guard.get_or_insert_with(HashMap::new);
    map.insert(name.to_string(), uart);
}

pub struct EspUartSerial;

struct EspUartStream {
    uart: Arc<dyn UartLike>,
}

#[async_trait]
impl SerialStream for EspUartStream {
    async fn read(&mut self, buf: &mut [u8]) -> Result<usize, SerialError> {
        let uart = self.uart.clone();
        let len = buf.len();
        let res = tokio::task::spawn_blocking(move || {
            let mut local = vec![0u8; len];
            let n = uart.read(&mut local, 1000).map_err(SerialError::Read)?;
            local.truncate(n);
            Ok::<Vec<u8>, SerialError>(local)
        })
        .await
        .map_err(|e| SerialError::Read(format!("spawn_blocking: {e}")))??;
        let n = res.len();
        buf[..n].copy_from_slice(&res);
        Ok(n)
    }
}

#[async_trait]
impl SerialPort for EspUartSerial {
    async fn open(&self, cfg: SerialConfig) -> Result<Box<dyn SerialStream>, SerialError> {
        let map = UART_REGISTRY.lock();
        let Some(map) = map.as_ref() else {
            return Err(SerialError::Open(format!(
                "UART {}: no UARTs registered (firmware must call register_uart() before opening)",
                cfg.path,
            )));
        };
        let Some(uart) = map.get(&cfg.path) else {
            let known: Vec<&String> = map.keys().collect();
            return Err(SerialError::Open(format!(
                "UART {} not registered; known: {known:?}",
                cfg.path,
            )));
        };
        Ok(Box::new(EspUartStream { uart: uart.clone() }))
    }
}
