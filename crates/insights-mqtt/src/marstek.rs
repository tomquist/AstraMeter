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

/// Normalise the Marstek-API `version` field into the `ver_v` integer the
/// MQTT app expects. Booleans / unparseable strings fall back to
/// `DEFAULT_VER_V`. Mirrors Python's `ver_v_from_marstek_api_version`.
pub fn ver_v_from_marstek_api_version(value: &serde_json::Value) -> i64 {
    match value {
        serde_json::Value::Bool(_) => DEFAULT_VER_V,
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i
            } else if let Some(f) = n.as_f64() {
                if f.fract() == 0.0 {
                    f as i64
                } else {
                    DEFAULT_VER_V
                }
            } else {
                DEFAULT_VER_V
            }
        }
        serde_json::Value::String(s) => {
            let t = s.trim();
            if t.is_empty() {
                DEFAULT_VER_V
            } else {
                t.parse::<i64>().unwrap_or(DEFAULT_VER_V)
            }
        }
        _ => DEFAULT_VER_V,
    }
}

/// Escape a CT002 reporting-row field for the `cd=4` CSV reply: the
/// Marstek app naïvely splits on `,` then `=`, so anything that could
/// confuse that parser is folded to `_`. Matches Python's
/// `_cd4_escape_field`.
pub fn cd4_escape_field(value: &str) -> String {
    value
        .chars()
        .map(|c| {
            if c == ',' || c == ';' || c == '=' {
                '_'
            } else {
                c
            }
        })
        .collect()
}

/// A flattened CT002 reporting row used to build the `cd=4` slave-list CSV.
#[derive(Debug, Clone)]
pub struct Cd4Row {
    pub device_type: String,
    pub consumer_id: String,
    pub last_ip: String,
    pub phase: i64,
}

/// Format CT002 reporting rows as the flat repeated `slv_t/slv_id/slv_ip/slv_p`
/// token list expected by the Marstek mobile app. Mirrors Python's
/// `format_cd4_slave_csv`.
pub fn format_cd4_slave_csv(rows: &[Cd4Row]) -> String {
    if rows.is_empty() {
        return String::new();
    }
    let mut parts: Vec<String> = Vec::with_capacity(rows.len());
    for row in rows {
        let host = if row.last_ip.trim().is_empty() {
            "0.0.0.0"
        } else {
            row.last_ip.trim()
        };
        parts.push(format!(
            "slv_t={},slv_id={},slv_ip={},slv_p={}",
            cd4_escape_field(&row.device_type),
            cd4_escape_field(&row.consumer_id),
            cd4_escape_field(host),
            row.phase
        ));
    }
    parts.join(",")
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

    #[test]
    fn ver_v_from_api_handles_all_shapes() {
        assert_eq!(ver_v_from_marstek_api_version(&serde_json::json!(200)), 200);
        assert_eq!(
            ver_v_from_marstek_api_version(&serde_json::json!("201")),
            201
        );
        assert_eq!(
            ver_v_from_marstek_api_version(&serde_json::json!(123.0)),
            123
        );
        assert_eq!(
            ver_v_from_marstek_api_version(&serde_json::json!(123.5)),
            DEFAULT_VER_V
        );
        assert_eq!(
            ver_v_from_marstek_api_version(&serde_json::json!(true)),
            DEFAULT_VER_V
        );
        assert_eq!(
            ver_v_from_marstek_api_version(&serde_json::json!("")),
            DEFAULT_VER_V
        );
        assert_eq!(
            ver_v_from_marstek_api_version(&serde_json::json!(null)),
            DEFAULT_VER_V
        );
    }

    #[test]
    fn cd4_csv_escapes_separators() {
        let rows = vec![
            Cd4Row {
                device_type: "HM,A=2".into(),
                consumer_id: "abc".into(),
                last_ip: "10.0.0.1".into(),
                phase: 1,
            },
            Cd4Row {
                device_type: "HMG-50".into(),
                consumer_id: "id;2".into(),
                last_ip: "".into(),
                phase: 2,
            },
        ];
        let s = format_cd4_slave_csv(&rows);
        assert!(s.contains("slv_t=HM_A_2"));
        assert!(s.contains("slv_id=id_2"));
        assert!(s.contains("slv_ip=0.0.0.0"));
        assert!(s.contains("slv_p=2"));
    }

    #[test]
    fn cd4_csv_empty_rows() {
        assert_eq!(format_cd4_slave_csv(&[]), "");
    }
}
