//! CT002 UDP server. Faithful port of `src/astrameter/ct002/ct002.py`.
//!
//! Pipeline per UDP request:
//! 1. Parse the request fields (`meter_dev_type`, `meter_mac_code`,
//!    `ct_type`, `ct_mac`, `phase`, `power`, …).
//! 2. Optionally validate the requesting CT MAC against the configured
//!    `CT_MAC` (silently drop on mismatch).
//! 3. Derive the consumer id from the battery MAC (`fields[1]`) or fall
//!    back to `addr:port`.
//! 4. Run a per-consumer dedupe check.
//! 5. Update [`Consumer`] state with reported phase/power/device-type/IP.
//! 6. Resolve a powermeter for the source IP (via `ClientFilter`), honour
//!    `WAIT_FOR_NEXT_MESSAGE`, and read fresh grid watts.
//! 7. If `active_control` is on, run the [`LoadBalancer`] to get per-phase
//!    setpoints; otherwise forward the raw grid reading.
//! 8. Build a 24-field response with the per-phase charge/discharge split
//!    and a wrapping `info_idx` counter.

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use astrameter_config::ClientFilter;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::Platform;
use parking_lot::Mutex;

use crate::balancer::{
    BalancerConfig, ConsumerMode, ConsumerReport, LoadBalancer, Reports, SATURATION_GRACE_SECONDS,
    SATURATION_STALL_TIMEOUT_SECONDS,
};
use crate::protocol::{build_payload, parse_request, RESPONSE_LABELS};

pub struct BoundMeter {
    pub meter: Arc<dyn Powermeter>,
    pub filter: ClientFilter,
    pub wait_for_next: bool,
}

/// Per-consumer state — mirrors Python `Consumer` in `ct002.py:62-80`.
#[derive(Debug, Clone)]
pub struct Consumer {
    pub consumer_id: String,
    /// Last grid reading set externally (currently unused on the Rust side
    /// — the meter is read per request).
    pub values: Option<Vec<f64>>,
    pub phase: char,
    pub power: i32,
    pub timestamp: f64,
    pub device_type: String,
    pub poll_interval: Option<f64>,
    pub manual_target: f64,
    pub manual_enabled: bool,
    pub active: bool,
    pub last_ip: String,
}

impl Consumer {
    fn new(consumer_id: String) -> Self {
        Self {
            consumer_id,
            values: None,
            phase: 'A',
            power: 0,
            timestamp: 0.0,
            device_type: String::new(),
            poll_interval: None,
            manual_target: 0.0,
            manual_enabled: false,
            active: true,
            last_ip: String::new(),
        }
    }
}

/// Knobs that map directly to the Python `CT002(__init__)` args.
#[derive(Debug, Clone)]
pub struct Ct002Settings {
    pub ct_type: String, // "HME-4" for CT002, "HME-3" for CT003
    pub ct_mac: String,
    pub wifi_rssi: i32,
    pub dedupe_time_window: Duration,
    pub consumer_ttl: Duration,
    pub debug_status: bool,
    pub active_control: bool,
    pub saturation_alpha: f64,
    pub min_target_for_saturation: f64,
    pub saturation_decay_factor: f64,
    pub saturation_grace_seconds: f64,
    pub saturation_stall_timeout_seconds: f64,
    pub saturation_detection: bool,
}

impl Default for Ct002Settings {
    fn default() -> Self {
        Self {
            ct_type: "HME-4".into(),
            ct_mac: String::new(),
            wifi_rssi: -50,
            dedupe_time_window: Duration::ZERO,
            consumer_ttl: Duration::from_secs(120),
            debug_status: false,
            active_control: true,
            saturation_alpha: 0.15,
            min_target_for_saturation: 20.0,
            saturation_decay_factor: 0.995,
            saturation_grace_seconds: SATURATION_GRACE_SECONDS,
            saturation_stall_timeout_seconds: SATURATION_STALL_TIMEOUT_SECONDS,
            saturation_detection: true,
        }
    }
}

