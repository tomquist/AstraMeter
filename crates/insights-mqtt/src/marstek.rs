//! Marstek MQTT responder. Faithful port of
//! `src/astrameter/mqtt_insights/marstek_mqtt.py`.
//!
//! Pure helpers (topic formatting, payload building, poll detection) plus
//! the [`MarstekBinding`] dataclass that the service stores per device.

pub const APP_TOPIC_TEMPLATES: &[&str] = &[
    "hame_energy/{ct_type}/App/{mac}/ctrl",
    "marstek_energy/{ct_type}/App/{mac}/ctrl",
];
pub const DEVICE_TOPIC_TEMPLATES: &[&str] = &[
    "hame_energy/{ct_type}/device/{mac}/ctrl",
    "marstek_energy/{ct_type}/device/{mac}/ctrl",
];

pub const DEFAULT_VER_V: i64 = 148;
pub const DEFAULT_FC4_V: &str = "202409090159";
pub const DEFAULT_CD1_KWH: (f64, f64, f64, f64) = (0.0, 0.0, 0.0, 0.0);

#[derive(Clone)]
pub struct MarstekBinding {
    pub device_id: String,
    pub ct_type: String,
    pub mac: String,
    pub wifi_rssi: i64,
    pub ver_v: i64,
    pub ble_s: i64,
    pub fc4_v: String,
    pub get_connected_slave_count: Option<std::sync::Arc<dyn Fn() -> i64 + Send + Sync>>,
    pub get_cd4_slave_csv: Option<std::sync::Arc<dyn Fn() -> String + Send + Sync>>,
}

pub fn normalize_mac(raw: &str) -> String {
    let cleaned: String = raw
        .chars()
        .filter(|c| *c != ':' && *c != '-')
        .collect::<String>()
        .trim()
        .to_lowercase();
    if cleaned.len() == 12 && cleaned.chars().all(|c| c.is_ascii_hexdigit()) {
        cleaned
    } else {
        String::new()
    }
}

