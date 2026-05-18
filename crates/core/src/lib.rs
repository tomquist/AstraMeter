//! AstraMeter core: powermeter trait, fundamental types, and errors.
//!
//! This crate contains zero I/O. It defines the contracts that the rest of the
//! workspace builds on.

#![forbid(unsafe_code)]

pub mod error;
pub mod powermeter;
pub mod registry;

pub use error::{Error, Result};
pub use powermeter::Powermeter;
pub use registry::PowermeterRegistry;

/// Current crate version, exposed for the `/version` health endpoint.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