/// `(device_id, consumer_id, event_payload)` callback for the insights
/// service. `event_payload` carries the Python `_call_event_listener` dict.
pub type EventListenerFn = Arc<dyn Fn(&str, &str, &serde_json::Value) + Send + Sync>;

pub struct Ct002Emulator {
    udp_port: u16,
    device_id: String,
    settings: Ct002Settings,
    balancer_cfg: BalancerConfig,
    meters: Vec<BoundMeter>,
    platform: Arc<Platform>,
    balancer: Arc<LoadBalancer>,
    consumers: Arc<Mutex<HashMap<String, Consumer>>>,
    last_dedupe: Arc<Mutex<HashMap<String, Instant>>>,
    info_idx: Arc<AtomicU8>,
    last_smooth_target: Arc<Mutex<f64>>,
    event_listener: Arc<Mutex<Option<EventListenerFn>>>,
    cancel: tokio_util::sync::CancellationToken,
    task: tokio::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
}

impl Ct002Emulator {
    pub fn new(
        udp_port: u16,
        ct_mac: String,
        meters: Vec<BoundMeter>,
        balancer_cfg: BalancerConfig,
        platform: Arc<Platform>,
    ) -> Self {
        let settings = Ct002Settings {
            ct_mac,
            ..Ct002Settings::default()
        };
        Self::with_settings(
            udp_port,
            String::new(),
            settings,
            balancer_cfg,
            meters,
            platform,
        )
    }

    pub fn with_settings(
        udp_port: u16,
        device_id: String,
        settings: Ct002Settings,
        balancer_cfg: BalancerConfig,
        meters: Vec<BoundMeter>,
        platform: Arc<Platform>,
    ) -> Self {
        let balancer = Arc::new(LoadBalancer::new(
            balancer_cfg,
            settings.saturation_alpha,
            settings.min_target_for_saturation,
            settings.saturation_decay_factor,
            settings.saturation_grace_seconds,
            settings.saturation_stall_timeout_seconds,
            settings.saturation_detection,
            None,
            None,
        ));
        Self {
            udp_port,
            device_id,
            settings,
            balancer_cfg,
            meters,
            platform,
            balancer,
            consumers: Arc::new(Mutex::new(HashMap::new())),
            last_dedupe: Arc::new(Mutex::new(HashMap::new())),
            info_idx: Arc::new(AtomicU8::new(0)),
            last_smooth_target: Arc::new(Mutex::new(0.0)),
            event_listener: Arc::new(Mutex::new(None)),
            cancel: tokio_util::sync::CancellationToken::new(),
            task: tokio::sync::Mutex::new(None),
        }
    }

    pub fn device_id(&self) -> String {
        self.device_id.clone()
    }

    pub fn set_event_listener(&self, listener: EventListenerFn) {
        *self.event_listener.lock() = Some(listener);
    }

    /// Snapshot of currently-known consumers.
    pub fn consumers_snapshot(&self) -> HashMap<String, Consumer> {
        self.consumers.lock().clone()
    }

    pub fn reporting_consumer_count(&self) -> usize {
        self.consumers
            .lock()
            .values()
            .filter(|c| c.timestamp > 0.0)
            .count()
    }

    /// CSV body the Marstek MQTT `cd=4` reply needs (see `marstek_mqtt.py`).
    pub fn reporting_consumer_csv(&self) -> String {
        let mut rows: Vec<&Consumer> = Vec::new();
        let consumers = self.consumers.lock();
        for c in consumers.values() {
            if c.timestamp > 0.0 {
                rows.push(c);
            }
        }
        rows.sort_by(|a, b| a.consumer_id.cmp(&b.consumer_id));
        let mut parts = Vec::with_capacity(rows.len());
        for c in rows {
            let host = if c.last_ip.is_empty() {
                "0.0.0.0".to_string()
            } else {
                c.last_ip.clone()
            };
            let phase = c.phase.to_ascii_lowercase();
            parts.push(format!(
                "slv_t={},slv_id={},slv_ip={},slv_p={}",
                escape_field(&c.device_type),
                escape_field(&c.consumer_id),
                escape_field(&host),
                phase,
            ));
        }
        parts.join(",")
    }

