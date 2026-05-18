//! Port of `src/astrameter/web_config.py` (config editor + supervisor signal).
//!
//! Provides `/api/config` GET/POST and `/api/restart`. Phase 7 keeps the
//! editor minimal — JSON in, JSON out; a richer HTML form (mirroring the
//! Python `web_config` UI) lands in a future commit.

use crate::{save_config_atomic, AppState, ReloadCommand};
use astrameter_config::Config;
use astrameter_core::{Error, Result};

pub async fn read_config(state: &AppState) -> Result<String> {
    tokio::fs::read_to_string(&state.config_path)
        .await
        .map_err(|e| Error::transport(format!("read config: {e}")))
}

/// Validate `body` as INI, then atomically replace `state.config_path` and
/// signal the supervisor to reload.
pub async fn write_config(state: &AppState, body: &str) -> Result<()> {
    // Parse to validate before touching disk.
    let _ = Config::parse(body)?;
    save_config_atomic(&state.config_path, body.as_bytes()).await?;
    let _ = state.reload_tx.send(ReloadCommand::ApplyNewConfig).await;
    Ok(())
}

#[cfg(not(target_os = "espidf"))]
pub mod axum_router {
    use super::*;
    use axum::{
        extract::State,
        http::StatusCode,
        response::{IntoResponse, Json, Response},
        routing::{get, post},
        Router,
    };
    use serde::{Deserialize, Serialize};

    pub fn build(state: AppState) -> Router {
        Router::new()
            .route(
                "/api/config",
                get(get_config_handler).post(post_config_handler),
            )
            .route("/api/restart", post(restart_handler))
            .with_state(state)
    }

    #[derive(Serialize)]
    struct ConfigPayload {
        content: String,
    }

    #[derive(Deserialize)]
    struct ConfigPost {
        content: String,
    }

    async fn get_config_handler(State(s): State<AppState>) -> Response {
        match read_config(&s).await {
            Ok(content) => Json(ConfigPayload { content }).into_response(),
            Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
        }
    }

    async fn post_config_handler(
        State(s): State<AppState>,
        Json(req): Json<ConfigPost>,
    ) -> Response {
        match write_config(&s, &req.content).await {
            Ok(()) => StatusCode::NO_CONTENT.into_response(),
            Err(e) => (StatusCode::BAD_REQUEST, e.to_string()).into_response(),
        }
    }

    async fn restart_handler(State(s): State<AppState>) -> StatusCode {
        let _ = s.reload_tx.send(ReloadCommand::ApplyNewConfig).await;
        StatusCode::ACCEPTED
    }
}
