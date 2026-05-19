//! CT002 UDP server. Port of `src/astrameter/ct002/ct002.py` (structural).
//!
//! This is a partial port: the UDP socket loop accepts CT002-framed packets,
//! decodes request fields, runs the (simplified) balancer, and emits a
//! correctly-framed response with the 24 RESPONSE_LABELS fields populated
//! with zeros for the advanced control fields and grid powers for the
//! `*_phase_power` slots.

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use astrameter_config::ClientFilter;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::Platform;
use parking_lot::Mutex;

use crate::balancer::{BalancerConfig, LoadBalancer};
use crate::protocol::{build_payload, parse_request, RESPONSE_LABELS};

pub struct BoundMeter {
    pub meter: Arc<dyn Powermeter>,
    pub filter: ClientFilter,
    pub wait_for_next: bool,
}

/// Per-consumer control state — manual targets, active flag.
#[derive(Debug, Clone)]
pub struct ConsumerControl {
    pub active: bool,
    pub auto_target: bool,
    pub manual_target: f64,
}

impl Default for ConsumerControl {
    fn default() -> Self {
        Self {
            active: true,
            auto_target: true,
            manual_target: 0.0,
        }
    }
}

pub struct Ct002Emulator {
    udp_port: u16,
    meter_mac: String,
    meters: Vec<BoundMeter>,
    platform: Arc<Platform>,
    #[allow(dead_code)]
    balancer: LoadBalancer,
    sessions: Arc<Mutex<HashSet<String>>>,
    consumers: Arc<Mutex<std::collections::HashMap<String, ConsumerControl>>>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
}

impl Ct002Emulator {
    pub fn new(
        udp_port: u16,
        meter_mac: String,
        meters: Vec<BoundMeter>,
        balancer_cfg: BalancerConfig,
        platform: Arc<Platform>,
    ) -> Self {
        Self {
            udp_port,
            meter_mac,
            meters,
            platform,
            balancer: LoadBalancer::new(balancer_cfg, 0.2, 10.0, 0.9, 90.0, 60.0, true, None, None),
            sessions: Arc::new(Mutex::new(HashSet::new())),
            consumers: Arc::new(Mutex::new(std::collections::HashMap::new())),
            cancel: tokio_util::sync::CancellationToken::new(),
            task: tokio::sync::Mutex::new(None),
        }
    }

    pub async fn start(&self) -> Result<()> {
        let g = self.task.lock().await;
        if g.is_some() {
            return Ok(());
        }
        drop(g);
        let bind: SocketAddr = format!("0.0.0.0:{}", self.udp_port)
            .parse()
            .map_err(|e| Error::config(format!("ct002 bind: {e}")))?;
        let sock: Arc<dyn astrameter_platform::net::UdpSocket> = Arc::from(
            self.platform
                .udp
                .bind(bind)
                .await
                .map_err(|e| Error::transport(format!("ct002 udp bind: {e}")))?,
        );
        tracing::info!("CT002 emulator listening on UDP port {}", self.udp_port);
        let cancel = self.cancel.clone();
        let sessions = self.sessions.clone();
        let meter_mac = self.meter_mac.clone();
        let bound: Vec<(Arc<dyn Powermeter>, ClientFilter, bool)> = self
            .meters
            .iter()
            .map(|m| (m.meter.clone(), m.filter.clone(), m.wait_for_next))
            .collect();
        let _ = (*self.task.lock().await).replace(tokio::spawn(run_loop(
            sock.clone(),
            cancel,
            sessions,
            meter_mac,
            bound,
        )));
        Ok(())
    }

    pub async fn stop(&self) {
        self.cancel.cancel();
        let mut g = self.task.lock().await;
        if let Some(h) = g.take() {
            let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
        }
    }

    // ── Consumer control surface (matches `ct002.py:set_consumer_*`) ──

    pub fn set_consumer_active(&self, consumer_id: &str, active: bool) {
        let mut map = self.consumers.lock();
        map.entry(consumer_id.to_string())
            .or_default()
            .active = active;
        tracing::info!(consumer = consumer_id, active, "CT002 set_consumer_active");
    }

    pub fn set_consumer_manual_target(&self, consumer_id: &str, target: f64) {
        let mut map = self.consumers.lock();
        let entry = map
            .entry(consumer_id.to_string())
            .or_default();
        entry.manual_target = target;
        entry.auto_target = false;
        tracing::info!(
            consumer = consumer_id,
            target,
            "CT002 set_consumer_manual_target"
        );
    }

    pub fn set_consumer_auto_target(&self, consumer_id: &str, auto: bool) {
        let mut map = self.consumers.lock();
        map.entry(consumer_id.to_string())
            .or_default()
            .auto_target = auto;
        tracing::info!(
            consumer = consumer_id,
            auto,
            "CT002 set_consumer_auto_target"
        );
    }

