//! Configuration loading.
//!
//! Round-trips `config.ini` while preserving comments (the web config editor
//! depends on this). Section-prefix dispatch into
//! [`astrameter_core::PowermeterRegistry`] arrives in Phase 1.

#![forbid(unsafe_code)]
