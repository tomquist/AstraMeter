//! Marstek CT002 / CT003 emulator. Direct port of `src/astrameter/ct002/`.
//!
//! - [`protocol`] mirrors `protocol.py` (wire encoding/decoding).
//! - [`server`]   mirrors `ct002.py` (UDP server + session tracking).
//! - [`balancer`] mirrors `balancer.py` (multi-battery load split).
//!
//! **Important:** `balancer` in this Rust port is a *simplified relay-mode
//! plus equal-split fallback* — it does **not** implement the advanced
//! efficiency rotation, saturation detection, or fair-share PID logic from
//! the Python version. Treat this as a structural skeleton suitable for
//! testing the wire protocol end-to-end. Run the Python implementation in
//! parallel until the full balancer is ported and record-replay validated
//! against captured Marstek traffic (see the migration plan, Phase 5).

#![forbid(unsafe_code)]

pub mod balancer;
pub mod protocol;
pub mod server;

pub use server::Ct002Emulator;
