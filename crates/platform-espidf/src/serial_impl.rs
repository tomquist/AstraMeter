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
use esp_idf_svc::sys::{
    self, uart_driver_install, uart_param_config, uart_port_t, uart_read_bytes, uart_set_pin,
    UART_PIN_NO_CHANGE,
};
use parking_lot::Mutex;
use std::collections::HashMap;
use std::os::raw::c_void;
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
pub fn register_uart(name: &str, uart: Arc<dyn UartLike>) {
    let mut guard = UART_REGISTRY.lock();
    let map = guard.get_or_insert_with(HashMap::new);
    map.insert(name.to_string(), uart);
}

/// Install the IDF UART driver for `uart_index` (0/1/2) at `baud_rate`
/// 8N1 on the given GPIO pin numbers, and return a [`UartLike`] that
/// `register_uart` can publish into the registry. Pin numbers come
/// straight from INI config (e.g. `[SML] SML_RX_GPIO=20`), so we
/// route through the raw IDF FFI to avoid the HAL's per-pin
/// type-state. The driver is installed once per `uart_index`; calling
/// twice with the same index returns the new wrapper but the IDF
/// state is shared (the second `uart_driver_install` will fail with
/// `ESP_ERR_INVALID_STATE`, which we treat as already-installed).
pub fn build_uart_driver(
    uart_index: u8,
    baud_rate: u32,
    rx_gpio: i32,
    tx_gpio: i32,
) -> Result<Arc<dyn UartLike>, String> {
    let port: uart_port_t = uart_index as uart_port_t;
    let cfg = sys::uart_config_t {
        baud_rate: baud_rate as i32,
        data_bits: sys::uart_word_length_t_UART_DATA_8_BITS,
        parity: sys::uart_parity_t_UART_PARITY_DISABLE,
        stop_bits: sys::uart_stop_bits_t_UART_STOP_BITS_1,
        flow_ctrl: sys::uart_hw_flowcontrol_t_UART_HW_FLOWCTRL_DISABLE,
        rx_flow_ctrl_thresh: 0,
        source_clk: sys::uart_sclk_t_UART_SCLK_DEFAULT,
        ..Default::default()
    };
    unsafe {
        let rc = uart_param_config(port, &cfg);
        if rc != sys::ESP_OK {
            return Err(format!("uart_param_config(uart{uart_index}): rc={rc}"));
        }
        let rc = uart_set_pin(
            port,
            tx_gpio,
            rx_gpio,
            UART_PIN_NO_CHANGE,
            UART_PIN_NO_CHANGE,
        );
        if rc != sys::ESP_OK {
            return Err(format!(
                "uart_set_pin(uart{uart_index}, tx={tx_gpio}, rx={rx_gpio}): rc={rc}"
            ));
        }
        // 2 KB RX buffer is plenty for SML frames (a typical OBIS push
        // is ~500 B). No TX buffer — SML is read-only.
        let rc = uart_driver_install(port, 2048, 0, 0, std::ptr::null_mut(), 0);
        if rc != sys::ESP_OK && rc != sys::ESP_ERR_INVALID_STATE as i32 {
            return Err(format!("uart_driver_install(uart{uart_index}): rc={rc}"));
        }
    }
    log::info!(
        "UART{uart_index} configured @ {baud_rate} 8N1 (rx=GPIO{rx_gpio}, tx=GPIO{tx_gpio})"
    );
    Ok(Arc::new(FfiUart { port }))
}

struct FfiUart {
    port: uart_port_t,
}

unsafe impl Send for FfiUart {}
unsafe impl Sync for FfiUart {}

impl UartLike for FfiUart {
    fn read(&self, buf: &mut [u8], timeout_ms: u32) -> Result<usize, String> {
        // `uart_read_bytes` uses FreeRTOS ticks. portTICK_PERIOD_MS is
        // 10 on the default 100 Hz tick, but `pdMS_TO_TICKS` is
        // unavailable in this binding; compute the equivalent using
        // CONFIG_FREERTOS_HZ baked into esp-idf-sys (always 100 on
        // ESP-IDF v5 unless the user overrode it).
        let ticks_per_ms = (sys::CONFIG_FREERTOS_HZ / 1000).max(1);
        let ticks = (timeout_ms * ticks_per_ms) as u32;
        let n = unsafe {
            uart_read_bytes(
                self.port,
                buf.as_mut_ptr() as *mut c_void,
                buf.len() as u32,
                ticks,
            )
        };
        if n < 0 {
            return Err(format!("uart_read_bytes returned {n}"));
        }
        Ok(n as usize)
    }
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
