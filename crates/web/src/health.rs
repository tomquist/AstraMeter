//! Port of `src/astrameter/web_server.py` (health endpoint + minimal router).
//!
//! Hosting backend differs per target:
//!   * Host: `axum::Router` (Phase 7 wires it up in the host binary).
//!   * ESP32: `esp-idf-svc::http::server::EspHttpServer` (Phase 8).
//!
//! Handlers are shared and live below.

use crate::AppState;
use serde::Serialize;

/// Health-check payload. Schema mirrors Python (`web_server.py`):
/// `{status: "healthy"|"degraded", service: "astrameter", git_commit?: "..."}`
/// — the Docker `HEALTHCHECK` greps for `"healthy"` so we keep that exact
/// string. Extra fields (`version`, `last_reload_ok`, `last_error`) are
/// additive and won't break the grep.
#[derive(Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub service: &'static str,
    pub version: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub git_commit: Option<String>,
    pub healthy: bool,
    pub last_reload_ok: Option<bool>,
    pub last_error: Option<String>,
}

pub fn health(state: &AppState) -> HealthResponse {
    let s = state.status.lock().clone();
    let git_commit = std::env::var("GIT_COMMIT_SHA")
        .ok()
        .filter(|s| !s.is_empty());
    HealthResponse {
        status: if s.healthy { "healthy" } else { "degraded" },
        service: "astrameter",
        version: astrameter_core::VERSION,
        git_commit,
        healthy: s.healthy,
        last_reload_ok: s.last_reload_ok,
        last_error: s.last_error,
    }
}

#[cfg(not(target_os = "espidf"))]
pub mod axum_router {
    use super::*;
    use axum::{extract::State, response::Json, routing::get, Router};

    pub fn build(state: AppState) -> Router {
        Router::new()
            .route("/health", get(health_handler))
            .route("/health/", get(health_handler))
            .route("/api", get(health_handler))
            .route("/api/", get(health_handler))
            .with_state(state)
    }

    async fn health_handler(State(s): State<AppState>) -> Json<HealthResponse> {
        Json(super::health(&s))
    }
}
