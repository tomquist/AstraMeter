//! `SML` — port of `src/astrameter/powermeter/sml.py`. Reads SML frames from
//! a serial port.

use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{
    serial::{SerialConfig, SerialPort, SerialStream},
    Platform,
};
use astrameter_sml::{OBIS_POWER_CURRENT, OBIS_POWER_L1, OBIS_POWER_L2, OBIS_POWER_L3};
use async_trait::async_trait;
use parking_lot::Mutex;
use tokio::sync::Mutex as AMutex;

pub struct Sml {
    device_path: String,
    obis_current: [u8; 6],
    obis_l1: [u8; 6],
    obis_l2: [u8; 6],
    obis_l3: [u8; 6],
    serial: Arc<dyn SerialPort>,
    stream: AMutex<Option<Box<dyn SerialStream>>>,
    cached: Mutex<Vec<f64>>,
    read_lock: AMutex<()>,
}

fn parse_obis_hex(raw: &str, label: &str) -> Result<[u8; 6]> {
    let s = raw.trim();
    if s.len() != 12 || !s.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(Error::config(format!(
            "{label} must be 12 hex digits, got {raw:?}"
        )));
    }
    let bytes = hex::decode(s.to_lowercase())
        .map_err(|e| Error::config(format!("{label} hex decode: {e}")))?;
    let mut out = [0u8; 6];
    out.copy_from_slice(&bytes);
    Ok(out)
}

#[async_trait]
impl Powermeter for Sml {
    async fn start(&self) -> Result<()> {
        let mut guard = self.stream.lock().await;
        if guard.is_some() {
            return Ok(());
        }
        let cfg = SerialConfig::sml_9600_8n1(&self.device_path);
        let s = self
            .serial
            .open(cfg)
            .await
            .map_err(|e| Error::transport(format!("sml open: {e}")))?;
        *guard = Some(s);
        Ok(())
    }

    async fn stop(&self) -> Result<()> {
        *self.stream.lock().await = None;
        Ok(())
    }

    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        // Coalesce: if a read is in flight, return the cached value rather
        // than blocking — same behaviour as the Python port.
        let lock = match self.read_lock.try_lock() {
            Ok(g) => g,
            Err(_) => return Ok(self.cached.lock().clone()),
        };
        let mut buf = vec![0u8; 4096];
        let mut total = 0;
        {
            let mut stream_guard = self.stream.lock().await;
            let stream = stream_guard
                .as_mut()
                .ok_or_else(|| Error::transport("sml: not started"))?;
            // Read up to 4 KB total (a full SML transport frame is < 1 KB).
            for _ in 0..8 {
                if total >= buf.len() {
                    break;
                }
                let res =
                    tokio::time::timeout(Duration::from_secs(2), stream.read(&mut buf[total..]))
                        .await;
                match res {
                    Ok(Ok(0)) => break,
                    Ok(Ok(n)) => {
                        total += n;
                        if let Some(_frame) = astrameter_sml::find_frame(&buf[..total]) {
                            break;
                        }
                    }
                    Ok(Err(e)) => return Err(Error::transport(format!("sml read: {e}"))),
                    Err(_) => break,
                }
            }
        }
        if let Some(frame) = astrameter_sml::find_frame(&buf[..total]) {
            let entries = astrameter_sml::parse_obis(frame).unwrap_or_default();
            let look = |obis: [u8; 6]| -> Option<f64> {
                entries.iter().find(|e| e.obis == obis).map(|e| e.value)
            };
            let l1 = look(self.obis_l1);
            let l2 = look(self.obis_l2);
            let l3 = look(self.obis_l3);
            let values = if let (Some(a), Some(b), Some(c)) = (l1, l2, l3) {
                vec![a, b, c]
            } else if let Some(agg) = look(self.obis_current) {
                vec![agg]
            } else {
                return Ok(self.cached.lock().clone());
            };
            *self.cached.lock() = values.clone();
            drop(lock);
            return Ok(values);
        }
        drop(lock);
        Ok(self.cached.lock().clone())
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    let path = section.get_required("SERIAL")?.to_string();
    let parse = |key: &str, default: [u8; 6]| -> Result<[u8; 6]> {
        match section.get_opt_string(key) {
            None => Ok(default),
            Some(s) => parse_obis_hex(&s, &format!("[{}] {}", section.name(), key)),
        }
    };
    Ok(Arc::new(Sml {
        device_path: path,
        obis_current: parse("OBIS_POWER_CURRENT", OBIS_POWER_CURRENT)?,
        obis_l1: parse("OBIS_POWER_L1", OBIS_POWER_L1)?,
        obis_l2: parse("OBIS_POWER_L2", OBIS_POWER_L2)?,
        obis_l3: parse("OBIS_POWER_L3", OBIS_POWER_L3)?,
        serial: platform.serial.clone(),
        stream: AMutex::new(None),
        cached: Mutex::new(vec![0.0]),
        read_lock: AMutex::new(()),
    }))
}
