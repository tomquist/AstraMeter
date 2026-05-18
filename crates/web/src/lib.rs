//! Web server: health endpoint + config editor.
//!
//! Handlers are shared across host and ESP32; only the router differs.
//! See `health` (ported from `src/astrameter/web_server.py`) and `config_ui`
//! (ported from `src/astrameter/web_config.py`). Phase 7.

#![forbid(unsafe_code)]

pub mod config_ui;
pub mod health;
