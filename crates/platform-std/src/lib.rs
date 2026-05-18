//! Host implementations of `astrameter-platform` traits.
//!
//! Real impls (reqwest, tokio-tungstenite, rumqttc, tokio-serial, socket2)
//! arrive in Phase 1. This crate exists now so the workspace topology is
//! complete and `astrameter-host` has somewhere to depend on.

#![forbid(unsafe_code)]