    // ── Control surface (Python `set_consumer_*` parity) ────────────────

    pub fn set_consumer_active(&self, consumer_id: &str, active: bool) {
        let mut map = self.consumers.lock();
        let c = map
            .entry(consumer_id.to_string())
            .or_insert_with(|| Consumer::new(consumer_id.to_string()));
        c.active = active;
        if active {
            drop(map);
            self.balancer.reset_consumer(consumer_id);
        }
        tracing::info!(consumer = consumer_id, active, "CT002 set_consumer_active");
    }

    pub fn set_consumer_manual_target(&self, consumer_id: &str, target: f64) {
        if !target.is_finite() {
            tracing::warn!("CT002 manual_target must be finite, got {target}");
            return;
        }
        let mut map = self.consumers.lock();
        let c = map
            .entry(consumer_id.to_string())
            .or_insert_with(|| Consumer::new(consumer_id.to_string()));
        c.manual_target = target;
        tracing::info!(
            consumer = consumer_id,
            target,
            "CT002 set_consumer_manual_target"
        );
    }

    pub fn set_consumer_auto_target(&self, consumer_id: &str, auto: bool) {
        let was_manual;
        {
            let mut map = self.consumers.lock();
            let c = map
                .entry(consumer_id.to_string())
                .or_insert_with(|| Consumer::new(consumer_id.to_string()));
            was_manual = c.manual_enabled;
            c.manual_enabled = !auto;
        }
        if auto && was_manual {
            self.balancer.reset_consumer(consumer_id);
        } else if !auto {
            self.balancer.detach_from_auto_pool(consumer_id);
        }
        tracing::info!(
            consumer = consumer_id,
            auto,
            "CT002 set_consumer_auto_target"
        );
    }

    pub fn force_efficiency_rotation(&self) {
        let current: HashSet<String> = {
            let consumers = self.consumers.lock();
            consumers
                .iter()
                .filter(|(_, c)| c.timestamp > 0.0 && c.active && !c.manual_enabled)
                .map(|(k, _)| k.clone())
                .collect()
        };
        self.balancer.force_rotation(&current);
        tracing::info!("CT002 force_efficiency_rotation");
    }

    // ── Lifecycle ────────────────────────────────────────────────────────

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
        tracing::info!("CT002 UDP server listening on port {}", self.udp_port);

        let ctx = ServerCtx {
            sock: sock.clone(),
            device_id: self.device_id.clone(),
            settings: self.settings.clone(),
            balancer_cfg: self.balancer_cfg,
            balancer: self.balancer.clone(),
            consumers: self.consumers.clone(),
            last_dedupe: self.last_dedupe.clone(),
            info_idx: self.info_idx.clone(),
            last_smooth_target: self.last_smooth_target.clone(),
            event_listener: self.event_listener.clone(),
            meters: self
                .meters
                .iter()
                .map(|m| (m.meter.clone(), m.filter.clone(), m.wait_for_next))
                .collect(),
            cancel: self.cancel.clone(),
        };
        let ctx = Arc::new(ctx);
        let recv_ctx = ctx.clone();
        let recv_handle = tokio::spawn(async move {
            recv_loop(recv_ctx).await;
        });
        let cleanup_ctx = ctx.clone();
        let cleanup_handle = tokio::spawn(async move {
            cleanup_loop(cleanup_ctx).await;
        });
        let _ = (*self.task.lock().await).replace(tokio::spawn(async move {
            let _ = tokio::join!(recv_handle, cleanup_handle);
        }));
        Ok(())
    }

    pub async fn stop(&self) {
        self.cancel.cancel();
        let mut g = self.task.lock().await;
        if let Some(h) = g.take() {
            let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
        }
    }
}

