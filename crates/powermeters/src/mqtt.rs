//! `mqtt` — port of `src/astrameter/powermeter/mqtt.py`. Real implementation
//! lands in Phase 2/3.

use std::sync::Arc;

use astrameter_config::Section;
use astrameter_core::{Error, Powermeter, Result};
use astrameter_platform::Platform;

pub fn create(_section: &Section<'_>, _platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>> {
    Err(Error::config(
        "mqtt powermeter not yet implemented in Rust port",
    ))
}
