//! Home Assistant MQTT Device Discovery payload builders.
//!
//! Faithful port of `src/astrameter/mqtt_insights/discovery.py`. Each
//! `build_*` function returns `(topic, payload)` matching the Python
//! function of the same name byte-for-byte.

use serde_json::{json, Value};

fn sanitize_id(value: &str) -> String {
    value
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

fn origin() -> Value {
    let sha = std::env::var("GIT_COMMIT_SHA").unwrap_or_default();
    let sw = if sha.is_empty() {
        "unknown".to_string()
    } else {
        sha
    };
    json!({
        "name": "astrameter",
        "sw_version": sw,
        "support_url": "https://github.com/tomquist/astrameter",
    })
}

fn system_availability(base_topic: &str) -> Value {
    json!({
        "topic": format!("{base_topic}/status"),
        "payload_available": "online",
        "payload_not_available": "offline",
    })
}

// ── CT002 consumer (per-battery) ──────────────────────────────────────────

pub fn build_ct002_consumer_discovery(
    base_topic: &str,
    device_id: &str,
    consumer_id: &str,
    ha_prefix: &str,
    device_type: &str,
    network_mac: &str,
    battery_ip: &str,
) -> (String, Value) {
    let safe_dev = sanitize_id(device_id);
    let safe_cid = sanitize_id(consumer_id);
    let node_id = format!("astrameter_ct002_{safe_dev}_{safe_cid}");
    let state_topic = format!("{base_topic}/ct002/{device_id}/consumer/{consumer_id}");
    let avail_topic = format!("{state_topic}/availability");
    let uid_prefix = format!("astrameter_ct002_{safe_dev}_{safe_cid}");
    let meter_identifier = format!("astrameter_ct002_{safe_dev}");

    let mut components = serde_json::Map::new();

    let power_sensors: &[(&str, Option<&str>, &str)] = &[
        (
            "grid_power_total",
            None,
            "{{ value_json.grid_power.total }}",
        ),
        (
            "grid_power_l1",
            Some("Grid Power L1"),
            "{{ value_json.grid_power.l1 }}",
        ),
        (
            "grid_power_l2",
            Some("Grid Power L2"),
            "{{ value_json.grid_power.l2 }}",
        ),
        (
            "grid_power_l3",
            Some("Grid Power L3"),
            "{{ value_json.grid_power.l3 }}",
        ),
        ("target_l1", Some("Target L1"), "{{ value_json.target.l1 }}"),
        ("target_l2", Some("Target L2"), "{{ value_json.target.l2 }}"),
        ("target_l3", Some("Target L3"), "{{ value_json.target.l3 }}"),
        (
            "reported_power",
            Some("Reported Power"),
            "{{ value_json.reported_power }}",
        ),
        (
            "last_target",
            Some("Last Target"),
            "{{ value_json.last_target }}",
        ),
    ];
    for (key, label, tmpl) in power_sensors {
        let name = match label {
            None => Value::Null,
            Some(l) => Value::String((*l).to_string()),
        };
        components.insert(
            (*key).to_string(),
            json!({
                "platform": "sensor",
                "unique_id": format!("{uid_prefix}_{key}"),
                "device_class": "power",
                "unit_of_measurement": "W",
                "state_topic": state_topic,
                "value_template": tmpl,
                "name": name,
            }),
        );
    }

    components.insert(
        "saturation".into(),
        json!({
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_saturation"),
            "name": "Saturation",
            "unit_of_measurement": "%",
            "state_topic": state_topic,
            "value_template": "{{ (value_json.saturation * 100) | round(1) }}",
        }),
    );

    components.insert(
        "phase".into(),
        json!({
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_phase"),
            "name": "Phase",
            "device_class": "enum",
            "options": ["A", "B", "C"],
            "state_topic": state_topic,
            "value_template": "{{ value_json.phase }}",
            "entity_category": "diagnostic",
        }),
    );

    let diag = &[
        ("device_type", "Device Type", "{{ value_json.device_type }}"),
        ("battery_ip", "Battery IP", "{{ value_json.battery_ip }}"),
        ("ct_type", "CT Type", "{{ value_json.ct_type }}"),
        ("ct_mac", "CT MAC", "{{ value_json.ct_mac }}"),
    ];
    for (key, label, tmpl) in diag {
        components.insert(
            (*key).to_string(),
            json!({
                "platform": "sensor",
                "unique_id": format!("{uid_prefix}_{key}"),
                "name": label,
                "state_topic": state_topic,
                "value_template": tmpl,
                "entity_category": "diagnostic",
            }),
        );
    }

    components.insert(
        "last_seen".into(),
        json!({
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_last_seen"),
            "name": "Last Seen",
            "device_class": "timestamp",
            "state_topic": state_topic,
            "value_template": "{{ value_json.last_seen }}",
            "entity_category": "diagnostic",
        }),
    );

    components.insert(
        "poll_interval".into(),
        json!({
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_poll_interval"),
            "name": "Poll Interval",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_topic": state_topic,
            "value_template": "{{ value_json.poll_interval }}",
            "entity_category": "diagnostic",
        }),
    );

    components.insert(
        "manual_target".into(),
        json!({
            "platform": "number",
            "unique_id": format!("{uid_prefix}_manual_target"),
            "name": "Manual Target",
            "unit_of_measurement": "W",
            "device_class": "power",
            "min": -10000,
            "max": 10000,
            "mode": "box",
            "state_topic": state_topic,
            "value_template": "{{ value_json.manual_target | default(0) }}",
            "command_topic": format!("{state_topic}/set"),
            "command_template": r#"{"manual_target": {{ value }}}"#,
            "entity_category": "config",
        }),
    );

    components.insert(
        "auto_target".into(),
        json!({
            "platform": "switch",
            "unique_id": format!("{uid_prefix}_auto_target"),
            "name": "Auto Target",
            "state_topic": state_topic,
            "command_topic": format!("{state_topic}/set"),
            "value_template": "{{ value_json.auto_target }}",
            "payload_on": r#"{"auto_target": true}"#,
            "payload_off": r#"{"auto_target": false}"#,
            "state_on": "True",
            "state_off": "False",
            "entity_category": "config",
        }),
    );

    components.insert(
        "active".into(),
        json!({
            "platform": "switch",
            "unique_id": format!("{uid_prefix}_active"),
            "name": "Active",
            "state_topic": state_topic,
            "command_topic": format!("{state_topic}/set"),
            "value_template": "{{ value_json.active }}",
            "payload_on": r#"{"active": true}"#,
            "payload_off": r#"{"active": false}"#,
            "state_on": "True",
            "state_off": "False",
            "optimistic": true,
        }),
    );

    let mac_slug = sanitize_id(consumer_id)
        .to_lowercase()
        .replace(['-', '_'], "");

    let mut device_info = serde_json::Map::new();
    device_info.insert(
        "identifiers".into(),
        json!([format!("astrameter_consumer_{mac_slug}")]),
    );
    let name = if device_type.is_empty() {
        format!("AstraMeter Consumer {mac_slug}")
    } else {
        format!("AstraMeter Consumer {device_type} {mac_slug}")
    };
    device_info.insert("name".into(), Value::String(name));
    device_info.insert("manufacturer".into(), Value::String("Marstek".into()));
    device_info.insert("via_device".into(), Value::String(meter_identifier.clone()));
    let mut connections = Vec::<Value>::new();
    if mac_slug.len() == 12 && mac_slug.chars().all(|c| c.is_ascii_hexdigit()) {
        let bt_mac: String = mac_slug
            .as_bytes()
            .chunks(2)
            .map(|c| std::str::from_utf8(c).unwrap().to_string())
            .collect::<Vec<_>>()
            .join(":")
            .to_uppercase();
        connections.push(json!(["bluetooth", bt_mac]));
    }
    if !network_mac.is_empty() {
        connections.push(json!(["mac", network_mac]));
    }
    if !battery_ip.is_empty() {
        connections.push(json!(["ip", battery_ip]));
    }
    if !connections.is_empty() {
        device_info.insert("connections".into(), Value::Array(connections));
    }
    if !device_type.is_empty() {
        device_info.insert("model_id".into(), Value::String(device_type.to_string()));
    }

    let payload = json!({
        "device": device_info,
        "origin": origin(),
        "components": components,
        "availability_mode": "all",
        "availability": [
            system_availability(base_topic),
            {
                "topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ],
        "state_topic": state_topic,
    });

    let topic = format!("{ha_prefix}/device/{node_id}/config");
    (topic, payload)
}

// ── CT002 device-level ────────────────────────────────────────────────────

pub fn build_ct002_device_discovery(
    base_topic: &str,
    device_id: &str,
    ha_prefix: &str,
    addon_slug: Option<&str>,
) -> (String, Value) {
    let safe_dev = sanitize_id(device_id);
    let node_id = format!("astrameter_ct002_{safe_dev}");
    let state_topic = format!("{base_topic}/ct002/{device_id}/status");
    let uid_prefix = format!("astrameter_ct002_{safe_dev}");

    let components = json!({
        "smooth_target": {
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_smooth_target"),
            "name": Value::Null,
            "device_class": "power",
            "unit_of_measurement": "W",
            "state_topic": state_topic,
            "value_template": "{{ value_json.smooth_target }}",
        },
        "active_control": {
            "platform": "binary_sensor",
            "unique_id": format!("{uid_prefix}_active_control"),
            "name": "Active Control",
            "device_class": "running",
            "state_topic": state_topic,
            "value_template": "{{ value_json.active_control }}",
            "payload_on": "True",
            "payload_off": "False",
        },
        "consumer_count": {
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_consumer_count"),
            "name": "Consumer Count",
            "state_topic": state_topic,
            "value_template": "{{ value_json.consumer_count }}",
            "entity_category": "diagnostic",
        },
        "force_rotation": {
            "platform": "button",
            "unique_id": format!("{uid_prefix}_force_rotation"),
            "name": "Force Rotation",
            "command_topic": format!("{base_topic}/ct002/{device_id}/set"),
            "payload_press": r#"{"force_rotation": true}"#,
            "entity_category": "config",
        },
    });

    let mut device_info = json!({
        "identifiers": node_id,
        "name": format!("AstraMeter CT002 {device_id}"),
        "manufacturer": "astrameter",
    });
    if let Some(addon) = addon_slug {
        device_info["via_device"] = Value::String(addon.to_string());
    }

    let payload = json!({
        "device": device_info,
        "origin": origin(),
        "components": components,
        "availability": [system_availability(base_topic)],
        "state_topic": state_topic,
    });
    let topic = format!("{ha_prefix}/device/{node_id}/config");
    (topic, payload)
}

// ── Shelly battery (per-battery) ──────────────────────────────────────────

pub fn build_shelly_battery_discovery(
    base_topic: &str,
    device_id: &str,
    battery_ip: &str,
    ha_prefix: &str,
) -> (String, Value) {
    let ip_slug = sanitize_id(battery_ip);
    let safe_dev = sanitize_id(device_id);
    let node_id = format!("astrameter_shelly_{safe_dev}_{ip_slug}");
    let state_topic = format!("{base_topic}/shelly/{device_id}/battery/{ip_slug}");
    let avail_topic = format!("{state_topic}/availability");
    let uid_prefix = format!("astrameter_shelly_{safe_dev}_{ip_slug}");

    let mut components = serde_json::Map::new();
    let power_sensors: &[(&str, Option<&str>, &str)] = &[
        (
            "grid_power_total",
            None,
            "{{ value_json.grid_power.total }}",
        ),
        (
            "grid_power_l1",
            Some("Grid Power L1"),
            "{{ value_json.grid_power.l1 }}",
        ),
        (
            "grid_power_l2",
            Some("Grid Power L2"),
            "{{ value_json.grid_power.l2 }}",
        ),
        (
            "grid_power_l3",
            Some("Grid Power L3"),
            "{{ value_json.grid_power.l3 }}",
        ),
    ];
    for (key, label, tmpl) in power_sensors {
        let name = match label {
            None => Value::Null,
            Some(l) => Value::String((*l).to_string()),
        };
        components.insert(
            (*key).to_string(),
            json!({
                "platform": "sensor",
                "unique_id": format!("{uid_prefix}_{key}"),
                "device_class": "power",
                "unit_of_measurement": "W",
                "state_topic": state_topic,
                "value_template": tmpl,
                "name": name,
            }),
        );
    }
    components.insert(
        "active".into(),
        json!({
            "platform": "binary_sensor",
            "unique_id": format!("{uid_prefix}_active"),
            "name": "Active",
            "device_class": "connectivity",
            "state_topic": state_topic,
            "value_template": "{{ value_json.active }}",
            "payload_on": "True",
            "payload_off": "False",
            "entity_category": "diagnostic",
        }),
    );
    components.insert(
        "last_seen".into(),
        json!({
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_last_seen"),
            "name": "Last Seen",
            "device_class": "timestamp",
            "state_topic": state_topic,
            "value_template": "{{ value_json.last_seen }}",
            "entity_category": "diagnostic",
        }),
    );
    components.insert(
        "poll_interval".into(),
        json!({
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_poll_interval"),
            "name": "Poll Interval",
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_topic": state_topic,
            "value_template": "{{ value_json.poll_interval }}",
            "entity_category": "diagnostic",
        }),
    );

    let payload = json!({
        "device": {
            "identifiers": node_id,
            "name": format!("AstraMeter Shelly Battery {battery_ip}"),
            "manufacturer": "astrameter",
            "via_device": format!("astrameter_shelly_{safe_dev}"),
        },
        "origin": origin(),
        "components": components,
        "availability_mode": "all",
        "availability": [
            system_availability(base_topic),
            {
                "topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ],
        "state_topic": state_topic,
    });
    let topic = format!("{ha_prefix}/device/{node_id}/config");
    (topic, payload)
}

// ── Shelly device-level ───────────────────────────────────────────────────

pub fn build_shelly_device_discovery(
    base_topic: &str,
    device_id: &str,
    ha_prefix: &str,
    addon_slug: Option<&str>,
) -> (String, Value) {
    let safe_dev = sanitize_id(device_id);
    let node_id = format!("astrameter_shelly_{safe_dev}");
    let state_topic = format!("{base_topic}/shelly/{device_id}/status");
    let uid_prefix = format!("astrameter_shelly_{safe_dev}");

    let components = json!({
        "battery_count": {
            "platform": "sensor",
            "unique_id": format!("{uid_prefix}_battery_count"),
            "name": "Battery Count",
            "state_topic": state_topic,
            "value_template": "{{ value_json.battery_count }}",
            "entity_category": "diagnostic",
        }
    });
    let mut device_info = json!({
        "identifiers": node_id,
        "name": format!("AstraMeter Shelly {device_id}"),
        "manufacturer": "astrameter",
    });
    if let Some(addon) = addon_slug {
        device_info["via_device"] = Value::String(addon.to_string());
    }
    let payload = json!({
        "device": device_info,
        "origin": origin(),
        "components": components,
        "availability": [system_availability(base_topic)],
        "state_topic": state_topic,
    });
    let topic = format!("{ha_prefix}/device/{node_id}/config");
    (topic, payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ct002_consumer_includes_required_components() {
        let (topic, payload) = build_ct002_consumer_discovery(
            "astrameter",
            "dev1",
            "112233445566",
            "homeassistant",
            "HMG-1",
            "",
            "192.168.1.10",
        );
        assert_eq!(
            topic,
            "homeassistant/device/astrameter_ct002_dev1_112233445566/config"
        );
        let components = &payload["components"];
        for key in [
            "grid_power_total",
            "saturation",
            "phase",
            "manual_target",
            "auto_target",
            "active",
            "last_seen",
            "poll_interval",
        ] {
            assert!(components.get(key).is_some(), "missing {key}");
        }
        // MAC-derived bluetooth connection added.
        let connections = &payload["device"]["connections"];
        assert!(connections.is_array());
    }

    #[test]
    fn shelly_device_includes_via_addon() {
        let (_, payload) = build_shelly_device_discovery(
            "astrameter",
            "shelly1",
            "homeassistant",
            Some("a0d7b954_astrameter"),
        );
        assert_eq!(payload["device"]["via_device"], "a0d7b954_astrameter");
    }
}
