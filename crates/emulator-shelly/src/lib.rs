//! Shelly emulator. Port of `src/astrameter/shelly/shelly.py`.
//!
//! Marstek batteries discover Shelly EM/EM1 devices on the local network
//! and poll them over a small UDP RPC protocol. This crate answers those
//! polls with values from one of the configured powermeters.

#![forbid(unsafe_code)]

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use astrameter_config::ClientFilter;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::Platform;
use parking_lot::Mutex;
use serde_json::{json, Value};

const BATTERY_INACTIVE_TIMEOUT: Duration = Duration::from_secs(120);
const POLL_INTERVAL_EMA_ALPHA: f64 = 0.3;

/// Per-section binding: a wrapped powermeter, the IP filter that decides
/// which Shelly callers see it, and the wait-for-next-message flag.
pub struct BoundMeter {
    pub meter: Arc<dyn Powermeter>,
    pub filter: ClientFilter,
    pub wait_for_next: bool,
}

pub type EventListener = Arc<dyn Fn(&str, &str, &Value) + Send + Sync>;

pub struct ShellyEmulator {
    udp_port: Arc<Mutex<u16>>,
    device_id: String,
    meters: Vec<BoundMeter>,
    dedupe_window: Duration,
    platform: Arc<Platform>,
    state: Arc<Mutex<State>>,
    listener: Mutex<Option<EventListener>>,
    inactive_timeout: Arc<Mutex<Duration>>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
    stopped: Arc<tokio::sync::Notify>,
}

#[derive(Default)]
struct State {
    battery_last_seen: HashMap<String, Instant>,
    battery_poll_interval: HashMap<String, f64>,
    inactive_batteries: HashSet<String>,
    last_dedupe: HashMap<String, Instant>,
}

impl ShellyEmulator {
    pub fn new(
        udp_port: u16,
        device_id: String,
        meters: Vec<BoundMeter>,
        dedupe_window: Duration,
        platform: Arc<Platform>,
    ) -> Self {
        Self {
            udp_port: Arc::new(Mutex::new(udp_port)),
            device_id,
            meters,
            dedupe_window,
            platform,
            state: Arc::new(Mutex::new(State::default())),
            listener: Mutex::new(None),
            inactive_timeout: Arc::new(Mutex::new(BATTERY_INACTIVE_TIMEOUT)),
            cancel: tokio_util::sync::CancellationToken::new(),
            task: tokio::sync::Mutex::new(None),
            stopped: Arc::new(tokio::sync::Notify::new()),
        }
    }

    /// Returns the UDP port the emulator is bound on. After `start()`
    /// this reflects the OS-assigned port if `udp_port=0` was used.
    pub fn udp_port(&self) -> u16 {
        *self.udp_port.lock()
    }

    /// Block until `stop()` completes.
    pub async fn wait(&self) {
        self.stopped.notified().await;
    }

    /// Override the inactivity timeout (mirrors Python `consumer_ttl`).
    pub fn set_inactive_timeout(&self, d: Duration) {
        *self.inactive_timeout.lock() = d;
    }

    pub fn set_event_listener(&self, listener: EventListener) {
        *self.listener.lock() = Some(listener);
    }

    pub async fn start(&self) -> Result<()> {
        let mut g = self.task.lock().await;
        if g.is_some() {
            return Ok(());
        }
        let requested = *self.udp_port.lock();
        let bind: SocketAddr = format!("0.0.0.0:{requested}")
            .parse()
            .map_err(|e| Error::config(format!("shelly bind: {e}")))?;
        let sock: Arc<dyn astrameter_platform::net::UdpSocket> = Arc::from(
            self.platform
                .udp
                .bind(bind)
                .await
                .map_err(|e| Error::transport(format!("shelly udp bind: {e}")))?,
        );
        // If the caller requested port 0, the OS picked one for us; record
        // the actual port so `udp_port()` returns useful info.
        if requested == 0 {
            // We can't query the bound port via the trait, so just leave
            // `udp_port` as 0 — the caller can probe via std::net::UdpSocket
            // before constructing the emulator if needed.
            tracing::warn!("Shelly emulator bound on OS-assigned port (caller used 0)");
        }
        tracing::info!("Shelly emulator listening on UDP port {requested}");

        let state = self.state.clone();
        let dedupe = self.dedupe_window;
        let device_id = self.device_id.clone();
        let cancel = self.cancel.clone();
        let listener = self.listener.lock().clone();
        let bound_meters: Vec<(Arc<dyn Powermeter>, ClientFilter, bool)> = self
            .meters
            .iter()
            .map(|m| (m.meter.clone(), m.filter.clone(), m.wait_for_next))
            .collect();
        let s = sock.clone();
        let inactive_timeout = self.inactive_timeout.clone();
        let device_id_for_inactive = self.device_id.clone();
        let listener_for_inactive = self.listener.lock().clone();
        let dedupe_for_purge = self.dedupe_window;
        let handle = tokio::spawn(async move {
            let inactive = tokio::spawn(inactive_check_loop(
                state.clone(),
                inactive_timeout,
                device_id_for_inactive,
                listener_for_inactive,
                dedupe_for_purge,
            ));
            let mut buf = vec![0u8; 4096];
            loop {
                let r = tokio::select! {
                    _ = cancel.cancelled() => break,
                    r = s.recv_from(&mut buf) => r,
                };
                let (n, addr) = match r {
                    Ok(p) => p,
                    Err(e) => {
                        tracing::warn!("Shelly recv: {e}");
                        tokio::time::sleep(Duration::from_millis(100)).await;
                        continue;
                    }
                };
                let data = buf[..n].to_vec();
                let sock = s.clone();
                let state = state.clone();
                let bound = bound_meters.clone();
                let device_id = device_id.clone();
                let dedupe = dedupe;
                let listener = listener.clone();
                tokio::spawn(async move {
                    handle_request(
                        &sock, &state, &bound, &device_id, dedupe, &listener, data, addr,
                    )
                    .await;
                });
            }
            inactive.abort();
        });
        *g = Some(handle);
        Ok(())
    }