struct ServerCtx {
    sock: Arc<dyn astrameter_platform::net::UdpSocket>,
    device_id: String,
    settings: Ct002Settings,
    #[allow(dead_code)]
    balancer_cfg: BalancerConfig,
    balancer: Arc<LoadBalancer>,
    consumers: Arc<Mutex<HashMap<String, Consumer>>>,
    last_dedupe: Arc<Mutex<HashMap<String, Instant>>>,
    info_idx: Arc<AtomicU8>,
    last_smooth_target: Arc<Mutex<f64>>,
    event_listener: Arc<Mutex<Option<EventListenerFn>>>,
    meters: Vec<(Arc<dyn Powermeter>, ClientFilter, bool)>,
    cancel: tokio_util::sync::CancellationToken,
}

async fn recv_loop(ctx: Arc<ServerCtx>) {
    let mut buf = vec![0u8; 4096];
    loop {
        let r = tokio::select! {
            _ = ctx.cancel.cancelled() => break,
            r = ctx.sock.recv_from(&mut buf) => r,
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
        let ctx = ctx.clone();
        tokio::spawn(async move {
            if let Err(e) = handle(ctx, &data, addr).await {
                tracing::warn!("CT002 handle: {e}");
            }
        });
    }
}

async fn cleanup_loop(ctx: Arc<ServerCtx>) {
    let mut ticker = tokio::time::interval(Duration::from_secs(5));
    loop {
        tokio::select! {
            _ = ctx.cancel.cancelled() => break,
            _ = ticker.tick() => {}
        }
        let now_secs = unix_secs();
        let ttl = ctx.settings.consumer_ttl.as_secs_f64();
        let mut stale = Vec::new();
        {
            let consumers = ctx.consumers.lock();
            for (k, c) in consumers.iter() {
                if c.timestamp > 0.0 && (now_secs - c.timestamp) > ttl {
                    stale.push(k.clone());
                }
            }
        }
        if !stale.is_empty() {
            let mut consumers = ctx.consumers.lock();
            for k in &stale {
                consumers.remove(k);
                ctx.balancer.remove_consumer(k);
                if let Some(cb) = ctx.event_listener.lock().clone() {
                    cb(&ctx.device_id, k, &serde_json::json!({"_removed": true}));
                }
            }
        }
        // Purge stale dedupe entries.
        let cutoff = Instant::now()
            - Duration::from_secs_f64(ttl.max(ctx.settings.dedupe_time_window.as_secs_f64()));
        ctx.last_dedupe.lock().retain(|_, t| *t > cutoff);
    }
}

fn unix_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn consumer_key(addr: SocketAddr, fields: &[String]) -> String {
    let battery_mac = fields.get(1).map(|s| s.as_str()).unwrap_or("");
    if !battery_mac.is_empty() {
        return battery_mac.to_ascii_lowercase();
    }
    format!("{}:{}", addr.ip(), addr.port())
}

fn validate_ct_mac(configured: &str, fields: &[String]) -> bool {
    if configured.is_empty() {
        return true;
    }
    let req = fields.get(3).map(|s| s.as_str()).unwrap_or("");
    if req.is_empty() {
        return false;
    }
    req.eq_ignore_ascii_case(configured)
}

async fn handle(ctx: Arc<ServerCtx>, data: &[u8], addr: SocketAddr) -> Result<()> {
    let fields = parse_request(data).map_err(|e| Error::decode(format!("ct002 parse: {}", e.0)))?;
    if fields.len() < 4 {
        return Ok(());
    }
    if !validate_ct_mac(&ctx.settings.ct_mac, &fields) {
        tracing::debug!("CT002 from {addr}: CT MAC mismatch — dropping");
        return Ok(());
    }
    let consumer_id = consumer_key(addr, &fields);

    // Dedupe.
    if ctx.settings.dedupe_time_window > Duration::ZERO {
        let now = Instant::now();
        let mut map = ctx.last_dedupe.lock();
        if let Some(prev) = map.get(&consumer_id) {
            if now.duration_since(*prev) < ctx.settings.dedupe_time_window {
                return Ok(());
            }
        }
        map.insert(consumer_id.clone(), now);
    }

    // Parse phase/power.
    let reported_phase_raw = fields.get(4).cloned().unwrap_or_default();
    let normalized_phase_char = match reported_phase_raw.trim().to_ascii_uppercase().as_str() {
        "A" => 'A',
        "B" => 'B',
        "C" => 'C',
        _ => 'A', // inspection mode
    };
    let in_inspection_mode = !matches!(
        reported_phase_raw.trim().to_ascii_uppercase().as_str(),
        "A" | "B" | "C"
    );
    let reported_power: i32 = fields
        .get(5)
        .and_then(|s| s.trim().parse::<f64>().ok())
        .map(|f| f as i32)
        .unwrap_or(0);
    let meter_dev_type = fields.first().cloned().unwrap_or_default();

    // Update Consumer.
    update_consumer_report(
        &ctx,
        &consumer_id,
        if in_inspection_mode {
            'A'
        } else {
            normalized_phase_char
        },
        reported_power,
        &meter_dev_type,
        &addr.ip().to_string(),
    );

    // Resolve meter for source IP.
    let ipv4 = match addr.ip() {
        std::net::IpAddr::V4(v) => v,
        std::net::IpAddr::V6(_) => return Ok(()),
    };
    let bound = ctx.meters.iter().find(|(_, f, _)| f.matches(ipv4));
    let raw_values: Vec<f64> = if let Some((meter, _f, wait_flag)) = bound {
        if *wait_flag {
            let _ = meter.wait_for_next_message(Duration::from_secs(2)).await;
        }
        match meter.get_powermeter_watts().await {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!("CT002 meter read failed for {addr}: {e}");
                vec![0.0, 0.0, 0.0]
            }
        }
    } else {
        tracing::debug!("CT002: no powermeter for {addr}");
        vec![0.0, 0.0, 0.0]
    };
    let mut three = raw_values.clone();
    while three.len() < 3 {
        three.push(0.0);
    }
    let raw3 = [three[0], three[1], three[2]];

    let meter_value = raw3[0] + raw3[1] + raw3[2];
    let is_active = ctx
        .consumers
        .lock()
        .get(&consumer_id)
        .map(|c| c.active)
        .unwrap_or(true);

    // Active-control: feed the balancer.
    let values = if ctx.settings.active_control && !in_inspection_mode {
        compute_smooth_target(&ctx, &consumer_id, raw3)
    } else {
        raw3
    };

    // Build the 24-field response.
    let response_fields = build_response_fields(&ctx, &fields, values);
    let owned: Vec<String> = response_fields;
    let payload = build_payload(&owned.iter().map(|s| s.as_str()).collect::<Vec<_>>())
        .map_err(|e| Error::transport(e.to_string()))?;
    ctx.sock
        .send_to(&payload, addr)
        .await
        .map_err(|e| Error::transport(format!("ct002 send: {e}")))?;

    // Event listener (CT002 consumer event).
    if !in_inspection_mode {
        let listener = ctx.event_listener.lock().clone();
        if let Some(cb) = listener {
            let consumer = ctx.consumers.lock().get(&consumer_id).cloned();
            let phase_s = consumer
                .as_ref()
                .map(|c| c.phase)
                .unwrap_or(normalized_phase_char);
            let device_type_s = consumer
                .as_ref()
                .map(|c| c.device_type.clone())
                .unwrap_or_default();
            let poll_interval = consumer.as_ref().and_then(|c| c.poll_interval);
            let manual_target = consumer.as_ref().map(|c| c.manual_target);
            let auto_target = consumer.as_ref().map(|c| !c.manual_enabled);
            let event = serde_json::json!({
                "grid_power": {
                    "l1": raw3[0],
                    "l2": raw3[1],
                    "l3": raw3[2],
                    "total": meter_value,
                },
                "target": {"l1": values[0], "l2": values[1], "l3": values[2]},
                "phase": phase_s.to_string(),
                "reported_power": reported_power,
                "device_type": device_type_s,
                "battery_ip": addr.ip().to_string(),
                "ct_type": fields.get(2).cloned().unwrap_or_default(),
                "ct_mac": fields.get(3).cloned().unwrap_or_default(),
                "saturation": ctx.balancer.get_saturation(&consumer_id),
                "last_target": ctx.balancer.get_last_target(&consumer_id),
                "active": is_active,
                "poll_interval": poll_interval,
                "last_seen": chrono::Utc::now().to_rfc3339(),
                "smooth_target": *ctx.last_smooth_target.lock(),
                "manual_target": manual_target,
                "auto_target": auto_target,
                "active_control": ctx.settings.active_control,
                "consumer_count": ctx.consumers
                    .lock()
                    .values()
                    .filter(|c| c.timestamp > 0.0)
                    .count(),
            });
            cb(&ctx.device_id, &consumer_id, &event);
        }
    }
    Ok(())
}