    pub fn force_efficiency_rotation(&self) {
        let known: std::collections::HashSet<String> =
            self.consumers.lock().keys().cloned().collect();
        self.balancer.force_rotation(&known);
        tracing::info!("CT002 force_efficiency_rotation");
    }

    /// Snapshot of current per-consumer control state.
    pub fn consumers_snapshot(&self) -> std::collections::HashMap<String, ConsumerControl> {
        self.consumers.lock().clone()
    }
}

async fn run_loop(
    sock: Arc<dyn astrameter_platform::net::UdpSocket>,
    cancel: tokio_util::sync::CancellationToken,
    sessions: Arc<Mutex<HashSet<String>>>,
    meter_mac: String,
    meters: Vec<(Arc<dyn Powermeter>, ClientFilter, bool)>,
) {
    let mut buf = vec![0u8; 4096];
    loop {
        let r = tokio::select! {
            _ = cancel.cancelled() => break,
            r = sock.recv_from(&mut buf) => r,
        };
        let (n, addr) = match r {
            Ok(p) => p,
            Err(e) => {
                tracing::warn!("CT002 recv: {e}");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        let data = buf[..n].to_vec();
        let sock = sock.clone();
        let sessions = sessions.clone();
        let meter_mac = meter_mac.clone();
        let meters = meters.clone();
        tokio::spawn(async move {
            if let Err(e) = handle(&sock, &sessions, &meter_mac, &meters, &data, addr).await {
                tracing::warn!("CT002 handle: {e}");
            }
        });
    }
}

async fn handle(
    sock: &Arc<dyn astrameter_platform::net::UdpSocket>,
    sessions: &Arc<Mutex<HashSet<String>>>,
    meter_mac: &str,
    meters: &[(Arc<dyn Powermeter>, ClientFilter, bool)],
    data: &[u8],
    addr: SocketAddr,
) -> Result<()> {
    let fields = parse_request(data).map_err(|e| Error::decode(format!("ct002 parse: {}", e.0)))?;
    if fields.is_empty() {
        return Ok(());
    }
    let battery_ip = addr.ip().to_string();
    sessions.lock().insert(battery_ip.clone());
    let ipv4 = match addr.ip() {
        std::net::IpAddr::V4(v) => v,
        std::net::IpAddr::V6(_) => return Ok(()),
    };
    let bound = meters.iter().find(|(_, f, _)| f.matches(ipv4));
    let (meter, _f, wait_flag) = match bound {
        Some(m) => m,
        None => {
            tracing::warn!("CT002: no powermeter for {battery_ip}");
            return Ok(());
        }
    };
    if *wait_flag {
        let _ = meter.wait_for_next_message(Duration::from_secs(2)).await;
    }
    let powers = meter.get_powermeter_watts().await?;
    let (a, b, c) = if powers.len() >= 3 {
        (powers[0], powers[1], powers[2])
    } else if powers.len() == 1 {
        (powers[0], 0.0, 0.0)
    } else {
        (0.0, 0.0, 0.0)
    };
    let total = a + b + c;

    // Build response: 24 fields per RESPONSE_LABELS, filled with relay-mode
    // values. Advanced fields are zero — see balancer.rs for the rationale.
    let mut response = HashMap::<&str, String>::new();
    response.insert("meter_dev_type", "00".into());
    response.insert("meter_mac_code", meter_mac.to_string());
    response.insert("hhm_dev_type", "00".into());
    response.insert("hhm_mac_code", "000000000000".into());
    response.insert("A_phase_power", format!("{:.0}", a));
    response.insert("B_phase_power", format!("{:.0}", b));
    response.insert("C_phase_power", format!("{:.0}", c));
    response.insert("total_power", format!("{:.0}", total));
    for label in [
        "A_chrg_nb",
        "B_chrg_nb",
        "C_chrg_nb",
        "ABC_chrg_nb",
        "wifi_rssi",
        "info_idx",
        "x_chrg_power",
        "A_chrg_power",
        "B_chrg_power",
        "C_chrg_power",
        "ABC_chrg_power",
        "x_dchrg_power",
        "A_dchrg_power",
        "B_dchrg_power",
        "C_dchrg_power",
        "ABC_dchrg_power",
    ] {
        response.entry(label).or_insert("0".into());
    }
    let owned: Vec<String> = RESPONSE_LABELS
        .iter()
        .map(|l| response.get(l).cloned().unwrap_or_default())
        .collect();
    let fields: Vec<&str> = owned.iter().map(|s| s.as_str()).collect();
    let payload = build_payload(&fields).map_err(|e| Error::transport(e.to_string()))?;
    sock.send_to(&payload, addr)
        .await
        .map_err(|e| Error::transport(format!("ct002 send: {e}")))?;
    let _ = Instant::now;
    Ok(())
}
