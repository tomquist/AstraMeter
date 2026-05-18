//! Marstek CT002 / CT003 emulator. Direct port of `src/astrameter/ct002/`.
//!
//! - [`protocol`] mirrors `protocol.py` (114 LOC, pure parsing).
//! - [`server`]   mirrors `ct002.py` (757 LOC, UDP server + session tracking).
//! - [`balancer`] mirrors `balancer.py` (1,270 LOC, multi-battery load split).
//!
//! Phase 5 ports these — `balancer` is record-replay validated against
//! captured Python output.

#![forbid(unsafe_code)]

pub mod balancer;
pub mod protocol;
pub mod server;