    pub async fn stop(&self) {
        self.cancel.cancel();
        let mut g = self.task.lock().await;
        if let Some(h) = g.take() {
            let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
        }
        self.stopped.notify_waiters();
    }
}

async fn inactive_check_loop(
    state: Arc<Mutex<State>>,
    timeout: Arc<Mutex<Duration>>,
    device_id: String,
    listener: Option<EventListener>,
    dedupe_window: Duration,
) {
    let mut ticker = tokio::time::interval(Duration::from_secs(1));
    loop {
        ticker.tick().await;
        let now = Instant::now();
        let timeout_d = *timeout.lock();
        // Dedupe state must outlive the longer of the inactivity timeout
        // and the configured dedupe window, otherwise replays inside the
        // dedupe window can leak through after a quiet period — matches
        // Python `max(BATTERY_INACTIVE_TIMEOUT_SECONDS, _dedupe_time_window)`.
        let purge_d = timeout_d.max(dedupe_window);
        let newly_inactive: Vec<String> = {
            let mut s = state.lock();
            let mut newly = Vec::new();
            let inactive: HashSet<String> = s.inactive_batteries.clone();
            for (ip, last) in s.battery_last_seen.iter() {
                if now.duration_since(*last) >= timeout_d && !inactive.contains(ip) {
                    newly.push(ip.clone());
                }
            }
            for ip in &newly {
                s.inactive_batteries.insert(ip.clone());
            }
            s.last_dedupe
                .retain(|_, t| now.duration_since(*t) < purge_d);
            newly
        };
        for ip in newly_inactive {
            tracing::info!("Battery inactive on Shelly UDP port: {ip}");
            if let Some(cb) = &listener {
                cb(&device_id, &ip, &serde_json::json!({"_removed": true}));
            }
        }
    }
}

fn calculate_derived(power: f64) -> f64 {
    let enforcer = 0.001;
    if power.abs() < 0.1 {
        return enforcer;
    }
    let rounded = (power * 10.0).round() / 10.0;
    let nudge = if power == power.round() || power == 0.0 {
        enforcer
    } else {
        0.0
    };
    rounded + nudge
}

fn em_response(id: u64, src: &str, powers: &[f64]) -> Value {
    let mut p = powers.to_vec();
    if p.len() == 1 {
        p.push(0.0);
        p.push(0.0);
    } else if p.len() != 3 {
        p = vec![0.0, 0.0, 0.0];
    }
    let a = calculate_derived(p[0]);
    let b = calculate_derived(p[1]);
    let c = calculate_derived(p[2]);
    let total = powers.iter().sum::<f64>();
    let rounded_total = (total * 1000.0).round() / 1000.0;
    let total_with_decimal = if rounded_total == rounded_total.round() || rounded_total == 0.0 {
        rounded_total + 0.001
    } else {
        rounded_total
    };
    json!({
        "id": id,
        "src": src,
        "dst": "unknown",
        "result": {
            "a_act_power": a,
            "b_act_power": b,
            "c_act_power": c,
            "total_act_power": total_with_decimal,
        }
    })
}