fn update_consumer_report(
    ctx: &ServerCtx,
    consumer_id: &str,
    phase: char,
    power: i32,
    device_type: &str,
    source_ip: &str,
) {
    let now = unix_secs();
    let mut map = ctx.consumers.lock();
    let c = map
        .entry(consumer_id.to_string())
        .or_insert_with(|| Consumer::new(consumer_id.to_string()));
    let prev_ts = c.timestamp;
    let prev_phase = if prev_ts > 0.0 { Some(c.phase) } else { None };
    // EMA-smoothed poll interval.
    if prev_ts > 0.0 {
        let raw_interval = now - prev_ts;
        let smoothed = match c.poll_interval {
            None => (raw_interval * 10.0).round() / 10.0,
            Some(prev) => {
                let alpha = 0.3;
                let v = alpha * raw_interval + (1.0 - alpha) * prev;
                (v * 10.0).round() / 10.0
            }
        };
        c.poll_interval = Some(smoothed);
    }
    c.phase = phase;
    c.power = power;
    c.timestamp = now;
    c.device_type = device_type.to_string();
    if !source_ip.is_empty() {
        c.last_ip = source_ip.to_string();
    }
    if let Some(prev) = prev_phase {
        if prev != phase {
            tracing::info!("CT002 consumer {consumer_id} phase changed: {prev} -> {phase}");
        }
    }
}

