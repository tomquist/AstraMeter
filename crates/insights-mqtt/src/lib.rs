//! MQTT Insights.
//!
//! Ports `src/astrameter/mqtt_insights/`:
//!   - [`service`]   — `service.py`     (858 LOC)
//!   - [`discovery`] — `discovery.py`   (446 LOC)
//!   - [`marstek`]   — `marstek_mqtt.py` (244 LOC)
//!
//! Phase 6.

#![forbid(unsafe_code)]

pub mod discovery;
pub mod marstek;
pub mod service;
