//! `SMA_ENERGY_METER` — port of
//! `src/astrameter/powermeter/sma_energy_meter.py`. UDP multicast.

use std::net::{Ipv4Addr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::{net::UdpBind, Platform};
use async_trait::async_trait;
use parking_lot::Mutex;
use tokio::sync::Notify;

const DEFAULT_GROUP: &str = "239.12.255.254";
const DEFAULT_PORT: u16 = 9522;
const POWER_DIVISOR: f64 = 10.0;

const C_TOTAL_PLUS: u32 = 0x00010400;
const C_TOTAL_MINUS: u32 = 0x00020400;
const C_L1_PLUS: u32 = 0x00150400;
const C_L1_MINUS: u32 = 0x00160400;
const C_L2_PLUS: u32 = 0x00290400;
const C_L2_MINUS: u32 = 0x002A0400;
const C_L3_PLUS: u32 = 0x003D0400;
const C_L3_MINUS: u32 = 0x003E0400;
const C_END: u32 = 0x00000000;
const C_SOFTWARE: u32 = 0x90000000;

pub struct SmaEnergyMeter {
    multicast_group: String,
    port: u16,
    serial_filter: u32,
    interface: String,
    udp: Arc<dyn UdpBind>,
    state: Arc<Mutex<SharedState>>,
    notify: Arc<Notify>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
}

#[derive(Default)]
struct SharedState {
    values: Option<Vec<f64>>,
    detected_serial: Option<u32>,
}

fn channel_len(ident: u32) -> usize {
    if ident == C_END {
        0
    } else if ((ident >> 8) & 0xFF) == 0x04 || ident == C_SOFTWARE {
        4
    } else if ((ident >> 8) & 0xFF) == 0x08 {
        8
    } else {
        4
    }
}

fn handle_packet(state: &Arc<Mutex<SharedState>>, serial_filter: u32, data: &[u8]) -> bool {
    if data.len() < 28 || &data[0..4] != b"SMA\x00" || data[5] != 0x04 || data[6] != 0x02 {
        return false;
    }
    if u16::from_be_bytes([data[16], data[17]]) != 0x6069 {
        return false;
    }
    let serial = u32::from_be_bytes([data[20], data[21], data[22], data[23]]);
    if serial_filter != 0 {
        if serial != serial_filter {
            return false;
        }
    } else {
        let mut st = state.lock();
        match st.detected_serial {
            None => {
                tracing::info!("SMA Energy Meter: auto-detected device with serial {serial}");
                st.detected_serial = Some(serial);
            }
            Some(s) if s != serial => return false,
            Some(_) => {}
        }
    }

    let mut raw: [(u32, f64); 8] = [
        (C_TOTAL_PLUS, 0.0),
        (C_TOTAL_MINUS, 0.0),
        (C_L1_PLUS, 0.0),
        (C_L1_MINUS, 0.0),
        (C_L2_PLUS, 0.0),
        (C_L2_MINUS, 0.0),
        (C_L3_PLUS, 0.0),
        (C_L3_MINUS, 0.0),
    ];
    let mut present = [false; 8];
    let mut pos = 28usize;
    while pos + 4 <= data.len() {
        let ident = u32::from_be_bytes([data[pos], data[pos + 1], data[pos + 2], data[pos + 3]]);
        if ident == C_END {
            break;
        }
        let clen = channel_len(ident);
        if pos + 4 + clen > data.len() {
            break;
        }
        if let Some(i) = raw.iter().position(|(c, _)| *c == ident) {
            let v = u32::from_be_bytes([data[pos + 4], data[pos + 5], data[pos + 6], data[pos + 7]])
                as f64
                / POWER_DIVISOR;
            raw[i].1 = v;
            present[i] = true;
        }
        pos += 4 + clen;
    }

    let has_phase =
        present[2] || present[3] || present[4] || present[5] || present[6] || present[7];
    let values = if has_phase {
        let l1 = raw[2].1 - raw[3].1;
        let l2 = raw[4].1 - raw[5].1;
        let l3 = raw[6].1 - raw[7].1;
        vec![l1, l2, l3]
    } else if present[0] || present[1] {
        vec![raw[0].1 - raw[1].1]
    } else {
        return false;
    };

    state.lock().values = Some(values);
    true
}

#[async_trait]
impl Powermeter for SmaEnergyMeter {
    async fn start(&self) -> Result<()> {
        let mut task_guard = self.task.lock().await;
        if task_guard.is_some() {
            return Ok(());
        }
        let group: Ipv4Addr = self
            .multicast_group
            .parse()
            .map_err(|e| Error::config(format!("SMA group: {e}")))?;
        let interface: Ipv4Addr = if self.interface.is_empty() {
            Ipv4Addr::new(0, 0, 0, 0)
        } else {
            self.interface
                .parse()
                .map_err(|e| Error::config(format!("SMA interface: {e}")))?
        };
        let addr: SocketAddr = SocketAddr::new(Ipv4Addr::new(0, 0, 0, 0).into(), self.port);
        let sock = self
            .udp
            .bind_multicast(addr, group, interface)
            .await
            .map_err(|e| Error::transport(format!("SMA bind_multicast: {e}")))?;
        tracing::info!(
            "SMA Energy Meter: listening on {}:{}",
            self.multicast_group,
            self.port
        );

        let state = self.state.clone();
        let notify = self.notify.clone();
        let serial_filter = self.serial_filter;
        let cancel = self.cancel.clone();
        let handle = tokio::spawn(async move {
            let mut buf = vec![0u8; 1500];
            loop {
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    r = sock.recv_from(&mut buf) => match r {
                        Ok((n, _from)) => {
                            if handle_packet(&state, serial_filter, &buf[..n]) {
                                notify.notify_waiters();
                            }
                        }
                        Err(e) => {
                            tracing::warn!("SMA recv error: {e}");
                            tokio::time::sleep(Duration::from_millis(200)).await;
                        }
                    }
                }
            }
        });
        *task_guard = Some(handle);
        Ok(())
    }

    async fn stop(&self) -> Result<()> {
        self.cancel.cancel();
        let mut task_guard = self.task.lock().await;
        if let Some(h) = task_guard.take() {
            let _ = tokio::time::timeout(Duration::from_secs(1), h).await;
        }
        Ok(())
    }

    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        self.state.lock().values.clone().ok_or(Error::NoValue)
    }

    async fn wait_for_message(&self, timeout: Duration) -> Result<()> {
        if self.state.lock().values.is_some() {
            return Ok(());
        }
        let n = self.notify.clone();
        tokio::time::timeout(timeout, n.notified())
            .await
            .map(|_| ())
            .map_err(|_| Error::Timeout {
                millis: timeout.as_millis() as u64,
            })
    }

    async fn wait_for_next_message(&self, timeout: Duration) -> Result<()> {
        let n = self.notify.clone();
        tokio::time::timeout(timeout, n.notified())
            .await
            .map(|_| ())
            .map_err(|_| Error::Timeout {
                millis: timeout.as_millis() as u64,
            })
    }
}

pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    Ok(Arc::new(SmaEnergyMeter {
        multicast_group: section.get_string("MULTICAST_GROUP", DEFAULT_GROUP),
        port: section.get_int("PORT", DEFAULT_PORT as i64)? as u16,
        serial_filter: section.get_int("SERIAL_NUMBER", 0)? as u32,
        interface: section.get_string("INTERFACE", ""),
        udp: platform.udp.clone(),
        state: Arc::new(Mutex::new(SharedState::default())),
        notify: Arc::new(Notify::new()),
        cancel: tokio_util::sync::CancellationToken::new(),
        task: tokio::sync::Mutex::new(None),
    }))
}