fn compute_smooth_target(ctx: &ServerCtx, consumer_id: &str, values: [f64; 3]) -> [f64; 3] {
    let total: f64 = values.iter().sum();
    *ctx.last_smooth_target.lock() = total;
    let mode = consumer_mode(ctx, consumer_id);
    let reports: Reports = {
        let consumers = ctx.consumers.lock();
        consumers
            .iter()
            .filter(|(_, c)| c.timestamp > 0.0)
            .map(|(cid, c)| {
                (
                    cid.clone(),
                    ConsumerReport {
                        power: c.power,
                        phase: c.phase,
                        device_type: c.device_type.clone(),
                    },
                )
            })
            .collect()
    };
    let (inactive, manual): (HashSet<String>, HashSet<String>) = {
        let consumers = ctx.consumers.lock();
        (
            consumers
                .iter()
                .filter(|(_, c)| !c.active)
                .map(|(k, _)| k.clone())
                .collect(),
            consumers
                .iter()
                .filter(|(_, c)| c.manual_enabled)
                .map(|(k, _)| k.clone())
                .collect(),
        )
    };
    ctx.balancer.compute_target(
        Some(consumer_id),
        mode,
        &reports,
        total,
        &inactive,
        &manual,
        values.to_vec(),
    )
}

fn consumer_mode(ctx: &ServerCtx, consumer_id: &str) -> ConsumerMode {
    let consumers = ctx.consumers.lock();
    match consumers.get(consumer_id) {
        None => ConsumerMode::Auto,
        Some(c) if !c.active => ConsumerMode::Inactive,
        Some(c) if c.manual_enabled => ConsumerMode::Manual(c.manual_target),
        Some(_) => ConsumerMode::Auto,
    }
}