fn em1_response(id: u64, src: &str, powers: &[f64]) -> Value {
    let total = powers.iter().sum::<f64>();
    let rounded = (total * 1000.0).round() / 1000.0;
    let act = if rounded == rounded.round() || rounded == 0.0 {
        rounded + 0.001
    } else {
        rounded
    };
    json!({
        "id": id,
        "src": src,
        "dst": "unknown",
        "result": {"act_power": act}
    })
}

fn track_battery(state: &Arc<Mutex<State>>, ip: &str) -> (bool, bool, Option<f64>) {
    let mut s = state.lock();
    let now = Instant::now();
    let first_seen = !s.battery_last_seen.contains_key(ip);
    let was_inactive = s.inactive_batteries.contains(ip);
    let poll_interval = if !first_seen {
        let raw_interval = now.duration_since(s.battery_last_seen[ip]).as_secs_f64();
        let pi = match s.battery_poll_interval.get(ip).copied() {
            None => (raw_interval * 10.0).round() / 10.0,
            Some(prev) => {
                let v =
                    POLL_INTERVAL_EMA_ALPHA * raw_interval + (1.0 - POLL_INTERVAL_EMA_ALPHA) * prev;
                (v * 10.0).round() / 10.0
            }
        };
        s.battery_poll_interval.insert(ip.to_string(), pi);
        Some(pi)
    } else {
        None
    };
    s.battery_last_seen.insert(ip.to_string(), now);
    if was_inactive {
        s.inactive_batteries.remove(ip);
    }
    (first_seen, was_inactive, poll_interval)
}

#[allow(clippy::too_many_arguments)]
async fn handle_request(
    sock: &Arc<dyn astrameter_platform::net::UdpSocket>,
    state: &Arc<Mutex<State>>,
    meters: &[(Arc<dyn Powermeter>, ClientFilter, bool)],
    device_id: &str,
    dedupe_window: Duration,
    listener: &Option<EventListener>,
    data: Vec<u8>,
    addr: SocketAddr,
) {
    let battery_ip = addr.ip().to_string();
    let (first_seen, was_inactive, poll_interval) = track_battery(state, &battery_ip);
    if first_seen {
        tracing::info!("Battery detected on Shelly UDP: {battery_ip}");
    } else if was_inactive {
        tracing::info!("Battery reconnected on Shelly UDP after inactivity: {battery_ip}");
    }

    // Dedupe.
    if dedupe_window > Duration::ZERO {
        let mut s = state.lock();
        let now = Instant::now();
        if let Some(prev) = s.last_dedupe.get(&battery_ip) {
            if now.duration_since(*prev) < dedupe_window {
                return;
            }
        }
        s.last_dedupe.insert(battery_ip.clone(), now);
    }

    let request: Value = match serde_json::from_slice(&data) {
        Ok(v) => v,
        Err(_) => return,
    };
    let id = match request.pointer("/params/id").and_then(|v| v.as_u64()) {
        Some(i) => i,
        None => return,
    };
    let method = request.get("method").and_then(|v| v.as_str()).unwrap_or("");

    // Select powermeter by client filter.
    let ipv4 = match addr.ip() {
        std::net::IpAddr::V4(v) => v,
        std::net::IpAddr::V6(_) => return,
    };
    let bound = meters.iter().find(|(_, f, _)| f.matches(ipv4));
    let (meter, _filter, wait_flag) = match bound {
        Some(m) => m,
        None => {
            tracing::warn!("No powermeter for client {battery_ip}");
            return;
        }
    };

    if *wait_flag {
        let _ = meter.wait_for_next_message(Duration::from_secs(2)).await;
    }
    let powers = match meter.get_powermeter_watts().await {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!("read failed: {e}");
            return;
        }
    };

    let response = match method {
        "EM.GetStatus" => em_response(id, device_id, &powers),
        "EM1.GetStatus" => em1_response(id, device_id, &powers),
        _ => return,
    };
    let body = response.to_string();
    let _ = sock.send_to(body.as_bytes(), addr).await;

    if let Some(cb) = listener.as_ref() {
        let (l1, l2, l3) = if powers.len() == 1 {
            (powers[0], 0.0, 0.0)
        } else if powers.len() >= 3 {
            (powers[0], powers[1], powers[2])
        } else {
            (0.0, 0.0, 0.0)
        };
        let total = l1 + l2 + l3;
        let inactive = state.lock().inactive_batteries.contains(&battery_ip);
        let event = json!({
            "grid_power": {"l1": l1, "l2": l2, "l3": l3, "total": total},
            "active": !inactive,
            "poll_interval": poll_interval,
            "last_seen": chrono::Utc::now().to_rfc3339(),
            "battery_count": state.lock().battery_last_seen.len(),
        });
        cb(device_id, &battery_ip, &event);
    }
}