fn parse_ctrl_kv(body: &[u8]) -> Option<std::collections::HashMap<String, String>> {
    if body.is_empty() {
        return None;
    }
    let text = std::str::from_utf8(body).ok()?;
    let mut out = std::collections::HashMap::new();
    for chunk in text.split(',') {
        if let Some((k, v)) = chunk.split_once('=') {
            let key = k.trim().to_lowercase();
            if !key.is_empty() {
                out.insert(key, v.trim().to_string());
            }
        }
    }
    Some(out)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PollContext {
    pub echo_cd: u32,
    pub slave_id: Option<i64>,
}

pub fn parse_poll_payload(body: &[u8]) -> Option<PollContext> {
    let kv = parse_ctrl_kv(body)?;
    let cd_str = kv.get("cd")?;
    let cd: u32 = cd_str.parse().ok()?;
    match cd {
        1 => Some(PollContext {
            echo_cd: 1,
            slave_id: None,
        }),
        4 => {
            let p1: i64 = kv.get("p1")?.parse().ok()?;
            Some(PollContext {
                echo_cd: 4,
                slave_id: Some(p1),
            })
        }
        _ => None,
    }
}

pub fn is_poll_payload(body: &[u8]) -> bool {
    parse_poll_payload(body).is_some()
}

pub fn parse_app_topic(topic: &str) -> Option<(String, String)> {
    for template in APP_TOPIC_TEMPLATES {
        let prefix = template.split("/{").next().unwrap_or("");
        if !topic.starts_with(prefix) {
            continue;
        }
        // Strip prefix and split.
        let rest = &topic[prefix.len() + 1..];
        let parts: Vec<&str> = rest.split('/').collect();
        if parts.len() != 4 {
            continue;
        }
        if parts[1] != "App" || parts[3] != "ctrl" {
            continue;
        }
        return Some((parts[0].to_string(), parts[2].to_lowercase()));
    }
    None
}

pub fn app_topics_for(b: &MarstekBinding) -> (String, String) {
    let render = |t: &str| t.replace("{ct_type}", &b.ct_type).replace("{mac}", &b.mac);
    (
        render(APP_TOPIC_TEMPLATES[0]),
        render(APP_TOPIC_TEMPLATES[1]),
    )
}

pub fn device_topics_for(b: &MarstekBinding) -> (String, String) {
    let render = |t: &str| t.replace("{ct_type}", &b.ct_type).replace("{mac}", &b.mac);
    (
        render(DEVICE_TOPIC_TEMPLATES[0]),
        render(DEVICE_TOPIC_TEMPLATES[1]),
    )
}

fn fmt_kwh(v: f64) -> String {
    format!("{v:.2}")
}

pub fn build_cd4_response(slave_kv_tail: &str) -> Vec<u8> {
    slave_kv_tail.as_bytes().to_vec()
}

pub fn build_response(
    binding: &MarstekBinding,
    watts: &[f64],
    poll: Option<PollContext>,
    connected_slave_count: i64,
    kwh_fields: Option<(f64, f64, f64, f64)>,
) -> Vec<u8> {
    let mut vs = watts.to_vec();
    while vs.len() < 3 {
        vs.push(0.0);
    }
    let a = vs[0].round() as i64;
    let b = vs[1].round() as i64;
    let c = vs[2].round() as i64;
    let total = a + b + c;
    let core = format!(
        "pwr_a={a},pwr_b={b},pwr_c={c},pwr_t={total},wif_r={},ver_v={},wif_s=2",
        binding.wifi_rssi, binding.ver_v
    );
    let (k0, k1, k2, k3) = kwh_fields.unwrap_or(DEFAULT_CD1_KWH);
    let cd1_tail = format!(
        "ble_s={},fc4_v={},kwh={},n_kwh={},used_kwh={},fed_kwh={}",
        binding.ble_s,
        binding.fc4_v,
        fmt_kwh(k0),
        fmt_kwh(k1),
        fmt_kwh(k2),
        fmt_kwh(k3),
    );
    let payload = if matches!(poll, Some(p) if p.echo_cd == 1) {
        format!(
            "pwr_a={a},pwr_b={b},pwr_c={c},pwr_t={total},wif_s=2,wif_r={},ver_v={},slv_n={},cur_d=0,{}",
            binding.wifi_rssi, binding.ver_v, connected_slave_count, cd1_tail
        )
    } else {
        core
    };
    payload.into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_binding() -> MarstekBinding {
        MarstekBinding {
            device_id: "dev1".into(),
            ct_type: "HMK-1".into(),
            mac: "aabbccddeeff".into(),
            wifi_rssi: -45,
            ver_v: DEFAULT_VER_V,
            ble_s: 0,
            fc4_v: DEFAULT_FC4_V.into(),
            get_connected_slave_count: None,
            get_cd4_slave_csv: None,
        }
    }

    #[test]
    fn poll_parsing() {
        assert_eq!(
            parse_poll_payload(b"cd=1"),
            Some(PollContext {
                echo_cd: 1,
                slave_id: None
            })
        );
        assert_eq!(
            parse_poll_payload(b"cd=4,p1=2"),
            Some(PollContext {
                echo_cd: 4,
                slave_id: Some(2)
            })
        );
        assert!(parse_poll_payload(b"cd=4").is_none());
        assert!(parse_poll_payload(b"hello").is_none());
    }

    #[test]
    fn normalize_mac_strips_punctuation() {
        assert_eq!(normalize_mac("AA:BB:CC:DD:EE:FF"), "aabbccddeeff");
        assert_eq!(normalize_mac("zz"), "");
    }

    #[test]
    fn app_topic_roundtrip() {
        let b = make_binding();
        let (old, _) = app_topics_for(&b);
        assert_eq!(
            parse_app_topic(&old),
            Some(("HMK-1".into(), "aabbccddeeff".into()))
        );
    }

    #[test]
    fn cd1_response_includes_runtime_frame() {
        let b = make_binding();
        let body = build_response(
            &b,
            &[100.0, 200.0, 300.0],
            Some(PollContext {
                echo_cd: 1,
                slave_id: None,
            }),
            2,
            None,
        );
        let s = String::from_utf8(body).unwrap();
        assert!(s.contains("pwr_t=600"));
        assert!(s.contains("slv_n=2"));
        assert!(s.contains("ble_s=0"));
    }

    #[test]
    fn cd4_response_is_passthrough() {
        let body = build_cd4_response("slv_t=HMK,slv_id=1");
        assert_eq!(body, b"slv_t=HMK,slv_id=1");
    }
}