fn collect_reports_by_phase(consumers: &HashMap<String, Consumer>) -> [(i32, i32, bool); 3] {
    // (chrg, dchrg, active) for A, B, C.
    let mut out = [(0_i32, 0_i32, false); 3];
    for c in consumers.values() {
        if c.timestamp <= 0.0 {
            continue;
        }
        let idx = match c.phase {
            'A' => 0,
            'B' => 1,
            'C' => 2,
            _ => 0,
        };
        if c.power == 0 {
            continue;
        }
        out[idx].2 = true;
        if c.power < 0 {
            out[idx].0 += c.power; // charge (negative)
        } else {
            out[idx].1 += c.power; // discharge (positive)
        }
    }
    out
}

fn build_response_fields(
    ctx: &ServerCtx,
    request_fields: &[String],
    values: [f64; 3],
) -> Vec<String> {
    let phase_power = [values[0], values[1], values[2]];
    let measured_total: i64 = phase_power.iter().map(|v| v.round() as i64).sum();
    let meter_dev_type = request_fields
        .first()
        .cloned()
        .unwrap_or_else(|| "HMG-50".to_string());
    let meter_mac = request_fields.get(1).cloned().unwrap_or_default();
    let ct_type = ctx.settings.ct_type.clone();
    let ct_mac = if !ctx.settings.ct_mac.is_empty() {
        ctx.settings.ct_mac.clone()
    } else {
        request_fields.get(3).cloned().unwrap_or_default()
    };

    let mut fields: Vec<String> = vec![
        ct_type,
        ct_mac,
        meter_dev_type,
        meter_mac,
        (phase_power[0].round() as i64).to_string(),
        (phase_power[1].round() as i64).to_string(),
        (phase_power[2].round() as i64).to_string(),
        measured_total.to_string(),
        "0".into(),
        "0".into(),
        "0".into(),
        "0".into(), // A/B/C/ABC_chrg_nb
        ctx.settings.wifi_rssi.to_string(),
        ctx.info_idx.load(Ordering::Relaxed).to_string(),
        "0".into(), // x_chrg_power
        "0".into(),
        "0".into(),
        "0".into(),
        "0".into(), // A/B/C/ABC_chrg_power
        "0".into(), // x_dchrg_power
        "0".into(),
        "0".into(),
        "0".into(),
        "0".into(), // A/B/C/ABC_dchrg_power
    ];

    let by_phase = collect_reports_by_phase(&ctx.consumers.lock());
    for (idx, (chrg, dchrg, active)) in by_phase.iter().enumerate() {
        if *active || phase_power[idx].round() as i64 != 0 {
            fields[8 + idx] = "1".into();
        }
        fields[15 + idx] = chrg.to_string();
        fields[20 + idx] = dchrg.to_string();
    }
    // Trim/extend to RESPONSE_LABELS length.
    while fields.len() < RESPONSE_LABELS.len() {
        fields.push("0".into());
    }
    fields.truncate(RESPONSE_LABELS.len());
    ctx.info_idx.fetch_add(1, Ordering::Relaxed);
    fields
}

fn escape_field(value: &str) -> String {
    value.replace([',', ';', '='], "_")
}
