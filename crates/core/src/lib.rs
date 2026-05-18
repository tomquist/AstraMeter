//! AstraMeter core: powermeter trait, fundamental types, and errors.
//!
//! Zero I/O. Defines the contracts the rest of the workspace builds on.

#![forbid(unsafe_code)]

pub mod error;
pub mod powermeter;

pub use error::{Error, Result};
pub use powermeter::Powermeter;

/// Current crate version, exposed for the `/version` health endpoint.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
