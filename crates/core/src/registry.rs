use std::collections::HashMap;
use std::sync::Arc;

use crate::{Powermeter, Result};

/// Runtime dispatch table from INI section-name prefix to powermeter factory.
///
/// This replaces the Python `create_powermeter()` if/elif chain in
/// `src/astrameter/config/config_loader.py` with a data-driven registry, so
/// users can swap meters via the web UI without a firmware rebuild.
///
/// The actual factory signature and the `PlatformCtx` type land alongside the
/// `astrameter-config` and `astrameter-platform` crates in Phase 1.
#[derive(Default)]
pub struct PowermeterRegistry {
    factories: HashMap<&'static str, Factory>,
}

/// Placeholder factory signature. Phase 1 replaces the `()` payloads with
/// concrete `Section` and `PlatformCtx` types.
pub type Factory = fn(&(), &()) -> Result<Arc<dyn Powermeter>>;

impl PowermeterRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, prefix: &'static str, factory: Factory) {
        self.factories.insert(prefix, factory);
    }

    pub fn lookup(&self, section_name: &str) -> Option<&Factory> {
        self.factories
            .iter()
            .find(|(prefix, _)| section_name.starts_with(*prefix))
            .map(|(_, f)| f)
    }

    pub fn prefixes(&self) -> impl Iterator<Item = &&'static str> {
        self.factories.keys()
    }
}
