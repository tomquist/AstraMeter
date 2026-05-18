//! Powermeter wrapper pipeline.
//!
//! Ports `src/astrameter/powermeter/wrappers/*.py`:
//!   - [`transform`] — POWER_OFFSET / POWER_MULTIPLIER
//!   - [`throttling`] — THROTTLE_INTERVAL rate limiting
//!   - [`smoothing`] — EMA + DEADBAND
//!   - [`hampel`]    — outlier rejection
//!   - [`pid`]       — PID controller
//!
//! Implementations arrive in Phase 2.

#![forbid(unsafe_code)]

pub mod hampel;
pub mod pid;
pub mod smoothing;
pub mod throttling;
pub mod transform;
