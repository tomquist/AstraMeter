//! Port of `src/astrameter/web_config.py` (web config editor + supervisor signal).
//!
//! Provides the rich JS-driven editor served from `/config` (and `/`), plus
//! the API endpoints it talks to:
//!   * `GET  /api/config`    -> `{sections: {name: {key:value}}, order: [...]}`
//!   * `POST /api/config`    -> same shape, returns `{success, error?}`
//!   * `GET  /api/key-types` -> `SECTION_KEY_TYPES` schema
//!   * `POST /api/restart`   -> 202 (drops a ReloadCommand on the channel)

use crate::{save_config_atomic, AppState, ReloadCommand};
use astrameter_config::Config;
use astrameter_core::{Error, Result};
use serde_json::{json, Value};

/// Bundled HTML editor. Restored from `src/astrameter/static/config_editor.html`.
pub const CONFIG_EDITOR_HTML: &str = include_str!("../assets/config_editor.html");

/// Bundled section-key-types schema. Mirrors Python `SECTION_KEY_TYPES`.
pub const SECTION_KEY_TYPES_JSON: &str = include_str!("../assets/section_key_types.json");

/// Read the active `config.ini` and produce `{sections, order}` per the
/// editor's expected schema.
pub async fn read_config_as_dict(state: &AppState) -> Result<Value> {
    let raw = match tokio::fs::read_to_string(&state.config_path).await {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(Error::transport(format!("read config: {e}"))),
    };
    let cfg = Config::parse(&raw)?;
    let mut sections = serde_json::Map::new();
    let mut order = Vec::new();
    for name in cfg.sections() {
        let Some(section) = cfg.section(name) else {
            continue;
        };
        let mut keys = serde_json::Map::new();
        for (k, v) in section.entries() {
            keys.insert(k.to_string(), Value::String(v.to_string()));
        }
        sections.insert(name.to_string(), Value::Object(keys));
        order.push(Value::String(name.to_string()));
    }
    Ok(json!({"sections": sections, "order": order}))
}

/// Serialise a `{sections, order}` payload back into INI text and write it
/// atomically + signal the supervisor.
pub async fn write_config_from_dict(state: &AppState, payload: &Value) -> Result<()> {
    let sections = payload
        .get("sections")
        .and_then(|v| v.as_object())
        .ok_or_else(|| Error::config("payload missing sections"))?;
    let order: Vec<String> = payload
        .get("order")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_else(|| sections.keys().cloned().collect());

    let mut text = String::new();
    for name in &order {
        text.push('[');
        text.push_str(name);
        text.push_str("]\n");
        let Some(keys) = sections.get(name).and_then(|v| v.as_object()) else {
            continue;
        };
        for (k, v) in keys {
            let value_str = match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                Value::Bool(b) => b.to_string(),
                Value::Null => String::new(),
                other => other.to_string(),
            };
            text.push_str(k);
            text.push_str(" = ");
            text.push_str(&value_str);
            text.push('\n');
        }
        text.push('\n');
    }
    Config::parse(&text)?; // validate
    save_config_atomic(&state.config_path, text.as_bytes()).await?;
    let _ = state.reload_tx.send(ReloadCommand::ApplyNewConfig).await;
    Ok(())
}

#[cfg(not(target_os = "espidf"))]
pub mod axum_router {
    use super::*;
    use axum::{
        extract::State,
        http::{header, StatusCode},
        response::{IntoResponse, Json, Response},
        routing::{get, post},
        Router,
    };

    pub fn build(state: AppState) -> Router {
        Router::new()
            .route("/", get(editor_handler))
            .route("/config", get(editor_handler))
            .route(
                "/api/config",
                get(get_config_handler).post(post_config_handler),
            )
            .route("/api/key-types", get(key_types_handler))
            .route("/api/restart", post(restart_handler))
            .with_state(state)
    }

    async fn editor_handler() -> Response {
        (
            [
                (header::CONTENT_TYPE, "text/html; charset=utf-8"),
                (header::CACHE_CONTROL, "no-cache"),
            ],
            super::CONFIG_EDITOR_HTML,
        )
            .into_response()
    }

    async fn key_types_handler() -> Response {
        (
            [(header::CONTENT_TYPE, "application/json")],
            super::SECTION_KEY_TYPES_JSON,
        )
            .into_response()
    }

    async fn get_config_handler(State(s): State<AppState>) -> Response {
        match read_config_as_dict(&s).await {
            Ok(payload) => Json(payload).into_response(),
            Err(e) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"success": false, "error": e.to_string()})),
            )
                .into_response(),
        }
    }

    async fn post_config_handler(State(s): State<AppState>, Json(req): Json<Value>) -> Response {
        match write_config_from_dict(&s, &req).await {
            Ok(()) => Json(json!({"success": true})).into_response(),
            Err(e) => (
                StatusCode::BAD_REQUEST,
                Json(json!({"success": false, "error": e.to_string()})),
            )
                .into_response(),
        }
    }

    async fn restart_handler(State(s): State<AppState>) -> Response {
        let _ = s.reload_tx.send(ReloadCommand::ApplyNewConfig).await;
        (StatusCode::ACCEPTED, Json(json!({"success": true}))).into_response()
    }
}
