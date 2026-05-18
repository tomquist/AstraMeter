//! Powermeter wrapper pipeline. Direct port of
//! `src/astrameter/powermeter/wrappers/`.

#![forbid(unsafe_code)]

pub mod hampel;
pub mod pid;
pub mod smoothing;
pub mod throttling;
pub mod transform;

pub use hampel::HampelPowermeter;
pub use pid::{PidMode, PidPowermeter};
pub use smoothing::{DeadbandPowermeter, SmoothedPowermeter};
pub use throttling::ThrottledPowermeter;
pub use transform::TransformedPowermeter;

use astrameter_core::Powermeter;
use std::sync::Arc;

/// Boxed handle used everywhere wrappers wrap a meter.
pub type SharedMeter = Arc<dyn Powermeter>;
