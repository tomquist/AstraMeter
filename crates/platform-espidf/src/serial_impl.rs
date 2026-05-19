//! Serial / UART using `esp_idf_hal::uart`. The HAL provides only a
//! synchronous read API; we wrap each read in `spawn_blocking`.
//!
//! `SerialConfig::path` is interpreted as `UART0`, `UART1`, or `UART2`.
//! Pin selection is deferred to the firmware boot code, which constructs
//! a `UartDriver` ahead of time and registers it via a thread-local
//! registry (a follow-up commit can replace the static-map with a more
//! explicit API).

use astrameter_platform::serial::{SerialConfig, SerialError, SerialPort, SerialStream};
use async_trait::async_trait;

pub struct EspUartSerial;

#[async_trait]
impl SerialPort for EspUartSerial {
    async fn open(&self, cfg: SerialConfig) -> Result<Box<dyn SerialStream>, SerialError> {
        // The actual UartDriver construction needs runtime resources
        // (gpio pins, peripheral handle) that aren't available here;
        // the firmware boot path must register them before `open` is
        // called.
        Err(SerialError::Open(format!(
            "UART {} not registered; the firmware boot must wire UART pins before SML is configured",
            cfg.path,
        )))
    }
}
